"""Regression tests for cleanup_orphans (issue #48).

cleanup_orphans must pass the per-team TeamSettings (which has workspaces_dir)
to env_ops._unmount_filestore, not the global Settings. Passing Settings raised
AttributeError on every orphan, which was swallowed, so the command silently
removed nothing.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

from oduflow import server
from oduflow.docker_ops import system_ops
from oduflow.settings import Settings, TeamSettings


class _FakeContainers:
    def list(self, all=False, filters=None):  # noqa: A002 - matches docker SDK
        return []


class _FakeClient:
    containers = _FakeContainers()


def _team_and_settings(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    return team, settings


def test_cleanup_orphans_removes_orphan_workspace(tmp_path):
    team, settings = _team_and_settings(tmp_path)

    os.makedirs(team.workspaces_dir, exist_ok=True)
    orphan_dir = os.path.join(team.workspaces_dir, "feature-x")
    os.makedirs(orphan_dir)

    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(system_ops, "_exec_sql", return_value=""),
        patch("oduflow.port_registry._load_registry", return_value={}),
        patch("oduflow.port_registry._save_registry"),
    ):
        result = system_ops.cleanup_orphans(settings, team, dry_run=False)

    # With the bug (passing Settings), _unmount_filestore raised AttributeError,
    # the workspace was left in place, and removed_workspaces was empty.
    assert "feature-x" in result["orphan_workspaces"]
    assert not os.path.exists(orphan_dir)


def test_cleanup_orphans_unmount_receives_teamsettings(tmp_path):
    team, settings = _team_and_settings(tmp_path)

    os.makedirs(team.workspaces_dir, exist_ok=True)
    os.makedirs(os.path.join(team.workspaces_dir, "feature-y"))

    seen = {}

    def _fake_unmount(env_name, passed_team):
        seen["env_name"] = env_name
        seen["team"] = passed_team

    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(system_ops, "_exec_sql", return_value=""),
        patch("oduflow.port_registry._load_registry", return_value={}),
        patch("oduflow.port_registry._save_registry"),
        patch(
            "oduflow.docker_ops.env_ops._unmount_filestore", side_effect=_fake_unmount
        ),
    ):
        system_ops.cleanup_orphans(settings, team, dry_run=False)

    assert seen["env_name"] == "feature-y"
    assert seen["team"] is team
    assert isinstance(seen["team"], TeamSettings)


def _exec_sql_router(db_rows, role_rows):
    """Return an _exec_sql side_effect that answers the DB-list and role-list
    queries and no-ops everything else (DROP DATABASE, etc.)."""

    def _router(client, settings, sql):
        lowered = sql.lower()
        if "pg_database" in lowered:
            return "\n".join(db_rows)
        if "pg_roles" in lowered:
            return "\n".join(role_rows)
        return ""

    return _router


def test_cleanup_orphans_excludes_productions(tmp_path):
    """P-C1: productions carry no branch label, so they never appear in
    live_branches. cleanup must never classify a production database, workspace,
    port reservation, or PG role as an orphan, while still catching genuine dev
    orphans that sit alongside them."""
    team, settings = _team_and_settings(tmp_path)

    os.makedirs(team.workspaces_dir, exist_ok=True)
    os.makedirs(os.path.join(team.workspaces_dir, "prod-erp"))
    os.makedirs(os.path.join(team.workspaces_dir, "feature-x"))

    db_rows = ["oduflow_1_prod-erp", "oduflow_1_feature-x"]
    role_rows = ["u_1_prod-erp", "u_1_feature-x"]
    registry = {"prod-erp": {"web": 8069}, "feature-x": {"web": 8070}}

    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(
            system_ops, "_exec_sql", side_effect=_exec_sql_router(db_rows, role_rows)
        ),
        patch("oduflow.port_registry._load_registry", return_value=dict(registry)),
        patch("oduflow.port_registry._save_registry"),
    ):
        result = system_ops.cleanup_orphans(settings, team, dry_run=True)

    # Production resources are never orphans.
    assert "oduflow_1_prod-erp" not in result["orphan_databases"]
    assert "prod-erp" not in result["orphan_workspaces"]
    assert "prod-erp" not in result["orphan_ports"]
    assert "u_1_prod-erp" not in result["orphan_roles"]

    # The genuine dev orphan is still detected in every category.
    assert "oduflow_1_feature-x" in result["orphan_databases"]
    assert "feature-x" in result["orphan_workspaces"]
    assert "feature-x" in result["orphan_ports"]
    assert "u_1_feature-x" in result["orphan_roles"]


def test_cleanup_orphans_never_rmtrees_production_workspace(tmp_path):
    """P-C1 (data-loss): a real cleanup run must leave a production workspace and
    its filestore fully intact."""
    team, settings = _team_and_settings(tmp_path)

    os.makedirs(team.workspaces_dir, exist_ok=True)
    prod_dir = os.path.join(team.workspaces_dir, "prod-erp")
    os.makedirs(prod_dir)
    sentinel = os.path.join(prod_dir, "filestore-sentinel")
    with open(sentinel, "w") as fh:
        fh.write("precious")

    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(system_ops, "_exec_sql", return_value=""),
        patch("oduflow.port_registry._load_registry", return_value={}),
        patch("oduflow.port_registry._save_registry"),
    ):
        result = system_ops.cleanup_orphans(settings, team, dry_run=False)

    assert "prod-erp" not in result["orphan_workspaces"]
    assert os.path.exists(sentinel)


def test_cleanup_orphans_drops_only_role_unused_by_renamed_environment(tmp_path):
    team, settings = _team_and_settings(tmp_path)
    workspace = os.path.join(team.workspaces_dir, "new-name")
    os.makedirs(workspace)
    with open(os.path.join(workspace, "env_credentials.json"), "w") as f:
        f.write('{"pg_user": "u_1_old-name", "pg_password": "secret"}')

    container = SimpleNamespace(
        name="oduflow-1-new-name-odoo",
        labels={settings.branch_label: "new-name"},
    )
    client = _FakeClient()
    client.containers = SimpleNamespace(list=lambda **_kwargs: [container])

    def sql_result(_client, _settings, statement, **_kwargs):
        if "FROM pg_roles" in statement:
            return "u_1_old-name\nu_1_unused"
        return ""

    with (
        patch.object(system_ops, "get_client", return_value=client),
        patch.object(system_ops, "_exec_sql", side_effect=sql_result),
        patch("oduflow.port_registry._load_registry", return_value={}),
    ):
        result = system_ops.cleanup_orphans(settings, team, dry_run=False)

    assert result["orphan_roles"] == ["u_1_unused"]


def test_run_cleanup_prints_orphan_roles(tmp_path, capsys):
    team, settings = _team_and_settings(tmp_path)
    result = {
        "dry_run": True,
        "orphan_databases": [],
        "orphan_workspaces": [],
        "orphan_ports": [],
        "orphan_roles": ["u_1_unused"],
    }

    with patch.object(system_ops, "cleanup_orphans", return_value=result):
        server._run_cleanup(settings, team)

    output = capsys.readouterr().out
    assert "PostgreSQL roles (1):" in output
    assert "u_1_unused" in output
    assert "1 resource(s) would be removed" in output
    assert "No orphaned resources found" not in output
