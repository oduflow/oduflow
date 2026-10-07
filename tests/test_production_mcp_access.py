"""Credential separation, including real HTTP MCP calls through both middlewares."""

import asyncio
from dataclasses import replace

import pytest
from fastmcp import FastMCP
from fastmcp.server.http import create_streamable_http_app
from starlette.testclient import TestClient

from oduflow import production_access as access
from oduflow.oauth_provider import OduflowOAuthProvider
from oduflow.scoped_access import (
    OduflowTokenVerifier,
    ScopedAccessMiddleware,
    ScopedEnvASGI,
)
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def settings(tmp_path):
    team = TeamSettings(
        team_id="1",
        auth_token="dev-key",
        production_token="p" * 40,
        data_dir=str(tmp_path),
    )
    return Settings(
        teams={"1": team}, base_data_dir=str(tmp_path), etc_dir=str(tmp_path)
    )


def test_tokens_must_not_overlap(settings):
    team = settings.teams["1"]
    settings.teams["1"] = replace(team, auth_token=team.production_token)
    with pytest.raises(ValueError, match="distinct"):
        settings.validate()


def test_production_token_not_dev_token(settings):
    assert settings.get_team_by_token("p" * 40) is None
    assert settings.get_team_by_production_token("dev-key") is None
    token = asyncio.run(OduflowTokenVerifier(settings).verify_token("p" * 40))
    assert token.client_id == "1"
    assert token.scopes == [access.PRODUCTION_SCOPE]


def test_oauth_accepts_production_bearer_without_odoo(settings):
    provider = OduflowOAuthProvider(settings)
    token = asyncio.run(provider.load_access_token("p" * 40))
    assert token.client_id == "1"
    assert token.scopes == [access.PRODUCTION_SCOPE]


def test_cross_team_production_token_collision(settings):
    settings.teams["2"] = replace(
        settings.teams["1"],
        team_id="2",
        hostname="other.example.com",
        auth_token="other-dev",
        port_range_start=50100,
        port_range_end=50200,
    )
    with pytest.raises(ValueError, match="distinct"):
        settings.validate()


@pytest.fixture
def http_client(settings):
    server = FastMCP("access-test")

    @server.tool()
    def create_environment(env_name: str = "test") -> str:
        return "dev reached"

    @server.tool()
    def list_productions() -> str:
        access.require_production_access()
        return "production reached"

    server.add_middleware(ScopedAccessMiddleware({"create_environment"}))
    server.add_middleware(access.ProductionAccessMiddleware(lambda: settings))
    app = create_streamable_http_app(
        server,
        "/mcp",
        auth=OduflowTokenVerifier(settings),
        stateless_http=True,
        json_response=True,
    )
    wrapped = access.ProductionASGI(ScopedEnvASGI(app))
    with TestClient(wrapped) as client:
        yield client


def rpc(client, path, token, method, params=None):
    response = client.post(
        path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "path,token,expected",
    [
        ("/mcp", "dev-key", ["create_environment"]),
        ("/production", "p" * 40, ["list_productions"]),
    ],
)
def test_fixed_tool_surfaces_even_when_production_disabled(
    http_client, path, token, expected
):
    result = rpc(http_client, path, token, "tools/list")
    assert [tool["name"] for tool in result["result"]["tools"]] == expected


@pytest.mark.parametrize(
    "path,token,name,allowed",
    [
        ("/mcp", "dev-key", "create_environment", True),
        ("/mcp", "dev-key", "list_productions", False),
        ("/production", "p" * 40, "list_productions", True),
        ("/production", "p" * 40, "create_environment", False),
        ("/production", "dev-key", "list_productions", False),
        ("/mcp", "p" * 40, "create_environment", False),
        ("/mcp/dev", "p" * 40, "create_environment", False),
    ],
)
def test_calls_enforce_route_and_credential(http_client, path, token, name, allowed):
    result = rpc(
        http_client, path, token, "tools/call", {"name": name, "arguments": {}}
    )
    failed = "error" in result or result.get("result", {}).get("isError", False)
    assert failed is not allowed


def test_dev_cannot_address_production_namespace(http_client):
    result = rpc(
        http_client,
        "/mcp",
        "dev-key",
        "tools/call",
        {"name": "create_environment", "arguments": {"env_name": "prod-erp"}},
    )
    assert "error" in result or result["result"].get("isError")


def test_shared_cluster_mutations_require_owner_on_multi_team(settings, monkeypatch):
    from types import SimpleNamespace

    from fastmcp.exceptions import ToolError

    settings.teams["2"] = replace(
        settings.teams["1"], team_id="2", production_token="q" * 40
    )
    middleware = access.ProductionAccessMiddleware(lambda: settings)
    monkeypatch.setattr(middleware, "_production", lambda: True)
    context = SimpleNamespace(
        message=SimpleNamespace(name="restore_cluster_pitr", arguments={})
    )

    async def backend(_context):
        pytest.fail("cluster restore must not reach the backend")

    with pytest.raises(ToolError, match="every team's"):
        asyncio.run(middleware.on_call_tool(context, backend))


def test_every_production_decorator_is_in_separate_surface():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/oduflow/server.py").read_text())
    names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "production_enabled"
            for decorator in node.decorator_list
        )
    }
    assert names == access.PRODUCTION_TOOLS


@pytest.mark.parametrize(
    "route,production,owner,allowed",
    [
        (False, True, "1", False),
        (True, True, "1", True),
        (True, True, "2", False),
        (True, False, "", False),
        (False, False, "", True),
    ],
)
def test_cached_output_respects_production_and_team(
    monkeypatch, route, production, owner, allowed
):
    from types import SimpleNamespace

    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(
        access,
        "get_access_token",
        lambda: SimpleNamespace(
            client_id="1", scopes=[access.PRODUCTION_SCOPE] if route else []
        ),
    )
    active_token = access._HTTP_ACTIVE.set(True)
    route_token = access._PRODUCTION_ROUTE.set(route)
    try:
        if allowed:
            access.check_cached_output_access(production, owner)
        else:
            with pytest.raises(ToolError):
                access.check_cached_output_access(production, owner)
    finally:
        access._HTTP_ACTIVE.reset(active_token)
        access._PRODUCTION_ROUTE.reset(route_token)


def test_non_ascii_bearer_is_rejected_not_crashed(settings):
    """A crafted header must be an ordinary 401, never a 500 from the lookup."""
    assert settings.get_team_by_token("ключ") is None
    assert settings.get_team_by_production_token("ключ") is None
    assert settings.get_team_by_ui_password("паролü") is None
    assert asyncio.run(OduflowTokenVerifier(settings).verify_token("ключ")) is None
    assert asyncio.run(OduflowOAuthProvider(settings).load_access_token("ключ")) is None


def test_non_ascii_bearer_over_http_is_unauthorized(http_client):
    response = http_client.post(
        "/mcp",
        headers={
            # Raw bytes: a real client can put any latin-1 byte in a header,
            # which the server decodes back into a non-ASCII str.
            "Authorization": "Bearer ké".encode("latin-1"),
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 401


def service_database_call(settings, monkeypatch, record, name="cache"):
    """Run the dev-surface service-database gate with no credential in scope."""
    from types import SimpleNamespace

    from oduflow import server, service_database_credentials

    team = settings.teams["1"]
    if record is not None:
        service_database_credentials.save(team, name, record)
    monkeypatch.setattr(access, "get_access_token", lambda: None)
    monkeypatch.setattr(server, "_resolve_team", lambda ctx=None: team)
    middleware = access.ProductionAccessMiddleware(lambda: settings)
    context = SimpleNamespace(
        message=SimpleNamespace(name="get_service_database", arguments={"name": name})
    )

    async def backend(_context):
        return "tool reached"

    return asyncio.run(middleware.on_call_tool(context, backend))


def dev_record():
    return {
        "name": "cache",
        "database": "cache_db",
        "username": "cache_user",
        "password": "secret",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def test_service_database_reachable_without_a_credential(settings, monkeypatch):
    """stdio and allow_insecure_http have no access token; the tool must still run."""
    assert service_database_call(settings, monkeypatch, dev_record()) == "tool reached"


def test_production_service_database_still_blocked_without_a_credential(
    settings, monkeypatch
):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="administrator access"):
        service_database_call(
            settings, monkeypatch, {**dev_record(), "cluster": "prod"}
        )


def test_unknown_service_database_defers_to_the_tool(settings, monkeypatch):
    """The middleware bypasses handle_errors, so it must not own this error."""
    assert service_database_call(settings, monkeypatch, None) == "tool reached"
