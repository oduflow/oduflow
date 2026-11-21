"""Helper containers whose stdout Oduflow parses, on any daemon log driver."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

import docker
from oduflow import postgres_migration
from oduflow.docker_ops import client as client_module
from oduflow.docker_ops.client import get_odoo_uid_gid, run_for_output


def _fake_client(output: bytes) -> MagicMock:
    client = MagicMock()
    helper = client.containers.create.return_value
    helper.wait.return_value = {"StatusCode": 0}
    helper.logs.return_value = output
    return client


def test_run_for_output_pins_json_file_and_removes_container():
    client = _fake_client(b"out\n")

    assert run_for_output(client, "alpine", ["echo", "out"], user="root") == b"out\n"

    client.containers.create.assert_called_once_with(
        image="alpine",
        command=["echo", "out"],
        user="root",
        log_config={"type": "json-file"},
    )
    assert client.containers.create.return_value.method_calls == [
        call.start(),
        call.wait(),
        call.logs(stdout=True, stderr=False),
        call.remove(force=True, v=True),
    ]


def test_run_for_output_propagates_container_error():
    client = _fake_client(b"bad pattern")
    helper = client.containers.create.return_value
    helper.wait.return_value = {"StatusCode": 2}
    with pytest.raises(docker.errors.ContainerError) as exc:
        run_for_output(client, "alpine", "grep x /nowhere")
    assert (exc.value.exit_status, exc.value.stderr) == (2, b"bad pattern")
    helper.logs.assert_called_once_with(stdout=False, stderr=True)
    helper.remove.assert_called_once_with(force=True, v=True)


@pytest.mark.parametrize("phase", ["start", "wait", "logs"])
def test_failed_helper_is_still_removed(phase):
    client = _fake_client(b"")
    helper = client.containers.create.return_value
    getattr(helper, phase).side_effect = docker.errors.APIError("unavailable")
    with pytest.raises(docker.errors.APIError, match="unavailable"):
        run_for_output(client, "alpine", "true")
    helper.remove.assert_called_once_with(force=True, v=True)


def test_missing_image_is_pulled_before_retrying_create():
    client = _fake_client(b"ok")
    client.containers.create.side_effect = [
        docker.errors.ImageNotFound("not local"),
        client.containers.create.return_value,
    ]
    assert run_for_output(client, "alpine:3", "true") == b"ok"
    client.images.pull.assert_called_once_with("alpine:3", platform=None)


def test_odoo_uid_gid_is_detected_not_defaulted(monkeypatch):
    monkeypatch.setattr(client_module, "_uid_gid_cache", {})
    client = _fake_client(b"uid=1001(odoo) gid=1002(odoo) groups=1002(odoo)\n")

    assert get_odoo_uid_gid(client, "custom/odoo:18") == "1001:1002"


def test_postgres_binary_major_is_read():
    client = _fake_client(b"postgres (PostgreSQL) 16.4\n")

    assert postgres_migration._binary_major(client, "sha256:abc") == 16
