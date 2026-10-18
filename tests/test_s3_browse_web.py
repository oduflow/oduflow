"""The dashboard's `POST /api/s3/browse` route.

The object browser reaches an operator-supplied endpoint on the server's
behalf, so the route must be reachable only by a real dashboard session — not
by an anonymous caller and not by a share link, whose scope is default-deny.
"""

from __future__ import annotations

import tempfile

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import template_import, ui_scope, web_ui
from oduflow.errors import NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import _AUTH_COOKIE, _make_ui_token, mount_web_ui

_PW = "s3cret"
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-s3browse-test-")

_LISTING = {
    "bucket": "store",
    "prefix": "backups/",
    "buckets": [],
    "folders": ["backups/acme/"],
    "files": [
        {
            "key": "backups/dump.pgdump",
            "size": 42,
            "last_modified": "2026-10-01T03:00:00+00:00",
            "etag": "e1",
        }
    ],
    "truncated": False,
    "next_token": "",
}


def _settings() -> Settings:
    return Settings(
        base_data_dir=_DATA_DIR,
        teams={"1": TeamSettings(team_id="1", ui_password=_PW)},
    )


@pytest.fixture
def client_and_settings():
    settings = _settings()
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), settings


@pytest.fixture
def logged_in(client_and_settings):
    client, settings = client_and_settings
    client.cookies.set(_AUTH_COOKIE, _make_ui_token(settings.teams["1"], settings))
    return client


def _capture(monkeypatch, result=None, error=None):
    seen: dict = {}

    def browse(settings, team, **kwargs):
        seen.update(kwargs)
        if error is not None:
            raise error
        return dict(result if result is not None else _LISTING)

    monkeypatch.setattr(template_import, "browse_source", browse)
    return seen


def test_anonymous_request_is_refused(client_and_settings, monkeypatch):
    client, _settings = client_and_settings
    _capture(monkeypatch)

    resp = client.post("/api/s3/browse", json={"bucket": "store"})

    assert resp.status_code in (401, 403)


def test_a_session_gets_the_listing_and_the_body_is_passed_through(
    logged_in, monkeypatch
):
    seen = _capture(monkeypatch)

    resp = logged_in.post(
        "/api/s3/browse",
        json={
            "bucket": "store",
            "prefix": "backups/",
            "continuation_token": "tok",
            "endpoint": "https://minio.example.com",
            "region": "eu-central-1",
            "access_key": "AK",
            "secret_key": "RAW",
            "template": "acme",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["folders"] == ["backups/acme/"]
    assert seen == {
        "bucket": "store",
        "prefix": "backups/",
        "continuation_token": "tok",
        "endpoint": "https://minio.example.com",
        "region": "eu-central-1",
        "access_key": "AK",
        "secret_key": "RAW",
        "template_name": "acme",
    }


def test_missing_fields_become_empty_strings(logged_in, monkeypatch):
    seen = _capture(monkeypatch)

    resp = logged_in.post("/api/s3/browse", json={})

    assert resp.status_code == 200
    assert set(seen.values()) == {""}


def test_a_non_object_body_is_rejected(logged_in, monkeypatch):
    _capture(monkeypatch)

    resp = logged_in.post("/api/s3/browse", json=["store"])

    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_invalid_json_is_rejected(logged_in, monkeypatch):
    _capture(monkeypatch)

    resp = logged_in.post(
        "/api/s3/browse",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid JSON body"


def test_a_bad_bucket_name_reads_back_as_an_error(logged_in, monkeypatch):
    _capture(monkeypatch, error=ValueError("Invalid S3 bucket name '../etc'."))

    resp = logged_in.post("/api/s3/browse", json={"bucket": "../etc"})

    assert resp.status_code == 400
    assert "Invalid S3 bucket name" in resp.json()["error"]


def test_a_flow_error_is_reported_not_swallowed(logged_in, monkeypatch):
    _capture(monkeypatch, error=NotFoundError("Listing s3://store/: not found (404)."))

    resp = logged_in.post("/api/s3/browse", json={"bucket": "store"})

    assert resp.status_code >= 400
    assert "not found" in resp.json()["error"]


def test_an_unexpected_error_does_not_leak_internals(logged_in, monkeypatch):
    _capture(monkeypatch, error=RuntimeError("secret-bearing traceback"))

    resp = logged_in.post("/api/s3/browse", json={"bucket": "store"})

    assert resp.status_code == 500
    assert resp.json()["error"] == "Internal server error."


def test_a_share_link_session_may_not_browse_buckets():
    """Share links are scoped to one environment; default-deny must cover a
    route that can reach arbitrary endpoints."""
    assert ui_scope.is_allowed("POST", "/api/s3/browse", "feature/x") is False


def test_the_route_is_not_public():
    assert "/api/s3/browse" not in web_ui._PUBLIC_PATHS
