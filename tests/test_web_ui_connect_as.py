"""REST endpoints behind the dashboard's Connect As button (issue #78).

api_connect_as mints a session and returns the cookie payload; api_env_users
feeds the user picker; api_connect_open is the same-host "Open" bridge that mints
a session and 303-redirects into the env with the session_id set via HTTP
Set-Cookie (so it overrides Odoo's HttpOnly cookie, which JS can't touch). All
delegate to odoo_ops and are exercised here with the docker layer mocked,
mirroring test_web_ui_create_lock's TestClient harness.
"""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from oduflow.errors import ExternalCommandError, NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _open_settings(tmp_path):
    # No ui_password -> the UI mounts without auth, so requests reach the API.
    return Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )


def _client(settings, locks=None):
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks or LockManager())
    return TestClient(app)


_MINT = {
    "sid": "s" * 80,
    "login": "jane@acme.com",
    "uid": "7",
    "base_url": "http://localhost:50001",
    "cookie_domain": "localhost",
    "url": "http://localhost:50001/web",
    "expires_at": "2026-07-20T00:00:00Z",
}


def test_connect_as_returns_cookie(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT)
        ) as mock,
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.post(
            "/api/environments/18.0/connect-as", json={"user": "jane@acme.com"}
        )
    data = resp.json()
    assert data["ok"] is True
    result = data["result"]
    assert result["cookie"] == {
        "name": "session_id",
        "value": "s" * 80,
        "domain": "localhost",
        "path": "/",
    }
    assert result["url"] == "http://localhost:50001/web"
    assert result["login"] == "jane@acme.com"
    # (settings, team, branch, user) — branch from the URL, user from the body.
    assert mock.call_args.args[2] == "18.0"
    assert mock.call_args.args[3] == "jane@acme.com"


def test_connect_as_defaults_to_admin(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT)
        ) as mock,
        patch("oduflow.web_ui.activity.touch"),
    ):
        client.post("/api/environments/18.0/connect-as", json={})
    assert mock.call_args.args[3] == "admin"


def test_connect_as_error_surfaced(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user",
            side_effect=NotFoundError("User 'ghost' not found"),
        ),
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.post("/api/environments/18.0/connect-as", json={"user": "ghost"})
    data = resp.json()
    assert data["ok"] is False
    assert data.get("error")


def test_connect_as_external_command_error_is_scrubbed(tmp_path, caplog):
    client = _client(_open_settings(tmp_path))
    internal_detail = "/srv/oduflow/private/path\nTraceback: registry failed"
    caplog.set_level("ERROR", logger="oduflow")
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user",
            side_effect=ExternalCommandError(
                "odoo shell (connect_as_user)", 1, internal_detail
            ),
        ),
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.post("/api/environments/18.0/connect-as", json={})

    assert resp.status_code == 500
    assert resp.json() == {
        "ok": False,
        "error": "Operation failed. Check server logs for details.",
    }
    assert internal_detail not in resp.text
    assert internal_detail in caplog.text


def test_connect_as_reads_body_before_locking(tmp_path):
    """The env lock must not be held while the request body is being read.

    request.json() waits on a client-controlled stream with no read timeout, so
    acquiring the branch lock first would let a stalled client 409 every other
    operation on the branch (and BusyError the whole team) for as long as it
    keeps the socket open.
    """
    order = []

    class _RecordingLocks(LockManager):
        def acquire_env(self, env_name, team_id=None, operation=""):
            order.append("lock")
            super().acquire_env(env_name, team_id, operation)

    client = _client(_open_settings(tmp_path), _RecordingLocks())
    real_json = Request.json

    async def _spy_json(self):
        order.append("body")
        return await real_json(self)

    with (
        patch("oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT)),
        patch("oduflow.web_ui.activity.touch"),
        patch.object(Request, "json", _spy_json),
    ):
        resp = client.post("/api/environments/18.0/connect-as", json={})

    assert resp.json()["ok"] is True
    assert order == ["body", "lock"]


def test_connect_open_redirects_and_sets_cookie(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT)
        ) as mock,
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.get(
            "/api/environments/18.0/connect-open",
            params={"user": "jane@acme.com"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost:50001/web"
    # session_id set server-side + HttpOnly so it overrides Odoo's HttpOnly cookie
    # that a JS document.cookie write cannot touch.
    set_cookie = resp.headers["set-cookie"]
    assert "session_id=" + "s" * 80 in set_cookie
    assert "httponly" in set_cookie.lower()
    # (settings, team, branch, user) — branch from the URL, user from the query.
    assert mock.call_args.args[2] == "18.0"
    assert mock.call_args.args[3] == "jane@acme.com"


def test_connect_open_defaults_to_admin(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT)
        ) as mock,
        patch("oduflow.web_ui.activity.touch"),
    ):
        client.get("/api/environments/18.0/connect-open", follow_redirects=False)
    assert mock.call_args.args[3] == "admin"


def test_connect_open_external_command_error_is_scrubbed(tmp_path, caplog):
    client = _client(_open_settings(tmp_path))
    internal_detail = "/mnt/extra-addons/private_module.py\nTraceback: registry failed"
    caplog.set_level("ERROR", logger="oduflow")
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user",
            side_effect=ExternalCommandError(
                "odoo shell (connect_as_user)", 1, internal_detail
            ),
        ),
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.get("/api/environments/18.0/connect-open", follow_redirects=False)

    assert resp.status_code == 500
    assert (
        resp.text == "Connect failed: Operation failed. Check server logs for details."
    )
    assert internal_detail not in resp.text
    assert internal_detail in caplog.text


def test_connect_open_unexpected_error_is_scrubbed(tmp_path):
    client = _client(_open_settings(tmp_path))
    internal_detail = "/srv/oduflow/private/unexpected"
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user",
            side_effect=RuntimeError(internal_detail),
        ),
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.get("/api/environments/18.0/connect-open", follow_redirects=False)

    assert resp.status_code == 500
    assert resp.text == "Connect failed: Internal server error."
    assert internal_detail not in resp.text


def test_connect_open_rejects_lifecycle_race(tmp_path):
    locks = LockManager()
    locks.acquire_env("18.0", "1", operation="delete_environment")
    client = _client(_open_settings(tmp_path), locks)
    try:
        with patch("oduflow.web_ui.odoo_ops.connect_as_user") as connect:
            resp = client.get(
                "/api/environments/18.0/connect-open", follow_redirects=False
            )
    finally:
        locks.release_env("18.0")

    assert resp.status_code == 409
    assert "Another operation on environment '18.0'" in resp.text
    assert "delete_environment" in resp.text
    connect.assert_not_called()


def _traefik_settings(tmp_path):
    return Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", hostname="dev.example.com")},
    )


_MINT_TRAEFIK = {
    "sid": "t" * 80,
    "login": "jane@acme.com",
    "uid": "7",
    "base_url": "https://180.dev.example.com",
    "cookie_domain": "180.dev.example.com",
    "url": "https://180.dev.example.com/web",
    "expires_at": "2026-07-20T00:00:00Z",
}


def test_connect_open_traefik_redirects_to_token_landing(tmp_path):
    # In traefik mode the env is on its own host, so connect-open does NOT set a
    # cookie here; it hands the browser a token and lands it on the env host.
    client = _client(_traefik_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT_TRAEFIK)
        ),
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.get(
            "/api/environments/18.0/connect-open",
            params={"user": "jane@acme.com"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("https://180.dev.example.com/oduflow-connect?token=")
    assert "session_id" not in resp.headers.get("set-cookie", "")


def test_connect_land_sets_host_only_cookie_and_redirects(tmp_path):
    from urllib.parse import parse_qs, urlparse

    client = _client(_traefik_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT_TRAEFIK)
        ),
        patch("oduflow.web_ui.activity.touch"),
    ):
        r1 = client.get(
            "/api/environments/18.0/connect-open",
            params={"user": "jane@acme.com"},
            follow_redirects=False,
        )
    token = parse_qs(urlparse(r1.headers["location"]).query)["token"][0]

    # Land on the env host (Traefik would route /oduflow-connect here). The
    # redirect is relative, so the browser keeps whatever scheme and host it
    # really arrived on — no forwarded-header interpretation involved.
    r2 = client.get(
        "/oduflow-connect",
        params={"token": token},
        headers={"host": "180.dev.example.com"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers["location"] == "/web"
    set_cookie = r2.headers["set-cookie"]
    assert "session_id=" + "t" * 80 in set_cookie
    assert "httponly" in set_cookie.lower()
    # Host-only: no Domain attribute, so it overrides Odoo's own host cookie.
    assert "domain=" not in set_cookie.lower()
    # One-time: reusing the token now fails.
    r3 = client.get(
        "/oduflow-connect",
        params={"token": token},
        headers={"host": "180.dev.example.com"},
        follow_redirects=False,
    )
    assert r3.status_code == 400


def test_connect_land_rejects_unknown_token(tmp_path):
    client = _client(_traefik_settings(tmp_path))
    r = client.get(
        "/oduflow-connect",
        params={"token": "nope"},
        headers={"host": "180.dev.example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_env_users_lists(tmp_path):
    client = _client(_open_settings(tmp_path))
    users = [
        {"login": "admin", "name": "Mitchell Admin", "share": False, "portal": False},
        {"login": "portal", "name": "Portal User", "share": True, "portal": True},
    ]
    with patch("oduflow.web_ui.odoo_ops.list_env_users", return_value=users):
        resp = client.get("/api/environments/18.0/users")
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["users"] == users
