"""REST surface for team secrets: write-only values, names-only listing."""

import asyncio
import json
from threading import Event

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import secret_store
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_list_starts_empty(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/secrets")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "secrets": []}


def test_set_then_list_never_returns_the_value(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/secrets/api-key/set", json={"value": "hunter2"})
    assert resp.status_code == 200
    assert resp.json()["result"] == {"name": "api-key", "created": True}

    resp = client.get("/api/secrets")
    body = resp.json()
    assert [s["name"] for s in body["secrets"]] == ["api-key"]
    assert "hunter2" not in resp.text
    assert "value" not in body["secrets"][0]

    # Replacing the value reports created=False and still leaks nothing.
    resp = client.post("/api/secrets/api-key/set", json={"value": "rotated"})
    assert resp.json()["result"] == {"name": "api-key", "created": False}


def test_invalid_name_and_empty_value_are_rejected(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/secrets/Bad%20Name/set", json={"value": "x"})
    assert resp.status_code == 400

    resp = client.post("/api/secrets/ok-name/set", json={"value": ""})
    assert resp.status_code == 400
    assert client.get("/api/secrets").json()["secrets"] == []


def test_delete(tmp_path):
    client = _client(tmp_path)
    client.post("/api/secrets/api-key/set", json={"value": "v"})

    resp = client.post("/api/secrets/api-key/delete")
    assert resp.status_code == 200
    assert client.get("/api/secrets").json()["secrets"] == []

    resp = client.post("/api/secrets/api-key/delete")
    assert resp.status_code == 404


def test_json_validation_and_preserved_type(tmp_path):
    client = _client(tmp_path)
    endpoint = "/api/secrets/key/set"
    assert (
        client.post(
            endpoint, json={"value": '{"token":"private"}', "value_type": "json"}
        ).status_code
        == 200
    )
    before = (tmp_path / "secrets.json").read_text()
    for value in ('{"private":}', "NaN", "Infinity"):
        response = client.post(endpoint, json={"value": value})
        assert response.status_code == 400
        assert "private" not in response.text
        assert (tmp_path / "secrets.json").read_text() == before
    listing = client.get("/api/secrets")
    assert listing.json()["secrets"][0]["value_type"] == "json"
    assert "private" not in listing.text
    assert (
        client.post(endpoint, json={"value": "plain", "value_type": "text"}).status_code
        == 200
    )
    assert client.get("/api/secrets").json()["secrets"][0]["value_type"] == "text"


def test_invalid_secret_type(tmp_path):
    client = _client(tmp_path)
    for value_type in ("xml", "", False, [], {}):
        response = client.post(
            "/api/secrets/key/set", json={"value": "{}", "value_type": value_type}
        )
        assert response.status_code == 400
    assert client.get("/api/secrets").json()["secrets"] == []


def test_update_json_preserves_siblings_and_returns_no_values(tmp_path):
    client = _client(tmp_path)
    original = {"database": {"password": "old-private", "user": "keep-private"}}
    client.post(
        "/api/secrets/config/set",
        json={"value": json.dumps(original), "value_type": "json"},
    )
    response = client.post(
        "/api/secrets/config/update-json",
        json={"path": "/database/password", "value": "new-private"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {"name": "config", "updated": True},
    }
    stored = json.loads((tmp_path / "secrets.json").read_text())
    original["database"]["password"] = "new-private"
    assert json.loads(stored["secrets"]["config"]["value"]) == original
    assert "private" not in client.get("/api/secrets").text

    response = client.post(
        "/api/secrets/config/update-json",
        json={"path": "/database/password", "value": None},
    )
    assert response.status_code == 200
    stored = json.loads((tmp_path / "secrets.json").read_text())
    original["database"]["password"] = None
    assert json.loads(stored["secrets"]["config"]["value"]) == original


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "private",
        {},
        {"path": "/key"},
        {"value": "private"},
        {"path": None, "value": "private"},
        {"path": "/missing", "value": "private"},
        {"path": "/key/private", "value": "private"},
    ],
)
def test_update_json_bad_body_or_path_leaves_store_untouched(tmp_path, body):
    client = _client(tmp_path)
    client.post(
        "/api/secrets/config/set",
        json={"value": '{"key":"old-private"}', "value_type": "json"},
    )
    before = (tmp_path / "secrets.json").read_bytes()
    response = client.post(
        "/api/secrets/config/update-json",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "private" not in response.text
    assert (tmp_path / "secrets.json").read_bytes() == before


@pytest.mark.parametrize(
    "body",
    [
        '{"private":',
        '{"path":"/key","value":NaN}',
        '{"path":"/key","value":{"private":Infinity}}',
    ],
)
def test_update_json_rejects_invalid_json(tmp_path, body):
    client = _client(tmp_path)
    client.post(
        "/api/secrets/config/set",
        json={"value": '{"key":"old-private"}', "value_type": "json"},
    )
    before = (tmp_path / "secrets.json").read_bytes()
    response = client.post(
        "/api/secrets/config/update-json",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "private" not in response.text
    assert (tmp_path / "secrets.json").read_bytes() == before


def test_update_json_text_secret_returns_400(tmp_path):
    client = _client(tmp_path)
    client.post("/api/secrets/config/set", json={"value": '{"key":"private"}'})
    before = (tmp_path / "secrets.json").read_bytes()
    response = client.post(
        "/api/secrets/config/update-json", json={"path": "/key", "value": "new-private"}
    )
    assert response.status_code == 400
    assert "only supported for JSON secrets" in response.json()["error"]
    assert "private" not in response.text
    assert (tmp_path / "secrets.json").read_bytes() == before


def test_update_json_missing_secret_returns_404(tmp_path):
    client = _client(tmp_path)
    response = client.post(
        "/api/secrets/missing/update-json", json={"path": "/key", "value": "private"}
    )
    assert response.status_code == 404
    assert "private" not in response.text
    assert client.get("/api/secrets").json()["secrets"] == []


@pytest.mark.parametrize(
    "action,body",
    [
        ("set", {"value": '{"key":"new"}'}),
        ("update-json", {"path": "/key", "value": "new"}),
    ],
)
def test_secret_write_does_not_block_other_requests(
    tmp_path, monkeypatch, action, body
):
    app = _client(tmp_path).app
    team = TeamSettings(team_id="1", data_dir=str(tmp_path))
    secret_store.set_secret(team, "config", '{"key":"old"}', "json")
    original_save = secret_store._save
    release_save = Event()

    async def exercise():
        loop = asyncio.get_running_loop()
        save_started = asyncio.Event()

        def slow_save(team, data):
            loop.call_soon_threadsafe(save_started.set)
            # Bound the wait so a regression blocking the loop fails, not hangs.
            assert release_save.wait(timeout=3), "Save blocked the event loop"
            original_save(team, data)

        monkeypatch.setattr(secret_store, "_save", slow_save)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            write = asyncio.create_task(
                client.post(f"/api/secrets/config/{action}", json=body)
            )
            try:
                await asyncio.wait_for(save_started.wait(), timeout=5)
                response = await asyncio.wait_for(client.get("/api/secrets"), timeout=2)
                assert response.status_code == 200
                assert not write.done(), "Other requests must finish while save waits"
            finally:
                release_save.set()
                await asyncio.wait_for(write, timeout=5)
            assert write.result().status_code == 200

    asyncio.run(exercise())
    stored = json.loads((tmp_path / "secrets.json").read_text())
    assert json.loads(stored["secrets"]["config"]["value"]) == {"key": "new"}
