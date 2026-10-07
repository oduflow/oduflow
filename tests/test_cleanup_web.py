from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import BusyError, ExternalCommandError
from oduflow.locking import LockManager, image_cleanup_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import _AUTH_COOKIE, _make_ui_token, mount_web_ui

IMAGE_ID = "sha256:" + "a" * 64
IMAGE_LOCK = image_cleanup_lock_key()
REVIEWED = {"orphan_databases": ["oduflow_2_old"], "orphan_roles": []}


@pytest.fixture
def setup(tmp_path):
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={
            key: TeamSettings(team_id=key, ui_password="password-" + key)
            for key in ("1", "2")
        },
    )
    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    client = TestClient(app)
    client.cookies.set(_AUTH_COOKIE, _make_ui_token(settings.teams["2"], settings))
    with (
        patch("oduflow.docker_ops.system_ops.cleanup_orphans") as cleanup,
        patch("oduflow.docker_ops.image_cleanup.list_unused_images") as scan,
        patch("oduflow.docker_ops.image_cleanup.remove_unused_images") as remove,
    ):
        cleanup.return_value = {"dry_run": True, "orphan_databases": []}
        scan.return_value = [{"id": IMAGE_ID, "tags": ["odoo:old"], "size_bytes": 100}]
        remove.return_value = {"removed": [IMAGE_ID], "skipped": [], "errors": []}
        yield client, settings, locks, cleanup, scan, remove


def test_default_is_dry_run_for_authenticated_team(setup):
    client, settings, _, cleanup, scan, remove = setup
    response = client.post("/api/cleanup", json={})
    assert response.status_code == 200
    cleanup.assert_called_once_with(settings, settings.teams["2"], dry_run=True)
    assert response.json()["result"]["images"][0]["id"] == IMAGE_ID
    scan.assert_called_once_with()
    remove.assert_not_called()


def test_dry_run_neither_waits_for_nor_blocks_environment_operations(setup):
    client, _, locks, cleanup, _, _ = setup

    def run(*args, **kwargs):
        with locks.env_lock("other-env", team_id="2"):
            pass
        return {"dry_run": True}

    cleanup.side_effect = run
    with locks.env_lock("building", team_id="2", operation="create_environment"):
        response = client.post("/api/cleanup", json={"force": False})
    assert response.status_code == 200


def test_any_team_can_confirm_image_cleanup_and_worker_holds_locks(setup):
    client, settings, locks, cleanup, scan, remove = setup

    def run(*args, **kwargs):
        with pytest.raises(BusyError):
            locks.acquire_env("new-env", team_id="2")
        with pytest.raises(BusyError):
            locks.acquire_env(IMAGE_LOCK)
        return {"dry_run": False}

    def remove_images(image_ids):
        # The team lock covers only the orphan removal.
        with locks.env_lock("new-env", team_id="2"):
            pass
        with pytest.raises(BusyError):
            locks.acquire_env(IMAGE_LOCK)
        return {"removed": image_ids, "skipped": [], "errors": []}

    cleanup.side_effect = run
    remove.side_effect = remove_images
    response = client.post(
        "/api/cleanup",
        json={"force": True, "orphans": REVIEWED, "image_ids": [IMAGE_ID]},
    )
    assert response.status_code == 200
    cleanup.assert_called_once_with(
        settings, settings.teams["2"], dry_run=False, only=REVIEWED
    )
    remove.assert_called_once_with([IMAGE_ID])
    scan.assert_not_called()
    with locks.team_lock("2"), locks.env_lock(IMAGE_LOCK):
        pass


def test_confirm_without_reviewed_orphans_removes_none(setup):
    client, _, _, cleanup, _, remove = setup
    response = client.post("/api/cleanup", json={"force": True})
    assert response.status_code == 200
    assert cleanup.call_args.kwargs == {"dry_run": False, "only": {}}
    remove.assert_not_called()


def test_orphan_only_confirm_does_not_wait_for_image_cleanup(setup):
    client, _, locks, cleanup, _, remove = setup
    with locks.env_lock(IMAGE_LOCK, operation="cleanup"):
        response = client.post(
            "/api/cleanup", json={"force": True, "orphans": REVIEWED}
        )
    assert response.status_code == 200
    cleanup.assert_called_once()
    remove.assert_not_called()


def test_busy_image_cleanup_removes_nothing(setup):
    client, _, locks, cleanup, _, remove = setup
    with locks.env_lock(IMAGE_LOCK, operation="cleanup"):
        response = client.post(
            "/api/cleanup",
            json={"force": True, "orphans": REVIEWED, "image_ids": [IMAGE_ID]},
        )
    assert response.status_code == 409
    cleanup.assert_not_called()
    remove.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        {"force": "false"},
        {"force": 1},
        {"image_ids": "all"},
        {"image_ids": ["odoo:latest"]},
        {"force": True, "orphans": []},
        {"force": True, "orphans": {"orphan_volumes": ["data"]}},
        {"force": True, "orphans": {"orphan_databases": "oduflow_2_old"}},
        {"force": True, "orphans": {"orphan_databases": [1]}},
    ],
)
def test_invalid_input_cannot_delete_resources(setup, body):
    client, _, _, cleanup, scan, remove = setup
    response = client.post("/api/cleanup", json=body)
    assert response.status_code == 400
    cleanup.assert_not_called()
    scan.assert_not_called()
    remove.assert_not_called()


def test_team_busy_does_not_release_another_operations_lock(setup):
    client, _, locks, cleanup, _, remove = setup
    with locks.env_lock("building", team_id="2", operation="create_environment"):
        response = client.post("/api/cleanup", json={"force": True})
        assert response.status_code == 409
        with pytest.raises(BusyError):
            locks.acquire_team("2")
    cleanup.assert_not_called()
    remove.assert_not_called()


def test_failure_is_scrubbed_and_releases_locks(setup):
    client, _, locks, cleanup, _, _ = setup
    cleanup.side_effect = ExternalCommandError("docker", 1, "secret command output")
    response = client.post(
        "/api/cleanup", json={"force": True, "image_ids": [IMAGE_ID]}
    )
    assert response.status_code == 500
    assert "secret command output" not in response.text
    with locks.team_lock("2"), locks.env_lock(IMAGE_LOCK):
        pass


def test_login_and_csrf_protection_apply(setup):
    client, _, _, cleanup, _, _ = setup
    response = client.post(
        "/api/cleanup",
        json={"force": True},
        headers={"Origin": "https://other.example"},
    )
    assert response.status_code == 403
    client.cookies.clear()
    response = client.post("/api/cleanup", json={"force": True})
    assert response.status_code == 401
    cleanup.assert_not_called()
