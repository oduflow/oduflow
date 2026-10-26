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


def test_env_named_subfolders_run_after_each_tier(tmp_path, caplog):
    """A subfolder named exactly after the environment runs right after its
    tier's top-level scripts — in both the team and the repo tier."""
    settings = _settings()
    team = _team(tmp_path)
    live_repo = tmp_path / "checkout"
    canonical = live_repo / ".oduflow" / "odoo_sanitize"
    (canonical / "main").mkdir(parents=True)
    team_dir = tmp_path / "team" / "odoo_sanitize"
    (team_dir / "main").mkdir(parents=True)
    client = _client({"oduflow.local_path": str(live_repo)})

    with (
        caplog.at_level(logging.INFO, logger="oduflow"),
        patch.object(
            sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner
        ) as run,
    ):
        logs = sanitizer.sanitize_environment(client, settings, team, env_name="main")

    assert [call.args[0] for call in run.call_args_list] == [
        str(team_dir),
        str(team_dir / "main"),
        str(canonical),
        str(canonical / "main"),
    ]
    assert [call.args[1] for call in run.call_args_list] == [
        "system",
        "system/main",
        "repo",
        "repo/main",
    ]
    # The match is logged so provisioning logs show what sanitization ran.
    match_lines = [line for line in logs if "matches environment 'main'" in line]
    assert len(match_lines) == 2
    assert str(canonical / "main") in caplog.text


def test_other_env_and_reserved_subfolders_are_ignored(tmp_path):
    """Only the exact env-name subfolder runs: other environments' folders and
    ``_``/``.``-prefixed shared-asset folders (e.g. ``_steps/``) never do."""
    settings = _settings()
    team = _team(tmp_path)
    live_repo = tmp_path / "checkout"
    canonical = live_repo / ".oduflow" / "odoo_sanitize"
    for sub in ("other-env", "_steps", ".hidden", "main-2"):
        (canonical / sub).mkdir(parents=True)
    client = _client({"oduflow.local_path": str(live_repo)})

    with patch.object(
        sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner
    ) as run:
        logs = sanitizer.sanitize_environment(client, settings, team, env_name="main")

    assert [call.args[0] for call in run.call_args_list] == [
        str(tmp_path / "team" / "odoo_sanitize"),
        str(canonical),
    ]
    assert not any("matches environment" in line for line in logs)


def test_reserved_env_names_never_select_a_subfolder(tmp_path):
    """``_``/``.``-prefixed names are reserved for shared assets; even an
    environment so named must not execute those folders."""
    settings = _settings()
    team = _team(tmp_path)
    live_repo = tmp_path / "checkout"
    canonical = live_repo / ".oduflow" / "odoo_sanitize"
    (canonical / "_steps").mkdir(parents=True)
    client = _client({"oduflow.local_path": str(live_repo)})

    with patch.object(
        sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner
    ) as run:
        sanitizer.sanitize_environment(client, settings, team, env_name="_steps")

    assert str(canonical / "_steps") not in [
        call.args[0] for call in run.call_args_list
    ]


def test_legacy_folder_has_no_env_subfolder_support(tmp_path):
    """The deprecated ``.odoo_sanitize`` tier keeps its old flat behaviour."""
    settings = _settings()
    team = _team(tmp_path)
    live_repo = tmp_path / "checkout"
    legacy = live_repo / ".odoo_sanitize"
    (legacy / "main").mkdir(parents=True)
    client = _client({"oduflow.local_path": str(live_repo)})

    with patch.object(
        sanitizer, "_run_scripts_from_dir", side_effect=_recorded_runner
    ) as run:
        sanitizer.sanitize_environment(client, settings, team, env_name="main")

    assert str(legacy / "main") not in [call.args[0] for call in run.call_args_list]


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
