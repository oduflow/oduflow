"""Dashboard login, signed sessions, and rejection of legacy Basic credentials."""

from __future__ import annotations

import base64
import tempfile

import pytest
from starlette.applications import Starlette
from starlette.routing import Router, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from oduflow import web_ui
from oduflow.licensing import TYPE_INDIVIDUAL, TYPE_UNLICENSED, LicenseInfo
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import (
    _AUTH_COOKIE,
    UIAuthMiddleware,
    _check_cookie_token,
    _get_signer,
    _make_ui_token,
    mount_web_ui,
)

_PW = "s3cret"

# A single shared data dir so every ``_settings()`` instance reads the same
# persisted signing secret (mirrors one real server with one data dir).
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-uiauth-test-")


def _settings(
    routing_mode: str = "port", etc_dir: str = "", *, prod_enabled: bool = False
) -> Settings:
    return Settings(
        routing_mode=routing_mode,
        prod_enabled=prod_enabled,
        base_data_dir=_DATA_DIR,
        etc_dir=etc_dir,
        teams={
            "1": TeamSettings(team_id="1", ui_password=_PW),
        },
    )


def _team(settings: Settings) -> TeamSettings:
    return settings.teams["1"]


def _basic(user: str, password: str) -> dict[str, str]:
    blob = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {blob}"}


def _full_app(settings: Settings) -> Starlette:
    """The real web UI sub-app (auto-wrapped in UIAuthMiddleware since a team
    has a ui_password)."""
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return app


# --- token helpers -------------------------------------------------------


def test_token_roundtrip():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    assert _check_cookie_token(token, settings) is _team(settings)


def test_token_rejects_tampered_and_garbage():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Flip the first character (payload start) -> signature no longer matches.
    # (The *last* character is unsafe to flip: it is the final base64 char of
    # the signature, whose two low bits are padding — '0' vs '1' can decode to
    # the same bytes, which made this test flaky.)
    flipped = ("0" if token[0] != "0" else "1") + token[1:]
    assert _check_cookie_token(flipped, settings) is None
    assert _check_cookie_token("1.deadbeef", settings) is None
    assert _check_cookie_token("nope", settings) is None
    assert _check_cookie_token("", settings) is None


def test_token_rejects_unknown_team():
    settings = _settings()
    # Validly-signed (same secret) but for a team that is not configured.
    token = _get_signer(settings).dumps(["99", "deadbeef"])
    assert _check_cookie_token(token, settings) is None


def test_token_rejects_foreign_secret(tmp_path):
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # A different data dir => a different signing secret => token must not pass.
    other = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", ui_password=_PW)},
    )
    web_ui._signers.pop(str(tmp_path), None)
    web_ui._secrets_cache.pop(str(tmp_path), None)
    assert _check_cookie_token(token, other) is None


def test_token_expires(monkeypatch):
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Any positive age now exceeds the (negative) max age -> SignatureExpired.
    monkeypatch.setattr(web_ui, "_SESSION_MAX_AGE", -1)
    assert _check_cookie_token(token, settings) is None


def test_secret_persists_across_restart():
    """A persistent server secret means tokens survive a process restart."""
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Simulate a restart: drop the in-memory caches; the secret file in the
    # data dir is re-read, yielding the same key.
    web_ui._signers.clear()
    web_ui._secrets_cache.clear()
    assert _check_cookie_token(token, settings) is _team(settings)


def test_token_revoked_on_password_change():
    """Changing ui_password invalidates outstanding cookies immediately."""
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Same team, same data dir/secret, but a new password: the embedded
    # fingerprint no longer matches -> the cookie is rejected at once.
    changed = Settings(
        routing_mode="port",
        base_data_dir=_DATA_DIR,
        teams={"1": TeamSettings(team_id="1", ui_password="new-password")},
    )
    assert _check_cookie_token(token, changed) is None


def test_token_rejected_when_ui_password_cleared():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Auth turned off for the team afterwards -> cookie no longer authenticates.
    disabled = Settings(
        routing_mode="port",
        base_data_dir=_DATA_DIR,
        teams={"1": TeamSettings(team_id="1", ui_password="")},
    )
    assert _check_cookie_token(token, disabled) is None


# --- HTTP dashboard ------------------------------------------------------


def test_dashboard_redirects_to_login_when_unauthenticated():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    # No Basic dialog is triggered anymore.
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_login_page_is_public():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "password" in resp.text.lower()


def test_login_success_sets_cookie_and_redirects():
    client = TestClient(_full_app(_settings()))
    resp = client.post("/login", data={"password": _PW}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert f"{_AUTH_COOKIE}=" in resp.headers.get("set-cookie", "")
    # The minted cookie now authenticates the dashboard without Basic.
    assert client.get("/").status_code == 200


def test_login_wrong_password_rejected():
    client = TestClient(_full_app(_settings()))
    resp = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert resp.status_code == 401
    assert _AUTH_COOKIE not in resp.headers.get("set-cookie", "")


def test_login_rate_limited_after_repeated_failures():
    # Issue #56: the login endpoint must throttle brute-force attempts.
    client = TestClient(_full_app(_settings()))
    for _ in range(10):
        resp = client.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert resp.status_code == 401
    # The 11th attempt (and beyond) is locked out, even with the right password.
    resp = client.post("/login", data={"password": _PW}, follow_redirects=False)
    assert resp.status_code == 429
    assert _AUTH_COOKIE not in resp.headers.get("set-cookie", "")


def test_login_redirects_when_already_authenticated():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    client = TestClient(_full_app(settings))
    client.cookies.set(_AUTH_COOKIE, token)
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_logout_clears_cookie():
    client = TestClient(_full_app(_settings()))
    client.post("/login", data={"password": _PW})
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert _AUTH_COOKIE in set_cookie
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


def test_api_unauthenticated_is_401_without_basic_challenge():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/api/license", follow_redirects=False)
    assert resp.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_api_session_auth_works(tmp_path):
    client = TestClient(_full_app(_settings(etc_dir=str(tmp_path / "conf"))))
    client.post("/login", data={"password": _PW})
    resp = client.get("/api/license")
    assert resp.status_code == 200
    assert resp.json()["license"]["type"] == TYPE_UNLICENSED


def test_api_license_activate_uses_settings_etc_dir(tmp_path, monkeypatch):
    calls = {}

    def install_license_from_text(key_text: str, etc_dir: str | None = None):
        calls["key_text"] = key_text
        calls["etc_dir"] = etc_dir
        return LicenseInfo(
            type=TYPE_INDIVIDUAL,
            name="Ada",
            email="ada@example.com",
        )

    monkeypatch.setattr(web_ui, "install_license_from_text", install_license_from_text)
    settings = _settings(etc_dir=str(tmp_path / "conf"))
    client = TestClient(_full_app(settings))

    client.post("/login", data={"password": _PW})
    resp = client.post(
        "/api/license/activate",
        json={"key": " fake-key "},
    )

    assert resp.status_code == 200
    assert resp.json()["license"]["name"] == "Ada"
    assert calls == {"key_text": "fake-key", "etc_dir": settings.etc_dir}


@pytest.mark.parametrize("path,status", [("/", 302), ("/api/license", 401)])
def test_basic_auth_is_rejected(path, status):
    client = TestClient(_full_app(_settings()))
    resp = client.get(path, headers=_basic("admin", _PW), follow_redirects=False)
    assert resp.status_code == status
    assert _AUTH_COOKIE not in client.cookies


def test_dashboard_cookie_fallback_without_basic():
    """The core regression: a request carrying only the cookie (no Authorization
    header) is the exact shape of a browser WebSocket handshake."""
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    client = TestClient(_full_app(settings))
    client.cookies.set(_AUTH_COOKIE, token)
    resp = client.get("/")
    assert resp.status_code == 200


def test_dashboard_invalid_cookie_redirects_to_login():
    client = TestClient(_full_app(_settings()))
    client.cookies.set(_AUTH_COOKIE, "1.deadbeef")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_non_ascii_cookie_does_not_crash():
    """A malformed/non-ASCII cookie must be rejected cleanly, never raise.

    The previous HMAC implementation called ``hmac.compare_digest`` on the raw
    cookie value, which raises ``TypeError`` on non-ASCII input (an
    unauthenticated 500). ``itsdangerous`` rejects it as ``BadData`` instead.
    """
    settings = _settings()
    assert _check_cookie_token("1.é", settings) is None
    assert _check_cookie_token("é", settings) is None


@pytest.mark.parametrize(
    "mode,proto,secure",
    [
        ("port", "", False),
        ("port", "https", True),
        ("port", "https, http", True),
        ("traefik", "", False),
    ],
)
def test_cookie_secure_flag(mode, proto, secure):
    client = TestClient(_full_app(_settings(mode)))
    headers = {"X-Forwarded-Proto": proto} if proto else {}
    resp = client.post(
        "/login", data={"password": _PW}, headers=headers, follow_redirects=False
    )
    cookie = resp.headers["set-cookie"].lower()
    assert ("secure" in cookie) is secure
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


# --- WebSocket handshake -------------------------------------------------


async def _stub_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("ok")
    await websocket.close()


def _ws_app(settings: Settings):
    router = Router(
        routes=[WebSocketRoute("/api/environments/{branch:path}/terminal", _stub_ws)]
    )
    return UIAuthMiddleware(router, lambda: settings)


_WS_URL = "/api/environments/main/terminal"


def test_ws_accepts_valid_cookie():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    client = TestClient(_ws_app(settings))
    with client.websocket_connect(
        _WS_URL, headers={"cookie": f"{_AUTH_COOKIE}={token}"}
    ) as ws:
        assert ws.receive_text() == "ok"


def test_ws_rejects_without_cookie():
    client = TestClient(_ws_app(_settings()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(_WS_URL):
            pass


def test_ws_rejects_invalid_cookie():
    client = TestClient(_ws_app(_settings()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            _WS_URL, headers={"cookie": f"{_AUTH_COOKIE}=1.deadbeef"}
        ):
            pass


def test_ws_rejects_basic_header():
    client = TestClient(_ws_app(_settings()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(_WS_URL, headers=_basic("admin", _PW)):
            pass


# --- production endpoints ---------------------------------------------------


def test_healthz_is_public():
    from unittest.mock import patch

    client = TestClient(_full_app(_settings()))
    with patch(
        "oduflow.health.collect_health", return_value={"ok": True, "checks": {}}
    ):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_healthz_degraded_is_503():
    from unittest.mock import patch

    client = TestClient(_full_app(_settings()))
    with patch(
        "oduflow.health.collect_health", return_value={"ok": False, "checks": {}}
    ):
        resp = client.get("/healthz")
    assert resp.status_code == 503


def test_github_webhook_is_public_but_verifies_hmac():
    # No UI session: the route is reachable, but a bad signature is 401.
    client = TestClient(_full_app(_settings(prod_enabled=True)))
    resp = client.post(
        "/api/webhooks/github",
        content=b"{}",
        headers={
            "x-github-event": "push",
            "x-hub-signature-256": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 401


def test_productions_api_requires_auth():
    client = TestClient(_full_app(_settings()))
    assert client.get("/api/productions").status_code == 401
    assert client.post("/api/productions/x/stop").status_code == 401


@pytest.fixture
def mfa_config(tmp_path, monkeypatch):
    import pyotp

    from oduflow import ui_totp

    now = 1_800_000_000
    secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    monkeypatch.setattr(ui_totp.time, "time", lambda: now)
    team = TeamSettings(team_id="1", ui_password=_PW, data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    ui_totp.enroll(settings, team, secret, pyotp.TOTP(secret).at(now - 30))
    return settings, pyotp.TOTP(secret).at(now)


@pytest.mark.parametrize("code", ["", "bad", "1234567", "１２３４５６"])
def test_mfa_login_rejects_missing_or_invalid_code(mfa_config, code):
    settings, _ = mfa_config
    client = TestClient(_full_app(settings))
    response = client.post(
        "/login", data={"password": _PW, "otp": code}, follow_redirects=False
    )
    assert response.status_code == 401
    assert _AUTH_COOKIE not in client.cookies
    assert client.get("/api/license").status_code == 401


def test_mfa_session_authenticates_http_and_websocket_and_rejects_replay(mfa_config):
    settings, code = mfa_config
    client = TestClient(_full_app(settings))
    response = client.post(
        "/login", json={"password": _PW, "otp": code}, follow_redirects=False
    )
    assert response.status_code == 303
    cookie = client.cookies.get(_AUTH_COOKIE)
    assert _check_cookie_token(cookie, settings) is settings.teams["1"]
    assert client.get("/").status_code == 200
    # Dashboard loads do not extend the authenticated session indefinitely.
    assert "set-cookie" not in client.get("/").headers
    with TestClient(_ws_app(settings)).websocket_connect(
        _WS_URL, headers={"cookie": f"{_AUTH_COOKIE}={cookie}"}
    ) as ws:
        assert ws.receive_text() == "ok"
    other = TestClient(_full_app(settings))
    assert other.post("/login", data={"password": _PW, "otp": code}).status_code == 401
    assert _AUTH_COOKIE not in other.cookies


def test_mfa_basic_does_not_bypass_factor(mfa_config):
    settings, _ = mfa_config
    client = TestClient(_full_app(settings))
    assert (
        client.get(
            "/", headers=_basic("admin", _PW), follow_redirects=False
        ).status_code
        == 302
    )
    assert client.get("/api/license", headers=_basic("admin", _PW)).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_ws_app(settings)).websocket_connect(
            _WS_URL, headers=_basic("admin", _PW)
        ):
            pass


def test_enrollment_and_reset_revoke_sessions_without_restart(tmp_path, monkeypatch):
    import pyotp

    from oduflow import ui_totp

    now = 1_800_000_000
    monkeypatch.setattr(ui_totp.time, "time", lambda: now)
    team = TeamSettings(team_id="1", ui_password=_PW, data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    client = TestClient(_full_app(settings))
    client.post("/login", data={"password": _PW})
    password_cookie = client.cookies.get(_AUTH_COOKIE)
    secret = pyotp.random_base32()
    ui_totp.enroll(settings, team, secret, pyotp.TOTP(secret).at(now - 30))
    assert _check_cookie_token(password_cookie, settings) is None
    assert client.get("/api/license").status_code == 401
    assert client.post("/login", data={"password": _PW}).status_code == 401
    assert (
        client.post(
            "/login",
            data={"password": _PW, "otp": pyotp.TOTP(secret).at(now)},
            follow_redirects=False,
        ).status_code
        == 303
    )
    mfa_cookie = client.cookies.get(_AUTH_COOKIE)
    ui_totp.reset(settings, team)
    assert _check_cookie_token(mfa_cookie, settings) is None
    assert _check_cookie_token(password_cookie, settings) is None
    assert (
        client.post(
            "/login", data={"password": _PW}, follow_redirects=False
        ).status_code
        == 303
    )


def test_login_generation_cannot_race_enrollment(tmp_path):
    import pyotp

    from oduflow import ui_totp

    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", ui_password=_PW)},
    )
    team = settings.teams["1"]
    generation = ui_totp.verify(settings, team, "")
    secret = pyotp.random_base32()
    ui_totp.enroll(settings, team, secret, pyotp.TOTP(secret).now())
    stale_cookie = _make_ui_token(team, settings, mfa_version=generation)
    assert _check_cookie_token(stale_cookie, settings) is None
    with pytest.raises(ValueError, match="TOTP verification"):
        _make_ui_token(team, settings)


def test_corrupt_mfa_state_denies_cookie_and_login(mfa_config):
    from oduflow import ui_totp

    settings, code = mfa_config
    client = TestClient(_full_app(settings))
    client.post("/login", data={"password": _PW, "otp": code})
    ui_totp._path(settings, settings.teams["1"]).write_text("{}")
    assert client.get("/api/license").status_code == 401
    assert client.post("/login", data={"password": _PW, "otp": code}).status_code == 503


def test_team_mfa_throttle_survives_new_app_instances(mfa_config):
    settings, code = mfa_config
    for _ in range(10):
        client = TestClient(_full_app(settings))
        assert (
            client.post("/login", data={"password": _PW, "otp": "bad"}).status_code
            == 401
        )
    client = TestClient(_full_app(settings))
    assert client.post("/login", data={"password": _PW, "otp": code}).status_code == 429


def test_cross_origin_login_cannot_consume_totp(mfa_config):
    settings, code = mfa_config
    client = TestClient(_full_app(settings))
    credentials = {"password": _PW, "otp": code}
    assert (
        client.post(
            "/login", data=credentials, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post("/login", data=credentials, follow_redirects=False).status_code
        == 303
    )


@pytest.mark.parametrize("body", [[], None, 42, "password"])
def test_malformed_json_login_is_rejected(body):
    client = TestClient(_full_app(_settings()))
    response = client.post("/login", json=body, follow_redirects=False)
    assert response.status_code == 401


def test_legacy_two_field_session_is_rejected():
    settings = _settings()
    token = _get_signer(settings).dumps(
        ["1", web_ui._password_fingerprint(web_ui._get_secret(settings), _PW)]
    )
    assert _check_cookie_token(token, settings) is None


def test_wrong_password_cannot_consume_valid_totp(mfa_config):
    settings, code = mfa_config
    client = TestClient(_full_app(settings))
    assert (
        client.post("/login", data={"password": "wrong", "otp": code}).status_code
        == 401
    )
    assert (
        client.post(
            "/login", data={"password": _PW, "otp": code}, follow_redirects=False
        ).status_code
        == 303
    )


def test_mfa_and_reset_are_scoped_to_the_passwords_team(mfa_config, tmp_path):
    from dataclasses import replace

    from oduflow import ui_totp

    settings, code = mfa_config
    other = TeamSettings(
        team_id="2", ui_password="other-password", data_dir=str(tmp_path / "team2")
    )
    settings = replace(settings, teams={**settings.teams, "2": other})
    client = TestClient(_full_app(settings))
    assert (
        client.post(
            "/login", data={"password": other.ui_password}, follow_redirects=False
        ).status_code
        == 303
    )
    other_cookie = client.cookies.get(_AUTH_COOKIE)
    assert _check_cookie_token(other_cookie, settings) is other
    protected = TestClient(_full_app(settings))
    assert (
        protected.post(
            "/login", data={"password": _PW}, follow_redirects=False
        ).status_code
        == 401
    )
    assert (
        protected.post(
            "/login", data={"password": _PW, "otp": code}, follow_redirects=False
        ).status_code
        == 303
    )
    full_cookie = protected.cookies.get(_AUTH_COOKIE)
    ui_totp.reset(settings, settings.teams["1"])
    assert _check_cookie_token(other_cookie, settings) is other
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_ws_app(settings)).websocket_connect(
            _WS_URL, headers={"cookie": f"{_AUTH_COOKIE}={full_cookie}"}
        ):
            pass


def test_session_helper_cannot_upgrade_auth_during_concurrent_enrollment(
    tmp_path, monkeypatch
):
    import pyotp

    from oduflow import ui_totp

    team = TeamSettings(team_id="1", ui_password=_PW, data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    read = ui_totp._load
    enrolled = False

    def enroll_after_read(path):
        nonlocal enrolled
        snapshot = read(path)
        if not enrolled:
            enrolled = True
            secret = pyotp.random_base32()
            ui_totp.enroll(settings, team, secret, pyotp.TOTP(secret).now())
        return snapshot

    monkeypatch.setattr(ui_totp, "_load", enroll_after_read)
    cookie = _make_ui_token(team, settings)
    assert _check_cookie_token(cookie, settings) is None
