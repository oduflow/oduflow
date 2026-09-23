"""Container image builds from environment checkouts, and registry publication.

The agent never receives a Docker socket or registry credentials: these
functions run server-side on the agent's behalf, behind the image build MCP
tools. The build source is the environment's managed checkout pinned to the
exact ``HEAD`` commit at admission time (``git archive`` of that object, sealed
under the environment lock), so a concurrent pull can never mix two revisions
into one context. The local Docker daemon is the staging area: a successful
build is tagged ``oduflow-build/team-<id>:<build-id>`` and publishing is
``docker tag`` + ``docker push`` of that exact image — never a rebuild.

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
import subprocess
import tarfile
import threading
import time
from typing import Any

import docker
from oduflow import env_tokens, image_builds
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


def environment_owner_id(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """Stable, non-secret identity for an environment instance.

    The environment MCP token survives rename and changes on delete/recreate.
    Persisting only its SHA-256 digest binds jobs to that lifecycle without
    putting the bearer credential in job.json.
    """
    container = _environment_container(settings, team, env_name)
    token = str(container.labels.get(env_tokens.MCP_TOKEN_LABEL, ""))
    if not token:
        raise PrerequisiteNotMetError(
            f"Environment '{env_name}' has no scoped MCP identity; recreate it "
            "before starting or accessing image builds."
        )
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
    token = str(container.labels.get(env_tokens.MCP_TOKEN_LABEL, ""))
    if not token:
        raise PrerequisiteNotMetError(
            f"Environment '{env_name}' has no scoped MCP identity; recreate it "
            "before starting image builds."
        )
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
) -> None:
    """Start the build in a background thread; the MCP call returns immediately."""
    thread = threading.Thread(
        target=_run_build,
        args=(team, registry_cfg, job, build_args),
        name=f"image-build-{job.build_id}",
        daemon=True,
    )
    thread.start()


_LOG_TRUNCATED_MARKER = b"\n[oduflow] build log truncated at configured limit\n"


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
                    log.write(f"\n[oduflow] build error: {result['error']}\n")
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


def _run_build(
    team: TeamSettings,
    registry_cfg: ImageRegistrySettings,
    job: BuildJob,
    build_args: dict[str, str],
) -> None:
    cancel = store.cancel_event(job.build_id)
    log_path = store.log_path(team, job.build_id)
    ctx_dir = store.context_dir(team, job.build_id)
    build_root = ctx_dir if job.context == "." else os.path.join(ctx_dir, job.context)
    local_tag = f"{staging_repository(team.team_id)}:{job.build_id}"
    job.status = image_builds.STATUS_BUILDING
    store.save(team, job)

    max_log_bytes = registry_cfg.max_log_mb * 1024 * 1024
    deadline = time.monotonic() + registry_cfg.build_timeout_seconds
    status = image_builds.STATUS_FAILED
    error = ""
    result: dict[str, Any] = {}
    process: Any = None
    result_recv: Any = None
    result_send: Any = None
    try:
        context = multiprocessing.get_context("spawn")
        result_recv, result_send = context.Pipe(duplex=False)
        process = context.Process(
            target=_docker_build_worker,
            args=(
                build_root,
                job.dockerfile,
                local_tag,
                job.target,
                build_args,
                log_path,
                max_log_bytes,
                result_send,
            ),
            name=f"image-build-worker-{job.build_id}",
            daemon=True,
        )
        process.start()
        result_send.close()
        while process.is_alive():
            if cancel is not None and cancel.is_set():
                status = image_builds.STATUS_CANCELLED
                error = "Cancelled by request."
                _terminate_build_process(process)
                _append_capped_log(
                    log_path, "\n[oduflow] build cancelled\n", max_log_bytes
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = f"Build timed out after {registry_cfg.build_timeout_seconds}s."
                _terminate_build_process(process)
                _append_capped_log(log_path, f"\n[oduflow] {error}\n", max_log_bytes)
                break
            process.join(timeout=min(0.25, remaining))
        else:
            process.join()
        if not error and result_recv.poll(timeout=1):
            result = result_recv.recv()
            if result.get("ok"):
                status = image_builds.STATUS_SUCCEEDED
            else:
                error = str(result.get("error") or "Build failed; see the log.")
        elif not error:
            error = f"Build worker exited unexpectedly (exit {process.exitcode})."
    except Exception as exc:  # noqa: BLE001 — terminal state must always be written
        error = redact_url_credentials(str(exc))
        logger.exception("Image build %s failed", job.build_id)
        if process is not None and process.is_alive():
            _terminate_build_process(process)
    finally:
        if result_recv is not None:
            result_recv.close()
        if result_send is not None:
            result_send.close()
    if status == image_builds.STATUS_SUCCEEDED:
        job.image_id = str(result["image_id"])
        job.local_tag = local_tag
        store.finish(team, job, status)
        _prune_staging_images(team, registry_cfg.keep_images)
    else:
        store.finish(team, job, status, error or "Build failed; see the log.")
    logger.info("Image build %s finished: %s", job.build_id, job.status)


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


def _prune_staging_images(team: TeamSettings, keep: int) -> None:
    """Untag old build jobs and remove their now-unused image objects."""
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
