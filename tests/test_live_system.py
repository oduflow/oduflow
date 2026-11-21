"""System recovery, orphan cleanup and the team coder on a disposable engine."""

from dataclasses import replace
from pathlib import Path

import pytest

import docker
from oduflow import health
from oduflow.docker_ops import stats, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.naming import get_agent_container_name

pytestmark = pytest.mark.integration


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
