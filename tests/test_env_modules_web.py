"""Dashboard install/upgrade module endpoints."""

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import BusyError, ExternalCommandError, NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path, locks=None):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks or LockManager())
    return TestClient(app)


def test_list_modules_returns_the_fixed_query_records(tmp_path):
    client = _client(tmp_path)
    modules = [
        {"name": "base", "version": "18.0.1.3"},
        {"name": "sale", "version": ""},
    ]

    with patch(
        "oduflow.web_ui.odoo_ops.list_installed_module_records",
        return_value=modules,
    ) as query:
        response = client.get("/api/environments/feature-x/modules")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "modules": modules}
    assert query.call_args.args[2] == "feature-x"


def test_list_modules_reports_a_missing_environment(tmp_path):
    client = _client(tmp_path)

    with patch(
        "oduflow.web_ui.odoo_ops.list_installed_module_records",
        side_effect=NotFoundError("Environment 'gone' does not exist."),
    ):
        response = client.get("/api/environments/gone/modules")

    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_install_restarts_the_container_on_success(tmp_path):
    client = _client(tmp_path)
    result = {"modules": ["sale"], "exit_code": 0, "output": "Modules loaded."}

    with (
        patch(
            "oduflow.web_ui.odoo_ops.install_odoo_modules", return_value=result
        ) as install,
        patch("oduflow.web_ui.env_ops.restart_environment") as restart,
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": "install", "modules": "sale, crm"},
        )

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["action"] == "install"
    assert body["modules_installed"] == ["sale"]
    assert body["modules_attempted"] == ["sale"]
    assert body["exit_code"] == 0
    assert body["container_restarted"] is True
    assert install.call_args.args[3:] == ("sale", "crm")
    assert restart.called


@pytest.mark.parametrize(
    ("action", "operation", "success_key", "message"),
    [
        ("install", "install_odoo_modules", "modules_installed", "Installed"),
        ("upgrade", "upgrade_odoo_modules", "modules_upgraded", "Upgraded"),
    ],
)
def test_module_apply_reports_success_when_the_followup_restart_fails(
    tmp_path, action, operation, success_key, message
):
    client = _client(tmp_path)
    result = {"modules": ["sale"], "exit_code": 0, "output": "Modules loaded."}

    with (
        patch(f"oduflow.web_ui.odoo_ops.{operation}", return_value=result),
        patch(
            "oduflow.web_ui.env_ops.restart_environment",
            side_effect=ExternalCommandError("docker restart", 1, "secret output"),
        ),
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": action, "modules": "sale"},
        )

    assert response.status_code == 200
    body = response.json()["result"]
    assert body[success_key] == ["sale"]
    assert body["modules_attempted"] == ["sale"]
    assert body["container_restarted"] is False
    assert body["exit_code"] == 0
    assert body["message"] == f"{message}: sale. Restart failed."
    assert "could not be restarted" in body["warnings"][0]
    assert "Check server logs" in body["warnings"][0]
    assert "secret output" not in str(body)


def test_upgrade_all_is_reported_as_every_installed_module(tmp_path):
    client = _client(tmp_path)
    result = {"modules": ["all"], "exit_code": 0, "output": "Modules loaded."}

    with (
        patch(
            "oduflow.web_ui.odoo_ops.upgrade_odoo_modules", return_value=result
        ) as upgrade,
        patch("oduflow.web_ui.env_ops.restart_environment"),
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": "upgrade", "modules": "all"},
        )

    assert response.status_code == 200
    body = response.json()["result"]
    assert upgrade.call_args.args[3:] == ("all",)
    assert body["modules_upgraded"] == ["all"]
    assert body["message"] == (
        "Upgraded: all installed modules. Odoo container restarted."
    )


def test_failed_install_returns_the_odoo_output_without_restarting(tmp_path):
    client = _client(tmp_path)
    result = {"modules": ["sale"], "exit_code": 1, "output": "CRITICAL failure"}

    with (
        patch("oduflow.web_ui.odoo_ops.install_odoo_modules", return_value=result),
        patch("oduflow.web_ui.env_ops.restart_environment") as restart,
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": "install", "modules": "sale"},
        )

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["exit_code"] == 1
    assert body["output"] == "CRITICAL failure"
    assert body["modules_attempted"] == ["sale"]
    assert "modules_installed" not in body
    assert not restart.called


def test_upgrade_restarts_the_container_on_success(tmp_path):
    client = _client(tmp_path)
    result = {"modules": ["sale"], "exit_code": 0, "output": ""}

    with (
        patch(
            "oduflow.web_ui.odoo_ops.upgrade_odoo_modules", return_value=result
        ) as upgrade,
        patch("oduflow.web_ui.env_ops.restart_environment") as restart,
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": "upgrade", "modules": "sale"},
        )

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["modules_upgraded"] == ["sale"]
    assert body["modules_attempted"] == ["sale"]
    assert body["container_restarted"] is True
    assert upgrade.called
    assert restart.called


def test_upgrade_of_an_uninstalled_module_is_reported(tmp_path):
    client = _client(tmp_path)

    with patch(
        "oduflow.web_ui.odoo_ops.upgrade_odoo_modules",
        side_effect=NotFoundError("Module ghost is unknown in environment 'x'."),
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": "upgrade", "modules": "ghost"},
        )

    assert response.status_code == 404
    assert "ghost" in response.json()["error"]


def test_unknown_action_is_rejected(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/environments/feature-x/modules",
        json={"action": "reinstall", "modules": "sale"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Action must be 'install' or 'upgrade'."


def test_empty_module_list_is_rejected(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/environments/feature-x/modules",
        json={"action": "install", "modules": " , "},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "At least one module name is required."


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (["install", "sale"], "Request body must be a JSON object."),
        ("install", "Request body must be a JSON object."),
        (
            {"action": 1, "modules": "sale"},
            "Action must be 'install' or 'upgrade'.",
        ),
        (
            {"action": "install", "modules": ["sale"]},
            "Modules must be a comma-separated string.",
        ),
    ],
)
def test_invalid_json_shapes_are_rejected(tmp_path, payload, error):
    client = _client(tmp_path)

    with patch("oduflow.web_ui.odoo_ops.install_odoo_modules") as install:
        response = client.post(
            "/api/environments/feature-x/modules",
            json=payload,
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": error}
    assert not install.called


def test_module_routes_reject_encoded_traversal_names(tmp_path):
    client = _client(tmp_path)

    with (
        patch("oduflow.web_ui.odoo_ops.list_installed_module_records") as listing,
        patch("oduflow.web_ui.odoo_ops.install_odoo_modules") as install,
    ):
        get_response = client.get("/api/environments/%2E%2E/modules")
        post_response = client.post(
            "/api/environments/%2E%2E/modules",
            json={"action": "install", "modules": "sale"},
        )

    assert get_response.status_code == 400
    assert post_response.status_code == 400
    assert not listing.called
    assert not install.called


@pytest.mark.parametrize("env_name", ["prod-erp", "Prod-erp", "pr.od-erp"])
def test_module_routes_reject_the_normalized_production_namespace(tmp_path, env_name):
    client = _client(tmp_path)

    with (
        patch("oduflow.web_ui.odoo_ops.list_installed_module_records") as listing,
        patch("oduflow.web_ui.odoo_ops.install_odoo_modules") as install,
    ):
        get_response = client.get(f"/api/environments/{env_name}/modules")
        post_response = client.post(
            f"/api/environments/{env_name}/modules",
            json={"action": "install", "modules": "sale"},
        )

    assert get_response.status_code == 400
    assert "production environment" in get_response.json()["error"]
    assert post_response.status_code == 400
    assert "production environment" in post_response.json()["error"]
    assert not listing.called
    assert not install.called


def test_invalid_module_name_is_a_client_error(tmp_path):
    client = _client(tmp_path)

    with patch(
        "oduflow.web_ui.odoo_ops.install_odoo_modules",
        side_effect=ValueError("Invalid module name 'sale;drop'."),
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": "install", "modules": "sale;drop"},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Invalid module name 'sale;drop'."}


def test_busy_environment_is_not_touched(tmp_path):
    locks = LockManager()
    client = _client(tmp_path, locks)

    with patch.object(locks, "acquire_env", side_effect=BusyError("env is busy")):
        with patch("oduflow.web_ui.odoo_ops.install_odoo_modules") as install:
            response = client.post(
                "/api/environments/feature-x/modules",
                json={"action": "install", "modules": "sale"},
            )

    assert response.status_code == 409
    assert not install.called


def test_the_lock_is_released_when_odoo_blows_up(tmp_path):
    locks = LockManager()
    client = _client(tmp_path, locks)

    with patch(
        "oduflow.web_ui.odoo_ops.install_odoo_modules",
        side_effect=RuntimeError("docker exploded"),
    ):
        response = client.post(
            "/api/environments/feature-x/modules",
            json={"action": "install", "modules": "sale"},
        )

    assert response.status_code == 500
    # A leaked lock would make the next call fail with 409 instead of running.
    locks.acquire_env("feature-x", "1")
    locks.release_env("feature-x")
