"""Project sanitization path resolution and compatibility coverage."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from oduflow import sanitizer


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.prefix = "oduflow"
    return settings


def _team(tmp_path) -> MagicMock:
    team = MagicMock()
    team.team_id = "1"
    team.data_dir = str(tmp_path / "team")
    team.workspaces_dir = str(tmp_path / "workspaces")
    return team


def _client(labels: dict[str, str]) -> MagicMock:
    container = MagicMock()
    container.labels = labels
    client = MagicMock()
    client.containers.get.return_value = container
    return client


def _recorded_runner(path, label, *_args):
    return [f"ran:{label}:{path}"]


def test_managed_checkout_uses_canonical_project_path(tmp_path):
    settings = _settings()
    team = _team(tmp_path)
    client = _client({})
    managed_repo = tmp_path / "workspaces" / "main" / "repo"
    canonical = managed_repo / ".oduflow" / "odoo_sanitize"
    canonical.mkdir(parents=True)

    with patch.object(
        sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner
    ) as run:
        logs = sanitizer.sanitize_environment(client, settings, team, env_name="main")

    assert [call.args[0] for call in run.call_args_list] == [
        str(tmp_path / "team" / "odoo_sanitize"),
        str(canonical),
    ]
    assert not any("moved to" in line for line in logs)


def test_include_team_scripts_false_runs_only_project_scripts(tmp_path):
    """A from-scratch environment gets the repo wiring, not the team scrub."""
    settings = _settings()
    team = _team(tmp_path)
    client = _client({})
    managed_repo = tmp_path / "workspaces" / "main" / "repo"
    canonical = managed_repo / ".oduflow" / "odoo_sanitize"
    canonical.mkdir(parents=True)

    with patch.object(
        sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner
    ) as run:
        logs = sanitizer.sanitize_environment(
            client,
            settings,
            team,
            env_name="main",
            include_team_scripts=False,
        )

    assert [call.args[0] for call in run.call_args_list] == [str(canonical)]
    assert logs == [f"ran:repo:{canonical}"]


def test_live_mount_uses_mounted_checkout_and_runs_legacy_first(tmp_path, caplog):
    settings = _settings()
    team = _team(tmp_path)
    live_repo = tmp_path / "checkout"
    legacy = live_repo / ".odoo_sanitize"
    canonical = live_repo / ".oduflow" / "odoo_sanitize"
    legacy.mkdir(parents=True)
    canonical.mkdir(parents=True)
    client = _client({"oduflow.local_path": str(live_repo)})

    with (
        caplog.at_level(logging.WARNING, logger="oduflow"),
        patch.object(
            sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner
        ) as run,
    ):
        logs = sanitizer.sanitize_environment(client, settings, team, env_name="main")

    assert [call.args[0] for call in run.call_args_list] == [
        str(tmp_path / "team" / "odoo_sanitize"),
        str(legacy),
        str(canonical),
    ]
    assert [call.args[1] for call in run.call_args_list] == [
        "system",
        "repo-legacy",
        "repo",
    ]
    assert any(".oduflow/odoo_sanitize" in line for line in logs)
    assert ".oduflow/odoo_sanitize" in caplog.text
    # The scripts still ran: callers read a WARNING line as a failed step.
    assert not any("WARNING:" in line for line in logs)


def test_canonical_live_mount_path_does_not_warn(tmp_path, caplog):
    settings = _settings()
    team = _team(tmp_path)
    live_repo = tmp_path / "checkout"
    canonical = live_repo / ".oduflow" / "odoo_sanitize"
    canonical.mkdir(parents=True)
    client = _client({"oduflow.local_path": str(live_repo)})

    with (
        caplog.at_level(logging.WARNING, logger="oduflow"),
        patch.object(sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner),
    ):
        logs = sanitizer.sanitize_environment(client, settings, team, env_name="main")

    assert not any("moved to" in line for line in logs)
    assert "moved to" not in caplog.text
