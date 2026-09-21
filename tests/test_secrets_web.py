"""REST surface for team secrets: write-only values, names-only listing."""

from starlette.applications import Starlette
from starlette.testclient import TestClient

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
