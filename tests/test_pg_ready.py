"""Tests for system_ops._wait_pg_ready resilience.

When the shared PostgreSQL container is momentarily not running (still starting,
or restarting after a crash — e.g. the disk filled up), Docker's exec_create
returns 409. _wait_pg_ready must treat that as "not ready yet" and retry, rather
than letting the raw APIError crash the whole server startup (which systemd then
turns into a restart loop).

The probe must also survive an exec that never finishes: docker-py disables the
socket timeout while reading exec output, so the readiness wait is run detached
and polled instead, and a hung probe is one more "not ready" round.
"""

from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings


def _client(exit_codes):
    """Docker client whose execs report *exit_codes* (int, or an exception)."""
    client = MagicMock()
    container = MagicMock()
    container.id = "cid"
    container.name = "oduflow-db"
    client.containers.get.return_value = container

    codes = iter(exit_codes) if isinstance(exit_codes, list) else None

    def exec_create(*args, **kwargs):
        outcome = next(codes) if codes is not None else exit_codes
        if isinstance(outcome, Exception):
            raise outcome
        client.api.exec_inspect.return_value = {"Running": False, "ExitCode": outcome}
        return {"Id": "exec-id"}

    client.api.exec_create.side_effect = exec_create
    return client


def test_tolerates_transient_409_then_ready():
    # First pg_isready hits a restarting container (409); next round it's up.
    client = _client(
        [
            docker.errors.APIError("409 Client Error: Conflict"),  # pg_isready
            0,  # pg_isready OK
            0,  # psql SELECT 1 OK
        ]
    )
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        system_ops._wait_pg_ready(client, Settings(), timeout=5)  # returns, no raise


def test_readiness_is_probed_over_tcp():
    # On a fresh volume the official entrypoint runs a temporary socket-only
    # server for setup and then stops it; only TCP proves the final server.
    client = _client(0)
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        system_ops._wait_pg_ready(client, Settings(), timeout=5)
    first_probe = client.api.exec_create.call_args_list[0].args[1]
    assert first_probe[:3] == ["pg_isready", "-h", "127.0.0.1"]


def test_raises_clean_error_when_never_ready():
    client = _client(docker.errors.APIError("409 Client Error: Conflict"))  # always
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        with pytest.raises(PrerequisiteNotMetError, match="did not become ready"):
            system_ops._wait_pg_ready(client, Settings(), timeout=3)


def test_retries_until_pg_accepts_connections():
    # pg_isready returns non-zero (starting) a couple of times, then ready.
    client = _client([1, 1, 0, 0])
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        system_ops._wait_pg_ready(client, Settings(), timeout=5)


def test_hung_probe_is_bounded_and_retried():
    # An exec that never reports "finished" must not block forever: it times out
    # and counts as one failed round.
    client = MagicMock()
    container = MagicMock()
    container.id = "cid"
    container.name = "oduflow-db"
    client.containers.get.return_value = container
    client.api.exec_create.return_value = {"Id": "exec-id"}
    client.api.exec_inspect.return_value = {"Running": True}  # never finishes

    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        with pytest.raises(PrerequisiteNotMetError, match="did not become ready"):
            system_ops._wait_pg_ready(client, Settings(), timeout=2, exec_timeout=0.01)


def test_exec_exit_code_reports_the_containers_status():
    client = MagicMock()
    container = MagicMock(id="cid")
    container.name = "oduflow-db"  # `name=` in the constructor is not the attribute
    client.api.exec_create.return_value = {"Id": "exec-id"}
    client.api.exec_inspect.side_effect = [
        {"Running": True},
        {"Running": False, "ExitCode": 3},
    ]
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        assert system_ops._exec_exit_code(client, container, ["true"]) == 3
    # Detached start is what keeps the call bounded by the client's own timeout.
    assert client.api.exec_start.call_args.kwargs["detach"] is True
