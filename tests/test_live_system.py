"""System recovery, orphan cleanup and the team coder on a disposable engine."""

import io
from dataclasses import replace
from pathlib import Path

import pytest

import docker
from oduflow import agent_uploads, health
from oduflow.docker_ops import env_ops, stats, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.naming import get_agent_container_name, get_agent_upload_dir

pytestmark = pytest.mark.integration


def test_destroy_then_import_on_same_host_data(live_environment, tmp_path):
    settings, team = live_environment
    dump = tmp_path / "seed.sql"
    dump.write_text(
        "CREATE TABLE ir_module_module (name text, state text, latest_version text);\n"
        "INSERT INTO ir_module_module VALUES ('base', 'installed', '19.0.1.0');\n"
    )
    system_ops.init_system(settings)
    system_ops.import_template(settings, team, source=str(dump), template_name="first")
    old_team_dir = (
        Path(settings.base_data_dir) / "pg_tablespaces" / f"team_{team.team_id}"
    )
    old_inode = old_team_dir.stat().st_ino
    result = system_ops.destroy_system(settings)
    system_ops.init_system(settings)
    imported = system_ops.import_template(
        settings, team, source=str(dump), template_name="second"
    )
    assert imported["dump_reloaded"]
    assert (
        system_ops._exec_sql(
            get_client(),
            settings,
            "SELECT latest_version FROM public.ir_module_module WHERE name='base'",
            db=system_ops.get_template_db_name("second", team.team_id),
            user=system_ops.get_template_owner_role(team.team_id),
        ).strip()
        == "19.0.1.0"
    )
    assert old_team_dir.stat().st_ino != old_inode
    assert (
        Path(result["preserved_tablespaces"]) / old_team_dir.name
    ).stat().st_ino == old_inode


def test_health_recovery_and_reviewed_cleanup(live_environment):
    settings, team = live_environment
    system_ops.init_system(settings)
    pg = get_client().containers.get(settings.shared_db_container)
    assert health.collect_health(settings, force=True)["ok"]
    pg.stop()
    degraded = health.collect_health(settings, force=True)
    assert not degraded["ok"] and degraded["checks"]["dev_pg"]["status"] == "error"
    system_ops.init_system(settings)
    assert (
        health.collect_health(settings, force=True)["checks"]["dev_pg"]["status"]
        == "ok"
    )

    workspaces = Path(team.workspaces_dir)
    reviewed = workspaces / "abandoned"
    reviewed.mkdir(parents=True)
    preview = system_ops.cleanup_orphans(settings, team)
    assert preview["orphan_workspaces"] == [reviewed.name] and reviewed.is_dir()
    newer = workspaces / "newer"
    newer.mkdir()
    removed = system_ops.cleanup_orphans(
        settings, team, dry_run=False, only={"orphan_workspaces": [reviewed.name]}
    )
    assert removed["orphan_workspaces"] == [reviewed.name] and not removed["errors"]
    assert newer.is_dir() and not reviewed.exists()


def test_coder_config_drift_preserves_home(live_environment, monkeypatch):
    settings, team = live_environment
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENCODE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    client = get_client()
    name = get_agent_container_name(team.team_id, settings.prefix)

    def configure(value):
        enabled = replace(team, agent_enabled=True, agent_env={"LIVE_CONFIG": value})
        system_ops.init_system(replace(settings, teams={team.team_id: enabled}))
        return client.containers.get(name)

    original = configure("first")
    assert (
        original.exec_run(["sh", "-ec", "echo persistent >/home/agent/proof"]).exit_code
        == 0
    )
    original.stop(timeout=1)
    restarted = configure("first")
    assert restarted.id == original.id and restarted.status == "running"
    updated = configure("second")
    assert updated.id != original.id
    assert updated.exec_run(["printenv", "LIVE_CONFIG"]) == (0, b"second\n")
    assert updated.exec_run(["cat", "/home/agent/proof"]) == (0, b"persistent\n")
    measured = {row["name"]: row for row in stats.get_container_stats(settings, team)}
    assert measured[name]["mem_usage_mb"] > 0
    system_ops.init_system(settings)
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(name)


def test_coder_attachment_follows_environment_rename(live_environment):
    settings, team = live_environment
    team = replace(
        team,
        agent_enabled=True,
        agent_env=dict.fromkeys(
            (
                "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "OPENCODE_API_KEY",
            ),
            "",
        ),
    )
    settings = replace(settings, teams={team.team_id: team})
    system_ops.init_system(settings)
    env_ops.create_environment(
        settings,
        team,
        "main",
        "https://github.com/oduist/oduflow_test.git",
        "odoo:19.0",
        env_name="old",
    )
    coder = get_client().containers.get(
        get_agent_container_name(team.team_id, settings.prefix)
    )
    uid = env_ops.agent_env_uid(coder, team, "old")
    assert uid is not None
    payload = b"Read this attachment.\n"
    item = agent_uploads.store_attachment(
        settings,
        team,
        "old",
        "notes.txt",
        "text/plain",
        io.BytesIO(payload),
        len(payload),
    )
    user = f"{uid}:{uid}"
    assert coder.exec_run(["cat", item["path"]], user=user) == (0, payload)
    env_ops.update_environment(settings, team, "old", rename_to="new", pull_image=False)
    assert env_ops.agent_env_uid(coder, team, "new") == uid
    moved = f"{get_agent_upload_dir('new')}/{item['id']}/notes.txt"
    assert coder.exec_run(["cat", moved], user=user) == (0, payload)
    agent_uploads.delete_attachment(settings, team, "new", item["id"])
    assert coder.exec_run(["test", "!", "-e", moved], user=user).exit_code == 0
    env_ops.delete_environment(settings, team, "new")
    assert coder.exec_run(["getent", "passwd", str(uid)]).exit_code != 0
