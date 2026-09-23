"""Container image builds: validation, job store, settings, archive safety."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from oduflow import image_builds
from oduflow.errors import BusyError, NotFoundError
from oduflow.image_builds import BuildJobStore
from oduflow.settings import ImageRegistrySettings, Settings, TeamSettings

_OWNER = "a" * 64

# -- input validation --


def test_repository_accepts_valid_names():
    assert image_builds.validate_repository("app") == "app"
    assert image_builds.validate_repository("tools/builder") == "tools/builder"
    assert image_builds.validate_repository(" my-app.v2 ") == "my-app.v2"


@pytest.mark.parametrize(
    "bad", ["", "App", "a//b", "a b", "-app", "app-", "../x", "a:b", "a\x00b"]
)
def test_repository_rejects_invalid_names(bad):
    with pytest.raises(ValueError):
        image_builds.validate_repository(bad)


def test_tags_parse_including_latest_and_semver():
    assert image_builds.validate_tags("1.4.0, latest,1.4.0") == ["1.4.0", "latest"]
    assert image_builds.validate_tags("feature_x-1") == ["feature_x-1"]


@pytest.mark.parametrize("bad", ["", "  ", ".hidden", "-x", "a" * 129, "a b", "a:b"])
def test_tags_reject_invalid(bad):
    with pytest.raises(ValueError):
        image_builds.validate_tags(bad)


def test_relative_path_normalizes_and_contains():
    assert image_builds.validate_relative_path("", "context") == "."
    assert image_builds.validate_relative_path("./sub/dir/", "context") == "sub/dir"
    assert image_builds.validate_relative_path("a/./b", "context") == "a/b"


@pytest.mark.parametrize("bad", ["/abs", "../up", "a/../../b", "a\x00b", "\\srv"])
def test_relative_path_rejects_escapes(bad):
    with pytest.raises(ValueError):
        image_builds.validate_relative_path(bad, "context")


def test_build_id_validation_blocks_traversal():
    good = image_builds.new_build_id()
    assert image_builds.validate_build_id(good) == good
    for bad in ["../../etc", "bld-XYZ", "bld-", "", "bld-abc/def"]:
        with pytest.raises(ValueError):
            image_builds.validate_build_id(bad)


# -- job store --


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def test_job_roundtrip_and_active_authority(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    job = store.create(
        team, "feature-x", "feature-x", "Dockerfile", ".", owner_id=_OWNER
    )
    assert job.status == image_builds.STATUS_QUEUED
    assert store.cancel_event(job.build_id) is not None
    # While active, load returns the live object (not a disk copy).
    assert store.load(team, "feature-x", _OWNER, job.build_id) is job
    # Ownership: the same ID under another env reports plain not-found.
    with pytest.raises(NotFoundError):
        store.load(team, "other-env", "b" * 64, job.build_id)


def test_job_identity_survives_rename_but_not_recreate(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    job = store.create(team, "old-name", "main", "Dockerfile", ".", owner_id=_OWNER)
    store.finish(team, job, image_builds.STATUS_SUCCEEDED)

    assert store.load(team, "new-name", _OWNER, job.build_id).build_id == job.build_id
    with pytest.raises(NotFoundError):
        store.load(team, "old-name", "b" * 64, job.build_id)


def test_active_build_limit_is_atomic_per_team(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    first = store.create(
        team, "one", "main", "Dockerfile", ".", owner_id=_OWNER, max_concurrent_builds=2
    )
    store.create(
        team,
        "two",
        "main",
        "Dockerfile",
        ".",
        owner_id="b" * 64,
        max_concurrent_builds=2,
    )

    with pytest.raises(BusyError, match="configured maximum is 2"):
        store.create(
            team,
            "three",
            "main",
            "Dockerfile",
            ".",
            owner_id="c" * 64,
            max_concurrent_builds=2,
        )
    store.finish(team, first, image_builds.STATUS_FAILED)
    store.create(
        team,
        "three",
        "main",
        "Dockerfile",
        ".",
        owner_id="c" * 64,
        max_concurrent_builds=2,
    )


def test_finish_persists_terminal_state_and_drops_context(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    job = store.create(team, "env", "env", "Dockerfile", ".", owner_id=_OWNER)
    ctx = store.context_dir(team, job.build_id)
    os.makedirs(ctx, exist_ok=True)
    store.finish(team, job, image_builds.STATUS_SUCCEEDED)
    assert not os.path.exists(ctx)
    assert store.cancel_event(job.build_id) is None
    loaded = store.load(team, "env", _OWNER, job.build_id)
    assert loaded.status == image_builds.STATUS_SUCCEEDED
    assert loaded.finished_at


def test_stale_running_job_becomes_interrupted_on_load(tmp_path):
    # Simulate a job left "building" by a dead process: on disk, not active.
    writer = BuildJobStore()
    team = _team(tmp_path)
    job = writer.create(team, "env", "env", "Dockerfile", ".", owner_id=_OWNER)
    job.status = image_builds.STATUS_BUILDING
    writer.save(team, job)
    os.makedirs(writer.context_dir(team, job.build_id), exist_ok=True)

    reader = BuildJobStore()  # fresh process: empty in-memory registry
    loaded = reader.load(team, "env", _OWNER, job.build_id)
    assert loaded.status == image_builds.STATUS_INTERRUPTED
    assert "restarted" in loaded.error
    assert not os.path.exists(reader.context_dir(team, job.build_id))
    # The interruption is persisted, not just reported.
    on_disk = json.loads(open(reader.job_json_path(team, job.build_id)).read())
    assert on_disk["status"] == image_builds.STATUS_INTERRUPTED


def test_terminal_job_survives_reload_unchanged(tmp_path):
    writer = BuildJobStore()
    team = _team(tmp_path)
    job = writer.create(team, "env", "env", "Dockerfile", ".", owner_id=_OWNER)
    job.local_tag = "oduflow-build/team-1:" + job.build_id
    writer.finish(team, job, image_builds.STATUS_SUCCEEDED)

    reader = BuildJobStore()
    loaded = reader.load(team, "env", _OWNER, job.build_id)
    assert loaded.status == image_builds.STATUS_SUCCEEDED
    assert loaded.local_tag == job.local_tag


def test_unknown_build_reports_not_found(tmp_path):
    store = BuildJobStore()
    with pytest.raises(NotFoundError):
        store.load(_team(tmp_path), "env", _OWNER, image_builds.new_build_id())


def test_publication_history_is_persisted(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    job = store.create(team, "env", "env", "Dockerfile", ".", owner_id=_OWNER)
    store.finish(team, job, image_builds.STATUS_SUCCEEDED)
    store.append_publication(
        team,
        job,
        {
            "at": image_builds.now_iso(),
            "host": "docker.io",
            "repository": "acme/app",
            "results": [{"tag": "latest", "digest": "sha256:abc", "error": ""}],
        },
    )
    loaded = BuildJobStore().load(team, "env", _OWNER, job.build_id)
    assert loaded.publications[0]["repository"] == "acme/app"
    assert loaded.publications[0]["results"][0]["tag"] == "latest"


def test_log_tail_reads_last_lines(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    job = store.create(team, "env", "env", "Dockerfile", ".", owner_id=_OWNER)
    with open(store.log_path(team, job.build_id), "w") as f:
        f.write("\n".join(f"line {i}" for i in range(50)) + "\n")
    tail = store.read_log_tail(team, job.build_id, 3)
    assert tail == "line 47\nline 48\nline 49\n"


# -- settings --


def _load_settings(tmp_path, registry_toml: str) -> Settings:
    toml = textwrap.dedent(
        f"""
        [storage]
        data_dir = "{tmp_path}/data"

        [team.1]
        auth_token = "tok"
        {registry_toml}
        """
    )
    path = tmp_path / "oduflow.toml"
    path.write_text(toml)
    return Settings.from_toml(str(path))


def test_settings_without_registry_section_disable_builds(tmp_path):
    settings = _load_settings(tmp_path, "")
    assert settings.get_team("1").image_registry is None


def test_settings_parse_registry_section(tmp_path):
    settings = _load_settings(
        tmp_path,
        textwrap.dedent(
            """
            [team.1.image_registry]
            repository_prefix = "acme/"
            username = "acme-ci"
            token = "registry-secret"
            build_timeout_seconds = 600
            """
        ),
    )
    registry = settings.get_team("1").image_registry
    assert registry is not None
    assert registry.repository_prefix == "acme"  # trailing slash trimmed
    assert registry.host == "docker.io"
    assert registry.username == "acme-ci"
    assert registry.token == "registry-secret"
    assert "registry-secret" not in repr(registry)
    assert registry.build_timeout_seconds == 600
    assert registry.max_context_mb == 512
    assert registry.max_log_mb == 16
    assert registry.max_concurrent_builds == 2


@pytest.mark.parametrize(
    "body",
    [
        # prefix is required
        '[team.1.image_registry]\nhost = "docker.io"',
        # invalid prefix grammar
        '[team.1.image_registry]\nrepository_prefix = "Acme"',
        # host must be a plain hostname
        '[team.1.image_registry]\nrepository_prefix = "acme"\nhost = "https://x.io"',
        # username and token come together
        '[team.1.image_registry]\nrepository_prefix = "acme"\nusername = "ci"',
        '[team.1.image_registry]\nrepository_prefix = "acme"\ntoken = "secret"',
        # limits must be positive
        '[team.1.image_registry]\nrepository_prefix = "acme"\nmax_context_mb = 0',
        '[team.1.image_registry]\nrepository_prefix = "acme"\nmax_log_mb = 0',
        '[team.1.image_registry]\nrepository_prefix = "acme"\nmax_concurrent_builds = 0',
    ],
)
def test_settings_reject_bad_registry_sections(tmp_path, body):
    with pytest.raises(ValueError):
        _load_settings(tmp_path, body)


def test_settings_reject_legacy_registry_token_env(tmp_path):
    body = textwrap.dedent(
        """
        [team.1.image_registry]
        repository_prefix = "acme"
        username = "ci"
        token_env = "ODUFLOW_REGISTRY_TOKEN"
        """
    )

    with pytest.raises(ValueError, match="username and token must be set together"):
        _load_settings(tmp_path, body)


# -- archive extraction safety --


def _tar_with(members: list[tarfile.TarInfo], payloads: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for member in members:
            data = payloads.get(member.name)
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def test_extract_member_writes_regular_files(tmp_path):
    from oduflow.docker_ops import build_ops

    info = tarfile.TarInfo("dir/ok.txt")
    info.size = 2
    raw = _tar_with([info], {"dir/ok.txt": b"hi"})
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for member in tar:
            build_ops._extract_member(tar, member, str(tmp_path))
    assert (tmp_path / "dir" / "ok.txt").read_text() == "hi"


def test_extract_member_rejects_escaping_symlink(tmp_path):
    from oduflow.docker_ops import build_ops

    link = tarfile.TarInfo("evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    raw = _tar_with([link], {})
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        with pytest.raises(Exception):
            for member in tar:
                build_ops._extract_member(tar, member, str(tmp_path))
    assert not (tmp_path / "evil").exists()


def test_environment_owner_digest_is_stable_and_secret_free(tmp_path):
    from oduflow.docker_ops import build_ops

    settings = Settings(teams={"1": _team(tmp_path)})
    team = settings.get_team("1")
    container = SimpleNamespace(labels={"oduflow.mcp_token": "bearer-secret"})
    with patch.object(build_ops, "_environment_container", return_value=container):
        owner_id = build_ops.environment_owner_id(settings, team, "renamed-env")

    assert owner_id == hashlib.sha256(b"bearer-secret").hexdigest()
    assert "bearer-secret" not in owner_id


# -- Docker build/publish lifecycle (Docker SDK fully mocked) --


class _ResultConnection:
    def __init__(self):
        self.value = None
        self.closed = False

    def send(self, value):
        self.value = value

    def close(self):
        self.closed = True


def test_build_worker_records_exact_image_and_caps_log(tmp_path):
    from oduflow.docker_ops import build_ops

    client = MagicMock()
    client.api.build.return_value = [
        {"stream": "x" * 200},
        {"stream": "never written after truncation"},
    ]
    client.images.get.return_value = SimpleNamespace(id="sha256:built")
    result = _ResultConnection()
    log_path = str(tmp_path / "build.log")

    with patch.object(build_ops, "get_client", return_value=client):
        build_ops._docker_build_worker(
            str(tmp_path),
            "Dockerfile",
            "oduflow-build/team-1:bld-abcdef123456",
            "",
            {},
            log_path,
            100,
            result,
        )

    assert result.value == {"ok": True, "error": "", "image_id": "sha256:built"}
    assert result.closed
    raw = (tmp_path / "build.log").read_bytes()
    assert len(raw) == 100
    assert b"build log truncated" in raw


class _NeverEndingProcess:
    exitcode = -15

    def __init__(self):
        self.alive = False
        self.terminated = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        return None

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.alive = False


class _PipeEnd:
    def close(self):
        return None

    def poll(self, timeout=None):
        return False


def test_silent_build_is_terminated_at_wall_clock_timeout(tmp_path):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    job = local_store.create(team, "env", "main", "Dockerfile", ".", owner_id=_OWNER)
    os.makedirs(local_store.context_dir(team, job.build_id), exist_ok=True)
    process = _NeverEndingProcess()
    fake_context = MagicMock()
    fake_context.Pipe.return_value = (_PipeEnd(), _PipeEnd())
    fake_context.Process.return_value = process
    registry = ImageRegistrySettings(repository_prefix="acme", build_timeout_seconds=1)

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(
            build_ops.multiprocessing, "get_context", return_value=fake_context
        ),
        patch.object(build_ops.time, "monotonic", side_effect=[0.0, 2.0]),
    ):
        build_ops._run_build(team, registry, job, {})

    assert process.terminated
    assert job.status == image_builds.STATUS_FAILED
    assert "timed out after 1s" in job.error


def test_silent_build_can_be_cancelled(tmp_path):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    job = local_store.create(team, "env", "main", "Dockerfile", ".", owner_id=_OWNER)
    event = local_store.cancel_event(job.build_id)
    assert event is not None
    event.set()
    os.makedirs(local_store.context_dir(team, job.build_id), exist_ok=True)
    process = _NeverEndingProcess()
    fake_context = MagicMock()
    fake_context.Pipe.return_value = (_PipeEnd(), _PipeEnd())
    fake_context.Process.return_value = process
    registry = ImageRegistrySettings(repository_prefix="acme")

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(
            build_ops.multiprocessing, "get_context", return_value=fake_context
        ),
    ):
        build_ops._run_build(team, registry, job, {})

    assert process.terminated
    assert job.status == image_builds.STATUS_CANCELLED
    assert job.error == "Cancelled by request."


def test_publish_removes_temporary_registry_tag(tmp_path):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    job = local_store.create(team, "env", "main", "Dockerfile", ".", owner_id=_OWNER)
    job.local_tag = f"oduflow-build/team-1:{job.build_id}"
    job.image_id = "sha256:built"
    local_store.finish(team, job, image_builds.STATUS_SUCCEEDED)
    client = MagicMock()
    client.api.tag.return_value = True
    client.api.push.return_value = [{"aux": {"Digest": "sha256:published"}}]
    client.images.get.return_value = SimpleNamespace(tags=[job.local_tag])
    registry = ImageRegistrySettings(repository_prefix="acme")

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "get_client", return_value=client),
    ):
        publication = build_ops.publish(team, registry, job, "app", ["1.0.0"])

    assert publication["results"][0]["digest"] == "sha256:published"
    client.images.remove.assert_called_once_with("acme/app:1.0.0")


def test_publish_cleans_tag_after_registry_error_and_uses_scoped_auth(tmp_path):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    job = local_store.create(team, "env", "main", "Dockerfile", ".", owner_id=_OWNER)
    job.local_tag = f"oduflow-build/team-1:{job.build_id}"
    job.image_id = "sha256:built"
    local_store.finish(team, job, image_builds.STATUS_SUCCEEDED)
    client = MagicMock()
    client.api.tag.return_value = True
    client.api.push.return_value = [{"error": "registry rejected push"}]
    client.images.get.return_value = SimpleNamespace(tags=[job.local_tag])
    registry = ImageRegistrySettings(
        repository_prefix="acme", username="ci", token="secret"
    )

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "get_client", return_value=client),
    ):
        publication = build_ops.publish(team, registry, job, "app", ["latest"])

    assert publication["results"][0]["error"] == "registry rejected push"
    assert client.api.push.call_args.kwargs["auth_config"] == {
        "username": "ci",
        "password": "secret",
    }
    client.images.remove.assert_called_once_with("acme/app:latest")


def test_retention_uses_job_age_and_removes_unused_images(tmp_path):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    jobs = []
    for index in range(3):
        job = local_store.create(
            team, f"env-{index}", "main", "Dockerfile", ".", owner_id=str(index) * 64
        )
        job.local_tag = f"oduflow-build/team-1:{job.build_id}"
        job.image_id = f"sha256:{index}"
        local_store.finish(team, job, image_builds.STATUS_SUCCEEDED)
        job.finished_at = f"2026-09-01T00:00:0{index}+00:00"
        local_store.save(team, job)
        jobs.append(job)

    client = MagicMock()
    client.images.get.side_effect = lambda image_id: SimpleNamespace(
        id=image_id, tags=[]
    )
    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "get_client", return_value=client),
    ):
        build_ops._prune_staging_images(team, keep=1)

    removed = [call.args[0] for call in client.images.remove.call_args_list]
    assert jobs[2].local_tag not in removed
    assert jobs[1].local_tag in removed
    assert jobs[0].local_tag in removed
    assert jobs[1].image_id in removed
    assert jobs[0].image_id in removed


@pytest.mark.parametrize("status", ["running", "exited"])
@pytest.mark.parametrize("uses_old_tag", [True, False])
def test_retention_preserves_container_tag_after_cached_rebuild(
    tmp_path, status, uses_old_tag
):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    jobs = []
    for index in range(2):
        job = local_store.create(
            team, f"env-{index}", "main", "Dockerfile", ".", owner_id=_OWNER
        )
        job.local_tag = f"oduflow-build/team-1:{job.build_id}"
        job.image_id = "sha256:cached"
        local_store.finish(team, job, image_builds.STATUS_SUCCEEDED)
        job.finished_at = f"2026-09-01T00:00:0{index}+00:00"
        local_store.save(team, job)
        jobs.append(job)

    tags = [job.local_tag for job in jobs]
    client = MagicMock()
    client.images.get.return_value = SimpleNamespace(tags=tags)
    client.images.remove.side_effect = tags.remove
    container = SimpleNamespace(
        status=status,
        attrs={"Config": {"Image": jobs[0 if uses_old_tag else 1].local_tag}},
    )
    client.containers.list.side_effect = lambda all: (
        [container] if all or status == "running" else []
    )
    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "get_client", return_value=client),
    ):
        build_ops._prune_staging_images(team, keep=1)

    if uses_old_tag:
        client.images.remove.assert_not_called()
        assert jobs[0].local_tag in tags
    else:
        # Sharing the image ID does not pin an unrelated, unused build tag.
        client.images.remove.assert_called_once_with(jobs[0].local_tag)
        assert jobs[0].local_tag not in tags


# -- version pinning between tool listing and docs is covered by
#    test_documentation_sync; scoped exposure is checked here --


def test_image_tools_are_on_the_scoped_allowlist():
    from oduflow.scoped_access import SCOPED_ALLOWLIST

    for tool in (
        "start_image_build",
        "get_image_build",
        "publish_image_build",
        "cancel_image_build",
    ):
        assert tool in SCOPED_ALLOWLIST
