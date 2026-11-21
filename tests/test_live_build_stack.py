"""Real BuildKit and Stack tests for an enrolled, disposable Linux Docker host.

From the synchronized checkout on that host:
    pytest --run-live-docker -m integration tests/test_live_build_stack.py

RUN-step tests use a private docker-container builder with a native snapshotter
so they can execute on nested filesystems without changing the daemon driver.
"""

import json
import socket
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

import docker
from oduflow import image_builds, stack_ops
from oduflow.docker_ops import build_ops, service_ops, volume_ops
from oduflow.docker_ops.client import get_client
from oduflow.errors import ConflictError
from oduflow.settings import ImageRegistrySettings
from oduflow.stack_loader import load_stack

pytestmark = pytest.mark.integration


@pytest.fixture
def build_team(live_environment):
    settings, team = live_environment
    team = replace(
        team,
        image_registry=ImageRegistrySettings(keep_images=0, build_timeout_seconds=150),
    )
    yield settings, team
    # Staging tags are not named with the live_environment prefix. Only
    # untag jobs recorded in this test's private data directory, after services.
    client = get_client()
    for service in service_ops.list_services(settings, team):
        service_ops.delete_service(settings, team, service["name"], save_preset=False)
    for volume in volume_ops.list_volumes(settings, team):
        volume_ops.delete_volume(settings, team, volume["name"])
    for job in image_builds.store.list_jobs(team):
        tag = f"{build_ops.staging_repository(team.team_id)}:{job.build_id}"
        with suppress(docker.errors.ImageNotFound):
            client.images.remove(tag)


@pytest.fixture
def source(build_team, tmp_path):
    """Serve only this tiny repo via Git smart HTTP; no external repo or auth.

    Stack correctly rejects loopback/file URLs. The Linux bridge gateway lets
    Git on the VM fetch its own source without binding the public interface.
    """
    repo = tmp_path / "fixture"
    repo.mkdir()
    payload = uuid.uuid4().hex
    (repo / "payload").write_text(payload + "\n")
    (repo / "Dockerfile").write_text(
        'FROM busybox:1.36.1\nCOPY --link payload /payload\nCMD ["sleep", "3600"]\n'
    )
    (repo / "failure").write_text("FROM busybox:1.36.1\nRUN exit 37\n")
    (repo / "slow").write_text(
        "FROM busybox:1.36.1\nCOPY payload /payload\nRUN cat /payload && sleep 120\n"
    )
    subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@invalid.example",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "build fixture",
        ],
        cwd=repo,
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    class UploadPack(BaseHTTPRequestHandler):
        def reply(self, kind, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-git-upload-pack-" + kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/fixture.git/info/refs?service=git-upload-pack":
                return self.send_error(404)
            refs = subprocess.check_output(
                [
                    "git",
                    "upload-pack",
                    "--stateless-rpc",
                    "--advertise-refs",
                    str(repo),
                ],
                timeout=10,
            )
            self.reply("advertisement", b"001e# service=git-upload-pack\n0000" + refs)

        def do_POST(self):
            if self.path != "/fixture.git/git-upload-pack":
                return self.send_error(404)
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 65536:
                return self.send_error(413)
            result = subprocess.check_output(
                ["git", "upload-pack", "--stateless-rpc", str(repo)],
                input=self.rfile.read(size),
                timeout=10,
            )
            self.reply("result", result)

    gateway = get_client().networks.get("bridge").attrs["IPAM"]["Config"][0]["Gateway"]
    with ThreadingHTTPServer((gateway, 0), UploadPack) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{gateway}:{httpd.server_port}/fixture.git", commit, payload
        finally:
            httpd.shutdown()
            thread.join()


@pytest.fixture
def local_registry(build_team):
    settings, _ = build_team
    registry = get_client().containers.run(
        "registry:2.8.3",
        detach=True,
        name=settings.prefix + "registry",
        ports={"5000/tcp": ("127.0.0.1", None)},
    )
    try:
        registry.reload()
        (binding,) = registry.ports["5000/tcp"]
        assert binding["HostIp"] == "127.0.0.1"
        host = "127.0.0.1:" + binding["HostPort"]
        deadline = time.monotonic() + 20
        while True:
            try:
                with urlopen(f"http://{host}/v2/", timeout=2) as response:
                    assert response.status == 200
                break
            except URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield host
    finally:
        registry.remove(force=True, v=True)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def test_build_run_and_publish_exact_image(build_team, source, local_registry):
    settings, team = build_team
    url, commit, payload = source
    client = get_client()
    job = build_ops.build_git_image(team, url, "main", "Dockerfile", ".", "", {})
    assert job.status == "succeeded", image_builds.store.read_log_tail(
        team, job.build_id, 40
    )
    assert (job.builder, job.commit) == ("buildkit", commit)
    assert (
        job.local_tag == f"{build_ops.staging_repository(team.team_id)}:{job.build_id}"
    )
    info = service_ops.create_service(
        settings, team, "probe", job.local_tag, _free_port()
    )
    container = client.containers.get(info["container_name"])
    assert (
        container.attrs["Image"] == client.images.get(job.local_tag).id == job.image_id
    )
    assert container.exec_run(["cat", "/payload"]) == (0, (payload + "\n").encode())

    cfg = replace(team.image_registry, host=local_registry, repository_prefix="live")
    tags = ["candidate", "verified"]
    repo = f"{local_registry}/live/probe"
    try:
        publication = build_ops.publish(team, cfg, job, "probe", tags)
        results = publication["results"]
        assert [r["tag"] for r in results] == tags
        assert all(not r["error"] for r in results), results
        with urlopen(
            f"http://{local_registry}/v2/live/probe/tags/list", timeout=3
        ) as response:
            assert set(json.load(response)["tags"]) == set(tags)
        for tag in tags:
            with pytest.raises(docker.errors.ImageNotFound):
                client.images.get(f"{repo}:{tag}")
        for tag in tags:
            assert client.images.pull(repo, tag).id == job.image_id
        saved = image_builds.store.load(
            team, "", image_builds.TEAM_OWNER_ID, job.build_id
        )
        assert saved.publications == [publication]
    finally:
        for tag in tags:
            with suppress(docker.errors.ImageNotFound):
                client.images.remove(f"{repo}:{tag}")


@pytest.fixture
def native_builder(build_team, tmp_path, monkeypatch):
    settings, _ = build_team
    name = settings.prefix + "native"
    monkeypatch.setenv("BUILDX_CONFIG", str(tmp_path / "buildx"))
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
        timeout=30,
    )
    try:
        subprocess.run(
            ["docker", "buildx", "inspect", "--bootstrap", name],
            check=True,
            timeout=180,
        )
        monkeypatch.setenv("BUILDX_BUILDER", name)
        yield
    finally:
        subprocess.run(["docker", "buildx", "rm", name], check=True, timeout=60)


@pytest.mark.parametrize(
    "dockerfile,expected", [("failure", "failed"), ("slow", "cancelled")]
)
def test_unsuccessful_build_leaves_no_staging_image(
    build_team, source, dockerfile, expected, monkeypatch, native_builder
):
    settings, team = build_team
    url, commit, payload = source
    job, remote = build_ops.admit_git_build(
        team, team.image_registry, url, "main", dockerfile, ".", "", {}
    )
    store = image_builds.store
    if dockerfile == "slow":
        from tool_helpers import call_tool

        from oduflow import server

        # Supply the test settings, not the VM's server config. The registered
        # cancel tool resolves its real team, job and running builder from here.
        monkeypatch.setattr(server, "_settings", settings)
        # Own the worker thread so fixture teardown cannot race a live build.
        event = store.cancel_event(job.build_id)
        worker = threading.Thread(
            target=build_ops.run_build,
            args=(team, team.image_registry, job, {}, remote),
        )
        worker.start()
        try:
            deadline = time.monotonic() + 90
            while payload not in store.read_log_tail(team, job.build_id, 100):
                assert worker.is_alive() and time.monotonic() < deadline, (
                    store.read_log_tail(team, job.build_id, 100)
                )
                time.sleep(0.1)
            assert "Cancellation requested" in call_tool(
                "cancel_image_build", build_id=job.build_id
            )
            worker.join(timeout=20)
            assert not worker.is_alive(), "BuildKit did not stop after cancellation"
        finally:
            event.set()
            worker.join(timeout=30)
    else:
        build_ops.run_build(team, team.image_registry, job, {}, remote)

    saved = store.load(team, "", image_builds.TEAM_OWNER_ID, job.build_id)
    log = store.read_log_tail(team, job.build_id, 100)
    assert (saved.status, saved.builder, saved.commit) == (
        expected,
        "buildkit",
        commit,
    ), log
    assert ("exit code: 37" if expected == "failed" else "build cancelled") in log
    assert saved.error and not saved.local_tag and not saved.image_id
    assert not Path(store.context_dir(team, job.build_id)).exists()
    with pytest.raises(docker.errors.ImageNotFound):
        get_client().images.get(
            f"{build_ops.staging_repository(team.team_id)}:{job.build_id}"
        )


def test_stack_reuses_build_and_reconciles_lifecycle(build_team, source, tmp_path):
    settings, team = build_team
    url, commit, payload = source
    volume = "data-" + payload[:12]  # Docker volume names do not use settings.prefix.
    path = tmp_path / "stack.yaml"
    volume_only = f"""apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata: {{name: live-build}}
spec:
  volumes:
    {volume}: {{}}
"""
    path.write_text(
        volume_only
        + f"""  services:
    probe:
      build: {{repoUrl: '{url}', ref: main}}
      port: {_free_port()}
      volumes:
        - {{source: {volume}, target: /data}}
"""
    )
    args = settings, team, load_stack(path), str(path)
    created = stack_ops.apply_stack(*args)
    assert [(a.operation, a.resource) for a in created.actions] == [
        ("create", "volumes." + volume),
        ("create", "services.probe"),
    ]
    (job,) = image_builds.store.list_jobs(team)
    assert (job.status, job.commit, job.builder) == ("succeeded", commit, "buildkit")
    client = get_client()
    container = service_ops.find_service_container(client, settings, team, "probe")
    assert container.attrs["Image"] == job.image_id
    assert container.attrs["Config"]["Image"] == job.local_tag
    assert container.labels["oduflow.stack-resource"] == "services.probe"
    assert container.exec_run(["cp", "/payload", "/data/proof"]).exit_code == 0
    assert stack_ops.apply_stack(*args).actions == ()
    assert [j.build_id for j in image_builds.store.list_jobs(team)] == [job.build_id]
    assert stack_ops.stack_status(*args)["inSync"] is True
    assert client.containers.get(container.name).id == container.id

    container.stop(timeout=1)
    drift = stack_ops.build_plan(*args)
    assert [(a.operation, a.resource) for a in drift.actions] == [
        ("start", "services.probe")
    ]
    assert stack_ops.apply_stack(*args).actions == drift.actions
    container.reload()
    assert container.status == "running"
    assert container.exec_run(["cat", "/data/proof"]) == (0, (payload + "\n").encode())

    # Removing a declaration is not a deletion API.
    path.write_text(volume_only)
    assert (
        stack_ops.apply_stack(settings, team, load_stack(path), str(path)).actions == ()
    )
    assert client.containers.get(container.name).id == container.id
    with pytest.raises(ConflictError, match="in use"):
        volume_ops.delete_volume(settings, team, volume)
    assert not service_ops.delete_service(settings, team, "probe", save_preset=False)[
        "preset_kept"
    ]
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(container.id)
    deleted = volume_ops.delete_volume(settings, team, volume)
    with pytest.raises(docker.errors.NotFound):
        client.volumes.get(deleted["docker_name"])
