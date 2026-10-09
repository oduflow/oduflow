"""Shared fixtures for integration tests."""

from __future__ import annotations

import atexit
import hashlib
import os
import time

import pytest

from oduflow.docker_ops import env_ops, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.errors import ExternalCommandError
from oduflow.naming import get_resource_name
from oduflow.settings import Settings, TeamSettings

_TEST_PREFIX = "oduflowtest-"
_TEST_NETWORK = "oduflowtest-net"
_TEST_DB_CONTAINER = "oduflowtest-db"
_TEST_DB_VOLUME = "oduflowtest-db-data"


@pytest.fixture(autouse=True)
def _server_lock_per_test(monkeypatch):
    """A test that starts the server must not leave its lock to the next one."""
    from oduflow import instance_lock

    monkeypatch.setattr(instance_lock, "_fd", None)
    yield
    if instance_lock._fd is not None:
        os.close(instance_lock._fd)


def _test_settings(tmp_dir: str) -> tuple[Settings, TeamSettings]:
    suffix = hashlib.sha1(tmp_dir.encode("utf-8")).hexdigest()[:10]
    team = TeamSettings(
        team_id="1",
        data_dir=tmp_dir,
        port_registry_path=os.path.join(tmp_dir, "ports.json"),
        port_range_start=51000,
        port_range_end=51100,
    )
    settings = Settings(
        routing_mode="port",
        db_user="odoo",
        db_password="odoo",
        prefix=f"{_TEST_PREFIX}{suffix}-",
        shared_network=f"{_TEST_NETWORK}-{suffix}",
        shared_db_container=f"{_TEST_DB_CONTAINER}-{suffix}",
        shared_db_volume=f"{_TEST_DB_VOLUME}-{suffix}",
        base_data_dir=tmp_dir,
        teams={"1": team},
    )
    return settings, team


def _cleanup_test_resources(settings: Settings, team: TeamSettings) -> None:
    """Remove all test containers and system resources."""
    try:
        for env in env_ops.list_environments(settings, team):
            try:
                env_ops.delete_environment(settings, team, env["env_name"])
            except Exception:
                pass
    except Exception:
        pass
    try:
        system_ops.destroy_system(settings)
    except Exception:
        pass


def _create_main_environment(settings: Settings, team: TeamSettings) -> None:
    for attempt in range(3):
        system_ops.init_system(settings)
        try:
            env_ops.create_environment(
                settings,
                team,
                branch="main",
                repo_url="https://github.com/oduist/oduflow_test.git",
                odoo_image="odoo:19.0",
                env_name="19.0",
            )
            return
        except ExternalCommandError as exc:
            if "database system is shutting down" not in str(exc) or attempt == 2:
                raise
            _cleanup_test_resources(settings, team)
            time.sleep(2)


def _wait_for_odoo_container_running(
    settings: Settings, team: TeamSettings, env_name: str, timeout: int = 60
) -> None:
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    while time.monotonic() < deadline:
        container = client.containers.get(container_name)
        container.reload()
        last_status = container.status
        if last_status == "running":
            return
        time.sleep(1)
    raise TimeoutError(f"{container_name} did not become running: {last_status}")


def _scenario(request: pytest.FixtureRequest) -> dict:
    callspec = getattr(request.node, "callspec", None)
    if callspec is None:
        return {}
    return callspec.params.get("scenario", {})


def _needs_system(scenario: dict) -> bool:
    return (
        scenario.get("needs_env", False)
        or scenario.get("tool")
        in {"create_environment", "delete_environment", "create_service_database"}
        or scenario.get("cli") == "destroy"
    )


def _needs_main_environment(scenario: dict) -> bool:
    return (
        scenario.get("needs_env", False) or scenario.get("tool") == "delete_environment"
    )


@pytest.fixture
def live_environment(request, tmp_path):
    """Prepare only the Docker resources required by the current scenario."""
    tmp_dir = str(tmp_path)
    settings, team = _test_settings(tmp_dir)
    scenario = _scenario(request)

    _cleanup_test_resources(settings, team)

    # Register atexit cleanup in case pytest is killed or crashes before
    # the yield finalizer runs.
    atexit.register(_cleanup_test_resources, settings, team)

    try:
        if _needs_main_environment(scenario):
            _create_main_environment(settings, team)
            _wait_for_odoo_container_running(settings, team, "19.0")
        elif _needs_system(scenario):
            system_ops.init_system(settings)

        if scenario.get("tool") == "start_environment":
            env_ops.stop_environment(settings, team, "19.0")

        yield settings, team
    finally:
        _cleanup_test_resources(settings, team)
        atexit.unregister(_cleanup_test_resources)
