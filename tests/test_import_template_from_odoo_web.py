"""Dashboard route tests for pulling a template from a running Odoo."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import ConflictError
from oduflow.locking import LockManager, template_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client_with_locks(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    return TestClient(app), settings, team, locks


def _payload(**overrides):
    payload = {
        "odoo_url": "http://odoo.example.com",
        "master_pwd": "master-secret",
        "db_name": "production",
        "template_name": "production-copy",
        "without_filestore": True,
    }
    payload.update(overrides)
    return payload


def _result():
    return {
        "template_name": "production-copy",
        "source_url": "http://odoo.example.com",
        "source_db": "production",
        "odoo_version": "19.0",
        "odoo_image": "odoo:19.0",
        "template_db": "oduflow_template_1_production_copy",
        "includes_filestore": False,
        "zip_size_mb": 12.5,
        "restore_seconds": 3.2,
        "affected_envs": ["feature-x"],
        "remount_failures": [],
        "internal_path": "/srv/oduflow/templates/production-copy",
    }


def test_import_from_odoo_calls_shared_backend(tmp_path):
    client, settings, team, _locks = _client_with_locks(tmp_path)
    with patch(
        "oduflow.web_ui.system_ops.import_from_odoo", return_value=_result()
    ) as import_from_odoo:
        response = client.post(
            "/api/templates/import-from-odoo",
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["template_name"] == "production-copy"
    assert body["result"]["includes_filestore"] is False
    assert "internal_path" not in body["result"]
    assert "master-secret" not in response.text
    import_from_odoo.assert_called_once_with(
        settings,
        team,
        odoo_url="http://odoo.example.com",
        master_pwd="master-secret",
        db_name="production",
        template_name="production-copy",
        without_filestore=True,
    )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"odoo_url": ""}, "odoo_url is required"),
        ({"master_pwd": ""}, "master_pwd is required"),
        ({"template_name": ""}, "template_name is required"),
        ({"template_name": "../escape"}, "template"),
        ({"without_filestore": "yes"}, "must be a boolean"),
    ],
)
def test_import_from_odoo_validates_request(tmp_path, overrides, error):
    client, _settings, _team, _locks = _client_with_locks(tmp_path)
    with patch("oduflow.web_ui.system_ops.import_from_odoo") as import_from_odoo:
        response = client.post(
            "/api/templates/import-from-odoo",
            json=_payload(**overrides),
        )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert error.lower() in response.json()["error"].lower()
    import_from_odoo.assert_not_called()


def test_import_from_odoo_surfaces_backend_conflict(tmp_path):
    client, _settings, _team, _locks = _client_with_locks(tmp_path)
    with patch(
        "oduflow.web_ui.system_ops.import_from_odoo",
        side_effect=ConflictError("Template already exists"),
    ):
        response = client.post(
            "/api/templates/import-from-odoo",
            json=_payload(),
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Template already exists"}


def test_import_from_odoo_is_not_blocked_by_a_team_operation(tmp_path):
    """An import builds a brand-new template, so it remounts nothing and has no
    reason to queue behind a publish or a refresh elsewhere in the team."""
    client, _settings, _team, locks = _client_with_locks(tmp_path)
    locks.acquire_team("1")
    try:
        with patch(
            "oduflow.web_ui.system_ops.import_from_odoo", return_value=_result()
        ) as import_from_odoo:
            response = client.post(
                "/api/templates/import-from-odoo",
                json=_payload(),
            )
    finally:
        locks.release_team("1")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    import_from_odoo.assert_called_once()


def test_import_from_odoo_returns_busy_for_the_same_template(tmp_path):
    client, _settings, _team, locks = _client_with_locks(tmp_path)
    locks.acquire_env(template_lock_key("1", "production-copy"))
    try:
        with patch("oduflow.web_ui.system_ops.import_from_odoo") as import_from_odoo:
            response = client.post(
                "/api/templates/import-from-odoo",
                json=_payload(),
            )
    finally:
        locks.release_env(template_lock_key("1", "production-copy"))

    assert response.status_code == 409
    assert response.json()["ok"] is False
    import_from_odoo.assert_not_called()


def test_import_from_odoo_ignores_another_template_s_lock(tmp_path):
    client, _settings, _team, locks = _client_with_locks(tmp_path)
    locks.acquire_env(template_lock_key("1", "unrelated"))
    try:
        with patch(
            "oduflow.web_ui.system_ops.import_from_odoo", return_value=_result()
        ) as import_from_odoo:
            response = client.post(
                "/api/templates/import-from-odoo",
                json=_payload(),
            )
    finally:
        locks.release_env(template_lock_key("1", "unrelated"))

    assert response.status_code == 200
    import_from_odoo.assert_called_once()


def test_import_from_odoo_does_not_block_dashboard_reads(tmp_path):
    client, _settings, _team, _locks = _client_with_locks(tmp_path)
    operation_started = Event()
    release_operation = Event()

    def import_from_odoo(*args, **kwargs):
        operation_started.set()
        assert release_operation.wait(timeout=5)
        return _result()

    with (
        client,
        patch(
            "oduflow.web_ui.system_ops.import_from_odoo",
            side_effect=import_from_odoo,
        ),
        patch("oduflow.web_ui.system_ops.list_templates", return_value=[]),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        importing = executor.submit(
            client.post,
            "/api/templates/import-from-odoo",
            json=_payload(),
        )
        assert operation_started.wait(timeout=5)
        templates = client.get("/api/templates")
        release_operation.set()
        imported = importing.result(timeout=5)

    assert templates.status_code == 200
    assert templates.json() == {"ok": True, "templates": []}
    assert imported.status_code == 200
