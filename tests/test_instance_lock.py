"""Servers sharing a data directory: many may run, an upgrade needs them gone."""

from __future__ import annotations

import fcntl
import os
import sys
from unittest.mock import patch

import pytest

from oduflow import instance_lock, server
from oduflow.errors import BusyError
from oduflow.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(base_data_dir=str(tmp_path / "data"))


@pytest.fixture
def other_server(settings):
    """Another process's shared lock (flock locks per open file, not per process)."""
    fd = instance_lock._open(settings)
    fcntl.flock(fd, fcntl.LOCK_SH)
    yield fd
    os.close(fd)


def test_servers_run_side_by_side(settings, other_server):
    instance_lock.hold_shared(settings)
    assert instance_lock._fd is not None


def test_exclusive_step_is_refused_while_another_server_runs(settings, other_server):
    instance_lock.hold_shared(settings)
    with pytest.raises(BusyError, match="Stop it to upgrade PostgreSQL"):
        with instance_lock.exclusive(settings, "upgrade PostgreSQL"):
            pytest.fail("ran next to another server")


def test_exclusive_step_runs_alone_and_shares_again(settings):
    instance_lock.hold_shared(settings)
    with instance_lock.exclusive(settings, "upgrade PostgreSQL"):
        late = instance_lock._open(settings)
        with pytest.raises(BlockingIOError):
            fcntl.flock(late, fcntl.LOCK_SH | fcntl.LOCK_NB)
    # Back to shared: another server can start again.
    fcntl.flock(late, fcntl.LOCK_SH | fcntl.LOCK_NB)
    os.close(late)


def test_server_starting_during_an_upgrade_is_refused(settings):
    upgrading = instance_lock._open(settings)
    fcntl.flock(upgrading, fcntl.LOCK_EX)
    try:
        with pytest.raises(BusyError, match="is upgrading PostgreSQL"):
            instance_lock.hold_shared(settings)
    finally:
        os.close(upgrading)


def test_lock_ends_with_the_process(settings):
    exited = instance_lock._open(settings)
    fcntl.flock(exited, fcntl.LOCK_SH)
    os.close(exited)  # what the kernel does when a server exits
    with instance_lock.exclusive(settings, "upgrade PostgreSQL"):
        pass


def test_serve_registers_before_startup_touches_anything(settings):
    events = []
    with (
        patch.object(sys, "argv", ["oduflow", "--transport", "http"]),
        patch.object(server, "find_toml"),
        patch.object(server, "_get_settings", return_value=settings),
        patch.object(
            instance_lock,
            "hold_shared",
            side_effect=lambda s: events.append("lock"),
        ),
        patch(
            "oduflow.docker_ops.client.wait_for_docker",
            side_effect=lambda: events.append("docker"),
        ),
        patch("oduflow.postgres_migration.validate_configuration"),
        patch.object(server.migrations, "run_pending"),
        patch("oduflow.postgres_migration.upgrade"),
        patch.object(server, "_ensure_initialized"),
        patch.object(server.quotas, "apply_all"),
        patch.object(server, "_start_http"),
    ):
        server._run_cli()
    assert events == ["lock", "docker"]
