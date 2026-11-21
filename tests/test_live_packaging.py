"""Build the distribution Dockerfile from tracked package files; never publish."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from contextlib import closing, suppress
from pathlib import Path

import pytest

import docker
from oduflow.docker_ops import image_cleanup
from oduflow.docker_ops.client import get_client
from oduflow.settings import tomllib

pytestmark = pytest.mark.integration


@pytest.fixture
def package_context(tmp_path):
    root = Path(__file__).resolve().parents[1]
    inventory = subprocess.check_output(
        ["git", "ls-files", "--", "src/oduflow"], cwd=root, text=True
    )
    paths = [Path(p) for p in inventory.splitlines()]
    assert Path("src/oduflow/server.py") in paths, "Missing tracked package inventory"
    assert all(
        path.is_relative_to("src/oduflow") and ".." not in path.parts for path in paths
    )
    paths += list(
        map(
            Path,
            (
                "Dockerfile",
                ".dockerignore",
                "requirements.txt",
                "pyproject.toml",
                "README.md",
                "LICENSE",
                "NOTICE",
                "COMMERCIAL-LICENSE.md",
                "EVALUATION-LICENSE.md",
            ),
        )
    )
    context = tmp_path / "context"
    for path in paths:
        source = root / path
        assert source.is_file() and source.resolve() == source, path
        target = context / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return context


def test_packaged_entrypoint_buildx_and_stack_plan(package_context, tmp_path):
    name = "oduflowtest-package-" + uuid.uuid4().hex[:16]
    tag = "oduflowtest-package:" + name
    env = {**os.environ, "BUILDX_CONFIG": str(tmp_path / "buildx")}
    log = tmp_path / "package-build.log"
    with closing(get_client()) as client:
        subprocess.run(
            [
                "docker",
                "buildx",
                "create",
                "--name",
                name,
                "--driver",
                "docker-container",
                "--buildkitd-flags=--oci-worker-snapshotter=native",
            ],
            check=True,
            env=env,
            timeout=60,
        )
        try:
            with log.open("wb") as output:
                result = subprocess.run(
                    [
                        "docker",
                        "buildx",
                        "build",
                        "--builder",
                        name,
                        "--progress=plain",
                        "--load",
                        "--tag",
                        tag,
                        "--label",
                        f"oduflowtest.package={name}",
                        str(package_context),
                    ],
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=1200,
                )
            assert result.returncode == 0, (
                f"{log}\n{log.read_text(errors='replace')[-7000:]}"
            )
            image = client.images.get(tag)
            assert image.labels["oduflowtest.package"] == name
            assert image.attrs["Config"]["Entrypoint"] == ["tini", "--", "oduflow"]

            def cli(*args, **kwargs):
                return client.containers.run(
                    image.id,
                    ["--no-telemetry", *args],
                    remove=True,
                    network_disabled=True,
                    **kwargs,
                ).decode()

            version = tomllib.loads((package_context / "pyproject.toml").read_text())[
                "project"
            ]["version"]
            assert cli("--version").strip() == f"oduflow {version}"
            assert "--no-telemetry" in cli("--help")
            assert b"github.com/docker/buildx" in client.containers.run(
                image.id,
                ["buildx", "version"],
                entrypoint="docker",
                remove=True,
                network_disabled=True,
            )

            # The public TOML does not expose all names/labels used by init and
            # destroy. Plan only: the socket is for inspecting volume existence.
            fixture = tmp_path / "fixture"
            fixture.mkdir()
            (fixture / "oduflow.toml").write_text(
                f'[storage]\ndata_dir="/tmp/data"\netc_dir="/tmp"\n[team.{name}]\nhostname="localhost"\n'
            )
            (fixture / "stack.yaml").write_text(
                f"apiVersion: oduflow.dev/v1alpha1\nkind: Stack\nmetadata: {{name: {name}}}\n"
                f"spec:\n  volumes:\n    {name}: {{}}\n"
            )
            plan = cli(
                "stack",
                "plan",
                "/fixture/stack.yaml",
                "--team",
                name,
                environment={"ODUFLOW_TOML": "/fixture/oduflow.toml"},
                volumes={
                    str(fixture): {"bind": "/fixture", "mode": "ro"},
                    "/var/run/docker.sock": {
                        "bind": "/var/run/docker.sock",
                        "mode": "ro",
                    },
                },
            )
            assert f"+ create volumes.{name}" in plan, plan
            with pytest.raises(docker.errors.NotFound):
                client.volumes.get(f"oduflow-vol-{name}-{name}")
        finally:
            try:
                with suppress(docker.errors.ImageNotFound):
                    client.images.remove(tag, noprune=True)
            finally:
                subprocess.run(
                    ["docker", "buildx", "rm", name], check=True, env=env, timeout=120
                )


def test_daemon_cleanup_only_removes_reviewed_unreferenced_image(tmp_path):
    # Import a unique one-file filesystem. No pull, parent layers, or tags
    # shared with any other test, even when another suite builds concurrently.
    name = "oduflowtest-cleanup-" + uuid.uuid4().hex
    archive = tmp_path / "rootfs.tar"
    with tarfile.open(archive, "w") as tar:
        data = name.encode()
        item = tarfile.TarInfo("proof")
        item.size = len(data)
        tar.addfile(item, io.BytesIO(data))
    with closing(get_client()) as client:
        reply = json.loads(
            client.api.import_image_from_data(
                archive.read_bytes(), repository=name, tag="owned"
            )
        )
        tag = f"{name}:owned"
        try:
            image_id = client.images.get(tag).id
            assert reply["status"] == image_id
            assert image_id in {row["id"] for row in image_cleanup.list_unused_images()}
            container = client.containers.create(
                image_id, command=["/unused"], network_disabled=True
            )
            try:
                refused = image_cleanup.remove_unused_images([image_id])
                assert refused == {
                    "removed": [],
                    "skipped": [{"id": image_id, "reason": "Now used by a container."}],
                    "errors": [],
                }
                assert client.images.get(tag).id == image_id
            finally:
                container.remove(v=True)
            assert image_cleanup.remove_unused_images([image_id]) == {
                "removed": [image_id],
                "skipped": [],
                "errors": [],
            }
            with pytest.raises(docker.errors.ImageNotFound):
                client.images.get(image_id)
        finally:
            with suppress(docker.errors.ImageNotFound):
                client.images.remove(tag, noprune=True)


def test_pruning_build_cache_keeps_loaded_image(tmp_path):
    tag = "oduflowtest-cache:" + uuid.uuid4().hex
    (tmp_path / "proof").write_bytes(os.urandom(32768))
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY --link proof /proof\n")
    with closing(get_client()) as client:
        try:
            subprocess.run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--builder",
                    "default",
                    "--load",
                    "-t",
                    tag,
                    str(tmp_path),
                ],
                check=True,
                timeout=120,
                capture_output=True,
            )
            image = client.images.get(tag)
            before = image_cleanup.build_cache_usage()
            assert before["reclaimable_bytes"] > 0, before
            pruned = image_cleanup.prune_build_cache()
            assert "error" not in pruned and pruned["reclaimed_bytes"] > 0, pruned
            assert (
                image_cleanup.build_cache_usage()["reclaimable_bytes"]
                < before["reclaimable_bytes"]
            )
            assert client.images.get(tag).id == image.id
        finally:
            with suppress(docker.errors.ImageNotFound):
                client.images.remove(tag, noprune=True)
