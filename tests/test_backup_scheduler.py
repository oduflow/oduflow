import datetime
from unittest.mock import patch

import pytest

from oduflow import backup_scheduler as sched
from oduflow import production_registry
from oduflow.locking import LockManager
from oduflow.settings import BackupSettings, Settings, TeamSettings

TZ = datetime.timezone.utc


def _dt(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 7, day, hour, minute, tzinfo=TZ)


class TestLastFireTime:
    def test_today_when_passed(self):
        now = _dt(10, 8, 30)
        assert sched.last_fire_time(now, "02:00") == _dt(10, 2, 0)

    def test_yesterday_when_not_yet(self):
        now = _dt(10, 1, 30)
        assert sched.last_fire_time(now, "02:00") == _dt(9, 2, 0)

    def test_weekly_constrained(self):
        # 2026-07-10 is a Friday; the previous Sunday is 2026-07-05.
        now = _dt(10, 12, 0)
        fire = sched.last_fire_time(now, "04:30", weekday=6)
        assert fire == _dt(5, 4, 30)
        assert fire.weekday() == 6


class TestIsDue:
    def test_fire_rule_catch_up(self):
        # Server down at 02:00, first tick at 08:00: due exactly once.
        now = _dt(10, 8, 0)
        fire = _dt(10, 2, 0)
        assert sched.is_due(now, fire, _dt(9, 2, 1), None, 0) is True
        # After a success at 08:01 the same slot is no longer due.
        assert sched.is_due(now, fire, _dt(10, 8, 1), None, 0) is False

    def test_restart_after_success_does_not_double_fire(self):
        now = _dt(10, 2, 5)
        fire = _dt(10, 2, 0)
        assert sched.is_due(now, fire, _dt(10, 2, 3), None, 0) is False

    def test_failed_attempts_retry_until_cap(self):
        now = _dt(10, 3, 0)
        fire = _dt(10, 2, 0)
        last_attempt = _dt(10, 2, 30)
        assert sched.is_due(now, fire, None, last_attempt, 1) is True
        assert sched.is_due(now, fire, None, last_attempt, 3) is False
        # The next day's slot resets the attempt budget (attempt < fire).
        tomorrow_fire = _dt(11, 2, 0)
        assert sched.is_due(_dt(11, 2, 1), tomorrow_fire, None, last_attempt, 3)

    def test_future_fire_not_due(self):
        assert sched.is_due(_dt(10, 1, 0), _dt(10, 2, 0), None, None, 0) is False


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", data_dir=str(data_dir))


@pytest.fixture
def settings(team, tmp_path):
    return Settings(
        base_data_dir=str(tmp_path),
        prod_enabled=True,
        backup=BackupSettings(
            bucket="b", access_key="a", secret_key="s", snapshot_time="02:00"
        ),
        teams={"1": team},
    )


class TestTick:
    def test_no_backup_config_is_noop(self, team, tmp_path):
        settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
        with patch.object(sched, "_run_snapshot_job") as job:
            sched.tick(settings, LockManager())
        job.assert_not_called()

    def test_disabled_production_is_noop(self, settings):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            prod_enabled=False,
            backup=settings.backup,
            teams=settings.teams,
        )
        with patch.object(sched, "_run_snapshot_job") as job:
            sched.tick(settings, LockManager())
        job.assert_not_called()

    def test_disabled_production_does_not_start_scheduler(self, settings):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            prod_enabled=False,
            backup=settings.backup,
            teams=settings.teams,
        )
        with patch("threading.Thread") as thread:
            assert sched.start_backup_scheduler(lambda: settings, LockManager()) is None
        thread.assert_not_called()

    def test_due_snapshot_fires(self, settings, team):
        production_registry.create_production(team, "erp", {})
        with (
            patch.object(sched, "_run_snapshot_job") as job,
            patch(
                "oduflow.docker_ops.system_ops.prod_infra_exists",
                return_value=False,
            ),
        ):
            sched.tick(settings, LockManager())
        job.assert_called_once()
        assert job.call_args[0][3] == "erp"

    def test_off_schedule_skipped(self, settings, team):
        production_registry.create_production(team, "erp", {})
        production_registry.set_nested(team, "erp", "backup", {"schedule": "off"})
        with (
            patch.object(sched, "_run_snapshot_job") as job,
            patch(
                "oduflow.docker_ops.system_ops.prod_infra_exists",
                return_value=False,
            ),
        ):
            sched.tick(settings, LockManager())
        job.assert_not_called()

    def test_recent_success_not_refired(self, settings, team):
        production_registry.create_production(team, "erp", {})
        now = datetime.datetime.now().astimezone()
        production_registry.set_nested(
            team,
            "erp",
            "backup",
            {"last_snapshot_at": now.isoformat()},
        )
        with (
            patch.object(sched, "_run_snapshot_job") as job,
            patch(
                "oduflow.docker_ops.system_ops.prod_infra_exists",
                return_value=False,
            ),
        ):
            sched.tick(settings, LockManager())
        job.assert_not_called()

    def test_busy_lock_skips_and_stays_due(self, settings, team):
        production_registry.create_production(team, "erp", {})
        locks = LockManager()
        locks.acquire_env("prod:1:erp")  # simulate a deploy in flight
        now = datetime.datetime.now().astimezone()
        with patch("oduflow.backup_ops.snapshot_production") as snap:
            sched._run_snapshot_job(settings, team, locks, "erp", now, now)
        snap.assert_not_called()


class TestSlotAttemptsReset:
    """A new schedule slot resets the retry budget (the counter previously
    only ever cleared on success, starving later slots of retries)."""

    def test_new_slot_resets_attempts(self, settings, team):
        production_registry.create_production(team, "erp", {})
        # Yesterday's slot exhausted its attempts.
        production_registry.set_nested(
            team,
            "erp",
            "backup",
            {"slot_attempts": 3, "last_attempt_at": _dt(9, 2, 1).isoformat()},
        )
        fire = _dt(10, 2, 0)  # today's slot, newer than the last attempt
        with patch(
            "oduflow.backup_ops.snapshot_production", side_effect=Exception("boom")
        ):
            sched._run_snapshot_job(
                settings, team, LockManager(), "erp", _dt(10, 8), fire
            )
        backup = production_registry.get_production(team, "erp")["backup"]
        assert backup["slot_attempts"] == 1  # reset to 0, then this attempt

    def test_same_slot_increments_attempts(self, settings, team):
        production_registry.create_production(team, "erp", {})
        production_registry.set_nested(
            team,
            "erp",
            "backup",
            {"slot_attempts": 1, "last_attempt_at": _dt(10, 2, 5).isoformat()},
        )
        fire = _dt(10, 2, 0)  # same slot the last attempt already belonged to
        with patch(
            "oduflow.backup_ops.snapshot_production", side_effect=Exception("boom")
        ):
            sched._run_snapshot_job(
                settings, team, LockManager(), "erp", _dt(10, 8), fire
            )
        backup = production_registry.get_production(team, "erp")["backup"]
        assert backup["slot_attempts"] == 2  # kept and incremented


class TestRequestBaseBackup:
    def test_next_tick_takes_a_base_backup(self, settings):
        # A recreated production cluster: the last success predates its archive.
        sched._save_cluster_state(
            settings,
            {
                "basebackup": {
                    "last_success_at": datetime.datetime.now().astimezone().isoformat()
                }
            },
        )
        sched.request_base_backup(settings)
        with (
            patch.object(sched, "_run_basebackup_job") as job,
            patch("oduflow.docker_ops.system_ops.prod_infra_exists", return_value=True),
            patch("oduflow.docker_ops.client.get_client"),
        ):
            sched.tick(settings, LockManager())
        job.assert_called_once()
