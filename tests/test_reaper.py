"""Idle-environment reaper.

The module had no test file, and its two thresholds decide whether a
developer's environment is stopped or **permanently deleted**. These tests pin
the boundaries (strictly-longer-than, not at-or-equal), the exemptions
(protected, busy, first sighting), and the config switch that turns each
behaviour off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from oduflow import activity, reaper
from oduflow.errors import BusyError, FlowError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings


def _iso(seconds_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return moment.isoformat(timespec="seconds")


def _env(name="main", running=True, protected=False):
    return {
        "env_name": name,
        "containers": [{"status": "running" if running else "exited"}],
        "protected": protected,
    }


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", data_dir=str(data_dir))


def _settings(tmp_path, team, stop_hours=48, delete_hours=72) -> Settings:
    # Settings is frozen, so each threshold variation needs its own instance.
    return Settings(
        base_data_dir=str(tmp_path),
        teams={"1": team},
        auto_stop_hours=stop_hours,
        auto_delete_hours=delete_hours,
    )


@pytest.fixture
def settings(team, tmp_path):
    return _settings(tmp_path, team)


@pytest.fixture
def ops():
    """Patch the two destructive env operations and env listing."""
    with (
        patch("oduflow.docker_ops.env_ops.list_environments") as listing,
        patch("oduflow.docker_ops.env_ops.stop_environment") as stop,
        patch("oduflow.docker_ops.env_ops.delete_environment") as delete,
    ):
        yield MagicMock(list=listing, stop=stop, delete=delete)


class TestIsRunning:
    def test_any_running_container_counts(self):
        assert reaper._is_running(
            {"containers": [{"status": "exited"}, {"status": "running"}]}
        )

    def test_all_stopped_is_not_running(self):
        assert not reaper._is_running({"containers": [{"status": "exited"}]})

    def test_no_containers_is_not_running(self):
        assert not reaper._is_running({})


class TestAutoStopThreshold:
    def test_idle_past_the_threshold_is_stopped(self, settings, team, ops):
        ops.list.return_value = [_env()]
        activity.touch(team, "main")
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(48 * 3600 + 60)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_called_once_with(settings, team, "main")

    def test_exactly_at_the_threshold_is_left_alone(self, settings, team, ops):
        # The check is `now - last > stop_after`, so the environment survives
        # its own deadline and is reaped on the next sweep.
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(48 * 3600 - 5)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_not_called()

    def test_recent_activity_is_left_alone(self, settings, team, ops):
        ops.list.return_value = [_env()]
        activity.touch(team, "main")

        reaper.sweep(settings, LockManager())

        ops.stop.assert_not_called()

    def test_a_protected_environment_is_never_stopped(self, settings, team, ops):
        ops.list.return_value = [_env(protected=True)]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(1000 * 3600)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_not_called()

    def test_auto_stop_disabled_by_zero_hours(self, settings, team, ops):
        settings = _settings(settings.base_data_dir, team, stop_hours=0)
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(1000 * 3600)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_not_called()

    def test_one_hour_is_a_real_threshold(self, settings, team, ops):
        # `stop_after > 0`, not `> 1`: the smallest configurable value works.
        settings = _settings(settings.base_data_dir, team, stop_hours=1)
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(3600 + 60)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_called_once()

    def test_a_first_sighting_seeds_the_clock_instead_of_reaping(
        self, settings, team, ops
    ):
        ops.list.return_value = [_env()]

        reaper.sweep(settings, LockManager())

        ops.stop.assert_not_called()
        assert "last_activity" in activity.get_all(team)["main"]

    def test_a_running_env_with_a_stale_stop_record_is_re_marked(
        self, settings, team, ops
    ):
        # Started outside our hooks (plain `docker start`).
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"stopped_at": _iso(1000 * 3600)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_not_called()
        rec = activity.get_all(team)["main"]
        assert "stopped_at" not in rec


class TestAutoDeleteThreshold:
    def test_stopped_past_the_threshold_is_deleted(self, settings, team, ops):
        ops.list.return_value = [_env(running=False)]
        activity._save(
            activity.activity_path(team),
            {"main": {"stopped_at": _iso(72 * 3600 + 60)}},
        )

        reaper.sweep(settings, LockManager())

        ops.delete.assert_called_once_with(settings, team, "main")

    def test_exactly_at_the_threshold_is_left_alone(self, settings, team, ops):
        ops.list.return_value = [_env(running=False)]
        activity._save(
            activity.activity_path(team),
            {"main": {"stopped_at": _iso(72 * 3600 - 5)}},
        )

        reaper.sweep(settings, LockManager())

        ops.delete.assert_not_called()

    def test_a_protected_environment_is_never_deleted(self, settings, team, ops):
        ops.list.return_value = [_env(running=False, protected=True)]
        activity._save(
            activity.activity_path(team),
            {"main": {"stopped_at": _iso(1000 * 3600)}},
        )

        reaper.sweep(settings, LockManager())

        ops.delete.assert_not_called()

    def test_auto_delete_disabled_by_zero_hours(self, settings, team, ops):
        settings = _settings(settings.base_data_dir, team, delete_hours=0)
        ops.list.return_value = [_env(running=False)]
        activity._save(
            activity.activity_path(team),
            {"main": {"stopped_at": _iso(1000 * 3600)}},
        )

        reaper.sweep(settings, LockManager())

        ops.delete.assert_not_called()

    def test_an_unrecorded_stop_starts_the_clock_instead_of_deleting(
        self, settings, team, ops
    ):
        # Manual `docker stop`, or an environment predating the tracker: the
        # delete clock starts now rather than from an unknown past.
        ops.list.return_value = [_env(running=False)]

        reaper.sweep(settings, LockManager())

        ops.delete.assert_not_called()
        assert activity.get_all(team)["main"]["stopped_by"] == "observed"


class TestConcurrencyAndFailures:
    def test_a_busy_environment_is_skipped(self, settings, team, ops):
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(1000 * 3600)}},
        )
        locks = MagicMock()
        locks.acquire_env.side_effect = BusyError("in flight")

        reaper.sweep(settings, locks)

        ops.stop.assert_not_called()
        locks.release_env.assert_not_called()

    def test_the_lock_is_released_after_a_stop(self, settings, team, ops):
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(1000 * 3600)}},
        )
        locks = MagicMock()

        reaper.sweep(settings, locks)

        locks.release_env.assert_called_once_with("main")

    def test_the_lock_is_released_when_the_stop_fails(self, settings, team, ops):
        ops.list.return_value = [_env()]
        ops.stop.side_effect = FlowError("cannot stop")
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": _iso(1000 * 3600)}},
        )
        locks = MagicMock()

        reaper.sweep(settings, locks)  # must not raise

        locks.release_env.assert_called_once_with("main")

    def test_an_unlistable_team_does_not_abort_the_sweep(self, settings, tmp_path):
        other = TeamSettings(
            team_id="2",
            data_dir=str(tmp_path / "team_2"),
            port_range_start=50100,
            port_range_end=50200,
        )
        settings.teams["2"] = other
        seen = []

        def _list(_settings, team):
            seen.append(team.team_id)
            if team.team_id == "1":
                raise RuntimeError("docker down")
            return []

        with (
            patch("oduflow.docker_ops.env_ops.list_environments", side_effect=_list),
            patch("oduflow.docker_ops.env_ops.stop_environment"),
        ):
            reaper.sweep(settings, LockManager())

        assert seen == ["1", "2"]

    def test_stale_records_are_pruned_during_the_sweep(self, settings, team, ops):
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {
                "main": {"last_activity": _iso(10)},
                "deleted": {"last_activity": _iso(10)},
            },
        )

        reaper.sweep(settings, LockManager())

        assert set(activity.get_all(team)) == {"main"}


class TestStartReaper:
    def _start(self, settings):
        with patch("threading.Thread") as thread:
            result = reaper.start_reaper(lambda: settings, LockManager())
        return result, thread

    def test_both_behaviours_disabled_starts_no_thread(self, settings, team):
        settings = _settings(settings.base_data_dir, team, 0, 0)

        result, thread = self._start(settings)

        assert result is None
        thread.assert_not_called()

    def test_auto_stop_alone_starts_the_thread(self, settings, team):
        settings = _settings(settings.base_data_dir, team, 48, 0)

        result, thread = self._start(settings)

        assert result is not None
        thread.return_value.start.assert_called_once()

    def test_auto_delete_alone_starts_the_thread(self, settings, team):
        settings = _settings(settings.base_data_dir, team, 0, 72)

        result, _ = self._start(settings)

        assert result is not None

    def test_enabling_auto_delete_warns_about_permanent_deletion(
        self, settings, team, caplog
    ):
        import logging

        settings = _settings(settings.base_data_dir, team, 48, 72)
        with (
            caplog.at_level(logging.WARNING, logger="oduflow"),
            patch("threading.Thread"),
        ):
            reaper.start_reaper(lambda: settings, LockManager())

        assert "PERMANENTLY DELETED" in caplog.text

    def test_no_deletion_warning_when_auto_delete_is_off(self, settings, team, caplog):
        import logging

        settings = _settings(settings.base_data_dir, team, 48, 0)
        with (
            caplog.at_level(logging.WARNING, logger="oduflow"),
            patch("threading.Thread"),
        ):
            reaper.start_reaper(lambda: settings, LockManager())

        assert "PERMANENTLY DELETED" not in caplog.text

    def test_one_hour_auto_delete_still_warns(self, settings, team, caplog):
        # `auto_delete_hours > 0`, not `> 1`.
        import logging

        settings = _settings(settings.base_data_dir, team, 48, 1)
        with (
            caplog.at_level(logging.WARNING, logger="oduflow"),
            patch("threading.Thread"),
        ):
            reaper.start_reaper(lambda: settings, LockManager())

        assert "PERMANENTLY DELETED" in caplog.text


class TestThresholdIsStrict:
    """Both deadlines use ``>``, not ``>=``.

    Pinning that needs the clock frozen: with a wall-clock ``now`` the exact
    boundary is unreachable, so an off-by-one in either comparison — or in the
    hours-to-seconds conversion — goes unnoticed.
    """

    @pytest.fixture
    def frozen(self, monkeypatch):
        monkeypatch.setattr(reaper.time, "time", lambda: 1_000_000.0)
        return 1_000_000.0

    def _at(self, offset: float, frozen: float) -> str:
        return datetime.fromtimestamp(frozen - offset, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    def test_idle_exactly_the_stop_threshold_survives(
        self, settings, team, ops, frozen
    ):
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": self._at(48 * 3600, frozen)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_not_called()

    def test_one_second_past_the_stop_threshold_is_reaped(
        self, settings, team, ops, frozen
    ):
        ops.list.return_value = [_env()]
        activity._save(
            activity.activity_path(team),
            {"main": {"last_activity": self._at(48 * 3600 + 1, frozen)}},
        )

        reaper.sweep(settings, LockManager())

        ops.stop.assert_called_once()

    def test_stopped_exactly_the_delete_threshold_survives(
        self, settings, team, ops, frozen
    ):
        ops.list.return_value = [_env(running=False)]
        activity._save(
            activity.activity_path(team),
            {"main": {"stopped_at": self._at(72 * 3600, frozen)}},
        )

        reaper.sweep(settings, LockManager())

        ops.delete.assert_not_called()

    def test_one_second_past_the_delete_threshold_is_reaped(
        self, settings, team, ops, frozen
    ):
        ops.list.return_value = [_env(running=False)]
        activity._save(
            activity.activity_path(team),
            {"main": {"stopped_at": self._at(72 * 3600 + 1, frozen)}},
        )

        reaper.sweep(settings, LockManager())

        ops.delete.assert_called_once()


class TestProdPurge:
    def test_disabled_by_default(self, settings, ops):
        ops.list.return_value = []
        with patch(
            "oduflow.docker_ops.production_ops.purge_deleted_productions"
        ) as purge:
            reaper.sweep(settings, LockManager())
        purge.assert_not_called()

    def test_enabled_purges_with_cutoff_and_locks(self, team, tmp_path, ops):
        ops.list.return_value = []
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": team},
            prod_purge_hours=72,
        )
        locks = LockManager()
        with patch(
            "oduflow.docker_ops.production_ops.purge_deleted_productions",
            return_value={"purged": [], "pending": [], "warnings": []},
        ) as purge:
            reaper.sweep(settings, locks)
        purge.assert_called_once_with(settings, team, older_than_hours=72, locks=locks)

    def test_purge_failure_does_not_break_the_sweep(self, team, tmp_path, ops):
        ops.list.return_value = [_env()]
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": team},
            prod_purge_hours=72,
        )
        with patch(
            "oduflow.docker_ops.production_ops.purge_deleted_productions",
            side_effect=RuntimeError("boom"),
        ):
            reaper.sweep(settings, LockManager())  # must not raise

    def test_prod_purge_alone_starts_the_thread(self, team, tmp_path):
        settings = Settings(
            base_data_dir=str(tmp_path),
            teams={"1": team},
            auto_stop_hours=0,
            auto_delete_hours=0,
            prod_purge_hours=72,
        )
        with patch("threading.Thread") as thread:
            result = reaper.start_reaper(lambda: settings, LockManager())
        assert result is not None
        thread.assert_called_once()
