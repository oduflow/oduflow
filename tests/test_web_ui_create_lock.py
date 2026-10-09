"""Regression test for issue #42.

api_create acquired the env lock inside the try whose finally always released
it, so a BusyError (the lock is held by another in-flight request) caused the
failing request to release a lock it never owned. The fix acquires the lock in
its own try and only releases it in a finally that runs solely when this request
acquired it.
"""

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import BusyError
from oduflow.hostname_registry import allocate_hostname, reserve_environment_slot
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _open_settings(tmp_path):
    # No ui_password -> the UI mounts without auth, so the POST reaches api_create.
    return Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )


def test_api_create_busy_keeps_foreign_lock(tmp_path):
    settings = _open_settings(tmp_path)
    locks = LockManager()
    # Request A holds the env lock for "main".
    locks.acquire_env("main")

    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    client = TestClient(app)

    with patch("oduflow.web_ui.env_ops.create_environment") as create:
        resp = client.post(
            "/api/environments/create",
            json={"env_name": "main", "repo_url": "r", "odoo_image": "i"},
        )
        # The busy request must never start real provisioning.
        create.assert_not_called()

    assert resp.json()["ok"] is False  # reported busy

    # The lock held by request A must still be held: the failed request must not
    # have released it. A fresh acquire therefore still reports busy.
    with pytest.raises(BusyError):
        locks.acquire_env("main")


def test_api_create_invalid_json_no_lock_error(tmp_path):
    # A malformed body must not raise on the finally (env_name was unbound before
    # the fix) — it should return a clean 400.
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    resp = client.post(
        "/api/environments/create",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_api_create_passes_short_hostname(tmp_path):
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    with patch(
        "oduflow.web_ui.env_ops.create_environment",
        return_value={"url": "https://dev2.example.com"},
    ) as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "main",
                "hostname": "dev2",
                "repo_url": "r",
                "odoo_image": "i",
            },
        )

    assert resp.json()["ok"] is True
    assert create.call_args.kwargs["hostname"] == "dev2"


def test_api_create_separate_branch_and_env_name(tmp_path):
    """The dashboard can name an environment differently from its branch."""
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    with patch(
        "oduflow.web_ui.env_ops.create_environment",
        return_value={"url": "http://localhost:50000"},
    ) as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "branch": "staging",
                "env_name": "oldstaging",
                "repo_url": "r",
                "odoo_image": "i",
            },
        )

    assert resp.json()["ok"] is True
    # _offload(fn, settings, team, branch, repo_url, odoo_image, ...)
    assert create.call_args.args[2] == "staging"
    assert create.call_args.kwargs["env_name"] == "oldstaging"


def test_api_create_passes_base_branch(tmp_path):
    """A base branch lets the dashboard start a branch origin does not have."""
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    with patch(
        "oduflow.web_ui.env_ops.create_environment",
        return_value={"url": "http://localhost:50000"},
    ) as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "branch": "feature/new",
                "base_branch": "main",
                "repo_url": "r",
                "odoo_image": "i",
            },
        )

    assert resp.json()["ok"] is True
    assert create.call_args.args[2] == "feature/new"
    assert create.call_args.kwargs["base_branch"] == "main"


@pytest.mark.parametrize(
    "branch, base_branch",
    [("feature/new", "-main"), ("bad..name", "main")],
)
def test_api_create_rejects_invalid_branch_with_base(tmp_path, branch, base_branch):
    """Both names reach checkout -b / push, so git must accept them first."""
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    with patch("oduflow.web_ui.env_ops.create_environment") as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "branch": branch,
                "base_branch": base_branch,
                "repo_url": "r",
                "odoo_image": "i",
            },
        )
        create.assert_not_called()

    assert resp.status_code == 400
    assert "Invalid branch name" in resp.json()["error"]


def test_api_create_env_name_defaults_to_branch(tmp_path):
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    with patch(
        "oduflow.web_ui.env_ops.create_environment",
        return_value={"url": "http://localhost:50000"},
    ) as create:
        resp = client.post(
            "/api/environments/create",
            json={"branch": "staging", "repo_url": "r", "odoo_image": "i"},
        )

    assert resp.json()["ok"] is True
    assert create.call_args.args[2] == "staging"
    assert create.call_args.kwargs["env_name"] == "staging"


def test_api_create_legacy_payload_without_branch(tmp_path):
    """A cached dashboard sends env_name only and means the branch by it."""
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    with patch(
        "oduflow.web_ui.env_ops.create_environment",
        return_value={"url": "http://localhost:50000"},
    ) as create:
        resp = client.post(
            "/api/environments/create",
            json={"env_name": "main", "repo_url": "r", "odoo_image": "i"},
        )

    assert resp.json()["ok"] is True
    assert create.call_args.args[2] == "main"
    assert create.call_args.kwargs["env_name"] == "main"


def test_api_recreate_uses_recorded_git_branch(tmp_path):
    """An environment named apart from its branch recreates on THAT branch."""
    from unittest.mock import MagicMock

    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    container = MagicMock()
    container.labels = {
        settings.repo_label: "https://github.com/x/y.git",
        settings.image_label: "odoo:19.0",
        "oduflow.template": "none",
        "oduflow.git_branch": "staging",
    }

    with (
        patch("oduflow.docker_ops.client.get_client") as mock_client,
        patch("oduflow.docker_ops.system_ops.check_disk_space"),
        patch("oduflow.docker_ops.system_ops.estimate_new_db_bytes", return_value=0),
        patch("oduflow.docker_ops.env_ops._ensure_system_ready"),
        patch("oduflow.docker_ops.env_ops.delete_environment") as delete,
        patch(
            "oduflow.docker_ops.env_ops.create_environment",
            return_value={"url": "http://localhost:50000"},
        ) as create,
    ):
        mock_client.return_value.containers.get.return_value = container
        resp = client.post("/api/environments/oldstaging/recreate")

    assert resp.status_code == 200
    assert delete.call_args.kwargs["preserve_share"] is True
    assert create.call_args.args[2] == "staging"
    assert create.call_args.kwargs["env_name"] == "oldstaging"


def test_api_recreate_restores_legacy_slot_to_branch_hostname(tmp_path):
    from unittest.mock import MagicMock

    data_dir = tmp_path / "team_1"
    team = TeamSettings(
        team_id="1",
        hostname="dev.example.com",
        data_dir=str(data_dir),
        hostname_registry_path=str(data_dir / "hostnames.json"),
        environment_slots=2,
        environment_hostname_mode="branch",
    )
    settings = Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    reserve_environment_slot(team.hostname_registry_path, "feature-a", 2)
    allocate_hostname(
        team.hostname_registry_path, "feature-a", 2, hostname_prefix="dev"
    )

    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)
    container = MagicMock()
    container.labels = {
        settings.repo_label: "https://github.com/x/y.git",
        settings.image_label: "odoo:19.0",
        settings.branch_label: "feature-a",
        "oduflow.template": "none",
        "oduflow.git_branch": "feature-a",
        "oduflow.hostname": "dev1",
    }

    with (
        patch("oduflow.docker_ops.client.get_client") as mock_client,
        patch("oduflow.docker_ops.system_ops.check_disk_space"),
        patch("oduflow.docker_ops.system_ops.estimate_new_db_bytes", return_value=0),
        patch("oduflow.docker_ops.env_ops._ensure_system_ready"),
        patch("oduflow.docker_ops.env_ops.delete_environment") as delete,
        patch(
            "oduflow.docker_ops.env_ops.create_environment",
            return_value={"url": "https://feature-a.dev.example.com"},
        ) as create,
    ):
        mock_client.return_value.containers.get.return_value = container
        resp = client.post("/api/environments/feature-a/recreate")

    assert resp.status_code == 200
    assert delete.call_args.kwargs["preserve_share"] is True
    assert create.call_args.kwargs["hostname"] == ""
    assert create.call_args.kwargs["hostname_source"] == ""


def test_api_create_rejects_invalid_hostname(tmp_path):
    """A hostname that is not a DNS label is a 400 with the reason, not a 500.

    The dashboard shows the API error verbatim, so an underscore typed into the
    Hostname field must come back explained ("expected one DNS label") instead
    of as an opaque "Internal server error."
    """
    settings = _open_settings(tmp_path)
    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    client = TestClient(app)

    with patch("oduflow.web_ui.env_ops.create_environment") as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "oldstaging",
                "hostname": "old_staging",
                "repo_url": "r",
                "odoo_image": "i",
            },
        )
        # Rejected before any provisioning starts.
        create.assert_not_called()

    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert "old_staging" in body["error"]
    assert "DNS label" in body["error"]
    # Rejecting before the lock is taken must leave the env free.
    locks.acquire_env("oldstaging")


def test_api_create_surfaces_value_error_from_create(tmp_path):
    """Input validation raised inside create_environment is a 400, not a 500."""
    settings = _open_settings(tmp_path)
    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    client = TestClient(app)

    message = "hostname is supported only when routing.mode = 'traefik'."
    with patch(
        "oduflow.web_ui.env_ops.create_environment", side_effect=ValueError(message)
    ):
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "main",
                "hostname": "dev2",
                "repo_url": "r",
                "odoo_image": "i",
            },
        )

    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": message}
    # The finally must still have released the lock this request acquired.
    locks.acquire_env("main")
