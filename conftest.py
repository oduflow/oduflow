import os
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

collect_ignore_glob = ["src/**"]


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-docker",
        action="store_true",
        help="Permit live tests on an enrolled Docker host",
    )


def pytest_collection_finish(session):
    if session.config.option.collectonly or not any(
        item.get_closest_marker(mark)
        for item in session.items
        for mark in ("integration", "heavyweight")
    ):
        return
    if not session.config.getoption("--run-live-docker"):
        raise pytest.UsageError(
            "Live Docker tests require --run-live-docker on a dedicated host"
        )
    if (
        os.environ.get("DOCKER_CONTEXT")
        or os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
        != "unix:///var/run/docker.sock"
    ):
        raise pytest.UsageError(
            "Live tests must use the dedicated host's local Docker socket"
        )
    marker = Path("/etc/oduflow-test/engine-id")
    if not marker.is_file() or not (expected := marker.read_text().strip()):
        raise pytest.UsageError(f"Dedicated test engine not enrolled in {marker}")
    import docker

    cli_id = subprocess.check_output(
        ["docker", "info", "--format", "{{.ID}}"], text=True
    ).strip()
    with closing(docker.from_env()) as client:
        if cli_id != expected or client.info()["ID"] != expected:
            raise pytest.UsageError(f"Docker Engine ID does not match {marker}")
