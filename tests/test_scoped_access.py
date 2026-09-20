"""Unit tests for scoped single-environment MCP access."""

from __future__ import annotations

import asyncio
import tempfile
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import scoped_access
from oduflow.docker_ops import env_ops
from oduflow.errors import NotFoundError
from oduflow.locking import LockManager
from oduflow.scoped_access import (
    OduflowTokenVerifier,
    ScopedAccessMiddleware,
    ScopedEnvASGI,
    build_env_param_tools,
    decide,
    strip_env_name,
)
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- decide ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url_env,token_env,expected",
    [
        (None, None, ("full", None)),  # root /mcp + team token
        (None, "a", ("deny", None)),  # env token must use its /mcp/<env> URL
        ("a", None, ("scoped", "a")),  # team token scoped by URL
        ("a", "a", ("scoped", "a")),  # env token matching its URL
        ("a", "b", ("deny", None)),  # env token against another env's URL
    ],
)
def test_decide(url_env, token_env, expected):
    assert decide(url_env, token_env) == expected


# --- strip_env_name --------------------------------------------------------


def test_strip_env_name():
    params = {
        "type": "object",
        "properties": {"env_name": {"type": "string"}, "modules": {"type": "string"}},
        "required": ["env_name", "modules"],
    }
    out = strip_env_name(params)
    assert "env_name" not in out["properties"]
    assert "modules" in out["properties"]
    assert out["required"] == ["modules"]
    # The original schema is left untouched.
    assert "env_name" in params["properties"]


# --- build_env_param_tools -------------------------------------------------


class _FakeTool:
    def __init__(self, name, parameters):
        self.name = name
        self.parameters = parameters

    def model_copy(self, update):
        return _FakeTool(self.name, update.get("parameters", self.parameters))


class _FakeMcp:
    def __init__(self, tools):
        self._tool_manager = type("Mgr", (), {"_tools": tools})()


def test_build_env_param_tools_only_allowlisted_with_env():
    tools = {
        "pull_and_apply": _FakeTool(
            "pull_and_apply", {"properties": {"env_name": {}, "install": {}}}
        ),
        "get_agent_instructions": _FakeTool(
            "get_agent_instructions", {"properties": {}}
        ),
        # Has env_name but is NOT allowlisted -> never injected into.
        "delete_environment": _FakeTool(
            "delete_environment", {"properties": {"env_name": {}}}
        ),
    }
    result = build_env_param_tools(_FakeMcp(tools))
    assert "pull_and_apply" in result
    assert "get_agent_instructions" not in result
    assert "delete_environment" not in result


# --- ScopedEnvASGI ---------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.scope = scope


def test_asgi_rewrites_scoped_path():
    rec = _Recorder()
    _run(
        ScopedEnvASGI(rec)(
            {"type": "http", "path": "/mcp/feature/x", "raw_path": b"/mcp/feature/x"},
            None,
            None,
        )
    )
    assert rec.scope["path"] == "/mcp"
    assert rec.scope["raw_path"] == b"/mcp"
    assert rec.scope[scoped_access.SCOPE_KEY] == "feature/x"


def test_asgi_passthrough_root_mcp():
    rec = _Recorder()
    _run(ScopedEnvASGI(rec)({"type": "http", "path": "/mcp"}, None, None))
    assert rec.scope["path"] == "/mcp"
    assert scoped_access.SCOPE_KEY not in rec.scope


def test_asgi_passthrough_other_paths():
    rec = _Recorder()
    _run(ScopedEnvASGI(rec)({"type": "http", "path": "/api/environments"}, None, None))
    assert rec.scope["path"] == "/api/environments"
    assert scoped_access.SCOPE_KEY not in rec.scope


def test_asgi_rewrites_scoped_well_known():
    rec = _Recorder()
    _run(
        ScopedEnvASGI(rec)(
            {
                "type": "http",
                "path": "/.well-known/oauth-protected-resource/mcp/env1",
            },
            None,
            None,
        )
    )
    assert rec.scope["path"] == "/.well-known/oauth-protected-resource/mcp"


@pytest.mark.parametrize(
    "req_path,expected",
    [
        ("/mcp/authorize", "/authorize"),
        ("/mcp/token", "/token"),
        ("/mcp/register", "/register"),
        (
            "/mcp/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server",
        ),
        (
            "/mcp/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ),
    ],
)
def test_asgi_routes_oauth_subpaths_to_root(req_path, expected):
    # A path-relative MCP client (e.g. claude.ai) hits OAuth endpoints under the
    # /mcp mount; they must reach the real root routes, NOT be treated as an
    # environment named "authorize"/"token"/… (which would 401 on the protected
    # /mcp endpoint).
    rec = _Recorder()
    _run(ScopedEnvASGI(rec)({"type": "http", "path": req_path}, None, None))
    assert rec.scope["path"] == expected
    assert rec.scope["raw_path"] == expected.encode()
    assert scoped_access.SCOPE_KEY not in rec.scope


def test_asgi_scoped_env_not_shadowed_by_oauth_names():
    # Only exact reserved paths are aliased; a real /mcp/<env> still scopes.
    rec = _Recorder()
    _run(ScopedEnvASGI(rec)({"type": "http", "path": "/mcp/feature/x"}, None, None))
    assert rec.scope["path"] == "/mcp"
    assert rec.scope[scoped_access.SCOPE_KEY] == "feature/x"


def test_asgi_non_http_untouched():
    rec = _Recorder()
    scope = {"type": "lifespan"}
    _run(ScopedEnvASGI(rec)(scope, None, None))
    assert rec.scope is scope


# --- ScopedAccessMiddleware ------------------------------------------------


def _scope(monkeypatch, url_env, token_env):
    monkeypatch.setattr(scoped_access, "scoped_env_from_request", lambda: url_env)
    monkeypatch.setattr(scoped_access, "env_from_access_token", lambda: token_env)


def _tools():
    return [
        _FakeTool(
            "pull_and_apply",
            {"properties": {"env_name": {}, "install": {}}, "required": ["env_name"]},
        ),
        _FakeTool("get_agent_instructions", {"properties": {}}),
        _FakeTool("report_issue", {"properties": {"details": {}}}),
        _FakeTool("delete_environment", {"properties": {"env_name": {}}}),
    ]


def _middleware():
    return ScopedAccessMiddleware(env_param_tools={"pull_and_apply"})


async def _list_next(_ctx):
    return _tools()


class _Msg:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Ctx:
    def __init__(self, name, arguments=None):
        self.message = _Msg(name, arguments if arguments is not None else {})


async def _call_next(ctx):
    return ("called", ctx.message.name, ctx.message.arguments)


def test_list_full_mode_unchanged(monkeypatch):
    _scope(monkeypatch, None, None)
    result = _run(_middleware().on_list_tools(object(), _list_next))
    assert [t.name for t in result] == [
        "pull_and_apply",
        "get_agent_instructions",
        "report_issue",
        "delete_environment",
    ]


def test_list_scoped_filters_and_strips(monkeypatch):
    _scope(monkeypatch, "main", None)
    result = _run(_middleware().on_list_tools(object(), _list_next))
    assert [t.name for t in result] == [
        "pull_and_apply",
        "get_agent_instructions",
        "report_issue",
    ]
    pa = next(t for t in result if t.name == "pull_and_apply")
    assert "env_name" not in pa.parameters["properties"]
    assert pa.parameters["required"] == []


def test_list_deny_returns_empty(monkeypatch):
    _scope(monkeypatch, None, "main")  # env token at root
    assert _run(_middleware().on_list_tools(object(), _list_next)) == []


def test_call_scoped_injects_env(monkeypatch):
    _scope(monkeypatch, "main", None)
    ctx = _Ctx("pull_and_apply", {"install": ["sale"]})
    res = _run(_middleware().on_call_tool(ctx, _call_next))
    assert ctx.message.arguments["env_name"] == "main"
    assert ctx.message.arguments["install"] == ["sale"]
    assert res[0] == "called"


@pytest.mark.parametrize("tool_name", ["get_agent_instructions", "report_issue"])
def test_call_scoped_no_env_param_not_injected(monkeypatch, tool_name):
    _scope(monkeypatch, "main", None)
    ctx = _Ctx(tool_name, {})
    _run(_middleware().on_call_tool(ctx, _call_next))
    assert "env_name" not in ctx.message.arguments


def test_call_scoped_rejects_non_allowlisted(monkeypatch):
    _scope(monkeypatch, "main", None)
    ctx = _Ctx("delete_environment", {})
    with pytest.raises(ToolError):
        _run(_middleware().on_call_tool(ctx, _call_next))


def test_call_deny_rejects(monkeypatch):
    _scope(monkeypatch, "a", "b")  # cross-env
    ctx = _Ctx("pull_and_apply", {})
    with pytest.raises(ToolError):
        _run(_middleware().on_call_tool(ctx, _call_next))


def test_call_full_mode_passthrough(monkeypatch):
    _scope(monkeypatch, None, None)
    ctx = _Ctx("delete_environment", {})
    res = _run(_middleware().on_call_tool(ctx, _call_next))
    assert res[0] == "called"


def test_context_reader_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(
        scoped_access,
        "scoped_env_from_request",
        lambda: (_ for _ in ()).throw(RuntimeError("request context unavailable")),
    )
    assert _run(_middleware().on_list_tools(object(), _list_next)) == []
    with pytest.raises(ToolError):
        _run(_middleware().on_call_tool(_Ctx("pull_and_apply"), _call_next))


# --- OduflowTokenVerifier --------------------------------------------------


def _settings() -> Settings:
    return Settings(
        teams={
            "1": TeamSettings(
                team_id="1",
                auth_token="team-tok",
                port_range_start=50000,
                port_range_end=50100,
            ),
        }
    )


def test_token_verifier(monkeypatch):
    mapping = {"team-tok": ("1", None), "env-tok": ("2", "envx")}
    monkeypatch.setattr(
        scoped_access.env_tokens,
        "resolve_token",
        lambda settings, token: mapping.get(token),
    )
    v = OduflowTokenVerifier(_settings())

    team_at = _run(v.verify_token("team-tok"))
    assert team_at is not None
    assert team_at.client_id == "1"
    assert team_at.scopes == []

    env_at = _run(v.verify_token("env-tok"))
    assert env_at is not None
    assert env_at.client_id == "2"
    assert env_at.scopes == ["oduflow_env:envx"]

    assert _run(v.verify_token("nope")) is None


# --- env_ops.get_env_token -------------------------------------------------


def test_get_env_token_uses_team_scoped_container_name(monkeypatch):
    from oduflow.naming import get_resource_name

    captured = {}

    class _Container:
        labels = {"oduflow.mcp_token": "sekret"}

    class _Containers:
        def get(self, name):
            captured["name"] = name
            return _Container()

    class _Client:
        containers = _Containers()

    monkeypatch.setattr(env_ops, "get_client", lambda: _Client())
    settings = Settings(prefix="oduflow-", teams={"7": TeamSettings(team_id="7")})
    team = settings.teams["7"]

    assert env_ops.get_env_token(settings, team, "feature/x") == "sekret"
    # The container name must be team-scoped (team_id in the name); a missing
    # team_id would raise TypeError from get_resource_name.
    assert captured["name"] == get_resource_name("feature/x", "odoo", "oduflow-", "7")


# --- web_ui /mcp-access endpoint -------------------------------------------


def _ui_settings() -> Settings:
    return Settings(
        base_data_dir=tempfile.mkdtemp(prefix="oduflow-mcpacc-"),
        teams={"1": TeamSettings(team_id="1")},  # no ui_password -> open
    )


def _ui_client(settings, base_url="http://testserver") -> TestClient:
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app, base_url=base_url)


def test_mcp_access_endpoint_returns_url_and_token(monkeypatch):
    settings = _ui_settings()
    monkeypatch.setattr(env_ops, "get_env_token", lambda s, t, e: "the-secret")
    resp = _ui_client(settings).get("/api/environments/feature%2Fx/mcp-access")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["token"] == "the-secret"
    assert data["result"]["url"].endswith("/mcp/feature/x")
    assert resp.headers["cache-control"] == "no-store"


def test_mcp_access_endpoint_token_missing(monkeypatch):
    settings = _ui_settings()
    monkeypatch.setattr(env_ops, "get_env_token", lambda s, t, e: None)
    resp = _ui_client(settings).get("/api/environments/main/mcp-access")
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["token"] is None
    assert data["result"]["url"].endswith("/mcp/main")


def test_mcp_access_port_mode_uses_request_origin(monkeypatch):
    settings = _ui_settings()
    monkeypatch.setattr(env_ops, "get_env_token", lambda s, t, e: "the-secret")

    resp = _ui_client(settings, "https://oduflow.example.com").get(
        "/api/environments/main/mcp-access"
    )

    assert resp.json()["result"]["url"] == "https://oduflow.example.com/mcp/main"


def test_mcp_access_endpoint_unknown_env(monkeypatch):
    settings = _ui_settings()

    def _raise(s, t, e):
        raise NotFoundError("Environment 'ghost' does not exist.")

    monkeypatch.setattr(env_ops, "get_env_token", _raise)
    resp = _ui_client(settings).get("/api/environments/ghost/mcp-access")
    data = resp.json()
    assert data["ok"] is False


# --- request/token env extraction ------------------------------------------


class TestScopedEnvFromRequest:
    """Reads the env out of the ASGI scope set by ``ScopedEnvASGI``."""

    def test_returns_the_scoped_env(self, monkeypatch):
        request = SimpleNamespace(scope={scoped_access.SCOPE_KEY: "feature/x"})
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_request", lambda: request
        )

        assert scoped_access.scoped_env_from_request() == "feature/x"

    def test_returns_none_for_an_unscoped_request(self, monkeypatch):
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_request",
            lambda: SimpleNamespace(scope={}),
        )

        assert scoped_access.scoped_env_from_request() is None

    def test_outside_a_request_context_is_not_an_error(self, monkeypatch):
        # Called from the CLI or a background thread there is no request.
        def _boom():
            raise RuntimeError("no active request")

        monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", _boom)

        assert scoped_access.scoped_env_from_request() is None


class TestEnvFromAccessToken:
    """Reads the bound env out of a per-env token's scopes."""

    def _token(self, scopes):
        return SimpleNamespace(scopes=scopes)

    def test_returns_the_env_the_token_is_bound_to(self, monkeypatch):
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token",
            lambda: self._token(["oduflow_env:feature/x"]),
        )

        assert scoped_access.env_from_access_token() == "feature/x"

    def test_the_env_scope_is_found_among_others(self, monkeypatch):
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token",
            lambda: self._token(["openid", "oduflow_env:main", "profile"]),
        )

        assert scoped_access.env_from_access_token() == "main"

    def test_a_team_token_carries_no_env(self, monkeypatch):
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token",
            lambda: self._token(["openid"]),
        )

        assert scoped_access.env_from_access_token() is None

    def test_no_scopes_at_all(self, monkeypatch):
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token",
            lambda: self._token(None),
        )

        assert scoped_access.env_from_access_token() is None

    def test_no_token(self, monkeypatch):
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token", lambda: None
        )

        assert scoped_access.env_from_access_token() is None

    def test_outside_an_authenticated_context_is_not_an_error(self, monkeypatch):
        def _boom():
            raise RuntimeError("no auth context")

        monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", _boom)

        assert scoped_access.env_from_access_token() is None

    def test_an_empty_env_scope_yields_an_empty_string(self, monkeypatch):
        # decide() treats "" as falsy, so this stays a full-access token
        # rather than silently binding to an env named "".
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token",
            lambda: self._token(["oduflow_env:"]),
        )

        assert scoped_access.env_from_access_token() == ""


# --- ASGI path rewriting boundaries ----------------------------------------


class TestScopedEnvPath:
    _asgi = scoped_access.ScopedEnvASGI

    def test_the_env_is_everything_after_the_prefix(self):
        assert self._asgi._scoped_env("/mcp/feature/x") == "feature/x"

    def test_the_bare_prefix_is_not_a_scoped_path(self):
        # "/mcp/" carries no env name; treating it as one would scope the
        # request to an environment called "".
        assert self._asgi._scoped_env("/mcp/") is None

    def test_the_root_endpoint_is_not_scoped(self):
        assert self._asgi._scoped_env("/mcp") is None

    def test_an_unrelated_path_is_not_scoped(self):
        assert self._asgi._scoped_env("/api/environments") is None
        assert self._asgi._scoped_env("/mcpx/main") is None

    def test_a_single_character_env_is_scoped(self):
        assert self._asgi._scoped_env("/mcp/a") == "a"


class TestWellKnownPath:
    _asgi = scoped_access.ScopedEnvASGI

    def _prefix(self) -> str:
        return self._asgi._WELL_KNOWN_PREFIX

    def test_a_scoped_resource_path_maps_back_to_mcp(self):
        assert self._asgi._well_known(f"{self._prefix()}/mcp/main") == (
            f"{self._prefix()}/mcp"
        )

    def test_the_bare_scoped_prefix_is_not_rewritten(self):
        assert self._asgi._well_known(f"{self._prefix()}/mcp/") is None

    def test_the_unscoped_resource_path_is_not_rewritten(self):
        assert self._asgi._well_known(f"{self._prefix()}/mcp") is None

    def test_an_unrelated_well_known_path_is_not_rewritten(self):
        assert self._asgi._well_known("/.well-known/openid-configuration") is None
