"""Container image builds from git sources, and registry publication.

The agent never receives a Docker socket or registry credentials: these
functions run server-side on the agent's behalf, behind the image build MCP
tools. A build has one of two sources, both pinned to an exact commit before
anything runs:

- an environment's managed checkout — its ``HEAD`` at admission time, sealed
  under the environment lock so a concurrent pull can never mix two revisions;
- a repository URL plus ref — resolved to a commit with ``git ls-remote`` at
  admission and fetched shallow into the job. No environment is involved, so a
  repository without any Odoo code can be built into a service image.

Either way the context is ``git archive`` of that immutable object. The local
Docker daemon is the staging area: a successful build is tagged
``oduflow-build/team-<id>:<build-id>`` and publishing is ``docker tag`` +
``docker push`` of that exact image — never a rebuild.

The Dockerfile runs under BuildKit (``docker buildx build``) when the docker
CLI and its buildx plugin are installed next to Oduflow, and under the Docker
Engine's legacy builder otherwise.

Single-trust-boundary by design: the build runs on the host Docker daemon, so
a Dockerfile is untrusted code with the same network reach as the team's own
environment containers. Acceptable for self-hosted single-team deployments;
multi-tenant isolation would need the isolated-builder architecture recorded
under "Shelved alternative" in
specs/0054-agent-container-image-build-and-publish.md.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Collection
from typing import Any

import docker
from oduflow import env_tokens, git_ops, image_builds
from oduflow.docker_ops.client import get_client
from oduflow.errors import (
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.image_builds import BuildJob, store
from oduflow.locking import image_build_lock_key, keyed_mutex, service_registry_key
from oduflow.naming import (
    get_repo_path,
    get_resource_name,
    redact_url_credentials,
    sanitize_repo_url,
)
from oduflow.settings import ImageRegistrySettings, Settings, TeamSettings

logger = logging.getLogger("oduflow")

_MAX_CONTEXT_FILES = 200_000
# tarfile.FilterError exists only on Pythons that ship extraction filters
# (3.12+, backported to 3.10.12+/3.11.4+); on older ones the filter= call
# raises TypeError and the manual fallback below applies the same checks.
_FILTER_ERRORS: tuple[type[BaseException], ...] = tuple(
    t for t in (getattr(tarfile, "FilterError", None),) if t is not None
)


STAGING_REPOSITORY_PREFIX = "oduflow-build/"


def staging_repository(team_id: str) -> str:
    """Local (daemon-only) repository holding a team's build candidates."""
    return f"{STAGING_REPOSITORY_PREFIX}team-{team_id}"


def build_settings(team: TeamSettings) -> ImageRegistrySettings:
    """Build limits: the team's section, or the defaults when it has none.

    Refuses when the operator switched image building off for the team.
    """
    cfg = team.image_registry or ImageRegistrySettings()
    if not cfg.builds_enabled:
        raise PrerequisiteNotMetError(
            f"Container image builds are disabled for team '{team.team_id}' "
            f"(builds_enabled = false in [team.{team.team_id}.image_registry])."
        )
    return cfg


def _environment_container(
    settings: Settings, team: TeamSettings, env_name: str
) -> Any:
    """Resolve and ownership-check one managed environment container."""
    from oduflow.docker_ops.env_ops import _assert_team_owns

    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(container, settings, team, env_name)
    return container


def _environment_token(container: Any, team: TeamSettings, env_name: str) -> str:
    """The environment's MCP token: registry first, legacy label fallback."""
    token = env_tokens.get(team, env_name) or str(
        container.labels.get(env_tokens.MCP_TOKEN_LABEL, "")
    )
    if not token:
        raise PrerequisiteNotMetError(
            f"Environment '{env_name}' has no scoped MCP identity; recreate it "
            "before starting or accessing image builds."
        )
    return token


def environment_owner_id(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """Stable, non-secret identity for an environment instance.

    The environment MCP token survives rename and recreate and changes on
    delete. Persisting only its SHA-256 digest binds jobs to that lifecycle
    without putting the bearer credential in job.json.
    """
    container = _environment_container(settings, team, env_name)
    token = _environment_token(container, team, env_name)
    return hashlib.sha256(token.encode()).hexdigest()


def env_checkout_for_build(
    settings: Settings, team: TeamSettings, env_name: str
) -> tuple[str, str, str]:
    """Resolve the managed checkout; returns (repo_path, branch, owner_id).

    Rejects live-mount environments: their checkout belongs to the user and has
    no Oduflow-managed revision to pin.
    """
    container = _environment_container(settings, team, env_name)
    if container.labels.get("oduflow.local_path"):
        raise PrerequisiteNotMetError(
            f"Environment '{env_name}' is live-mounted from a local path; image "
            "builds require an Oduflow-managed git checkout."
        )
    branch = container.labels.get("oduflow.git_branch", env_name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        raise NotFoundError(
            f"Environment '{env_name}' has no managed git checkout at its "
            "workspace; image builds require one."
        )
    token = _environment_token(container, team, env_name)
    owner_id = hashlib.sha256(token.encode()).hexdigest()
    return repo_path, branch, owner_id


def snapshot_context(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    job: BuildJob,
    repo_path: str,
) -> None:
    """Seal the build source: export the checkout's HEAD commit into the job.

    Called while the caller holds the environment lock, so HEAD cannot move
    under us; ``git archive <sha>`` reads the immutable object, so later pulls
    never touch the sealed context. Records the commit on the job.
    """
    try:
        sha = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError(
            "git rev-parse", e.returncode, redact_url_credentials(e.stderr or str(e))
        )
    except subprocess.TimeoutExpired:
        raise ExternalCommandError("git rev-parse", -1, "Timed out (30s).")
    job.commit = sha
    _export_commit(team, registry_cfg, job, repo_path, sha)


def _export_commit(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    job: BuildJob,
    repo_path: str,
    sha: str,
) -> None:
    """Extract one commit of *repo_path* into the job's sealed context."""
    ctx_dir = store.context_dir(team, job.build_id)
    os.makedirs(ctx_dir, exist_ok=True)
    max_bytes = registry_cfg.max_context_mb * 1024 * 1024
    proc = subprocess.Popen(
        ["git", "-C", repo_path, "archive", "--format=tar", sha],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    total = 0
    count = 0
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                total += max(member.size, 0)
                count += 1
                if total > max_bytes:
                    raise ValueError(
                        f"Build context exceeds the configured limit of "
                        f"{registry_cfg.max_context_mb} MB. Shrink the context "
                        "or raise [team.X.image_registry] max_context_mb."
                    )
                if count > _MAX_CONTEXT_FILES:
                    raise ValueError(
                        f"Build context exceeds {_MAX_CONTEXT_FILES} files."
                    )
                _extract_member(tar, member, ctx_dir)
    finally:
        proc.stdout.close()
        stderr = b""
        if proc.stderr is not None:
            stderr = proc.stderr.read()
            proc.stderr.close()
        returncode = proc.wait()
    if returncode != 0:
        raise ExternalCommandError(
            "git archive",
            returncode,
            redact_url_credentials(stderr.decode(errors="replace")),
        )

    build_root = ctx_dir if job.context == "." else os.path.join(ctx_dir, job.context)
    if not os.path.isdir(build_root):
        raise ValueError(
            f"context {job.context!r} is not a directory at commit {sha[:12]}."
        )
    if not os.path.isfile(os.path.join(build_root, job.dockerfile)):
        raise ValueError(
            f"dockerfile {job.dockerfile!r} not found under context "
            f"{job.context!r} at commit {sha[:12]}."
        )


class _BuildCancelled(Exception):
    """The job's cancel event was set."""


class _BuildTimedOut(Exception):
    """The job ran past its wall-clock deadline."""


_HEARTBEAT_SECONDS = 60.0


class _Heartbeat:
    """Reports on the ``oduflow`` logger that a job is still running.

    A build writes to its own log file, so a long one is otherwise silent on
    the logger — and when a Stack is applied at server start, the startup
    watchdog reads that silence as a wedged process and exits. The job's
    deadline still bounds how long these reports can go on.
    """

    def __init__(self, build_id: str, started: float) -> None:
        self._build_id = build_id
        self._started = started
        self._last = started

    def __call__(self, now: float, phase: str) -> None:
        if now - self._last < _HEARTBEAT_SECONDS:
            return
        self._last = now
        logger.info(
            "Image build %s is still %s (%ds elapsed)",
            self._build_id,
            phase,
            int(now - self._started),
        )


def _stop_process_group(proc: subprocess.Popen[Any]) -> None:
    """Terminate a child started in its own session, with its descendants."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue


def resolve_remote_ref(
    team: TeamSettings, remote_url: str, ref: str
) -> tuple[str, str]:
    """Resolve a branch, tag or the default branch of a remote to a commit.

    Returns ``(commit, label)``. One ``git ls-remote`` round-trip, which also
    proves the URL and the team's credentials before a job is created. A full
    commit SHA is taken as given: its existence is checked by the fetch.
    """
    from oduflow.extra_addons import _raise_if_auth_error, _remote_git_env

    if image_builds.is_commit_sha(ref):
        return ref, ref
    patterns = (
        [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
        if ref
        else ["HEAD"]
    )
    display = sanitize_repo_url(remote_url)
    try:
        output = subprocess.run(
            ["git", "ls-remote", "--symref", remote_url, *patterns],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=_remote_git_env(team),
        ).stdout
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        _raise_if_auth_error(remote_url, stderr)
        raise ExternalCommandError(
            "git ls-remote", e.returncode, redact_url_credentials(stderr)
        )
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git ls-remote", -1, f"Reading refs of '{display}' timed out (60s)."
        )

    refs: dict[str, str] = {}
    default_branch = ""
    for line in output.splitlines():
        value, _, name = line.partition("\t")
        if value.startswith("ref: ") and name == "HEAD":
            default_branch = value[len("ref: ") :].removeprefix("refs/heads/")
        elif name:
            refs[name] = value
    if not ref:
        if "HEAD" not in refs:
            raise NotFoundError(
                f"Repository '{display}' has no default branch to build; pass ref."
            )
        return refs["HEAD"], default_branch or "HEAD"
    # A branch wins over a tag of the same name, as in `git checkout`. The
    # peeled entry is the commit an annotated tag points at.
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if name in refs:
            return refs[name], ref
    raise NotFoundError(
        f"Ref '{ref}' is neither a branch nor a tag of '{display}'. Pass an "
        "existing branch or tag name, or a full commit SHA."
    )


def git_remote(
    team: TeamSettings, repo_url: str, git_user: str = ""
) -> tuple[str, str]:
    """``(remote_url, display_url)`` for a repository build source.

    Inline credentials move to the team's credential store. The remote URL may
    carry the username git needs to pick the stored token; the display URL
    never carries credentials and is what jobs and build keys record.
    """
    clean_url, stored_user = git_ops.extract_and_store_inline_credentials(
        repo_url, team.git_credentials_file()
    )
    remote_url = git_ops.inject_credential_user(clean_url, git_user or stored_user)
    return remote_url, sanitize_repo_url(clean_url)


def find_built_image(team: TeamSettings, key: str, prefer: str = "") -> str:
    """Staging tag of a finished build with these inputs, or "" if none is left.

    *prefer* is the tag a service already runs: it wins over a newer build of
    the same inputs so an in-sync service is not recreated for an equal image.
    """
    tags = [job.local_tag for job in store.find_succeeded(team, key)]
    if prefer in tags:
        tags.insert(0, prefer)
    return next((tag for tag in tags if staging_image_exists(tag)), "")


def build_git_image(
    team: TeamSettings,
    repo_url: str,
    ref: str,
    dockerfile: str,
    context: str,
    target: str,
    build_args: dict[str, str],
    git_user: str = "",
    commit: str = "",
    keep_tags: Collection[str] = (),
) -> BuildJob:
    """Build a repository image and wait for the result (Stack apply).

    *keep_tags* are staging tags the caller is about to run: retention leaves
    them alone even though no container references them yet.
    """
    registry_cfg = build_settings(team)
    job, remote_url = admit_git_build(
        team,
        registry_cfg,
        repo_url,
        ref,
        dockerfile,
        context,
        target,
        build_args,
        git_user=git_user,
        commit=commit,
    )
    run_build(team, registry_cfg, job, build_args, remote_url, keep_tags=keep_tags)
    return job


def admit_git_build(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    repo_url: str,
    ref: str,
    dockerfile: str,
    context: str,
    target: str,
    build_args: dict[str, str],
    git_user: str = "",
    commit: str = "",
) -> tuple[BuildJob, str]:
    """Pin a repository build to a commit and create its job.

    Returns ``(job, remote_url)``. ``remote_url`` may carry the credential
    username git needs to pick the stored token; the job keeps the URL without
    it. Inline credentials are moved to the team's credential store first, so
    they never reach job.json or the build log. A caller that already resolved
    *ref* passes the *commit* to build exactly what it planned.
    """
    remote_url, display_url = git_remote(team, repo_url, git_user)
    if commit:
        label = ref or commit
    else:
        commit, label = resolve_remote_ref(team, remote_url, ref)
    job = store.create(
        team,
        "",
        label,
        dockerfile,
        context,
        target=target,
        build_arg_names=list(build_args),
        owner_id=image_builds.TEAM_OWNER_ID,
        max_concurrent_builds=registry_cfg.max_concurrent_builds,
        repo_url=display_url,
        commit=commit,
        build_key=image_builds.build_key(
            display_url, commit, dockerfile, context, target, build_args
        ),
    )
    return job, remote_url


def _run_git(
    args: list[str],
    env: dict[str, str],
    cancel: threading.Event | None,
    deadline: float,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[int, str]:
    """Run git so that cancellation and the job deadline can interrupt it."""
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        while True:
            try:
                _out, stderr = proc.communicate(timeout=0.25)
                return proc.returncode, stderr or ""
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if cancel is not None and cancel.is_set():
                    raise _BuildCancelled
                if now >= deadline:
                    raise _BuildTimedOut
                if progress is not None:
                    progress(now, "fetching its source")
    except BaseException:
        # Cancel, deadline or Ctrl-C. git sits in its own session, out of
        # reach of the terminal's signal, so nothing else would stop it.
        _stop_process_group(proc)
        proc.communicate()
        raise


def _fetch_git_source(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    job: BuildJob,
    remote_url: str,
    cancel: threading.Event | None,
    deadline: float,
    progress: Callable[[float, str], None] | None = None,
) -> None:
    """Download the pinned commit of a repository build and seal its context.

    The commit is fetched by SHA, shallow, into a bare repository that lives
    only as long as the job. Servers that refuse a SHA request get the ref
    fetched by name instead; if the ref moved since admission the pinned commit
    is then absent and the build fails rather than building something else.
    """
    from oduflow.extra_addons import _raise_if_auth_error, _remote_git_env

    env = _remote_git_env(team)
    source = store.source_dir(team, job.build_id)
    subprocess.run(
        ["git", "init", "--quiet", "--bare", source],
        check=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    fetch = [
        "git",
        "-C",
        source,
        "fetch",
        "--quiet",
        "--depth",
        "1",
        "--no-tags",
        "--recurse-submodules=no",
        remote_url,
    ]
    returncode, stderr = _run_git([*fetch, job.commit], env, cancel, deadline, progress)
    if returncode != 0 and job.branch != job.commit:
        by_name = "HEAD" if job.branch == "HEAD" else job.branch
        returncode, stderr = _run_git(
            [*fetch, by_name], env, cancel, deadline, progress
        )
    if returncode != 0:
        _raise_if_auth_error(remote_url, stderr)
        raise ExternalCommandError(
            "git fetch", returncode, redact_url_credentials(stderr)
        )
    present = subprocess.run(
        ["git", "-C", source, "cat-file", "-e", f"{job.commit}^{{commit}}"],
        capture_output=True,
        timeout=30,
        env=env,
    )
    if present.returncode != 0:
        raise NotFoundError(
            f"Commit {job.commit[:12]} is not available from '{job.repo_url}': "
            f"'{job.branch}' moved while the build was starting, or the commit "
            "does not exist. Start the build again."
        )
    _export_commit(team, registry_cfg, job, source, job.commit)
    shutil.rmtree(source, ignore_errors=True)


def _extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, dest: str) -> None:
    """Extract one archive member, refusing traversal and escaping links.

    ``git archive`` of our own commit emits clean repo-relative names, but the
    repository content itself is untrusted (symlinks can point anywhere), so
    Python's "data" extraction filter does the vetting. Older 3.10 point
    releases lack the ``filter`` parameter; the fallback applies the same
    checks by hand.
    """
    try:
        tar.extract(member, dest, filter="data")
        return
    except TypeError:
        pass
    except _FILTER_ERRORS as exc:
        raise ValueError(
            f"Unsafe entry in build context archive: {member.name!r} ({exc})"
        )
    name = member.name
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError(f"Unsafe path in build context archive: {name!r}")
    if member.islnk() or member.issym():
        target = member.linkname
        if target.startswith("/") or ".." in target.split("/"):
            raise ValueError(f"Unsafe link in build context archive: {name!r}")
    if member.isdev():
        raise ValueError(f"Device node in build context archive: {name!r}")
    tar.extract(member, dest)


def run_build_async(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    job: BuildJob,
    build_args: dict[str, str],
    remote_url: str = "",
) -> None:
    """Start the build in a background thread; the MCP call returns immediately.

    *remote_url* is set for a repository build, whose source is still to be
    fetched; an environment build arrives with its context already sealed.
    """
    thread = threading.Thread(
        target=run_build,
        args=(team, registry_cfg, job, build_args, remote_url),
        name=f"image-build-{job.build_id}",
        daemon=True,
    )
    thread.start()


_LOG_TRUNCATED_MARKER = b"\n[build] build log truncated at configured limit\n"


class _CappedLog:
    """Binary log writer that never lets one build exceed its configured cap."""

    def __init__(self, path: str, max_bytes: int) -> None:
        self._file = open(path, "ab")
        self._max_bytes = max_bytes
        self._written = self._file.tell()
        self._truncated = self._written >= max_bytes

    def __enter__(self) -> _CappedLog:
        return self

    def __exit__(self, *_args: object) -> None:
        self._file.close()

    def write(self, text: object) -> None:
        if self._truncated:
            return
        data = str(text).encode(errors="replace")
        remaining = self._max_bytes - self._written
        if len(data) <= remaining:
            self._file.write(data)
            self._file.flush()
            self._written += len(data)
            return
        payload_bytes = max(0, remaining - len(_LOG_TRUNCATED_MARKER))
        final = (
            data[:payload_bytes] + _LOG_TRUNCATED_MARKER[: remaining - payload_bytes]
        )
        self._file.write(final)
        self._file.flush()
        self._written += len(final)
        self._truncated = True


def _append_capped_log(path: str, text: str, max_bytes: int) -> None:
    with _CappedLog(path, max_bytes) as log:
        log.write(text)


def _docker_build_worker(
    build_root: str,
    dockerfile: str,
    local_tag: str,
    target: str,
    build_args: dict[str, str],
    log_path: str,
    max_log_bytes: int,
    result_conn: Any,
) -> None:
    """Build in a child process so timeout/cancel can close the Docker stream."""
    result: dict[str, Any] = {"ok": False, "error": "", "image_id": ""}
    try:
        client = get_client()
        stream = client.api.build(
            path=build_root,
            dockerfile=dockerfile,
            tag=local_tag,
            target=target or None,
            buildargs=build_args or None,
            rm=True,
            forcerm=True,
            decode=True,
        )
        with _CappedLog(log_path, max_log_bytes) as log:
            for chunk in stream:
                if "stream" in chunk:
                    log.write(chunk["stream"])
                elif "status" in chunk:
                    log.write(str(chunk["status"]) + "\n")
                if "errorDetail" in chunk or "error" in chunk:
                    detail = chunk.get("errorDetail") or {}
                    result["error"] = redact_url_credentials(
                        str(detail.get("message") or chunk.get("error") or "")
                    )
                    log.write(f"\n[build] build error: {result['error']}\n")
                    break
            else:
                image = client.images.get(local_tag)
                result["ok"] = True
                result["image_id"] = str(image.id)
    except Exception as exc:  # noqa: BLE001 — parent must receive a terminal result
        result["error"] = redact_url_credentials(str(exc))
        logger.exception("Docker image build worker failed for %s", local_tag)
    try:
        result_conn.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        result_conn.close()


def _terminate_build_process(process: Any) -> None:
    process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _docker_cli_env() -> dict[str, str]:
    """Environment that points the docker CLI at the daemon the SDK uses.

    docker-py knows nothing of CLI contexts: it follows ``DOCKER_HOST`` and
    otherwise the default socket. Left alone, the CLI follows its current
    context (Docker Desktop, colima, rootless) and would load the built image
    into a daemon the rest of Oduflow never looks at.
    """
    env = dict(os.environ)
    env.pop("DOCKER_CONTEXT", None)
    if not env.get("DOCKER_HOST"):
        env["DOCKER_HOST"] = docker.constants.DEFAULT_UNIX_SOCKET.removeprefix("http+")
    return env


def _buildx_available() -> bool:
    """Whether the docker CLI with its buildx plugin can run BuildKit builds.

    Checked per build rather than cached: an operator can install the plugin
    without restarting Oduflow.
    """
    docker_cli = shutil.which("docker")
    if not docker_cli:
        return False
    try:
        return (
            subprocess.run(
                [docker_cli, "buildx", "version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                env=_docker_cli_env(),
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


class _LegacyBuild:
    """The Docker Engine's legacy builder, driven through the SDK in a child.

    A child process owns the Docker HTTP connection, so terminating it also
    ends a Dockerfile step that produces no output.
    """

    builder = "legacy"

    def __init__(
        self,
        build_root: str,
        job: BuildJob,
        local_tag: str,
        build_args: dict[str, str],
        log_path: str,
        max_log_bytes: int,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        self._recv, send = context.Pipe(duplex=False)
        self._process = context.Process(
            target=_docker_build_worker,
            args=(
                build_root,
                job.dockerfile,
                local_tag,
                job.target,
                build_args,
                log_path,
                max_log_bytes,
                send,
            ),
            name=f"image-build-worker-{job.build_id}",
            daemon=True,
        )
        try:
            self._process.start()
        finally:
            send.close()

    def is_alive(self) -> bool:
        return bool(self._process.is_alive())

    def join(self, timeout: float | None = None) -> None:
        self._process.join(timeout=timeout)

    def terminate(self) -> None:
        if self._process.is_alive():
            _terminate_build_process(self._process)

    def result(self) -> dict[str, Any]:
        if self._recv.poll(timeout=1):
            return dict(self._recv.recv())
        return {
            "ok": False,
            "error": "Build worker exited unexpectedly "
            f"(exit {self._process.exitcode}).",
        }

    def close(self) -> None:
        self._recv.close()


class _BuildKitBuild:
    """BuildKit through ``docker buildx build``, the builder Dockerfiles expect.

    The CLI runs in its own session: ending that process group closes the
    BuildKit session, which cancels the build inside the daemon.
    """

    builder = "buildkit"

    def __init__(
        self,
        build_root: str,
        job: BuildJob,
        local_tag: str,
        build_args: dict[str, str],
        log_path: str,
        max_log_bytes: int,
    ) -> None:
        command = [
            "docker",
            "buildx",
            "build",
            "--progress=plain",
            "--load",
            "--tag",
            local_tag,
            # The CLI resolves --file against its working directory, not the
            # context, so the path is spelled out in full.
            "--file",
            os.path.join(build_root, job.dockerfile),
        ]
        if job.target:
            command += ["--target", job.target]
        for key, value in build_args.items():
            command += ["--build-arg", f"{key}={value}"]
        command.append(build_root)
        self._local_tag = local_tag
        self._last_error = ""
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={
                **_docker_cli_env(),
                # Keep the staging image a plain single-platform image, as the
                # legacy builder produces: a provenance attestation would turn
                # it into an index whose digest changes on every cached rebuild.
                "BUILDX_NO_DEFAULT_ATTESTATIONS": "1",
                "DOCKER_CLI_HINTS": "false",
                "NO_COLOR": "1",
            },
        )
        self._pump = threading.Thread(
            target=self._pump_output,
            args=(log_path, max_log_bytes),
            name=f"image-build-log-{job.build_id}",
            daemon=True,
        )
        self._pump.start()

    def _pump_output(self, log_path: str, max_log_bytes: int) -> None:
        # Keeps reading after the log cap is reached: a full pipe would block
        # the build.
        assert self._process.stdout is not None
        with _CappedLog(log_path, max_log_bytes) as log:
            for raw in self._process.stdout:
                line = redact_url_credentials(raw.decode(errors="replace"))
                log.write(line)
                if line.startswith("ERROR:"):
                    self._last_error = line[len("ERROR:") :].strip()

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def join(self, timeout: float | None = None) -> None:
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def terminate(self) -> None:
        _stop_process_group(self._process)

    def result(self) -> dict[str, Any]:
        self._pump.join(timeout=10)
        returncode = self._process.returncode
        if returncode != 0:
            return {
                "ok": False,
                "error": self._last_error
                or f"docker buildx build failed (exit {returncode}); see the log.",
            }
        try:
            image = get_client().images.get(self._local_tag)
        except docker.errors.ImageNotFound:
            return {
                "ok": False,
                "error": "docker buildx build succeeded, but the image is not "
                "in the Docker daemon Oduflow uses: the selected buildx "
                "builder did not load it there. Select the default builder "
                "(`docker buildx use default`) and build again.",
            }
        return {"ok": True, "error": "", "image_id": str(image.id)}

    def close(self) -> None:
        if self._process.stdout is not None:
            self._process.stdout.close()


_LEGACY_BUILDER_NOTICE = (
    "[build] BuildKit is unavailable (no docker CLI with the buildx plugin next "
    "to Oduflow); using the Docker Engine's legacy builder. Dockerfiles that "
    "rely on BuildKit ($BUILDPLATFORM, RUN --mount, COPY --link) will fail.\n"
)


def run_build(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    job: BuildJob,
    build_args: dict[str, str],
    remote_url: str = "",
    keep_tags: Collection[str] = (),
) -> None:
    """Run one admitted job to a terminal state. Never raises.

    Blocks for the duration of the build, bounded by the configured timeout;
    ``run_build_async`` is the non-blocking entry point. *keep_tags* are
    exempt from the retention pass that follows a successful build.
    """
    cancel = store.cancel_event(job.build_id)
    log_path = store.log_path(team, job.build_id)
    ctx_dir = store.context_dir(team, job.build_id)
    build_root = ctx_dir if job.context == "." else os.path.join(ctx_dir, job.context)
    local_tag = f"{staging_repository(team.team_id)}:{job.build_id}"
    job.status = image_builds.STATUS_BUILDING
    store.save(team, job)

    max_log_bytes = registry_cfg.max_log_mb * 1024 * 1024
    started = time.monotonic()
    deadline = started + registry_cfg.build_timeout_seconds
    heartbeat = _Heartbeat(job.build_id, started)
    status = image_builds.STATUS_FAILED
    error = ""
    result: dict[str, Any] = {}
    build: _LegacyBuild | _BuildKitBuild | None = None
    try:
        if remote_url:
            _append_capped_log(
                log_path,
                f"[build] fetching {job.repo_url} @ {job.commit}\n",
                max_log_bytes,
            )
            _fetch_git_source(
                team, registry_cfg, job, remote_url, cancel, deadline, heartbeat
            )
        runner = _BuildKitBuild if _buildx_available() else _LegacyBuild
        job.builder = runner.builder
        store.save(team, job)
        if runner is _LegacyBuild:
            _append_capped_log(log_path, _LEGACY_BUILDER_NOTICE, max_log_bytes)
        build = runner(build_root, job, local_tag, build_args, log_path, max_log_bytes)
        while build.is_alive():
            if cancel is not None and cancel.is_set():
                raise _BuildCancelled
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise _BuildTimedOut
            heartbeat(now, "building")
            build.join(timeout=min(0.25, remaining))
        build.join()
        result = build.result()
        if result.get("ok"):
            status = image_builds.STATUS_SUCCEEDED
        else:
            error = str(result.get("error") or "Build failed; see the log.")
    except _BuildCancelled:
        status = image_builds.STATUS_CANCELLED
        error = "Cancelled by request."
        if build is not None:
            build.terminate()
        _append_capped_log(log_path, "\n[build] build cancelled\n", max_log_bytes)
    except _BuildTimedOut:
        error = f"Build timed out after {registry_cfg.build_timeout_seconds}s."
        if build is not None:
            build.terminate()
        _append_capped_log(log_path, f"\n[build] {error}\n", max_log_bytes)
    except Exception as exc:  # noqa: BLE001 — terminal state must always be written
        error = redact_url_credentials(str(exc))
        logger.exception("Image build %s failed", job.build_id)
        if build is not None:
            build.terminate()
        _append_capped_log(log_path, f"\n[build] {error}\n", max_log_bytes)
    except BaseException:
        # Ctrl-C during a Stack apply: do not leave the build running behind
        # a CLI that has already exited.
        if build is not None:
            build.terminate()
        store.finish(team, job, image_builds.STATUS_INTERRUPTED, "Interrupted.")
        raise
    finally:
        if build is not None:
            build.close()
    if status == image_builds.STATUS_SUCCEEDED:
        job.image_id = str(result["image_id"])
        job.local_tag = local_tag
        store.finish(team, job, status)
        _prune_staging_images(team, registry_cfg.keep_images, keep_tags)
    else:
        store.finish(team, job, status, error or "Build failed; see the log.")
    logger.info("Image build %s finished: %s", job.build_id, job.status)


def staging_image_exists(local_tag: str) -> bool:
    """Whether a build's staging image is still in the local daemon."""
    try:
        get_client().images.get(local_tag)
    except docker.errors.ImageNotFound:
        return False
    return True


def _remove_image_if_unused(client: Any, image_id: str) -> None:
    """Remove an untagged image; Docker refuses while a container uses it."""
    if not image_id:
        return
    try:
        image = client.images.get(image_id)
    except docker.errors.ImageNotFound:
        return
    if image.tags:
        return
    try:
        client.images.remove(image_id)
    except docker.errors.APIError:
        logger.debug("Image %s is still in use; keeping it", image_id, exc_info=True)


def _prune_staging_images(
    team: TeamSettings, keep: int, keep_tags: Collection[str] = ()
) -> None:
    """Untag old build jobs and remove their now-unused image objects.

    A container protects the tag it runs; *keep_tags* protects the tags a
    Stack apply has resolved but not started yet, since it finishes every
    build before it creates the first service.
    """
    if keep <= 0:
        return
    try:
        client = get_client()
        jobs = [
            job
            for job in store.list_jobs(team)
            if job.status == image_builds.STATUS_SUCCEEDED and job.local_tag
        ]
        jobs.sort(key=lambda job: job.finished_at or job.created_at, reverse=True)
        for job in jobs[keep:]:
            if job.local_tag in keep_tags:
                continue
            with (
                keyed_mutex(image_build_lock_key(team.team_id, job.build_id)),
                keyed_mutex(service_registry_key(team.team_id)),
            ):
                # Docker can untag an in-use image when another tag exists
                # (e.g. a cached rebuild). Preserve the exact reference used
                # by running AND stopped containers, not just the image ID.
                # Service creation/recreation holds this key across lookup
                # and run, so that window cannot look unused to retention.
                if any(
                    container.attrs.get("Config", {}).get("Image") == job.local_tag
                    for container in client.containers.list(all=True)
                ):
                    continue
                try:
                    client.images.remove(job.local_tag)
                except docker.errors.ImageNotFound:
                    pass
                except docker.errors.APIError:
                    logger.debug(
                        "Could not untag staging image %s", job.local_tag, exc_info=True
                    )
                _remove_image_if_unused(client, job.image_id)
    except Exception:  # noqa: BLE001 — pruning must never fail a build
        logger.warning(
            "Staging image prune failed for team %s", team.team_id, exc_info=True
        )


def publish(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    job: BuildJob,
    repository: str,
    tags: list[str],
) -> dict[str, Any]:
    """Tag + push the staging image to every requested tag; per-tag outcomes.

    Registry tag writes are not a transaction: partial success is possible and
    each tag reports its own result. Pushing an unchanged image to an existing
    tag is naturally idempotent. Credentials, when configured, are passed
    request-scoped to the Docker API — never via a daemon-wide login.
    """
    full_repo = f"{registry_cfg.repository_prefix}/{repository}"
    ref = (
        full_repo
        if registry_cfg.host == "docker.io"
        else f"{registry_cfg.host}/{full_repo}"
    )
    auth_config = None
    if registry_cfg.username:
        auth_config = {
            "username": registry_cfg.username,
            "password": registry_cfg.token,
        }

    client = get_client()
    results: list[dict[str, Any]] = []
    with keyed_mutex(image_build_lock_key(team.team_id, job.build_id)):
        try:
            client.images.get(job.local_tag)
        except docker.errors.ImageNotFound:
            raise NotFoundError(
                f"The staging image for build '{job.build_id}' is no longer in the "
                "local Docker daemon (pruned or removed). Run start_image_build "
                "again — the layer cache makes the rebuild cheap."
            )

        for tag in tags:
            outcome: dict[str, Any] = {"tag": tag, "digest": "", "error": ""}
            destination = f"{ref}:{tag}"
            tagged = False
            try:
                tagged = bool(client.api.tag(job.local_tag, ref, tag))
                if not tagged:
                    raise docker.errors.APIError(
                        f"Docker did not create the temporary tag {destination}"
                    )
                for chunk in client.api.push(
                    ref, tag=tag, stream=True, decode=True, auth_config=auth_config
                ):
                    if "aux" in chunk and isinstance(chunk["aux"], dict):
                        outcome["digest"] = str(chunk["aux"].get("Digest", ""))
                    if "errorDetail" in chunk or "error" in chunk:
                        detail = chunk.get("errorDetail") or {}
                        outcome["error"] = redact_url_credentials(
                            str(detail.get("message") or chunk.get("error") or "")
                        )
                        break
            except Exception as exc:  # noqa: BLE001 — report each tag independently
                explanation = getattr(exc, "explanation", "")
                outcome["error"] = redact_url_credentials(explanation or str(exc))
            finally:
                if tagged:
                    try:
                        client.images.remove(destination)
                    except docker.errors.ImageNotFound:
                        pass
                    except docker.errors.APIError:
                        logger.warning(
                            "Could not remove temporary publish tag %s",
                            destination,
                            exc_info=True,
                        )
                _remove_image_if_unused(client, job.image_id)
            results.append(outcome)

    publication = {
        "at": image_builds.now_iso(),
        "host": registry_cfg.host,
        "repository": full_repo,
        "results": results,
    }
    store.append_publication(team, job, publication)
    return publication
