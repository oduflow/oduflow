import errno
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oduflow import wal_monitor as wal
from oduflow.docker_ops import system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import BackupSettings, Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        base_data_dir=str(tmp_path),
        prod_enabled=True,
        backup=BackupSettings(bucket="test", access_key="a", secret_key="s"),
    )


@pytest.fixture(autouse=True)
def reset_monitors():
    from oduflow import walg

    for registry in (wal._monitors, wal._emergency, wal._policies, walg._readiness):
        registry.clear()
    yield
    for registry in (wal._monitors, wal._emergency, wal._policies, walg._readiness):
        registry.clear()


def sample(now=1000, free=10 * wal.GIB, queued=5, archived=0):
    return {
        "sampled_at": now,
        "disk_total_bytes": 100 * wal.GIB,
        "disk_free_bytes": free,
        "wal_bytes": 80 * 1024**2,
        "ready_count": queued,
        "ready_bytes": queued * 16 * 1024**2,
        "oldest_ready_seconds": 900 if queued else 0,
        "pg_status": "running",
        "archiver": {
            "archived_count": archived,
            "archive_mode": "on",
            "archive_command": "/bin/sh /opt/oduflow-bin/wal-archive.sh 120",
            "stats_reset": "epoch",
        },
    }


def test_idle_is_not_stalled(settings):
    result = wal.assess(
        settings, sample(queued=0, free=30 * wal.GIB), {"last_progress_at": 0}, [], {}
    )
    assert result["status"] == "ok"
    assert result["no_progress_seconds"] == 0


def test_pending_without_success_is_stalled_even_without_failed_count(settings):
    result = wal.assess(settings, sample(), {"last_progress_at": 0}, [], {})
    assert result["status"] == "error"
    assert "stopped making progress" in result["detail"]


def test_successful_archiving_resets_stall_clock(settings):
    previous = sample(now=985)
    previous["last_progress_at"] = 0
    result = wal.assess(settings, sample(archived=1), previous, [], {})
    assert result["last_progress_at"] == 1000
    assert result["status"] == "warn"  # old backlog is still worth showing


def test_disk_guard_uses_available_bytes_and_predicts_reserve(settings):
    history = [sample(now=940, free=16 * wal.GIB)]
    previous = {**history[0], "projected_breach": True}
    result = wal.assess(settings, sample(free=10 * wal.GIB), previous, history, {})
    assert result["critical_disk"] is True
    assert result["seconds_to_reserve"] == 80
    result = wal.assess(settings, sample(free=23 * 1024**2), None, [], {"paused": True})
    assert result["critical_disk"] is True
    assert result["status"] == "error"


def test_single_disk_burst_does_not_latch_the_whole_cluster(settings):
    # df covers the whole filesystem: a filestore copy or image pull can
    # project a breach for one sample without ever reaching the reserve.
    history = [sample(now=940, free=16 * wal.GIB)]
    burst = wal.assess(settings, sample(free=10 * wal.GIB), history[0], history, {})
    assert burst["projected_breach"] is True
    assert burst["critical_disk"] is False

    # The burst is over: the five-minute average still projects a breach, but
    # nothing is consuming the disk right now, so nothing may latch.
    calmed = wal.assess(
        settings,
        sample(now=1060, free=10 * wal.GIB),
        burst,
        history + [sample(free=10 * wal.GIB)],
        {},
    )
    assert calmed["seconds_to_reserve"] is not None
    assert calmed["projected_breach"] is False
    assert calmed["critical_disk"] is False

    # A leak that keeps consuming does latch, one sample later.
    sustained = wal.assess(
        settings,
        sample(now=1060, free=4 * wal.GIB),
        burst,
        history + [sample(free=10 * wal.GIB)],
        {},
    )
    assert sustained["critical_disk"] is True


def test_burst_projection_does_not_stop_containers(settings):
    client, pg, app, events = containers(settings)
    monitor = wal.Monitor()

    def tick(now, free):
        with (
            patch.object(wal, "sample_local", return_value=sample(now=now, free=free)),
            patch("oduflow.walg.storage_status", return_value={"status": "ok"}),
        ):
            monitor.tick(settings, client)

    tick(940, 16 * wal.GIB)
    tick(1000, 10 * wal.GIB)  # the burst itself may not latch
    assert events == []
    assert wal.state(settings)["latched"] is False

    tick(1060, 4 * wal.GIB)  # a leak that continues still stops production
    assert events == [app.name, pg.name]
    assert wal.state(settings)["latched"] is True


def test_unpersisted_restart_policies_are_not_lost_to_a_full_disk(settings):
    client, pg, app, events = containers(settings)
    with patch.object(wal, "_save", side_effect=OSError(errno.ENOSPC, "full")):
        wal.enforce_stop(client, settings, {"version": 1, "latched": True})
    assert "restart_policies" not in wal.state(settings)
    # The first pass already disabled restarts, so Docker now reports "no".
    for container in [app, pg]:
        container.attrs = {"HostConfig": {"RestartPolicy": {"Name": "no"}}}

    wal._save(settings, {"version": 1, "latched": True})
    wal.enforce_stop(client, settings, wal.state(settings))
    saved = wal.state(settings)["restart_policies"]
    assert saved == {
        app.id: {"Name": "unless-stopped"},
        pg.id: {"Name": "unless-stopped"},
    }


def test_release_restores_the_original_restart_policy(settings):
    client, pg, app, events = containers(settings)
    with patch.object(wal, "_save", side_effect=OSError(errno.ENOSPC, "full")):
        wal.enforce_stop(client, settings, {"version": 1, "latched": True})
    for container in [app, pg]:
        container.attrs = {"HostConfig": {"RestartPolicy": {"Name": "no"}}}
        container.update.reset_mock()

    wal._save(
        settings,
        {"version": 1, "latched": True, "recovering": True, "recovery_started_at": 900},
    )
    current = sample(queued=0)
    current["archiver"]["last_archived_time"] = "1970-01-01 00:16:00+00:00"
    with (
        patch.object(wal, "sample_local", return_value=current),
        patch("oduflow.walg.storage_status", return_value={"status": "ok"}),
        patch.object(wal, "recovery_fence"),
    ):
        wal.control(settings, client, "release", "ALL-PRODUCTIONS")
    for container in [app, pg]:
        container.update.assert_called_with(restart_policy={"Name": "unless-stopped"})


def test_healthy_sample_clears_a_stale_preflight_failure(settings):
    from oduflow import walg

    client, pg, app, events = containers(settings)
    walg._readiness[settings.base_data_dir] = {
        "status": "error",
        "detail": "WAL-G storage probe timed out",
        "checked_at": time.time() - 10 * wal.STALE_AFTER,
    }
    monitor = wal._monitor(settings)
    with (
        patch.object(
            wal,
            "sample_local",
            return_value=sample(now=time.time(), queued=0, free=50 * wal.GIB),
        ),
        patch("oduflow.walg.storage_status", return_value={"status": "ok"}),
    ):
        monitor.tick(settings, client)
    assert walg.readiness_status(settings) == {}
    assert wal.status(settings)["status"] == "ok"


def test_recent_preflight_failure_still_drives_the_status(settings):
    from oduflow import walg

    walg._readiness[settings.base_data_dir] = {
        "status": "error",
        "detail": "WAL-G storage access denied; check credentials and permissions",
        "checked_at": time.time(),
    }
    monitor = wal._monitor(settings)
    monitor.latest = {
        **sample(now=time.time(), queued=0, free=50 * wal.GIB),
        "status": "ok",
    }
    result = wal.status(settings)
    assert result["status"] == "error"
    assert "access denied" in result["detail"]
    walg._readiness.clear()


def test_queue_size_stops_production_even_with_plenty_of_disk(settings):
    client, pg, app, events = containers(settings)
    current = sample(free=50 * wal.GIB, queued=600)
    with patch.object(wal, "sample_local", return_value=current):
        wal.Monitor().tick(settings, client)
    assert wal.state(settings)["latched"] is True
    assert "queue" in wal.state(settings)["reason"]
    assert events == [app.name, pg.name]


def test_recovery_can_drain_oversized_queue_but_cannot_release(settings):
    client, pg, app, events = containers(settings)
    current = sample(free=50 * wal.GIB, queued=600)
    wal._save(settings, {"version": 1, "latched": True, "recovering": True})
    with (
        patch.object(wal, "sample_local", return_value=current),
        patch("oduflow.walg.storage_status", return_value={"status": "ok"}),
    ):
        wal.Monitor().tick(settings, client)
        with pytest.raises(PrerequisiteNotMetError, match="queue to fall"):
            wal.control(settings, client, "release", "ALL-PRODUCTIONS")
    pg.stop.assert_not_called()
    assert app.name in events


def test_queue_warning_is_visible_before_stop(settings):
    current = sample(free=50 * wal.GIB, queued=130)
    result = wal.assess(settings, current, None, [], {})
    assert result["status"] == "warn"
    assert "warning size" in result["detail"]
    assert result["critical_queue"] is False


def test_parse_sample_counts_real_segment_sizes_and_non_root_free_space():
    text = """DISK
Filesystem 1-blocks Used Available Capacity Mounted
/dev/test 100000 70000 23000 75% /var/lib/postgresql/data
WAL
000000010000000000000001 16777216
000000010000000000000002 16777216
READY
000000010000000000000001.ready 900.0
CURRENT
77 990 000000010000000000000001
LAST
970 980 124 000000010000000000000001
"""
    result = wal.parse_sample(text, 1000)
    assert result["disk_free_bytes"] == 23000
    assert result["wal_bytes"] == 33554432
    assert result["ready_bytes"] == 16777216
    assert result["ready_count"] == 1
    assert result["oldest_ready_seconds"] == 100
    assert result["attempt"]["seconds"] == 10
    assert result["last_attempt"]["exit_code"] == 124


def test_corrupt_state_blocks_production_start(settings):
    Path(wal._path(settings)).write_text("{broken")
    with pytest.raises(PrerequisiteNotMetError, match="protection is active"):
        system_ops.ensure_prod_infra(MagicMock(), settings, force=True)


def test_guard_blocks_base_backup_and_pitr_before_touching_cluster(settings):
    from oduflow import walg

    wal._save(settings, {"version": 1, "latched": True})
    client = MagicMock()
    with pytest.raises(PrerequisiteNotMetError):
        walg.backup_push(client, settings)
    client.containers.get.assert_not_called()
    with patch("oduflow.docker_ops.client.get_client") as get_client:
        with pytest.raises(PrerequisiteNotMetError):
            walg.pitr_restore_cluster(settings)
    get_client.assert_not_called()


def test_full_disk_can_overwrite_reserved_state(settings):
    wal._save(settings, {"version": 1, "latched": False})
    with patch.object(
        wal.tempfile, "mkstemp", side_effect=OSError(errno.ENOSPC, "full")
    ):
        wal._save(settings, {"version": 1, "latched": True, "reason": "disk full"})
    assert wal.state(settings)["latched"] is True
    assert Path(wal._path(settings)).stat().st_size >= wal._STATE_SIZE


def test_status_reads_do_not_contact_docker_or_wait_for_monitor(settings):
    monitor = wal._monitor(settings)
    monitor.latest = {**sample(now=time.time()), "status": "ok", "detail": "healthy"}
    assert wal.status(settings)["stale"] is False
    monitor.latest["sampled_at"] = 1
    assert wal.status(settings)["status"] == "error"
    assert wal.status(settings)["stale"] is True


def containers(settings):
    events = []
    client = MagicMock()
    pg, app = MagicMock(), MagicMock()
    for container, name in [(pg, settings.prod_db_container), (app, "production-odoo")]:
        container.name = name
        container.id = name + "-id"
        container.status = "running"
        container.attrs = {"HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}
        container.stop.side_effect = lambda timeout, name=name: events.append(name)
    client.containers.list.return_value = [pg, app]
    client.containers.get.side_effect = lambda name: (
        pg if name in {pg.name, pg.id} else app
    )
    return client, pg, app, events


def test_critical_disk_stops_apps_then_pg_and_survives_restart(settings):
    client, pg, app, events = containers(settings)
    monitor = wal.Monitor()
    with (
        patch.object(wal, "sample_local", return_value=sample(free=wal.GIB)),
        patch("oduflow.walg.storage_status") as probe,
    ):
        monitor.tick(settings, client)
    assert events == [app.name, pg.name]
    for container in [app, pg]:
        container.update.assert_called_with(restart_policy={"Name": "no"})
    probe.assert_not_called()  # protection precedes remote probes
    wal._emergency.clear()  # simulate a server restart
    assert wal.state(settings)["latched"] is True
    with patch.object(system_ops, "ensure_prod_infra") as ensure:
        system_ops.reconcile_prod_workloads(client, settings)
    ensure.assert_not_called()


def test_failed_app_stop_still_stops_pg_and_retries_next_tick(settings):
    client, pg, app, events = containers(settings)
    app.stop.side_effect = RuntimeError("stuck app")
    with pytest.raises(PrerequisiteNotMetError, match="will retry"):
        wal.enforce_stop(client, settings, {"version": 1, "latched": True})
    pg.stop.assert_called_once()
    assert wal.state(settings)["latched"] is True


def test_pg_only_recovery_never_restarts_apps(settings):
    client, pg, app, events = containers(settings)
    pg.exec_run.return_value = (0, b"")
    wal._save(settings, {"version": 1, "latched": True})
    with (
        patch.object(wal, "sample_local", return_value=sample()),
        patch("oduflow.walg.write_walg_config"),
        patch("oduflow.walg.apply_walg_config_ownership"),
        patch("oduflow.walg.apply_archive_command"),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(wal, "recovery_fence"),
    ):
        wal.control(settings, client, "recover", "ALL-PRODUCTIONS")
    pg.start.assert_called_once()
    app.start.assert_not_called()
    assert wal.state(settings)["recovering"] is True
    assert wal.state(settings)["latched"] is True


def test_release_requires_actual_new_archive_even_if_queue_empty(settings):
    client, pg, app, events = containers(settings)
    wal._save(
        settings,
        {"version": 1, "latched": True, "recovering": True, "recovery_started_at": 900},
    )
    current = sample(queued=0)
    with patch.object(wal, "sample_local", return_value=current):
        with pytest.raises(PrerequisiteNotMetError, match="confirmed WAL"):
            wal.control(settings, client, "release", "ALL-PRODUCTIONS")
    assert wal.state(settings)["latched"] is True
    current["archiver"]["last_archived_time"] = "1970-01-01 00:16:00+00:00"
    with (
        patch.object(wal, "sample_local", return_value=current),
        patch("oduflow.walg.storage_status", return_value={"status": "ok"}),
        patch.object(wal, "recovery_fence"),
    ):
        wal.control(settings, client, "release", "ALL-PRODUCTIONS")
    assert wal.state(settings)["latched"] is False
    app.start.assert_not_called()


def test_recovery_refuses_insufficient_headroom(settings):
    wal._save(settings, {"version": 1, "latched": True})
    client = MagicMock()
    with patch.object(wal, "sample_local", return_value=sample(free=3 * wal.GIB)):
        with pytest.raises(PrerequisiteNotMetError, match="Free more space"):
            wal.control(settings, client, "recover", "ALL-PRODUCTIONS")
    client.containers.get.assert_not_called()


def test_pause_retains_archive_command_and_persists(settings):
    client, pg, app, events = containers(settings)
    with (
        patch.object(wal, "cancel_attempt"),
        patch("oduflow.walg.apply_archive_command") as archive,
    ):
        wal.control(settings, client, "pause", "ALL-PRODUCTIONS")
    archive.assert_not_called()
    assert wal.state(settings)["paused"] is True
    assert (Path(settings.base_data_dir) / "walg/archive-paused").exists()


def test_pause_does_not_disable_disk_protection(settings):
    client, pg, app, events = containers(settings)
    wal._save(settings, {"version": 1, "paused": True, "latched": False})
    with patch.object(wal, "sample_local", return_value=sample(free=wal.GIB)):
        wal.Monitor().tick(settings, client)
    pg.stop.assert_called_once()


def test_stopped_sampling_uses_read_only_daemon_volume(settings):
    client = MagicMock()
    client.containers.get.return_value.status = "exited"
    helper = client.containers.run.return_value
    helper.wait.return_value = {"StatusCode": 1}
    helper.logs.return_value = b"unavailable"
    with pytest.raises(PrerequisiteNotMetError):
        wal.sample_local(client, settings)
    args = client.containers.run.call_args.kwargs
    assert args["volumes"][settings.prod_db_volume]["mode"] == "ro"
    assert args["user"] == "postgres"
    helper.remove.assert_called_once_with(force=True, v=True)


@pytest.mark.parametrize(
    "paused,command,expected",
    [
        (False, "exit 0", 0),
        (False, "exit 137", 1),
        (True, "exit 0", 1),
        (False, "sleep 30", 1),
    ],
)
def test_archive_wrapper_never_acknowledges_failure(
    tmp_path, paused, command, expected
):
    root = Path(__file__).parents[1] / "src/oduflow/templates/wal-archive.sh"
    script = (
        root.read_text()
        .replace("/tmp/oduflow-wal", str(tmp_path / "runtime"))
        .replace("/etc/walg", str(tmp_path))
        .replace("/opt/oduflow-bin", str(tmp_path))
    )
    binary = tmp_path / "wal-g"
    binary.write_text("#!/bin/sh\n" + command + "\n")
    binary.chmod(0o755)
    if paused:
        (tmp_path / "archive-paused").touch()
    result = subprocess.run(
        ["sh", "-c", script, "archive", "1", "pg_wal/segment", "segment"],
        timeout=8,
        capture_output=True,
    )
    assert result.returncode == expected
    if command == "sleep 30":
        assert "124" in (tmp_path / "runtime/last").read_text()
    if paused:
        assert not (tmp_path / "runtime/current").exists()


def test_actions_require_cluster_confirmation(settings):
    client = MagicMock()
    with pytest.raises(PrerequisiteNotMetError, match="ALL-PRODUCTIONS"):
        wal.control(settings, client, "pause", "")
    client.containers.get.assert_not_called()


def test_disk_protection_also_works_without_backup_configuration(settings):
    result = wal.assess(
        replace(settings, backup=None), sample(free=wal.GIB), None, [], {}
    )
    assert result["critical_disk"] is True
    assert result["status"] == "error"
