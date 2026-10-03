"""Helper containers whose stdout Oduflow parses, on any daemon log driver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import docker
from oduflow import postgres_migration
from oduflow.docker_ops import client as client_module
from oduflow.docker_ops.client import get_odoo_uid_gid, run_for_output


def _local_log_daemon(output: bytes) -> MagicMock:
    """A client whose ``containers.run`` behaves like docker-py on a daemon
    defaulting to the ``local`` log driver: no output unless the container
    logs through ``json-file`` or ``journald``."""

    def run(image, command, **kwargs):
        log_type = (kwargs.get("log_config") or {}).get("type", "local")
        return output if log_type in ("json-file", "journald") else None

    client = MagicMock()
    client.containers.run.side_effect = run
    return client


def test_run_for_output_pins_json_file_and_removes_container():
    client = _local_log_daemon(b"out\n")

    assert run_for_output(client, "alpine", ["echo", "out"], user="root") == b"out\n"

    args, kwargs = client.containers.run.call_args
    assert args == ("alpine", ["echo", "out"])
    assert kwargs["log_config"] == {"type": "json-file"}
    assert kwargs["remove"] is True
    assert kwargs["user"] == "root"


def test_run_for_output_propagates_container_error():
    client = MagicMock()
    client.containers.run.side_effect = docker.errors.ContainerError(
        "c", 1, "grep", "alpine", b""
    )

    with pytest.raises(docker.errors.ContainerError):
        run_for_output(client, "alpine", "grep x /nowhere")


def test_odoo_uid_gid_is_detected_not_defaulted(monkeypatch):
    monkeypatch.setattr(client_module, "_uid_gid_cache", {})
    client = _local_log_daemon(b"uid=1001(odoo) gid=1002(odoo) groups=1002(odoo)\n")

    assert get_odoo_uid_gid(client, "custom/odoo:18") == "1001:1002"


def test_postgres_binary_major_is_read():
    client = _local_log_daemon(b"postgres (PostgreSQL) 16.4\n")

    assert postgres_migration._binary_major(client, "sha256:abc") == 16
