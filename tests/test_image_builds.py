"""Container image builds: validation, job store, settings, archive safety."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import textwrap
import time
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
    writer._release(job.build_id)  # the kernel drops the lock with the process

    reader = BuildJobStore()  # fresh process: empty in-memory registry
    loaded = reader.load(team, "env", _OWNER, job.build_id)
    assert loaded.status == image_builds.STATUS_INTERRUPTED
    assert "restarted" in loaded.error
    assert not os.path.exists(reader.context_dir(team, job.build_id))
    # The interruption is persisted, not just reported.
    on_disk = json.loads(open(reader.job_json_path(team, job.build_id)).read())
    assert on_disk["status"] == image_builds.STATUS_INTERRUPTED


def test_job_running_in_another_process_is_left_alone(tmp_path):
    # `oduflow stack apply` builds in the CLI process while the server reads
    # the same job directory: a live owner must not be declared interrupted.
    cli = BuildJobStore()
    team = _team(tmp_path)
    job = cli.create(team, "", "main", "Dockerfile", ".", owner_id=_OWNER)
    job.status = image_builds.STATUS_BUILDING
    cli.save(team, job)
    context = cli.context_dir(team, job.build_id)
    os.makedirs(context, exist_ok=True)

    server = BuildJobStore()
    loaded = server.load(team, "", _OWNER, job.build_id)

    assert loaded.status == image_builds.STATUS_BUILDING
    assert os.path.isdir(context)
    on_disk = json.loads(open(server.job_json_path(team, job.build_id)).read())
    assert on_disk["status"] == image_builds.STATUS_BUILDING

    cli.finish(team, job, image_builds.STATUS_SUCCEEDED)
    assert (
        server.load(team, "", _OWNER, job.build_id).status
        == image_builds.STATUS_SUCCEEDED
    )


def test_job_without_owner_lock_is_treated_as_abandoned(tmp_path):
    # Jobs written before owner locks existed have no lock file to probe.
    writer = BuildJobStore()
    team = _team(tmp_path)
    job = writer.create(team, "env", "env", "Dockerfile", ".", owner_id=_OWNER)
    job.status = image_builds.STATUS_BUILDING
    writer.save(team, job)
    writer._release(job.build_id)
    os.remove(writer._owner_lock_path(team, job.build_id))

    loaded = BuildJobStore().load(team, "env", _OWNER, job.build_id)
    assert loaded.status == image_builds.STATUS_INTERRUPTED


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


# -- cleanup of stale job directories --


def _finished(store, team, status, tagged=True):
    job = store.create(
        team,
        "",
        "main",
        "Dockerfile",
        ".",
        owner_id=image_builds.TEAM_OWNER_ID,
        max_concurrent_builds=99,
    )
    if tagged:
        job.local_tag = f"oduflow-build/team-1:{job.build_id}"
    store.finish(team, job, status)
    return job


def _no_image(_tag):
    return False


def test_cleanup_selects_finished_jobs_nothing_can_use(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    failed = _finished(store, team, image_builds.STATUS_FAILED)
    cancelled = _finished(store, team, image_builds.STATUS_CANCELLED)
    interrupted = _finished(store, team, image_builds.STATUS_INTERRUPTED)
    lost = _finished(store, team, image_builds.STATUS_SUCCEEDED)
    untagged = _finished(store, team, image_builds.STATUS_SUCCEEDED, tagged=False)
    kept = _finished(store, team, image_builds.STATUS_SUCCEEDED)
    present = {kept.local_tag}
    expected = sorted(
        job.build_id for job in (failed, cancelled, interrupted, lost, untagged)
    )

    stale = store.stale_job_ids(team, present.__contains__)
    assert stale == expected
    # Listing is read-only.
    assert all(os.path.isdir(store.job_dir(team, build_id)) for build_id in stale)

    removed, errors = store.remove_stale_jobs(
        team, [*stale, kept.build_id], present.__contains__
    )
    assert (removed, errors) == (expected, [])
    assert not any(os.path.exists(store.job_dir(team, i)) for i in expected)
    # A Stack can still reuse it and publish_image_build still finds it.
    assert (
        store.load(team, "", image_builds.TEAM_OWNER_ID, kept.build_id).local_tag
        == kept.local_tag
    )


def test_cleanup_keeps_builds_that_are_still_running(tmp_path):
    owner = BuildJobStore()
    team = _team(tmp_path)
    queued = owner.create(team, "", "main", "Dockerfile", ".", owner_id=_OWNER)
    building = owner.create(team, "", "main", "Dockerfile", ".", owner_id=_OWNER)
    building.status = image_builds.STATUS_BUILDING
    owner.save(team, building)
    ids = [queued.build_id, building.build_id]

    # The owning process, and another one beside it (`oduflow cleanup`).
    for view in (owner, BuildJobStore()):
        assert view.stale_job_ids(team, _no_image) == []
        assert view.remove_stale_jobs(team, ids, _no_image) == ([], [])
    assert all(os.path.isdir(owner.job_dir(team, build_id)) for build_id in ids)


def test_cleanup_keeps_terminal_job_while_its_owner_lock_is_held(tmp_path):
    # Inside finish(): the terminal state is on disk, the owner is still
    # dropping the context and has not released the lock yet.
    owner = BuildJobStore()
    team = _team(tmp_path)
    job = owner.create(team, "", "main", "Dockerfile", ".", owner_id=_OWNER)
    job.status = image_builds.STATUS_FAILED
    owner.save(team, job)

    cleaner = BuildJobStore()
    assert cleaner.stale_job_ids(team, _no_image) == []
    assert cleaner.remove_stale_jobs(team, [job.build_id], _no_image) == ([], [])
    assert os.path.isdir(owner.job_dir(team, job.build_id))

    owner._release(job.build_id)
    assert cleaner.stale_job_ids(team, _no_image) == [job.build_id]
    # Without a usable flock the owner still knows its own active jobs.
    assert owner.stale_job_ids(team, _no_image) == []


def _abandoned(team):
    """A job left "building" on disk by a process that died."""
    writer = BuildJobStore()
    job = writer.create(team, "env", "env", "Dockerfile", ".", owner_id=_OWNER)
    job.status = image_builds.STATUS_BUILDING
    writer.save(team, job)
    os.makedirs(writer.context_dir(team, job.build_id))
    writer._release(job.build_id)  # the kernel drops the lock with the process
    return job


def _status_on_disk(store, team, build_id):
    with open(store.job_json_path(team, build_id)) as f:
        return json.load(f)["status"]


def test_cleanup_scan_lists_abandoned_job_without_touching_it(tmp_path):
    team = _team(tmp_path)
    job = _abandoned(team)
    cleaner = BuildJobStore()

    assert cleaner.stale_job_ids(team, _no_image) == [job.build_id]
    # The preview is read-only: declaring the interruption is Confirm's job.
    assert _status_on_disk(cleaner, team, job.build_id) == image_builds.STATUS_BUILDING
    assert os.path.isdir(cleaner.context_dir(team, job.build_id))


def test_cleanup_interrupts_and_removes_abandoned_job(tmp_path):
    team = _team(tmp_path)
    job = _abandoned(team)
    cleaner = BuildJobStore()
    seen: list[str] = []
    real_rmtree = image_builds.shutil.rmtree

    def rmtree(path, **kwargs):
        if path == cleaner.job_dir(team, job.build_id):
            # Ended the way load() ends it, before the directory goes.
            seen.append(_status_on_disk(cleaner, team, job.build_id))
            seen.append(str(os.path.exists(cleaner.context_dir(team, job.build_id))))
        real_rmtree(path, **kwargs)

    with patch.object(image_builds.shutil, "rmtree", side_effect=rmtree):
        result = cleaner.remove_stale_jobs(team, [job.build_id], _no_image)
    assert result == ([job.build_id], [])
    assert seen == [image_builds.STATUS_INTERRUPTED, "False"]
    assert not os.path.exists(cleaner.job_dir(team, job.build_id))


def test_cleanup_never_touches_running_job_whose_owner_lock_is_held(tmp_path):
    # Listed as abandoned in the preview, but an owner holds the lock by the
    # time of Confirm: neither rewritten nor removed.
    team = _team(tmp_path)
    job = _abandoned(team)
    cleaner = BuildJobStore()
    assert cleaner.stale_job_ids(team, _no_image) == [job.build_id]

    owner = BuildJobStore()
    owner._claim(team, job.build_id)
    assert cleaner.stale_job_ids(team, _no_image) == []
    assert cleaner.remove_stale_jobs(team, [job.build_id], _no_image) == ([], [])
    assert _status_on_disk(cleaner, team, job.build_id) == image_builds.STATUS_BUILDING
    assert os.path.isdir(cleaner.context_dir(team, job.build_id))
    owner._release(job.build_id)


def test_cleanup_rechecks_the_owner_lock_before_interrupting(tmp_path):
    # The owner takes the lock after the job was judged abandoned, right
    # before its state would be rewritten.
    team = _team(tmp_path)
    job = _abandoned(team)
    cleaner = BuildJobStore()
    owner = BuildJobStore()
    judge = cleaner._stale_job

    def judge_then_claim(*args):
        found = judge(*args)
        if found is not None:
            owner._claim(team, job.build_id)
        return found

    with patch.object(cleaner, "_stale_job", side_effect=judge_then_claim):
        result = cleaner.remove_stale_jobs(team, [job.build_id], _no_image)
    assert result == ([], [])
    assert _status_on_disk(cleaner, team, job.build_id) == image_builds.STATUS_BUILDING
    assert os.path.isdir(cleaner.context_dir(team, job.build_id))
    owner._release(job.build_id)


def test_cleanup_keeps_abandoned_looking_job_its_owner_just_finished(tmp_path):
    # Read as "building", but the owner finished it between that read and the
    # lock probe: the finished state decides, and its image is present.
    team = _team(tmp_path)
    job = _abandoned(team)
    cleaner = BuildJobStore()
    stale = image_builds.BuildJob.from_dict(job.to_dict())
    job.status = image_builds.STATUS_SUCCEEDED
    job.local_tag = f"oduflow-build/team-1:{job.build_id}"
    cleaner.save(team, job)

    read = cleaner._read
    outdated = [stale]

    def first_read_is_outdated(*args):
        return outdated.pop() if outdated else read(*args)

    with patch.object(cleaner, "_read", side_effect=first_read_is_outdated):
        result = cleaner.remove_stale_jobs(team, [job.build_id], lambda _tag: True)
    assert result == ([], [])
    assert _status_on_disk(cleaner, team, job.build_id) == image_builds.STATUS_SUCCEEDED


def test_cleanup_rechecks_each_job_right_before_removing_it(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    gone = _finished(store, team, image_builds.STATUS_FAILED)
    reclaimed = _finished(store, team, image_builds.STATUS_FAILED)
    lost = _finished(store, team, image_builds.STATUS_SUCCEEDED)
    reviewed = store.stale_job_ids(team, _no_image)
    assert len(reviewed) == 3

    # Between the preview and Confirm: an owner takes one job's lock, and the
    # missing staging image of another is back.
    owner = BuildJobStore()
    owner._claim(team, reclaimed.build_id)
    checked: list[str] = []

    def image_exists(tag):
        checked.append(tag)
        return True

    removed, errors = store.remove_stale_jobs(
        team, [*reviewed, "../../etc", "bld-000000000000"], image_exists
    )
    assert (removed, errors) == ([gone.build_id], [])
    assert checked == [lost.local_tag]
    for job in (reclaimed, lost):
        assert os.path.isdir(store.job_dir(team, job.build_id))
    owner._release(reclaimed.build_id)


def test_cleanup_reports_failed_removal_and_continues(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    stuck = _finished(store, team, image_builds.STATUS_FAILED)
    unknown = _finished(store, team, image_builds.STATUS_SUCCEEDED)
    fine = _finished(store, team, image_builds.STATUS_CANCELLED)
    real_rmtree = image_builds.shutil.rmtree

    def rmtree(path, **kwargs):
        if not path.endswith(stuck.build_id):
            real_rmtree(path, **kwargs)

    def image_exists(_tag):
        raise RuntimeError("daemon unreachable")

    with patch.object(image_builds.shutil, "rmtree", side_effect=rmtree):
        removed, errors = store.remove_stale_jobs(
            team, [stuck.build_id, unknown.build_id, fine.build_id], image_exists
        )
    assert removed == [fine.build_id]
    assert errors == [stuck.build_id, unknown.build_id]
    # A job whose image could not be checked is never deleted on a guess.
    assert os.path.isdir(store.job_dir(team, unknown.build_id))


def test_cleanup_ignores_other_teams_and_unreadable_jobs(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    foreign = _finished(store, team, image_builds.STATUS_FAILED)
    foreign.team_id = "2"
    store.save(team, foreign)
    broken = _finished(store, team, image_builds.STATUS_FAILED)
    with open(store.job_json_path(team, broken.build_id), "w") as f:
        f.write("{not json")
    os.makedirs(os.path.join(store.jobs_dir(team), "not-a-build"))

    assert store.stale_job_ids(team, _no_image) == []
    assert BuildJobStore().stale_job_ids(_team(tmp_path / "empty"), _no_image) == []


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
        # credentials publish somewhere: they need a prefix
        '[team.1.image_registry]\nusername = "ci"\ntoken = "secret"',
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


def test_settings_section_without_prefix_only_tunes_build_limits(tmp_path):
    settings = _load_settings(
        tmp_path, "[team.1.image_registry]\nbuild_timeout_seconds = 3600"
    )
    registry = settings.get_team("1").image_registry
    assert registry is not None
    assert registry.repository_prefix == ""
    assert registry.build_timeout_seconds == 3600


def test_settings_can_switch_builds_off_for_a_team(tmp_path):
    from oduflow.docker_ops import build_ops
    from oduflow.errors import PrerequisiteNotMetError

    team = _load_settings(
        tmp_path, "[team.1.image_registry]\nbuilds_enabled = false"
    ).get_team("1")
    assert team.image_registry is not None
    assert team.image_registry.builds_enabled is False
    with pytest.raises(PrerequisiteNotMetError, match="builds are disabled"):
        build_ops.build_settings(team)

    # On by default, with or without the section.
    assert build_ops.build_settings(_team(tmp_path)).builds_enabled is True
    with pytest.raises(ValueError, match="builds_enabled must be true or false"):
        _load_settings(tmp_path, '[team.1.image_registry]\nbuilds_enabled = "no"')


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
        patch.object(build_ops, "_buildx_available", return_value=False),
        patch.object(build_ops.time, "monotonic", side_effect=[0.0, 2.0]),
    ):
        build_ops.run_build(team, registry, job, {})

    assert job.builder == "legacy"

    assert process.terminated
    assert job.status == image_builds.STATUS_FAILED
    assert "timed out after 1s" in job.error


def test_silent_build_can_be_cancelled(tmp_path):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    job = local_store.create(team, "env", "main", "Dockerfile", ".", owner_id=_OWNER)
    local_store.cancel_path(team, job.build_id).touch()
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
        patch.object(build_ops, "_buildx_available", return_value=False),
    ):
        build_ops.run_build(team, registry, job, {})

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


def test_retention_spares_tags_an_apply_has_not_started_yet(tmp_path):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    jobs = []
    for index in range(3):
        job = local_store.create(
            team, "", "main", "Dockerfile", ".", owner_id=image_builds.TEAM_OWNER_ID
        )
        job.local_tag = f"oduflow-build/team-1:{job.build_id}"
        job.image_id = f"sha256:{index}"
        local_store.finish(team, job, image_builds.STATUS_SUCCEEDED)
        job.finished_at = f"2026-09-01T00:00:0{index}+00:00"
        local_store.save(team, job)
        jobs.append(job)

    client = MagicMock()
    client.containers.list.return_value = []  # no service runs any of them yet
    client.images.get.side_effect = lambda image_id: SimpleNamespace(
        id=image_id, tags=[]
    )
    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "get_client", return_value=client),
    ):
        build_ops._prune_staging_images(team, keep=1, keep_tags={jobs[0].local_tag})

    removed = [call.args[0] for call in client.images.remove.call_args_list]
    assert jobs[0].local_tag not in removed
    assert jobs[0].image_id not in removed
    assert jobs[1].local_tag in removed


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


# -- repository builds: no environment, source named by URL --


def test_git_ref_validation():
    for good in ("main", "release/1.2", "v1.0.0", "a" * 40):
        assert image_builds.validate_git_ref(f" {good} ") == good
    assert image_builds.validate_git_ref("") == ""
    for bad in ("-main", "a b", "a..b", "main:other", "x*", "a//b", "tip.lock", "x/"):
        with pytest.raises(ValueError):
            image_builds.validate_git_ref(bad)


def test_build_key_follows_every_input():
    base = ("https://git.example/app.git", "c" * 40, "Dockerfile", ".", "", {"A": "1"})
    key = image_builds.build_key(*base)
    assert key == image_builds.build_key(*base)
    assert "1" != key and len(key) == 64
    for index, changed in enumerate(
        ("https://git.example/other.git", "d" * 40, "Other", "sub", "prod", {"A": "2"})
    ):
        variant = list(base)
        variant[index] = changed
        assert image_builds.build_key(*variant) != key


def test_team_build_is_invisible_to_environments(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)
    job = store.create(
        team,
        "",
        "main",
        "Dockerfile",
        ".",
        owner_id=image_builds.TEAM_OWNER_ID,
        repo_url="https://git.example/app.git",
        commit="c" * 40,
        build_key="k" * 64,
    )
    store.finish(team, job, image_builds.STATUS_SUCCEEDED)

    loaded = store.load(team, "", image_builds.TEAM_OWNER_ID, job.build_id)
    assert loaded.repo_url == "https://git.example/app.git"
    assert loaded.commit == "c" * 40
    # A scoped agent always addresses builds through its environment identity.
    with pytest.raises(NotFoundError):
        store.load(team, "feature-x", _OWNER, job.build_id)


def test_find_succeeded_matches_team_builds_by_key_newest_first(tmp_path):
    store = BuildJobStore()
    team = _team(tmp_path)

    def finished(key, owner, status, stamp):
        job = store.create(
            team, "", "main", "Dockerfile", ".", owner_id=owner, build_key=key
        )
        job.local_tag = f"oduflow-build/team-1:{job.build_id}"
        store.finish(team, job, status)
        job.finished_at = stamp
        store.save(team, job)
        return job

    team_owner = image_builds.TEAM_OWNER_ID
    old = finished("k1", team_owner, image_builds.STATUS_SUCCEEDED, "2026-01-01")
    new = finished("k1", team_owner, image_builds.STATUS_SUCCEEDED, "2026-01-02")
    finished("k1", team_owner, image_builds.STATUS_FAILED, "2026-01-03")
    finished("k2", team_owner, image_builds.STATUS_SUCCEEDED, "2026-01-04")
    finished("k1", _OWNER, image_builds.STATUS_SUCCEEDED, "2026-01-05")

    assert [job.build_id for job in store.find_succeeded(team, "k1")] == [
        new.build_id,
        old.build_id,
    ]
    assert store.find_succeeded(team, "") == []


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    ).stdout.strip()


@pytest.fixture
def service_repo(tmp_path):
    """A repository with no Odoo code in it: a Dockerfile below docker/."""
    repo = tmp_path / "remote"
    (repo / "docker").mkdir(parents=True)
    (repo / "docker" / "Dockerfile").write_text("FROM scratch\nCOPY app.txt /\n")
    (repo / "app.txt").write_text("v1\n")
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v1")
    _git(repo, "tag", "-a", "v1-annotated", "-m", "release")
    (repo / "app.txt").write_text("v2\n")
    _git(repo, "commit", "-q", "-am", "two")
    return repo, first, _git(repo, "rev-parse", "HEAD")


def test_resolve_remote_ref_branch_tags_default_and_sha(tmp_path, service_repo):
    from oduflow.docker_ops import build_ops

    repo, first, head = service_repo
    team = _team(tmp_path / "data")
    url = str(repo)

    assert build_ops.resolve_remote_ref(team, url, "main") == (head, "main")
    assert build_ops.resolve_remote_ref(team, url, "") == (head, "main")
    assert build_ops.resolve_remote_ref(team, url, "v1") == (first, "v1")
    # An annotated tag resolves to the commit it points at, not the tag object.
    assert build_ops.resolve_remote_ref(team, url, "v1-annotated") == (
        first,
        "v1-annotated",
    )
    assert build_ops.resolve_remote_ref(team, url, first) == (first, first)
    with pytest.raises(NotFoundError, match="neither a branch nor a tag"):
        build_ops.resolve_remote_ref(team, url, "missing")


class _RecordingBuild:
    """Stands in for a Docker builder; records the context it was handed."""

    builder = "buildkit"
    seen: dict = {}

    def __init__(self, build_root, job, local_tag, build_args, log_path, max_bytes):
        type(self).seen = {
            "dockerfile": os.path.join(build_root, job.dockerfile),
            "dockerfile_exists": os.path.isfile(
                os.path.join(build_root, job.dockerfile)
            ),
            "app": open(os.path.join(build_root, "app.txt")).read(),
            "has_git_dir": os.path.exists(os.path.join(build_root, ".git")),
            "build_args": build_args,
            "local_tag": local_tag,
        }

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None

    def terminate(self):
        return None

    def result(self):
        return {"ok": True, "error": "", "image_id": "sha256:built"}

    def close(self):
        return None


def test_repository_build_fetches_the_pinned_commit(tmp_path, service_repo):
    from oduflow.docker_ops import build_ops

    repo, first, head = service_repo
    team = _team(tmp_path / "data")
    registry = ImageRegistrySettings()
    local_store = BuildJobStore()

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "_buildx_available", return_value=True),
        patch.object(build_ops, "_BuildKitBuild", _RecordingBuild),
        patch.object(build_ops, "_prune_staging_images"),
    ):
        job, remote_url = build_ops.admit_git_build(
            team, registry, str(repo), "v1", "docker/Dockerfile", ".", "", {"A": "1"}
        )
        assert (job.commit, job.branch, job.env_name) == (first, "v1", "")
        assert job.owner_id == image_builds.TEAM_OWNER_ID
        # The branch moves on after admission; the build must not follow it.
        (repo / "app.txt").write_text("v3\n")
        _git(repo, "commit", "-q", "-am", "three")
        build_ops.run_build(team, registry, job, {"A": "1"}, remote_url)

    assert job.status == image_builds.STATUS_SUCCEEDED, job.error
    assert job.builder == "buildkit"
    assert job.local_tag == f"oduflow-build/team-1:{job.build_id}"
    assert _RecordingBuild.seen["dockerfile_exists"]
    assert _RecordingBuild.seen["app"] == "v1\n"
    assert not _RecordingBuild.seen["has_git_dir"]
    assert _RecordingBuild.seen["build_args"] == {"A": "1"}
    # Nothing of the source outlives the job.
    assert not os.path.exists(local_store.source_dir(team, job.build_id))
    assert not os.path.exists(local_store.context_dir(team, job.build_id))
    assert head != first


def test_repository_build_refuses_a_commit_the_remote_does_not_have(
    tmp_path, service_repo
):
    from oduflow.docker_ops import build_ops

    repo, _first, _head = service_repo
    team = _team(tmp_path / "data")
    registry = ImageRegistrySettings()
    local_store = BuildJobStore()

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "_BuildKitBuild", _RecordingBuild),
        patch.object(build_ops, "_buildx_available", return_value=True),
    ):
        # Admitted against a commit that is gone by the time of the fetch: the
        # by-name fallback succeeds, but must not be built in its place.
        job, remote_url = build_ops.admit_git_build(
            team,
            registry,
            str(repo),
            "main",
            "docker/Dockerfile",
            ".",
            "",
            {},
            commit="f" * 40,
        )
        build_ops.run_build(team, registry, job, {}, remote_url)

    assert job.status == image_builds.STATUS_FAILED
    assert "Start the build again" in job.error


def test_repository_build_reports_a_missing_dockerfile(tmp_path, service_repo):
    from oduflow.docker_ops import build_ops

    repo, _first, _head = service_repo
    team = _team(tmp_path / "data")
    local_store = BuildJobStore()

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "_buildx_available", return_value=True),
    ):
        job = build_ops.build_git_image(
            team, str(repo), "main", "Dockerfile", ".", "", {}
        )

    assert job.status == image_builds.STATUS_FAILED
    assert "dockerfile 'Dockerfile' not found" in job.error
    assert "not found" in local_store.read_log_tail(team, job.build_id, 10)


def test_admission_keeps_inline_credentials_out_of_the_job(tmp_path):
    from oduflow.docker_ops import build_ops

    data = tmp_path / "data"
    data.mkdir()
    team = _team(data)
    local_store = BuildJobStore()

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(
            build_ops, "resolve_remote_ref", return_value=("c" * 40, "main")
        ) as resolve,
    ):
        job, remote_url = build_ops.admit_git_build(
            team,
            ImageRegistrySettings(),
            "https://ci:s3cret@git.example/acme/app.git",
            "main",
            "Dockerfile",
            ".",
            "",
            {},
        )

    assert job.repo_url == "https://git.example/acme/app.git"
    assert "s3cret" not in json.dumps(job.to_dict())
    assert "s3cret" not in remote_url
    assert resolve.call_args.args[1] == "https://ci@git.example/acme/app.git"
    assert "s3cret" in open(team.git_credentials_file()).read()


# -- BuildKit through the docker CLI (a fake `docker` on PATH) --


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """Install a scripted `docker` executable; returns a setter for its body."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"

    def install(body: str) -> None:
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return install


def test_buildx_detection_follows_the_cli_plugin(fake_docker, monkeypatch):
    from oduflow.docker_ops import build_ops

    fake_docker('[ "$1 $2" = "buildx version" ] && exit 0; exit 1')
    assert build_ops._buildx_available() is True
    fake_docker("echo \"docker: 'buildx' is not a docker command.\" >&2; exit 1")
    assert build_ops._buildx_available() is False
    monkeypatch.setenv("PATH", "/nonexistent")
    assert build_ops._buildx_available() is False


def test_docker_cli_follows_the_sdk_daemon_not_the_cli_context(monkeypatch):
    from oduflow.docker_ops import build_ops

    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    env = build_ops._docker_cli_env()
    assert "DOCKER_CONTEXT" not in env
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"

    monkeypatch.setenv("DOCKER_HOST", "tcp://docker.internal:2375")
    assert build_ops._docker_cli_env()["DOCKER_HOST"] == "tcp://docker.internal:2375"


def _buildkit_job(tmp_path, **kwargs):
    local_store = BuildJobStore()
    team = _team(tmp_path / "data")
    job = local_store.create(team, "env", "main", owner_id=_OWNER, **kwargs)
    return job


def test_buildkit_build_passes_the_context_and_reports_the_image(tmp_path, fake_docker):
    from oduflow.docker_ops import build_ops

    argv_file = tmp_path / "argv"
    fake_docker(
        f"""
        printf '%s\\n' "$@" > {argv_file}
        echo "#1 [internal] load build definition"
        echo "#2 DONE 0.1s"
        """
    )
    job = _buildkit_job(
        tmp_path, dockerfile="docker/Dockerfile", context=".", target="runtime"
    )
    client = MagicMock()
    client.images.get.return_value = SimpleNamespace(id="sha256:kit")
    log_path = str(tmp_path / "build.log")

    with patch.object(build_ops, "get_client", return_value=client):
        build = build_ops._BuildKitBuild(
            "/ctx", job, "oduflow-build/team-1:x", {"A": "1"}, log_path, 10_000
        )
        build.join()
        result = build.result()
        build.close()

    assert result == {"ok": True, "error": "", "image_id": "sha256:kit"}
    assert argv_file.read_text().splitlines() == [
        "buildx",
        "build",
        "--progress=plain",
        "--load",
        "--tag",
        "oduflow-build/team-1:x",
        "--file",
        "/ctx/docker/Dockerfile",
        "--target",
        "runtime",
        "--build-arg",
        "A=1",
        "/ctx",
    ]
    assert "#2 DONE" in open(log_path).read()


def test_buildkit_reports_an_image_that_was_not_loaded(
    tmp_path, fake_docker, monkeypatch
):
    import docker
    from oduflow.docker_ops import build_ops

    host_file = tmp_path / "host"
    fake_docker(f'printf %s "$DOCKER_HOST" > {host_file}')
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    job = _buildkit_job(tmp_path, dockerfile="Dockerfile", context=".")
    client = MagicMock()
    client.images.get.side_effect = docker.errors.ImageNotFound("no such image")

    with patch.object(build_ops, "get_client", return_value=client):
        build = build_ops._BuildKitBuild(
            "/ctx", job, "tag:x", {}, str(tmp_path / "build.log"), 10_000
        )
        build.join()
        result = build.result()
        build.close()

    assert result["ok"] is False
    assert "did not load it" in result["error"]
    # The CLI was pointed at the socket the SDK talks to.
    assert host_file.read_text() == "unix:///var/run/docker.sock"


def test_buildkit_failure_surfaces_the_solver_error(tmp_path, fake_docker):
    from oduflow.docker_ops import build_ops

    fake_docker(
        """
        echo "#5 ERROR: process did not complete" >&2
        echo "ERROR: failed to solve: https://u:tok@git.example/x exit code: 1" >&2
        exit 1
        """
    )
    job = _buildkit_job(tmp_path, dockerfile="Dockerfile", context=".")
    log_path = str(tmp_path / "build.log")

    build = build_ops._BuildKitBuild("/ctx", job, "tag:x", {}, log_path, 10_000)
    build.join()
    result = build.result()
    build.close()

    assert result["ok"] is False
    assert result["error"].startswith("failed to solve:")
    assert "tok" not in result["error"]
    assert "tok" not in open(log_path).read()


def test_buildkit_build_is_terminated_with_its_process_group(tmp_path, fake_docker):
    from oduflow.docker_ops import build_ops

    pid_file = tmp_path / "child.pid"
    fake_docker(
        f"""
        sleep 300 &
        echo $! > {pid_file}
        wait
        """
    )
    job = _buildkit_job(tmp_path, dockerfile="Dockerfile", context=".")
    build = build_ops._BuildKitBuild(
        "/ctx", job, "tag:x", {}, str(tmp_path / "build.log"), 10_000
    )
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        build.join(timeout=0.05)
    child = int(pid_file.read_text())

    build.terminate()
    build.close()

    assert not build.is_alive()
    for _ in range(100):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        # The builder already exited; joining it does not wait for init to
        # reap its child. Give the process group time to disappear.
        time.sleep(0.05)
    else:
        pytest.fail("the build's child process survived termination")


def test_buildkit_runs_cancellable_from_the_job_loop(tmp_path, fake_docker):
    from oduflow.docker_ops import build_ops

    fake_docker("sleep 300")
    local_store = BuildJobStore()
    team = _team(tmp_path / "data")
    job = local_store.create(team, "env", "main", "Dockerfile", ".", owner_id=_OWNER)
    os.makedirs(local_store.context_dir(team, job.build_id), exist_ok=True)
    local_store.cancel_path(team, job.build_id).touch()

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "_buildx_available", return_value=True),
    ):
        build_ops.run_build(team, ImageRegistrySettings(), job, {})

    assert job.status == image_builds.STATUS_CANCELLED
    assert job.builder == "buildkit"


class _SlowBuild(_RecordingBuild):
    """A builder that stays alive for a few turns of the job loop."""

    def __init__(self, *_args):
        self._turns = 3

    def is_alive(self):
        self._turns -= 1
        return self._turns >= 0


def test_long_build_keeps_the_oduflow_logger_alive(tmp_path):
    # Build output goes to the job log only; the startup watchdog reads a
    # silent logger as a wedged process when a Stack is applied at start.
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    team = _team(tmp_path)
    job = local_store.create(team, "env", "main", "Dockerfile", ".", owner_id=_OWNER)
    os.makedirs(local_store.context_dir(team, job.build_id), exist_ok=True)
    clock = iter(range(0, 10_000, 45))

    with (
        patch.object(build_ops, "store", local_store),
        patch.object(build_ops, "_buildx_available", return_value=True),
        patch.object(build_ops, "_BuildKitBuild", _SlowBuild),
        patch.object(build_ops, "_prune_staging_images"),
        patch.object(build_ops.time, "monotonic", side_effect=lambda: next(clock)),
        patch.object(build_ops.logger, "info") as info,
    ):
        build_ops.run_build(team, ImageRegistrySettings(), job, {})

    assert job.status == image_builds.STATUS_SUCCEEDED
    beats = [call.args for call in info.call_args_list if "still" in call.args[0]]
    # Polled at 45s, 90s and 135s: one report per elapsed minute.
    assert [args[1:] for args in beats] == [
        (job.build_id, "building", 90),
    ]


def test_interrupted_fetch_does_not_leave_git_running(tmp_path):
    from oduflow.docker_ops import build_ops

    pid_file = tmp_path / "git.pid"

    def interrupt(_now, _phase):
        if pid_file.exists() and pid_file.read_text().strip():
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        build_ops._run_git(
            ["sh", "-c", f"echo $$ > {pid_file}; exec sleep 300"],
            dict(os.environ),
            None,
            float("inf"),
            interrupt,
        )

    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# -- MCP tools: source selection and the registry-less path --


def _tool(name):
    import asyncio
    import inspect

    from oduflow.server import mcp as mcp_server

    fn = mcp_server._tool_manager._tools[name].fn

    def call(**kwargs):
        result = fn(**kwargs)
        return asyncio.run(result) if inspect.isawaitable(result) else result

    return call


@pytest.fixture
def server_team(tmp_path):
    import oduflow.server as server

    team = _team(tmp_path / "data")
    settings = Settings(teams={"1": team})
    with (
        patch.object(server, "_resolve_team", return_value=team),
        patch.object(server, "_get_settings", return_value=settings),
    ):
        yield team


def test_start_image_build_requires_exactly_one_source(server_team):
    from fastmcp.exceptions import ToolError

    start = _tool("start_image_build")
    with pytest.raises(ToolError, match="exactly one build source"):
        start()
    with pytest.raises(ToolError, match="exactly one build source"):
        start(env_name="feature-x", repo_url="https://git.example/app.git")
    with pytest.raises(ToolError, match="ref applies to repo_url builds only"):
        start(env_name="feature-x", ref="main")


def test_start_image_build_from_repository_needs_no_registry_or_environment(
    server_team,
):
    from oduflow.docker_ops import build_ops

    local_store = BuildJobStore()
    with (
        patch.object(build_ops, "store", local_store),
        patch.object(image_builds, "store", local_store),
        patch.object(build_ops, "resolve_remote_ref", return_value=("c" * 40, "main")),
        patch.object(build_ops, "run_build_async") as run_async,
        patch("oduflow.server.git_ops.validate_repo_url"),
    ):
        message = _tool("start_image_build")(
            repo_url="https://git.example/acme/app.git",
            dockerfile="docker/base/Dockerfile",
            build_args="A=1",
        )
        job = run_async.call_args.args[2]
        status = _tool("get_image_build")(build_id=job.build_id)

    assert server_team.image_registry is None
    assert "https://git.example/acme/app.git ref 'main' @ cccccccccccc" in message
    assert f'get_image_build(build_id="{job.build_id}")' in message
    assert "publish_image_build" not in message
    assert run_async.call_args.args[3] == {"A": "1"}
    assert (
        run_async.call_args.kwargs["remote_url"] == "https://git.example/acme/app.git"
    )
    assert (job.dockerfile, job.context, job.env_name) == (
        "docker/base/Dockerfile",
        ".",
        "",
    )
    assert "Source: https://git.example/acme/app.git ref 'main'" in status


def test_start_image_build_is_refused_when_the_operator_disabled_builds(
    tmp_path,
):
    from fastmcp.exceptions import ToolError

    import oduflow.server as server
    from oduflow.docker_ops import build_ops

    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        image_registry=ImageRegistrySettings(builds_enabled=False),
    )
    with (
        patch.object(server, "_resolve_team", return_value=team),
        patch.object(server, "_get_settings", return_value=Settings(teams={"1": team})),
        patch.object(build_ops, "resolve_remote_ref") as resolve,
        patch("oduflow.server.git_ops.validate_repo_url"),
        pytest.raises(ToolError, match="builds are disabled"),
    ):
        _tool("start_image_build")(repo_url="https://git.example/acme/app.git")

    resolve.assert_not_called()


def test_publishing_without_a_registry_is_a_prerequisite_error(server_team):
    from fastmcp.exceptions import ToolError

    local_store = BuildJobStore()
    job = local_store.create(
        server_team, "", "main", "Dockerfile", ".", owner_id=image_builds.TEAM_OWNER_ID
    )
    job.local_tag = f"oduflow-build/team-1:{job.build_id}"
    local_store.finish(server_team, job, image_builds.STATUS_SUCCEEDED)

    with patch.object(image_builds, "store", local_store):
        status = _tool("get_image_build")(build_id=job.build_id)
        with pytest.raises(ToolError, match="No container registry is configured"):
            _tool("publish_image_build")(
                build_id=job.build_id, repository="app", tags="latest"
            )

    assert f"Local image: {job.local_tag}" in status
    assert "Publishing is unavailable" in status


def test_scoped_endpoint_hides_and_refuses_repository_sources():
    from fastmcp.exceptions import ToolError

    from oduflow import scoped_access

    schema = {
        "properties": {"env_name": {}, "repo_url": {}, "ref": {}, "dockerfile": {}},
        "required": ["repo_url"],
    }
    stripped = scoped_access.strip_hidden_params("start_image_build", schema)
    assert set(stripped["properties"]) == {"env_name", "dockerfile"}
    assert stripped["required"] == []
    assert "repo_url" in schema["properties"]  # the shared schema is untouched
    assert scoped_access.strip_hidden_params("get_image_build", schema) is schema

    scoped_access.reject_hidden_params(
        "start_image_build", {"repo_url": "", "ref": None}
    )
    with pytest.raises(ToolError, match="does not accept repo_url"):
        scoped_access.reject_hidden_params(
            "start_image_build", {"repo_url": "https://git.example/x.git"}
        )
