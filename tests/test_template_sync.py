"""Cron jobs never run in the web process; failed downloads never publish."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import template_sync as sync
from oduflow.errors import PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.locking import LockManager, template_lock_key
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


@pytest.fixture
def setup(tmp_path, monkeypatch):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": team},
        template_cron_dir=str(tmp_path / "cron"),
        template_cron_container="oduflow",
    )
    Path(settings.template_cron_dir).mkdir()
    Path(team.get_template_dir("prod")).mkdir(parents=True)
    toml = tmp_path / "oduflow.toml"
    toml.write_text("")
    monkeypatch.setattr(sync, "find_toml", lambda: str(toml))
    return settings, team


def config(**kw):
    return {
        "enabled": True,
        "host": "backup.example.com",
        "user": "backup",
        "port": 2222,
        "path": "/backups/ready",
        "schedule": "0 3 * * *",
        **kw,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "-oProxyCommand=bad"),
        ("host", "host\n* * * * * root bad"),
        ("user", "root;bad"),
        ("path", "/backup\ncmd"),
        ("port", True),
        ("port", 65536),
        ("enabled", "yes"),
        ("schedule", "60 3 * * *"),
        ("schedule", "* * * * *\ncmd"),
        ("schedule", "*/0 * * * *"),
        ("schedule", "0 3 31-1 * *"),
        ("schedule", "0 3 * 13 *"),
    ],
)
def test_rejects_invalid_cron_and_ssh_input(field, value):
    with pytest.raises(ValueError):
        sync.validate_config(config(**{field: value}))


def test_cron_jobs_and_remove_leave_unrelated_jobs(setup):
    settings, team = setup
    unrelated = Path(settings.template_cron_dir) / "other"
    unrelated.write_text("do not change")
    sync.save_config(settings, team, "prod", config())
    cron = sync._cron_path(settings, team, "prod")
    text = cron.read_text()
    assert "0 3 * * * root /usr/bin/docker exec" in text
    # The minute tick only starts Python when a request is queued.
    pending = sync.job_dir(team, "prod") / "pending"
    assert (
        f"* * * * * root /usr/bin/docker exec oduflow test -e {pending} && "
        "/usr/bin/docker exec -e"
    ) in text
    assert "--pending" in text
    assert "backup.example.com" not in text  # untrusted fields stay in JSON
    assert cron.stat().st_mode & 0o777 == 0o600
    sync.save_config(settings, team, "prod", config(enabled=False))
    assert "0 3 * * *" not in cron.read_text()
    sync.request_run(team, "prod")
    inbox = sync.job_dir(team, "prod") / "inbox"
    (inbox / "filestore").mkdir(parents=True)
    (sync.job_dir(team, "prod") / "run.log").write_text("kept")
    sync.remove_config(settings, team, "prod")
    assert not cron.exists()
    assert not (sync.job_dir(team, "prod") / "pending").exists()
    # Nothing re-syncs from the downloaded backup any more; logs stay.
    assert not inbox.exists()
    assert (sync.job_dir(team, "prod") / "run.log").read_text() == "kept"
    assert unrelated.read_text() == "do not change"
    sync.remove_job(team, "prod")
    assert not sync.job_dir(team, "prod").exists()


def test_native_cron_runs_as_the_oduflow_user(setup):
    import os
    import pwd

    settings, _ = setup
    own = pwd.getpwuid(os.geteuid()).pw_name
    native = Settings(base_data_dir=settings.base_data_dir)
    assert sync._cron_user(native) == own
    other = "nobody" if own != "nobody" else "daemon"
    with pytest.raises(ValueError, match="user running Oduflow"):
        sync._cron_user(Settings(template_cron_user=other))
    # Under Docker the host user only runs `docker exec`.
    assert sync._cron_user(settings) == "root"


def test_cron_quotes_percent_and_config_path(setup, monkeypatch, tmp_path):
    settings, team = setup
    monkeypatch.setattr(sync, "find_toml", lambda: str(tmp_path / "odd % ' $(no).toml"))
    text = sync.cron_text(settings, team, "prod", config())
    assert r"\%" in text
    assert "'ODUFLOW_TOML=" in text
    assert "'\"'\"'" in text


def test_api_only_queues_and_observes_files(setup, monkeypatch):
    settings, team = setup
    app = Starlette()
    locks = LockManager()
    mount_web_ui(app, lambda: settings, locks)
    client = TestClient(app)
    # Neither saving nor queuing may spawn a worker or rsync.
    spawn = Mock(side_effect=AssertionError("UI spawned a child"))
    monkeypatch.setattr(sync.subprocess, "run", spawn)
    assert client.post("/api/templates/prod/sync", json=config()).status_code == 200
    assert client.post("/api/templates/prod/sync-now").status_code == 200
    body = client.get("/api/templates/prod/sync").json()
    assert body["pending"] and body["cron_installed"]
    assert body["config"]["port"] == 2222
    other_process = LockManager(locks.lock_dir)
    with other_process.env_lock(template_lock_key("1", "prod")):
        assert client.delete("/api/templates/prod/sync").status_code == 409
    assert client.delete("/api/templates/prod/sync").status_code == 200
    spawn.assert_not_called()


def test_api_environment_opt_in_is_explicit_and_team_scoped(setup, monkeypatch):
    from oduflow.docker_ops import env_ops
    from oduflow.naming import get_workspace_path

    settings, team = setup
    Path(get_workspace_path("dev", team.workspaces_dir)).mkdir(parents=True)
    monkeypatch.setattr(env_ops, "require_environment", lambda *a: None)
    monkeypatch.setattr(
        env_ops,
        "list_environments",
        lambda *a: [{"env_name": "dev", "template_name": "prod"}],
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)
    assert (
        client.post(
            "/api/environments/dev/template-sync", json={"auto_refresh": "yes"}
        ).status_code
        == 400
    )
    assert not sync.env_policy(team, "dev")
    assert (
        client.post(
            "/api/environments/dev/template-sync", json={"auto_refresh": True}
        ).status_code
        == 200
    )
    assert sync.env_policy(team, "dev")["auto_refresh"]
    other = TeamSettings(
        team_id="2", data_dir=str(Path(team.data_dir).parent / "team2")
    )
    assert not sync.env_policy(other, "dev")


def test_unreadable_opt_in_never_authorizes_a_reset(setup):
    from oduflow.naming import get_workspace_path

    _, team = setup
    workspace = Path(get_workspace_path("dev", team.workspaces_dir))
    workspace.mkdir(parents=True)
    (workspace / ".template-sync.json").write_text('{"auto_refresh": tr')
    assert sync.env_policy(team, "dev") == {}
    (workspace / ".template-sync.json").write_text("[true]")
    assert sync.env_policy(team, "dev") == {}


def mock_worker(setup, monkeypatch):
    from oduflow import template_generations as gen
    from oduflow.docker_ops import client, env_ops, system_ops

    settings, team = setup
    sync.save_config(settings, team, "prod", config())
    monkeypatch.setattr(sync, "_transfer", Mock(return_value="snapshot-1"))
    monkeypatch.setattr(client, "get_client", Mock())
    pin = Mock()
    refresh = Mock()
    importer = Mock(return_value={})
    monkeypatch.setattr(gen, "pin_existing", pin)
    monkeypatch.setattr(gen, "record_template_snapshot", Mock())
    monkeypatch.setattr(gen, "data_snapshot", Mock(return_value=""))
    monkeypatch.setattr(gen, "snapshot", Mock(return_value="a" * 32))
    monkeypatch.setattr(gen, "collect", Mock())
    monkeypatch.setattr(gen, "refresh_environment", refresh)
    monkeypatch.setattr(system_ops, "import_template", importer)
    monkeypatch.setattr(
        env_ops,
        "list_environments",
        lambda *a: [
            {"env_name": name, "template_name": "prod"}
            for name in ["dev", "off", "protected"]
        ],
    )
    monkeypatch.setattr(
        sync, "env_policy", lambda t, name: {"auto_refresh": name != "off"}
    )
    monkeypatch.setattr(env_ops, "is_protected", lambda s, t, name: name == "protected")
    return importer, refresh, pin


def test_worker_only_refreshes_opted_in_unprotected_environments(setup, monkeypatch):
    importer, refresh, _ = mock_worker(setup, monkeypatch)
    result = sync.run(*setup, "prod")
    assert result["status"] == "partial"
    assert result["updated_envs"] == ["dev"]
    assert result["skipped_envs"] == ["protected"]
    assert refresh.call_count == 1
    assert importer.call_args.kwargs["overwrite"]
    assert sync.inspect_job(*setup, "prod")["state"]["finished_at"]
    # Environments created from the template from now on know its snapshot.
    from oduflow import template_generations as gen

    gen.record_template_snapshot.assert_called_once_with(setup[1], "prod", "snapshot-1")


def test_environment_already_on_the_snapshot_is_left_alone(setup, monkeypatch):
    from oduflow import template_generations as gen

    _, refresh, _ = mock_worker(setup, monkeypatch)
    gen.data_snapshot.side_effect = lambda team, name: "snapshot-1"
    result = sync.run(*setup, "prod")
    assert result["updated_envs"] == []
    refresh.assert_not_called()
    # Nothing moves to a new generation, so none is hardlinked.
    gen.snapshot.assert_not_called()


def test_no_generation_without_an_opted_in_environment(setup, monkeypatch):
    from oduflow import template_generations as gen

    mock_worker(setup, monkeypatch)
    monkeypatch.setattr(sync, "env_policy", lambda t, name: {})
    assert sync.run(*setup, "prod")["status"] == "success"
    gen.snapshot.assert_not_called()
    gen.collect.assert_called_once()


def test_import_failure_keeps_environment_databases(setup, monkeypatch):
    importer, refresh, _ = mock_worker(setup, monkeypatch)
    importer.side_effect = RuntimeError("restore failed")
    with pytest.raises(RuntimeError, match="restore failed"):
        sync.run(*setup, "prod")
    refresh.assert_not_called()
    state = sync.inspect_job(*setup, "prod")
    assert state["state"]["status"] == "failed"
    assert "restore failed" in state["log"]


def test_transfer_failure_never_touches_template_or_envs(setup, monkeypatch):
    importer, refresh, pin = mock_worker(setup, monkeypatch)
    monkeypatch.setattr(
        sync, "_transfer", Mock(side_effect=RuntimeError("rsync failed"))
    )
    with pytest.raises(RuntimeError):
        sync.run(*setup, "prod")
    importer.assert_not_called()
    pin.assert_not_called()
    refresh.assert_not_called()


def test_concurrent_job_preserves_pending_request(setup, monkeypatch):
    mock_worker(setup, monkeypatch)
    settings, team = setup
    sync.request_run(team, "prod")
    manager = LockManager(str(Path(settings.base_data_dir) / ".locks"))
    with manager.env_lock(template_lock_key("1", "prod")):
        assert sync.run(settings, team, "prod", pending=True) == {"status": "busy"}
    assert sync.inspect_job(settings, team, "prod")["pending"]
    sync.run(settings, team, "prod", pending=True)
    assert not sync.inspect_job(settings, team, "prod")["pending"]


def test_busy_template_queues_the_scheduled_run(setup, monkeypatch):
    importer, _, _ = mock_worker(setup, monkeypatch)
    settings, team = setup
    manager = LockManager(str(Path(settings.base_data_dir) / ".locks"))
    with manager.env_lock(template_lock_key("1", "prod")):
        assert sync.run(settings, team, "prod") == {"status": "deferred"}
    importer.assert_not_called()
    assert sync.inspect_job(settings, team, "prod")["pending"]
    sync.run(settings, team, "prod", pending=True)
    importer.assert_called_once()


def test_busy_team_after_transfer_requeues_the_request(setup, monkeypatch):
    importer, _, pin = mock_worker(setup, monkeypatch)
    settings, team = setup
    monkeypatch.setattr(sync, "TEAM_LOCK_TIMEOUT", 0.05)
    sync.request_run(team, "prod")
    manager = LockManager(str(Path(settings.base_data_dir) / ".locks"))
    with manager.env_lock("dev", "1"):
        result = sync.run(settings, team, "prod", pending=True)
    assert result["status"] == "deferred"
    assert "Queued again" in result["error"]
    pin.assert_not_called()
    importer.assert_not_called()
    job = sync.inspect_job(settings, team, "prod")
    assert job["pending"]
    assert job["state"]["status"] == "deferred"


def test_worker_crash_is_reported_without_pid_guessing(setup):
    settings, team = setup
    sync.save_config(settings, team, "prod", config())
    atomic_write_private_json(
        str(sync.job_dir(team, "prod") / "status.json"), {"status": "running"}
    )
    assert sync.inspect_job(settings, team, "prod")["state"]["status"] == "interrupted"


def test_transfer_rejects_changing_ready_marker(setup, monkeypatch):
    _, team = setup
    inbox = sync.job_dir(team, "prod") / "inbox"
    calls = []
    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/" + name)

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "rsync":
            (inbox / "READY").write_text("snapshot-1")
            (inbox / "filestore").mkdir()
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(
            returncode=0, stdout="snapshot-1" if len(calls) == 1 else "snapshot-2"
        )

    monkeypatch.setattr(sync.subprocess, "run", command)
    with pytest.raises(PrerequisiteNotMetError, match="changed during transfer"):
        sync._transfer(team, config(), inbox)
    assert "StrictHostKeyChecking=yes" in calls[0]
    assert "--protect-args" in calls[1]
    assert "--inplace" not in calls[1]
    assert str(inbox) + "/" == calls[1][-1]
