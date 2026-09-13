"""Dashboard endpoints for copying production data back into dev.

The invariant these guard: ``allow_copy_to_dev_mcp`` gates agents (MCP tools)
only. The dashboard is the administrator's own console and must keep working
with the flag turned off — and it is the only place the flag can be changed.
"""

import os
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import production_registry
from oduflow.errors import BusyError, ConflictError
from oduflow.locking import LockManager, prod_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

PROD = "erp"


def _client(tmp_path) -> tuple[TestClient, LockManager, TeamSettings]:
    team = TeamSettings(
        team_id="1", hostname="example.com", data_dir=str(tmp_path / "team_1")
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        prod_enabled=True,
        teams={"1": team},
    )
    app = Starlette()
    locks = LockManager()
    mount_web_ui(app, lambda: settings, locks)
    return TestClient(app), locks, team


def _register(team: TeamSettings, **overrides) -> None:
    record = {
        "domain": "erp.example.com",
        "repo_url": "https://example.com/org/repo.git",
        "branch": "production",
        "odoo_image": "odoo:18.0",
    }
    record.update(overrides)
    production_registry.create_production(team, PROD, record)


def _publish_result() -> dict[str, object]:
    return {
        "status": "promoted",
        "prod_name": PROD,
        "dump": "/data/templates/snap/dump.pgdump",
        "filestore": "/data/templates/snap/filestore",
        "template_db": "oduflow_tpl_1_snap",
        "affected_envs": ["qa"],
        "remount_failures": [["broken", "overlay busy"]],
    }


# -- save as template ---------------------------------------------------------


def test_save_as_template_publishes_and_reports_affected_envs(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team)

    with patch(
        "oduflow.docker_ops.system_ops.publish_production_as_template",
        return_value=_publish_result(),
    ) as publish:
        response = client.post(
            f"/api/productions/{PROD}/save-as-template",
            json={"template_name": "snap"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["template_name"] == "snap"
    assert body["result"]["template_db"] == "oduflow_tpl_1_snap"
    assert body["result"]["affected_envs"] == ["qa"]
    assert body["result"]["remount_failures"] == [["broken", "overlay busy"]]
    assert publish.call_args.kwargs == {"template_name": "snap", "overwrite": False}
    assert publish.call_args.args[1:] == (team, PROD)


def test_save_as_template_forwards_explicit_overwrite(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team)

    with patch(
        "oduflow.docker_ops.system_ops.publish_production_as_template",
        return_value=_publish_result(),
    ) as publish:
        response = client.post(
            f"/api/productions/{PROD}/save-as-template",
            json={"template_name": "snap", "overwrite": True},
        )

    assert response.status_code == 200
    assert publish.call_args.kwargs["overwrite"] is True


def test_save_as_template_marks_a_name_clash_as_a_conflict(tmp_path):
    # The dashboard offers a re-baseline on this, so it must be able to tell a
    # taken name apart from any other rejection.
    client, _, team = _client(tmp_path)
    _register(team)

    with patch(
        "oduflow.docker_ops.system_ops.publish_production_as_template",
        side_effect=ConflictError("Template 'snap' already exists."),
    ):
        response = client.post(
            f"/api/productions/{PROD}/save-as-template",
            json={"template_name": "snap"},
        )

    assert response.status_code == 400
    assert response.json()["conflict"] is True


@pytest.mark.parametrize(
    ("template_name", "error"),
    [("", "template_name is required"), ("../escape", "")],
)
def test_save_as_template_validates_the_template_name(tmp_path, template_name, error):
    client, _, team = _client(tmp_path)
    _register(team)

    with patch(
        "oduflow.docker_ops.system_ops.publish_production_as_template"
    ) as publish:
        response = client.post(
            f"/api/productions/{PROD}/save-as-template",
            json={"template_name": template_name},
        )

    assert response.status_code == 400
    if error:
        assert response.json()["error"] == error
    publish.assert_not_called()


def test_save_as_template_rejects_an_unknown_production(tmp_path):
    client, locks, _ = _client(tmp_path)

    with patch(
        "oduflow.docker_ops.system_ops.publish_production_as_template"
    ) as publish:
        response = client.post(
            f"/api/productions/{PROD}/save-as-template",
            json={"template_name": "snap"},
        )

    assert response.status_code == 404
    publish.assert_not_called()
    # The team lock is never taken for a production that does not exist.
    locks.acquire_team("1")
    locks.release_team("1")


def test_save_as_template_bounces_off_a_busy_team(tmp_path):
    client, locks, team = _client(tmp_path)
    _register(team)
    locks.acquire_team("1", operation="import_template_from_odoo")
    try:
        with patch(
            "oduflow.docker_ops.system_ops.publish_production_as_template"
        ) as publish:
            response = client.post(
                f"/api/productions/{PROD}/save-as-template",
                json={"template_name": "snap"},
            )
        assert response.status_code == 409
        assert "import_template_from_odoo" in response.json()["error"]
        publish.assert_not_called()
        # The foreign holder still owns the lock.
        with pytest.raises(BusyError):
            locks.acquire_team("1")
    finally:
        locks.release_team("1")


def test_save_as_template_bounces_off_a_busy_production(tmp_path):
    client, locks, team = _client(tmp_path)
    _register(team)
    locks.acquire_env(prod_lock_key("1", PROD), operation="update_production")
    try:
        with patch(
            "oduflow.docker_ops.system_ops.publish_production_as_template"
        ) as publish:
            response = client.post(
                f"/api/productions/{PROD}/save-as-template",
                json={"template_name": "snap"},
            )
        assert response.status_code == 409
        publish.assert_not_called()
        # The team lock taken first was given back.
        locks.acquire_team("1")
        locks.release_team("1")
    finally:
        locks.release_env(prod_lock_key("1", PROD))


def test_save_as_template_releases_the_team_lock(tmp_path):
    client, locks, team = _client(tmp_path)
    _register(team)

    with patch(
        "oduflow.docker_ops.system_ops.publish_production_as_template",
        side_effect=RuntimeError("boom"),
    ):
        response = client.post(
            f"/api/productions/{PROD}/save-as-template",
            json={"template_name": "snap"},
        )

    assert response.status_code == 500
    locks.acquire_team("1")
    locks.release_team("1")
    locks.acquire_env(prod_lock_key("1", PROD))
    locks.release_env(prod_lock_key("1", PROD))


# -- the MCP gate toggle ------------------------------------------------------


def test_copy_to_dev_mcp_toggle_flips_the_registry_value(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team)
    assert production_registry.get_production(team, PROD)["allow_copy_to_dev_mcp"]

    off = client.post(
        f"/api/productions/{PROD}/copy-to-dev-mcp", json={"enabled": False}
    )
    assert off.status_code == 200
    assert (
        production_registry.get_production(team, PROD)["allow_copy_to_dev_mcp"] is False
    )

    on = client.post(f"/api/productions/{PROD}/copy-to-dev-mcp", json={"enabled": True})
    assert on.status_code == 200
    assert (
        production_registry.get_production(team, PROD)["allow_copy_to_dev_mcp"] is True
    )


@pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 1}, {}, []])
def test_copy_to_dev_mcp_toggle_rejects_a_non_boolean(tmp_path, body):
    # This is the only place the gate can change hands: a JSON string "false"
    # must not read as True, and a malformed body is a 400, not a 500.
    client, _, team = _client(tmp_path)
    _register(team, allow_copy_to_dev_mcp=False)

    response = client.post(f"/api/productions/{PROD}/copy-to-dev-mcp", json=body)

    assert response.status_code == 400
    assert (
        production_registry.get_production(team, PROD)["allow_copy_to_dev_mcp"] is False
    )


def test_copy_to_dev_mcp_toggle_rejects_an_unknown_production(tmp_path):
    client, _, _ = _client(tmp_path)

    response = client.post(
        f"/api/productions/{PROD}/copy-to-dev-mcp", json={"enabled": False}
    )

    assert response.status_code == 404


def test_production_listing_exposes_the_flag(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team, allow_copy_to_dev_mcp=False)

    with patch(
        "oduflow.docker_ops.production_ops.list_productions",
        return_value=[{"name": PROD, "allow_copy_to_dev_mcp": False}],
    ):
        response = client.get("/api/productions")

    assert response.status_code == 200
    assert response.json()["productions"][0]["allow_copy_to_dev_mcp"] is False


# -- create environment from a production -------------------------------------


def _create_from_production(client, **extra):
    payload = {"branch": "qa", "from_production": PROD}
    payload.update(extra)
    return client.post("/api/environments/create", json=payload)


def _template_ready(value: bool):
    return patch("oduflow.docker_ops.system_ops.template_is_ready", return_value=value)


def test_create_from_production_publishes_the_managed_template(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team)

    def _publish(settings, tm, prod_name, template_name, **kwargs):
        # Stand in for the real publish: leave the template directory (with its
        # metadata) behind, as the create path then reads it.
        path = tm.get_template_metadata_path(template_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(
                '{"odoo_image": "odoo:18.0", "repo_url": "https://x/y.git", '
                '"source_production": "erp"}'
            )
        return _publish_result()

    with (
        _template_ready(False),
        patch(
            "oduflow.docker_ops.system_ops.publish_production_as_template",
            side_effect=_publish,
        ) as publish,
        patch(
            "oduflow.docker_ops.env_ops.create_environment",
            return_value={"env_name": "qa"},
        ) as create,
    ):
        response = _create_from_production(client)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["from_production"] == PROD
    assert body["template_name"] == f"prod-{PROD}"
    assert publish.call_args.args[2:4] == (PROD, f"prod-{PROD}")
    assert publish.call_args.kwargs == {"overwrite": True}
    assert create.call_args.kwargs["template_name"] == f"prod-{PROD}"
    # Metadata supplied the code origin the request left out.
    assert create.call_args.args[3] == "https://x/y.git"
    assert create.call_args.args[4] == "odoo:18.0"


def test_create_from_production_reuses_an_existing_managed_template(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team)
    metadata = team.get_template_metadata_path(f"prod-{PROD}")
    os.makedirs(os.path.dirname(metadata), exist_ok=True)
    with open(metadata, "w") as f:
        f.write(
            '{"odoo_image": "odoo:18.0", "repo_url": "https://x/y.git", '
            '"source_production": "erp"}'
        )

    with (
        _template_ready(True),
        patch(
            "oduflow.docker_ops.system_ops.publish_production_as_template"
        ) as publish,
        patch(
            "oduflow.docker_ops.env_ops.create_environment",
            return_value={"env_name": "qa"},
        ) as create,
    ):
        response = _create_from_production(client)

    assert response.status_code == 200
    publish.assert_not_called()
    assert create.call_args.kwargs["template_name"] == f"prod-{PROD}"


def test_create_from_production_republishes_a_half_published_template(tmp_path):
    # A directory with metadata but no template database (a failed first
    # publish) must not be mistaken for a published template.
    client, _, team = _client(tmp_path)
    _register(team)
    metadata = team.get_template_metadata_path(f"prod-{PROD}")
    os.makedirs(os.path.dirname(metadata), exist_ok=True)
    with open(metadata, "w") as f:
        f.write(
            '{"odoo_image": "odoo:18.0", "repo_url": "https://x/y.git", '
            '"source_production": "erp"}'
        )

    with (
        _template_ready(False),
        patch(
            "oduflow.docker_ops.system_ops.publish_production_as_template",
            return_value=_publish_result(),
        ) as publish,
        patch(
            "oduflow.docker_ops.env_ops.create_environment",
            return_value={"env_name": "qa"},
        ),
    ):
        response = _create_from_production(client)

    assert response.status_code == 200
    publish.assert_called_once()
    assert publish.call_args.kwargs == {"overwrite": True}


def test_create_from_production_needs_production_hosting(tmp_path):
    team = TeamSettings(
        team_id="1", hostname="example.com", data_dir=str(tmp_path / "team_1")
    )
    settings = Settings(
        base_data_dir=str(tmp_path), prod_enabled=False, teams={"1": team}
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)
    _register(team)

    with patch(
        "oduflow.docker_ops.system_ops.publish_production_as_template"
    ) as publish:
        response = _create_from_production(client)

    assert response.status_code == 400
    assert "Production hosting is disabled" in response.json()["error"]
    publish.assert_not_called()


def test_create_from_production_rejects_an_unknown_production(tmp_path):
    client, locks, _ = _client(tmp_path)

    with (
        patch(
            "oduflow.docker_ops.system_ops.publish_production_as_template"
        ) as publish,
        patch("oduflow.docker_ops.env_ops.create_environment") as create,
    ):
        response = _create_from_production(client)

    assert response.status_code == 404
    publish.assert_not_called()
    create.assert_not_called()
    # The environment lock is released even on the early failure.
    locks.acquire_env("qa", "1")
    locks.release_env("qa")


def test_create_rejects_a_production_and_a_template_together(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team)

    with patch("oduflow.docker_ops.env_ops.create_environment") as create:
        response = _create_from_production(client, template_name="other")

    assert response.status_code == 400
    assert "cannot be combined" in response.json()["error"]
    create.assert_not_called()


# -- the invariant: the dashboard is never gated by the flag ------------------


def test_dashboard_copies_work_when_mcp_copies_are_disabled(tmp_path):
    client, _, team = _client(tmp_path)
    _register(team, allow_copy_to_dev_mcp=False)
    metadata = team.get_template_metadata_path(f"prod-{PROD}")
    os.makedirs(os.path.dirname(metadata), exist_ok=True)
    with open(metadata, "w") as f:
        f.write(
            '{"odoo_image": "odoo:18.0", "repo_url": "https://x/y.git", '
            '"source_production": "erp"}'
        )

    with (
        _template_ready(True),
        patch(
            "oduflow.docker_ops.system_ops.publish_production_as_template",
            return_value=_publish_result(),
        ),
        patch(
            "oduflow.docker_ops.env_ops.create_environment",
            return_value={"env_name": "qa"},
        ),
    ):
        published = client.post(
            f"/api/productions/{PROD}/save-as-template",
            json={"template_name": "snap"},
        )
        created = _create_from_production(client)

    assert published.status_code == 200
    assert published.json()["ok"] is True
    assert created.status_code == 200
    assert created.json()["ok"] is True
