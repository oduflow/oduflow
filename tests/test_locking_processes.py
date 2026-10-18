"""Actual independent interpreters must contend with the live server's gates."""

import subprocess
import sys

import pytest

from oduflow.errors import BusyError
from oduflow.locking import LockManager


@pytest.mark.parametrize(
    "held,attempt",
    [
        ("l.acquire_env('dev', '1')", "l.acquire_team('1')"),
        ("l.acquire_team('1')", "l.acquire_env('other', '1')"),
        ("l.acquire_env('tpl:1:prod')", "l.acquire_env('tpl:1:prod')"),
        ("l.acquire_system()", "l.acquire_env('prod:1:erp')"),
        ("l.acquire_env('prod:1:erp')", "l.acquire_system()"),
    ],
)
def test_cross_process_exclusion_and_release_after_exit(tmp_path, held, attempt):
    script = f"from oduflow.locking import LockManager\nl=LockManager({str(tmp_path)!r})\n{held}\nprint('locked', flush=True)\ninput()\n"
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        manager = LockManager(str(tmp_path))
        with pytest.raises(BusyError, match="Another process"):
            exec(attempt, {"l": manager})
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
    # A process death releases kernel locks; leftover files are not stale locks.
    exec(attempt, {"l": manager})


def test_distinct_environments_and_teams_can_progress(tmp_path):
    a, b = LockManager(str(tmp_path)), LockManager(str(tmp_path))
    with a.env_lock("first", "1"):
        with b.env_lock("second", "1"):
            with b.team_lock("2"):
                pass
    # A rejected team acquire must release its local lock and file handles.
    with a.env_lock("first", "1"):
        with pytest.raises(BusyError):
            b.acquire_team("1")
    with b.team_lock("1"):
        pass


def test_blocking_acquire_skips_at_once_while_another_process_runs_pitr(tmp_path):
    import time

    script = f"from oduflow.locking import LockManager\nl=LockManager({str(tmp_path)!r})\nl.acquire_system()\nprint('locked', flush=True)\ninput()\n"
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        manager = LockManager(str(tmp_path))
        started = time.monotonic()
        # Waiting out a cluster-wide restore is pointless: skip as on a timeout.
        assert manager.acquire_env_blocking("prod:1:erp", timeout=30) is False
        assert time.monotonic() - started < 5
        # The production's own lock is free again for the next caller.
        assert manager.acquire_env_blocking("dev", timeout=1) is True
        manager.release_env("dev")
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()


def test_unusable_lock_directory_is_a_clear_error(tmp_path):
    from oduflow.errors import PrerequisiteNotMetError

    blocker = tmp_path / "locks"
    blocker.write_text("not a directory")
    manager = LockManager(str(blocker))
    with pytest.raises(PrerequisiteNotMetError, match="Cannot take a lock"):
        manager.acquire_env("dev", "1")
    # The in-process lock was given back with the failure.
    manager.lock_dir = str(tmp_path / "ok")
    with manager.env_lock("dev", "1"):
        pass


def test_root_gives_new_lock_files_to_the_data_directory_owner(tmp_path, monkeypatch):
    import os

    from oduflow import locking

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    owner = data_dir.stat()
    monkeypatch.setattr(locking.os, "geteuid", lambda: 0)
    expected = None if owner.st_uid == 0 else (owner.st_uid, owner.st_gid)
    assert locking._prepare_lock_dir(str(data_dir / ".locks")) == expected
    # A data directory root itself owns needs no change.
    monkeypatch.setattr(
        locking.os,
        "stat",
        lambda path, **kw: os.stat_result((0o40700, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    )
    assert locking._prepare_lock_dir(str(data_dir / ".locks")) is None
