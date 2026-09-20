"""CSRF protection for the web dashboard.

The dashboard authenticates browsers with an ambient session cookie, so cross-site state-changing requests must be rejected. ``SameSite=Strict``
is the first line of defence; ``UIAuthMiddleware`` adds a server-side
Origin/Referer backstop for unsafe HTTP methods and WebSocket handshakes.
Non-browser clients (curl, the import shell script) send no Origin/Referer and
are unaffected.
"""

from __future__ import annotations

import tempfile

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route, Router
from starlette.testclient import TestClient

from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import (
    _AUTH_COOKIE,
    UIAuthMiddleware,
    _is_cross_origin,
    _make_ui_token,
)

_PW = "s3cret"


def _headers(**kw: str) -> Headers:
    return Headers(raw=[(k.encode(), v.encode()) for k, v in kw.items()])


def test_is_cross_origin_matches_host():
    assert _is_cross_origin(_headers(host="h:8080", origin="https://h:8080")) is False
    assert _is_cross_origin(_headers(host="h", origin="https://h")) is False


def test_is_cross_origin_rejects_foreign_origin():
    assert _is_cross_origin(_headers(host="h", origin="https://evil.example")) is True


def test_is_cross_origin_referer_fallback():
    assert (
        _is_cross_origin(_headers(host="h", referer="https://evil.example/x")) is True
    )
    assert _is_cross_origin(_headers(host="h", referer="https://h/dash")) is False


def test_is_cross_origin_allows_missing_headers():
    # Native clients (curl) carry no ambient cookie -> nothing to forge.
    assert _is_cross_origin(_headers(host="h")) is False


def _client() -> TestClient:
    settings = Settings(
        routing_mode="port",
        base_data_dir=tempfile.mkdtemp(prefix="oduflow-csrf-test-"),
        teams={"1": TeamSettings(team_id="1", ui_password=_PW)},
    )

    async def ok(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    router = Router(
        routes=[
            Route("/api/dummy", ok, methods=["GET", "POST"]),
        ]
    )
    app = UIAuthMiddleware(router, lambda: settings)
    client = TestClient(app, base_url="http://testserver")
    client.cookies.set(_AUTH_COOKIE, _make_ui_token(settings.teams["1"], settings))
    return client


def test_cross_origin_post_blocked():
    client = _client()
    r = client.post(
        "/api/dummy",
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "Cross-origin" in r.json()["error"]


def test_same_origin_post_allowed():
    client = _client()
    r = client.post(
        "/api/dummy",
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_post_without_origin_allowed():
    client = _client()
    r = client.post("/api/dummy")
    assert r.status_code == 200


def test_safe_method_never_blocked_by_csrf():
    # A cross-origin GET is not state-changing; the Origin check must not apply.
    client = _client()
    r = client.get("/api/dummy", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
