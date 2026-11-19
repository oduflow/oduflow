"""Regression tests for cleanup_orphans (issue #48).

cleanup_orphans must pass the per-team TeamSettings (which has workspaces_dir)
to env_ops._unmount_filestore, not the global Settings. Passing Settings raised
AttributeError on every orphan, which was swallowed, so the command silently
removed nothing.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from oduflow import image_builds, server
from oduflow.docker_ops import system_ops
from oduflow.errors import BusyError
from oduflow.locking import LockManager
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


def test_cleanup_reports_failed_workspace_without_claiming_it_was_removed(tmp_path):
    team, settings = _team_and_settings(tmp_path)
    orphan_dir = os.path.join(team.workspaces_dir, "feature-x")
    os.makedirs(orphan_dir)
    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(system_ops, "_exec_sql", return_value=""),
        patch("oduflow.port_registry._load_registry", return_value={}),
        patch(
            "oduflow.docker_ops.env_ops._unmount_filestore",
            side_effect=RuntimeError("private details"),
        ),
    ):
        result = system_ops.cleanup_orphans(settings, team, dry_run=False)
    assert result["orphan_workspaces"] == []
    assert os.path.isdir(orphan_dir)
    assert result["errors"] == [
        "Workspace feature-x: removal failed. Check server logs."
    ]


def test_cleanup_removes_only_reviewed_orphans(tmp_path):
    team, settings = _team_and_settings(tmp_path)
    for entry in ("reviewed", "appeared-after-preview"):
        os.makedirs(os.path.join(team.workspaces_dir, entry))

    def sql_result(_client, _settings, statement, **_kwargs):
        return "oduflow_1_unreviewed" if "FROM pg_database" in statement else ""

    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(system_ops, "_exec_sql", side_effect=sql_result) as sql,
        patch("oduflow.port_registry._load_registry", return_value={}),
    ):
        result = system_ops.cleanup_orphans(
            settings,
            team,
            dry_run=False,
            only={"orphan_workspaces": ["reviewed", "no-longer-orphaned"]},
        )
    assert result["orphan_workspaces"] == ["reviewed"]
    assert not os.path.exists(os.path.join(team.workspaces_dir, "reviewed"))
    assert os.path.isdir(os.path.join(team.workspaces_dir, "appeared-after-preview"))
    assert result["orphan_databases"] == []
    assert not any("DROP" in c.args[2] for c in sql.call_args_list)


def test_run_cleanup_prints_removal_errors(tmp_path, capsys):
    team, settings = _team_and_settings(tmp_path)
    result = {
        "dry_run": False,
        "orphan_databases": [],
        "orphan_workspaces": [],
        "orphan_ports": [],
        "orphan_roles": [],
        "errors": ["Workspace feature-x: removal failed. Check server logs."],
    }

    with patch.object(system_ops, "cleanup_orphans", return_value=result):
        server._run_cleanup(settings, team, dry_run=False)

    output = capsys.readouterr().out
    assert "No orphaned resources removed." in output
    assert "Error: Workspace feature-x: removal failed" in output


def test_run_cleanup_force_takes_the_team_lock(tmp_path):
    # The CLI shares the server's file lock space: while an environment
    # operation of the team runs in another process (here: another
    # LockManager), --force must not delete anything.
    team, settings = _team_and_settings(tmp_path)
    server_locks = LockManager(os.path.join(settings.base_data_dir, ".locks"))
    server_locks.acquire_env("feature-x", team.team_id, operation="update_environment")
    try:
        with (
            patch.object(system_ops, "cleanup_orphans") as cleanup,
            patch.object(server, "ATTACH_SWAP_LOCK_TIMEOUT", 0.05),
            pytest.raises(BusyError),
        ):
            server._run_cleanup(settings, team, dry_run=False)
        cleanup.assert_not_called()

        # A dry run reads only and stays lock-free.
        preview = {
            "dry_run": True,
            "orphan_databases": [],
            "orphan_workspaces": [],
            "orphan_ports": [],
            "orphan_roles": [],
        }
        with patch.object(system_ops, "cleanup_orphans", return_value=preview):
            server._run_cleanup(settings, team)
    finally:
        server_locks.release_env("feature-x")


def test_run_cleanup_prints_scaled_checkout_sizes(tmp_path, capsys):
    team, settings = _team_and_settings(tmp_path)
    result = {
        "dry_run": True,
        "orphan_databases": [],
        "orphan_workspaces": [],
        "orphan_ports": [],
        "orphan_roles": [],
        "orphan_extra_checkouts": ["enterprise/aaa", "enterprise/bbb", "themes/ccc"],
        "orphan_extra_checkout_sizes": {
            "enterprise/aaa": 300 * 1024,
            "enterprise/bbb": 42 * 1024**2,
            "themes/ccc": 3 * 1024**3,
        },
        "errors": ["Extra-addons checkouts of broken: scan failed, all are kept."],
    }

    with patch.object(system_ops, "cleanup_orphans", return_value=result):
        server._run_cleanup(settings, team)

    output = capsys.readouterr().out
    assert "enterprise/aaa (300 KB)" in output
    assert "enterprise/bbb (42.0 MB)" in output
    assert "themes/ccc (3.0 GB)" in output
    assert "Error: Extra-addons checkouts of broken" in output


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


def test_cleanup_orphans_skips_production_namespace(tmp_path):
    """Production containers carry no branch label, so without the prod-*
    exclusion a LIVE production's workspace looked orphaned and --force
    destroyed it. Deleted-production leftovers have their own tombstone-gated
    purge path and must not be double-handled here either."""
    team, settings = _team_and_settings(tmp_path)

    os.makedirs(team.workspaces_dir, exist_ok=True)
    prod_dir = os.path.join(team.workspaces_dir, "prod-erp")
    os.makedirs(prod_dir)

    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(system_ops, "_exec_sql", return_value=""),
        patch("oduflow.port_registry._load_registry", return_value={}),
        patch("oduflow.port_registry._save_registry"),
    ):
        result = system_ops.cleanup_orphans(settings, team, dry_run=False)

    assert "prod-erp" not in result["orphan_workspaces"]
    assert os.path.isdir(prod_dir)


def _finished_build(team, status):
    """A finished image build job on disk, written as another process would."""
    store = image_builds.BuildJobStore()
    job = store.create(
        team,
        "",
        "main",
        "Dockerfile",
        ".",
        owner_id=image_builds.TEAM_OWNER_ID,
        max_concurrent_builds=99,
    )
    job.local_tag = f"oduflow-build/team-1:{job.build_id}"
    store.finish(team, job, status)
    return job


def _cleanup_with_images(settings, team, present, **kwargs):
    with (
        patch.object(system_ops, "get_client", return_value=_FakeClient()),
        patch.object(system_ops, "_exec_sql", return_value=""),
        patch("oduflow.port_registry._load_registry", return_value={}),
        patch(
            "oduflow.docker_ops.build_ops.staging_image_exists",
            side_effect=lambda tag: tag in present,
        ),
    ):
        return system_ops.cleanup_orphans(settings, team, **kwargs)


def test_cleanup_orphans_previews_and_removes_stale_build_jobs(tmp_path):
    team, settings = _team_and_settings(tmp_path)
    failed = _finished_build(team, image_builds.STATUS_FAILED)
    lost = _finished_build(team, image_builds.STATUS_SUCCEEDED)
    usable = _finished_build(team, image_builds.STATUS_SUCCEEDED)
    running = image_builds.BuildJobStore()
    active = running.create(team, "", "main", "Dockerfile", ".", owner_id="x" * 64)
    present = {usable.local_tag}
    stale = sorted([failed.build_id, lost.build_id])
    jobs_dir = image_builds.BuildJobStore.jobs_dir(team)

    preview = _cleanup_with_images(settings, team, present, dry_run=True)
    assert preview["orphan_build_jobs"] == stale
    assert len(os.listdir(jobs_dir)) == 4

    result = _cleanup_with_images(settings, team, present, dry_run=False)
    assert result["orphan_build_jobs"] == stale
    assert result["errors"] == []
    assert sorted(os.listdir(jobs_dir)) == sorted([usable.build_id, active.build_id])
    running._release(active.build_id)


def test_cleanup_removes_only_reviewed_build_jobs(tmp_path):
    team, settings = _team_and_settings(tmp_path)
    reviewed = _finished_build(team, image_builds.STATUS_FAILED)
    finished_after_preview = _finished_build(team, image_builds.STATUS_CANCELLED)
    usable = _finished_build(team, image_builds.STATUS_SUCCEEDED)

    result = _cleanup_with_images(
        settings,
        team,
        {usable.local_tag},
        dry_run=False,
        # The reviewed list also names a build that is not stale (any more).
        only={"orphan_build_jobs": [reviewed.build_id, usable.build_id]},
    )
    assert result["orphan_build_jobs"] == [reviewed.build_id]
    assert sorted(os.listdir(image_builds.BuildJobStore.jobs_dir(team))) == sorted(
        [finished_after_preview.build_id, usable.build_id]
    )
    # Confirm without the group removes no build job.
    result = _cleanup_with_images(settings, team, set(), dry_run=False, only={})
    assert result["orphan_build_jobs"] == []
    assert os.path.isdir(
        image_builds.store.job_dir(team, finished_after_preview.build_id)
    )


def test_cleanup_reaps_abandoned_build_job_only_on_confirm(tmp_path):
    team, settings = _team_and_settings(tmp_path)
    writer = image_builds.BuildJobStore()
    job = writer.create(team, "", "main", "Dockerfile", ".", owner_id="x" * 64)
    job.status = image_builds.STATUS_BUILDING
    writer.save(team, job)
    writer._release(job.build_id)  # its process died
    job_json = writer.job_json_path(team, job.build_id)
    before = open(job_json).read()

    preview = _cleanup_with_images(settings, team, set(), dry_run=True)
    assert preview["orphan_build_jobs"] == [job.build_id]
    assert open(job_json).read() == before

    result = _cleanup_with_images(
        settings,
        team,
        set(),
        dry_run=False,
        only={"orphan_build_jobs": preview["orphan_build_jobs"]},
    )
    assert result["orphan_build_jobs"] == [job.build_id]
    assert result["errors"] == []
    assert not os.path.exists(writer.job_dir(team, job.build_id))


def test_cleanup_reports_build_job_that_could_not_be_removed(tmp_path):
    team, settings = _team_and_settings(tmp_path)
    job = _finished_build(team, image_builds.STATUS_FAILED)

    with patch.object(image_builds.shutil, "rmtree"):
        result = _cleanup_with_images(settings, team, set(), dry_run=False)
    assert result["orphan_build_jobs"] == []
    assert result["errors"] == [
        f"Image build job {job.build_id}: removal failed. Check server logs."
    ]


def test_run_cleanup_prints_stale_build_jobs(tmp_path, capsys):
    team, settings = _team_and_settings(tmp_path)
    result = {
        "dry_run": True,
        "orphan_databases": [],
        "orphan_workspaces": [],
        "orphan_ports": [],
        "orphan_roles": [],
        "orphan_build_jobs": ["bld-0123456789ab"],
    }

    with patch.object(system_ops, "cleanup_orphans", return_value=result):
        server._run_cleanup(settings, team)

    output = capsys.readouterr().out
    assert "Image build jobs (1):\n    - bld-0123456789ab" in output
    assert "1 resource(s) would be removed." in output
