import json
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.docker_ops import system_ops
from oduflow.errors import BusyError
from oduflow.locking import LockManager, template_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    return TestClient(app), locks


def _metadata(tmp_path, content='{"odoo_image": "odoo:18.0"}', name="default"):
    path = tmp_path / "templates" / name / "metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_template_metadata_get_and_update(tmp_path):
    metadata_path = _metadata(tmp_path)
    client, _ = _client(tmp_path)

    opened = client.get("/api/templates/default/metadata")
    assert opened.status_code == 200
    revision = opened.json()["revision"]

    saved = client.put(
        "/api/templates/default/metadata",
        json={
            "content": '{"odoo_image":"odoo:19.0","custom":true}',
            "revision": revision,
        },
    )

    assert saved.status_code == 200
    assert saved.json()["ok"] is True
    assert json.loads(metadata_path.read_text()) == {
        "odoo_image": "odoo:19.0",
        "custom": True,
    }


def test_template_metadata_supports_nested_template_names(tmp_path):
    _metadata(tmp_path, name="customer/base")
    client, _ = _client(tmp_path)

    response = client.get("/api/templates/customer%2Fbase/metadata")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_template_metadata_update_rejects_invalid_json(tmp_path):
    metadata_path = _metadata(tmp_path)
    original = metadata_path.read_text()
    client, _ = _client(tmp_path)
    revision = client.get("/api/templates/default/metadata").json()["revision"]

    response = client.put(
        "/api/templates/default/metadata",
        json={"content": "{broken", "revision": revision},
    )

    assert response.status_code == 400
    assert "line 1" in response.json()["error"]
    assert metadata_path.read_text() == original


def test_template_metadata_update_rejects_stale_revision(tmp_path):
    metadata_path = _metadata(tmp_path)
    client, _ = _client(tmp_path)
    revision = client.get("/api/templates/default/metadata").json()["revision"]
    metadata_path.write_text('{"filesystem": "edit"}', encoding="utf-8")

    response = client.put(
        "/api/templates/default/metadata",
        json={"content": '{"dashboard": "edit"}', "revision": revision},
    )

    assert response.status_code == 409
    assert "changed after it was opened" in response.json()["error"]
    assert json.loads(metadata_path.read_text()) == {"filesystem": "edit"}


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ([], "JSON body must be an object"),
        ({"content": "{}"}, "revision must be a string"),
        ({"revision": "x"}, "content must be a string"),
    ],
)
def test_template_metadata_update_validates_request_body(tmp_path, body, error):
    _metadata(tmp_path)
    client, _ = _client(tmp_path)

    response = client.put("/api/templates/default/metadata", json=body)

    assert response.status_code == 400
    assert response.json()["error"] == error


def test_template_metadata_update_is_not_blocked_by_a_team_operation(tmp_path):
    # Editing metadata.json rewrites one file and remounts nothing, so it takes
    # the template's own key rather than freezing the team behind a publish.
    _metadata(tmp_path)
    client, locks = _client(tmp_path)
    revision = client.get("/api/templates/default/metadata").json()["revision"]
    locks.acquire_team("1")
    try:
        response = client.put(
            "/api/templates/default/metadata",
            json={"content": "{}", "revision": revision},
        )
        assert response.status_code == 200
        with pytest.raises(BusyError):
            locks.acquire_team("1")
    finally:
        locks.release_team("1")


def test_template_metadata_update_busy_keeps_foreign_template_lock(tmp_path):
    _metadata(tmp_path)
    client, locks = _client(tmp_path)
    revision = client.get("/api/templates/default/metadata").json()["revision"]
    key = template_lock_key("1", "default")
    locks.acquire_env(key)
    try:
        response = client.put(
            "/api/templates/default/metadata",
            json={"content": "{}", "revision": revision},
        )
        assert response.status_code == 409
        with pytest.raises(BusyError):
            locks.acquire_env(key)
    finally:
        locks.release_env(key)


def test_template_metadata_update_ignores_another_template_s_lock(tmp_path):
    _metadata(tmp_path)
    client, locks = _client(tmp_path)
    revision = client.get("/api/templates/default/metadata").json()["revision"]
    locks.acquire_env(template_lock_key("1", "other"))
    try:
        response = client.put(
            "/api/templates/default/metadata",
            json={"content": "{}", "revision": revision},
        )
        assert response.status_code == 200
    finally:
        locks.release_env(template_lock_key("1", "other"))


def test_template_list_reads_during_a_team_operation(tmp_path):
    # Listing is a pure read: it must not bounce off a running publish, and must
    # not touch the team lock either (same contract as the list_templates tool).
    client, locks = _client(tmp_path)
    locks.acquire_team("1")
    try:
        with patch.object(system_ops, "get_client", return_value=MagicMock()):
            response = client.get("/api/templates")
        assert response.status_code == 200
        with pytest.raises(BusyError):
            locks.acquire_team("1")
    finally:
        locks.release_team("1")


def test_template_metadata_get_rejects_invalid_or_missing_template(tmp_path):
    client, _ = _client(tmp_path)

    invalid = client.get("/api/templates/%2E%2E/metadata")
    missing = client.get("/api/templates/missing/metadata")

    assert invalid.status_code in (400, 404)
    assert missing.status_code == 404
