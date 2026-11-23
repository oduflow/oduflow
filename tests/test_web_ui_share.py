"""Sharing one environment's dashboard: /env/<name> and the scoped session.

The operator mints a link from the environment card; whoever opens it trades
its ``?key=`` for a scoped cookie and gets the dashboard reduced to that one
environment. The server-side allowlist (``oduflow.ui_scope``), not the page
rendering, is the boundary — these tests exercise it through the real app.
"""

from __future__ import annotations

import tempfile

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from oduflow import env_share, ui_scope, web_ui
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings

_PW = "s3cret"
_ENV = "feature/x"
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-share-test-")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        routing_mode="port",
        base_data_dir=_DATA_DIR,
        teams={
            "1": TeamSettings(
                team_id="1", ui_password=_PW, data_dir=str(tmp_path / "team1")
            )
        },
    )


@pytest.fixture
def app(settings, monkeypatch) -> Starlette:
    # Every route under test is either pure web plumbing or stubbed here; no
    # Docker daemon is involved.
    monkeypatch.setattr(
        web_ui.env_ops,
        "list_environments",
        lambda s, team: [
            {"env_name": _ENV, "branch": _ENV, "containers": []},
            {"env_name": "other", "branch": "other", "containers": []},
        ],
    )
    monkeypatch.setattr(
        web_ui.env_ops, "require_environment", lambda s, team, env_name: None
    )
    application = Starlette()
    web_ui.mount_web_ui(application, lambda: settings, LockManager())
    return application


def _operator(app) -> TestClient:
    client = TestClient(app)
    resp = client.post("/login", data={"password": _PW}, follow_redirects=False)
    assert resp.status_code == 303
    return client


def _share_url(app) -> str:
    client = _operator(app)
    resp = client.post(f"/api/environments/{_ENV}/share")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["shared"] is True
    return str(result["url"])


def _visitor(app, url: str) -> TestClient:
    """A browser that follows a share link and keeps its cookie."""
    client = TestClient(app)
    path = url.split("/", 3)[3]
    resp = client.get("/" + path, follow_redirects=False)
    assert resp.status_code == 200, resp.text
    assert ui_scope.SHARE_COOKIE in client.cookies
    return client


# --- minting the link ----------------------------------------------------


def test_share_link_points_at_the_scoped_page(app, settings):
    url = _share_url(app)
    assert f"/env/{_ENV}?key=" in url
    key = url.split("key=", 1)[1]
    assert env_share.verify(settings.teams["1"], _ENV, key)


def test_share_status_round_trip(app):
    client = _operator(app)
    assert client.get(f"/api/environments/{_ENV}/share").json()["result"] == {
        "shared": False,
        "url": None,
        "created_at": None,
    }
    client.post(f"/api/environments/{_ENV}/share")
    result = client.get(f"/api/environments/{_ENV}/share").json()["result"]
    assert result["shared"] is True and result["created_at"]
    client.post(f"/api/environments/{_ENV}/share/revoke")
    assert client.get(f"/api/environments/{_ENV}/share").json()["result"]["shared"] is (
        False
    )


def test_sharing_requires_the_operator_session(app):
    client = TestClient(app)
    assert client.post(f"/api/environments/{_ENV}/share").status_code == 401


# --- opening the link ----------------------------------------------------


def test_key_is_exchanged_for_a_cookie_and_dropped_from_the_url(app):
    url = _share_url(app)
    client = TestClient(app)
    resp = client.get("/" + url.split("/", 3)[3], follow_redirects=False)
    # A page, not an HTTP redirect: a link opened from another site keeps that
    # site as initiator across redirects, and the browser would then withhold
    # the SameSite=Strict cookie from the landing request.
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert f'<meta http-equiv="refresh" content="0;url=/env/{_ENV}">' in resp.text
    assert f'location.replace("/env/{_ENV}")' in resp.text
    # Landing URL carries no key: it must not sit in history or a Referer.
    assert "key=" not in resp.text
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{ui_scope.SHARE_COOKIE}=")
    assert "HttpOnly" in cookie and "strict" in cookie.lower()


def test_scoped_page_renders_for_the_cookie_holder(app):
    client = _visitor(app, _share_url(app))
    resp = client.get(f"/env/{_ENV}")
    assert resp.status_code == 200
    assert f'data-scoped-env="{_ENV}"' in resp.text
    # The chat-first workspace, for a share-link visitor.
    assert "/static/workspace/assets/" in resp.text
    assert 'data-viewer="share"' in resp.text


def test_engineer_view_is_the_scoped_dashboard(app):
    client = _visitor(app, _share_url(app))
    resp = client.get(f"/env/{_ENV}?view=console")
    assert resp.status_code == 200
    assert f'data-scoped-env="{_ENV}"' in resp.text
    assert "environments.filter(function (env)" in resp.text
    assert "/static/workspace/assets/" not in resp.text


def test_workspace_escapes_the_environment_name(app):
    client = _operator(app)
    resp = client.get('/env/x"><script>alert(1)</script>')
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert 'data-scoped-env="x&quot;&gt;&lt;script&gt;' in resp.text


def test_page_without_a_key_or_cookie_is_refused(app):
    client = TestClient(app)
    assert client.get(f"/env/{_ENV}").status_code == 401


def test_wrong_and_revoked_keys_are_refused(app):
    url = _share_url(app)
    assert TestClient(app).get(f"/env/{_ENV}?key=nope").status_code == 403
    _operator(app).post(f"/api/environments/{_ENV}/share/revoke")
    assert TestClient(app).get("/" + url.split("/", 3)[3]).status_code == 403


def test_operator_can_preview_the_shared_view(app):
    client = _operator(app)
    resp = client.get(f"/env/{_ENV}")
    assert resp.status_code == 200
    assert f'data-scoped-env="{_ENV}"' in resp.text
    # An operator's workspace links back to the dashboard instead of "Leave".
    assert 'data-viewer="operator"' in resp.text
    resp = client.get(f"/env/{_ENV}?view=console")
    assert "environments.filter(function (env)" in resp.text


def test_share_cookie_scopes_an_existing_operator_session(app):
    client = _operator(app)
    result = client.post(f"/api/environments/{_ENV}/share").json()["result"]
    path = str(result["url"]).split("/", 3)[3]

    assert client.get("/" + path, follow_redirects=False).status_code == 200
    envs = client.get("/api/environments").json()["environments"]
    assert [e["env_name"] for e in envs] == [_ENV]


# --- what the scoped session may do --------------------------------------


def test_environment_list_is_filtered_to_the_shared_environment(app):
    client = _visitor(app, _share_url(app))
    envs = client.get("/api/environments").json()["environments"]
    assert [e["env_name"] for e in envs] == [_ENV]


@pytest.mark.parametrize("path", ["/", "/bookmarked-page"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_page_navigation_returns_to_the_shared_workspace(app, path, method):
    client = _visitor(app, _share_url(app))
    resp = client.request(method, path, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/env/{_ENV}"
    page = client.get(resp.headers["location"])
    assert page.status_code == 200
    assert f'data-scoped-env="{_ENV}"' in page.text
    # A share-link viewer gets the workspace's "Leave" control (and the
    # Engineer view keeps the dashboard's own).
    assert 'data-viewer="share"' in page.text
    console = client.get(resp.headers["location"] + "?view=console")
    assert 'onclick="logout()"' in console.text


def test_team_surfaces_are_denied(app):
    client = _visitor(app, _share_url(app))
    for path in ("/api/templates", "/api/services", "/api/volumes", "/api/stats"):
        resp = client.get(path, headers={"accept": "text/html"})
        assert resp.status_code == 403, path
        assert resp.json()["error"] == "Not available for a shared link."
    assert client.post("/api/environments/create", json={}).status_code == 403
    assert client.post("/", follow_redirects=False).status_code == 403


def test_destructive_actions_on_the_shared_environment_are_denied(app):
    client = _visitor(app, _share_url(app))
    assert client.post("/api/cleanup", json={"force": True}).status_code == 403
    for action in ("delete", "recreate", "update", "protect", "save-as-template"):
        resp = client.post(f"/api/environments/{_ENV}/{action}")
        assert resp.status_code == 403, action


def test_other_environments_are_denied(app):
    client = _visitor(app, _share_url(app))
    assert client.get("/api/environments/other/logs").status_code == 403
    assert client.post("/api/environments/other/restart").status_code == 403


def test_a_share_link_cannot_manage_its_own_sharing(app):
    client = _visitor(app, _share_url(app))
    assert client.get(f"/api/environments/{_ENV}/share").status_code == 403
    assert client.post(f"/api/environments/{_ENV}/share/rotate").status_code == 403
    assert client.post(f"/api/environments/{_ENV}/share/revoke").status_code == 403


def test_agent_cli_websocket_reaches_the_handler_for_a_share_link(
    app, settings, monkeypatch
):
    # The handler must bail at its agent_enabled check before ever touching
    # the stub client below; make that precondition of the fixture explicit.
    assert settings.teams["1"].agent_enabled is False
    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: object())
    client = _visitor(app, _share_url(app))
    with client.websocket_connect(f"/api/environments/{_ENV}/agent") as websocket:
        message = websocket.receive_text()
        close = websocket.receive()
    # Past the scope middleware: the handler itself answers, here because the
    # test team has no coding agent enabled.
    assert "coding agent is disabled" in message
    assert close["code"] == 1011


class _FakeExecSocket:
    """The raw socket behind `docker exec`: one banner, then EOF."""

    def __init__(self) -> None:
        self._chunks = [b"agent ready\r\n"]

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, _data: bytes) -> None:
        pass

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        pass


def test_agent_cli_exec_runs_as_the_environments_uid_with_its_scoped_token(
    app, settings, monkeypatch
):
    """The core invariant of opening Agent CLI to share links: the PTY is
    exec'd as the environment's own uid with only that environment's scoped
    MCP token, never as a shared identity (ADR 0079)."""
    import dataclasses

    from oduflow.docker_ops import env_ops
    from oduflow.naming import get_agent_checkout_dir

    settings.teams["1"] = dataclasses.replace(settings.teams["1"], agent_enabled=True)

    uid = 4242
    token = "scoped-token-for-feature-x"
    execs: list[dict] = []

    class _FakeApi:
        def exec_create(self, container_id, cmd, **kwargs):
            execs.append({"container": container_id, "cmd": cmd, **kwargs})
            return {"Id": "exec-1"}

        def exec_start(self, exec_id, **kwargs):
            sock = _FakeExecSocket()
            outer = type("S", (), {"_sock": sock, "close": lambda self: None})()
            return outer

        def exec_resize(self, exec_id, **kwargs):
            pass

    class _FakeContainers:
        def get(self, name):
            return type("C", (), {"id": "agent-ctr", "status": "running"})()

    class _FakeClient:
        api = _FakeApi()
        containers = _FakeContainers()

    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: _FakeClient())
    monkeypatch.setattr(env_ops, "agent_env_uid", lambda container, team, env_name: uid)
    monkeypatch.setattr(
        env_ops, "refresh_agent_mcp_config", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(env_ops, "get_env_token", lambda s, t, e: token)

    client = _visitor(app, _share_url(app))
    with client.websocket_connect(f"/api/environments/{_ENV}/agent") as websocket:
        assert "agent ready" in websocket.receive_text()

    assert len(execs) == 1
    exec_call = execs[0]
    expected_user, expected_env = env_ops.agent_exec_identity(uid)
    # The environment's own uid, not root and not a team-shared account.
    assert exec_call["user"] == expected_user == f"{uid}:{uid}"
    # Its private HOME/TMPDIR and ONLY its scoped token; the team auth token
    # never enters the exec environment.
    environment = exec_call["environment"]
    assert environment["HOME"] == expected_env["HOME"]
    assert environment["ODUFLOW_MCP_TOKEN"] == token
    assert environment["ODUFLOW_MCP_URL"].endswith(f"/mcp/{_ENV}")
    assert exec_call["workdir"] == get_agent_checkout_dir(_ENV)
    # Wrapped by agent_session_command (private umask), running the agent CLI.
    assert exec_call["cmd"][:2] == ["sh", "-c"]
    assert "claude" in exec_call["cmd"]


def test_agent_cli_websocket_of_another_environment_is_refused(app):
    client = _visitor(app, _share_url(app))
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/environments/other/agent"):
            pass
    # 1008 (policy violation) is the middleware's refusal, not a handler error:
    # the request never reaches the agent container.
    assert excinfo.value.code == 1008


def test_feedback_is_available_from_a_shared_view(app):
    client = _visitor(app, _share_url(app))
    resp = client.post(
        "/api/feedback/link",
        json={"kind": "feedback", "details": "Feedback from a shared view"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["url"].startswith("https://github.com/")


# --- ending access -------------------------------------------------------


def test_rotating_the_link_invalidates_live_sessions(app):
    client = _visitor(app, _share_url(app))
    assert client.get("/api/environments").status_code == 200
    _operator(app).post(f"/api/environments/{_ENV}/share/rotate")
    # The cookie carries a fingerprint of the secret it was minted from.
    assert client.get("/api/environments").status_code == 401


def test_revoking_the_link_invalidates_live_sessions(app):
    client = _visitor(app, _share_url(app))
    _operator(app).post(f"/api/environments/{_ENV}/share/revoke")
    assert client.get("/api/environments").status_code == 401


def test_scoped_logout_explains_both_ways_back_in(app):
    client = _visitor(app, _share_url(app))
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    # Back to their own page, which tells them how to return.
    assert resp.headers["location"] == f"/env/{_ENV}"
    assert ui_scope.SHARE_COOKIE not in client.cookies
    assert client.get("/api/environments").status_code == 401

    page = client.get(resp.headers["location"])
    assert page.status_code == 401
    assert "?key=" in page.text
    assert "/login" in page.text

    # And an operator who previewed the link can still sign in from there.
    resp = client.post("/login", data={"password": _PW})
    assert resp.status_code == 200
    assert resp.url.path == "/"
    envs = client.get("/api/environments").json()["environments"]
    assert [e["env_name"] for e in envs] == [_ENV, "other"]


def test_scoped_logout_restores_an_existing_operator_session(app):
    client = _operator(app)
    result = client.post(f"/api/environments/{_ENV}/share").json()["result"]
    path = str(result["url"]).split("/", 3)[3]
    assert client.get("/" + path, follow_redirects=False).status_code == 200
    assert len(client.get("/api/environments").json()["environments"]) == 1
    page = client.get("/")
    assert page.url.path == f"/env/{_ENV}"
    assert page.status_code == 200

    resp = client.post("/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert ui_scope.SHARE_COOKIE not in client.cookies
    assert client.get("/").status_code == 200
    envs = client.get("/api/environments").json()["environments"]
    assert [e["env_name"] for e in envs] == [_ENV, "other"]


def test_logout_signs_out_an_operator_holding_a_revoked_share_cookie(app):
    client = _operator(app)
    result = client.post(f"/api/environments/{_ENV}/share").json()["result"]
    path = str(result["url"]).split("/", 3)[3]
    assert client.get("/" + path, follow_redirects=False).status_code == 200
    _operator(app).post(f"/api/environments/{_ENV}/share/revoke")
    # The dead cookie no longer scopes anything: they are back on the full
    # dashboard, so Logout has to mean a real sign-out.
    envs = client.get("/api/environments").json()["environments"]
    assert [e["env_name"] for e in envs] == [_ENV, "other"]

    resp = client.post("/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert web_ui._AUTH_COOKIE not in client.cookies
    assert ui_scope.SHARE_COOKIE not in client.cookies
    assert client.get("/api/environments").status_code == 401


def test_deleting_the_environment_drops_its_share(app, settings, monkeypatch):
    team = settings.teams["1"]
    secret = env_share.create_or_get(team, _ENV)
    assert env_share.verify(team, _ENV, secret)
    env_share.remove(team, _ENV)
    assert env_share.get(team, _ENV) is None


def test_shared_links_remain_scoped_without_otp_after_enrollment_and_reset(
    app, settings
):
    import pyotp

    from oduflow import ui_totp

    url = _share_url(app)
    existing = _visitor(app, url)
    team = settings.teams["1"]
    secret = pyotp.random_base32()
    ui_totp.enroll(settings, team, secret, pyotp.TOTP(secret).now())
    # Both an existing guest session and a newly opened link stay usable.
    for visitor in (existing, _visitor(app, url)):
        assert visitor.get(f"/env/{_ENV}").status_code == 200
        assert visitor.get("/api/license").status_code == 403
        assert visitor.get(f"/api/environments/{_ENV}/share").status_code == 403
        assert web_ui._AUTH_COOKIE not in visitor.cookies
    ui_totp.reset(settings, team)
    assert existing.get(f"/env/{_ENV}").status_code == 200
    assert existing.get("/api/license").status_code == 403
