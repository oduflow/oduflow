import logging
import os
import re
import time
from typing import Any, cast

import docker
from docker import DockerClient
from oduflow.errors import FlowError, PrerequisiteNotMetError

logger = logging.getLogger("oduflow")

_uid_gid_cache: dict[str, str] = {}
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{12,64}\b")
# Absolute paths: host mount sources, /var/lib/docker/volumes/..., and the
# "/name" form Docker uses when it quotes a container name.
_ABS_PATH_RE = re.compile(r"(?<![\w.-])/[\w.@:+-]+(?:/[\w.@:+-]+)*")


def docker_error_detail(exc: docker.errors.DockerException) -> str:
    """Return the actionable Docker detail without the SDK HTTP wrapper.

    ``APIError.__str__`` prefixes the daemon's explanation with the Docker API
    URL, status code and response reason.  Those details are useful in server
    logs but noisy (and unnecessarily revealing) in an MCP response.  The
    daemon explanation is the part that tells an agent what it can fix.

    Only use this where the explanation describes caller-supplied parameters
    (a service image, port or volume), and even there hex IDs and absolute
    paths are scrubbed: the daemon quotes container names as ``/name`` and
    mount failures include host and ``/var/lib/docker`` paths.  Elsewhere
    Docker errors stay masked by ``handle_errors``.
    """
    explanation = getattr(exc, "explanation", None)
    if isinstance(explanation, bytes):
        explanation = explanation.decode("utf-8", errors="replace")
    if explanation:
        # Container/image IDs and absolute paths are internal identifiers with
        # no value to a caller; drop them even from caller-facing explanations.
        detail = _HEX_ID_RE.sub("<id>", str(explanation).strip())
        return _ABS_PATH_RE.sub("<path>", detail)

    if isinstance(exc, docker.errors.APIError):
        status = exc.status_code
        reason = getattr(exc.response, "reason", "") if exc.response is not None else ""
        status_detail = " ".join(str(part) for part in (status, reason) if part)
        return (
            f"Docker API request failed ({status_detail})."
            if status_detail
            else "Docker API request failed."
        )

    detail = str(exc).strip()
    return detail or exc.__class__.__name__


def docker_operation_error(
    action: str, exc: docker.errors.DockerException
) -> FlowError:
    """Translate an expected Docker SDK failure into a client-safe FlowError."""
    return FlowError(f"Docker failed to {action}: {docker_error_detail(exc)}")


def get_client() -> DockerClient:
    try:
        return docker.from_env()
    except docker.errors.DockerException as exc:
        # Raise a FlowError (not SystemExit/BaseException) so @handle_errors and
        # the create_environment fallback can catch it and return a clean error
        # instead of tearing down the request task or the server loop. The CLI
        # entry point translates this into a friendly process exit.
        raise PrerequisiteNotMetError(
            "Cannot connect to Docker. Please make sure Docker is installed "
            "and running. https://docs.docker.com/get-docker/"
        ) from exc


def run_for_output(
    client: DockerClient,
    image: str,
    command: str | list[str],
    *,
    stdout: bool = True,
    stderr: bool = False,
    **kwargs: Any,
) -> bytes:
    """Run a throwaway container to completion and return its stdout.

    docker-py's ``containers.run()`` reads the output back only for containers
    that log through ``json-file`` or ``journald``; under any other daemon
    default (``local`` is a common one) it silently returns ``None``. Pin
    ``json-file`` for these short-lived helpers so parsing never depends on the
    host's logging configuration. A non-zero exit still raises
    ``docker.errors.ContainerError``.
    """
    # run(remove=True) removes the container but not anonymous volumes
    # declared by its image. Daemon-side auto_remove races log collection.
    # Keep the handle from create through start/wait/logs, even on failure.
    kwargs["log_config"] = {"type": "json-file"}
    try:
        container = client.containers.create(image=image, command=command, **kwargs)
    except docker.errors.ImageNotFound:
        client.images.pull(image, platform=kwargs.get("platform"))
        container = client.containers.create(image=image, command=command, **kwargs)

    completed = False
    try:
        container.start()
        status = container.wait()["StatusCode"]
        if status != 0:
            error_output = container.logs(stdout=False, stderr=True)
            raise docker.errors.ContainerError(
                container, status, command, image, error_output
            )
        output = container.logs(stdout=stdout, stderr=stderr)
        completed = True
        return cast(bytes, output)
    finally:
        try:
            container.remove(force=True, v=True)
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError:
            if completed:
                raise
            logger.warning(
                "Could not remove failed helper %s", container.id, exc_info=True
            )


def pull_with_progress(client: DockerClient, image: str, label: str) -> None:
    """Pull *image*, logging the daemon's progress at most every 15 seconds.

    ``images.pull`` drains the progress stream silently, so a large image (the
    coder image is ~1 GB compressed) shows no sign of life for minutes; during
    startup that silence would also trip the startup watchdog. An error the
    daemon reports inside the stream raises like a failed request.
    """
    last_progress = 0.0
    for event in client.api.pull(image, stream=True, decode=True):
        if event.get("error"):
            raise PrerequisiteNotMetError(f"Cannot pull {image}: {event['error']}")
        now = time.monotonic()
        if event.get("status") and (
            "progressDetail" not in event or now - last_progress >= 15
        ):
            detail = (event["status"], event.get("id"), event.get("progress"))
            logger.info("%s: %s", label, " ".join(str(p) for p in detail if p))
            last_progress = now


def wait_for_docker(timeout: float = 60.0, poll_seconds: float = 2.0) -> None:
    """Block until the Docker daemon answers, or raise after *timeout*.

    The unit is ordered after ``docker.service``, but a host-wide restart batch
    (unattended-upgrades restarting containerd, say) can still hand us a socket
    that exists while the daemon behind it is not answering yet. Retrying for a
    minute turns that race into a slower start instead of a failed one.
    """
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        try:
            get_client().ping()
            if announced:
                logger.info("Docker daemon is answering")
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise PrerequisiteNotMetError(
                    f"The Docker daemon did not answer within {timeout:.0f}s: {exc}"
                ) from exc
            if not announced:
                logger.info("Waiting for the Docker daemon to answer (%s)", exc)
                announced = True
            time.sleep(poll_seconds)


def get_odoo_uid_gid(client: DockerClient, image: str) -> str:
    if image in _uid_gid_cache:
        return _uid_gid_cache[image]
    try:
        raw = run_for_output(client, image, "id", entrypoint="").decode().strip()
        import re

        uid_m = re.search(r"uid=(\d+)", raw)
        gid_m = re.search(r"gid=(\d+)", raw)
        if uid_m and gid_m:
            result = f"{uid_m.group(1)}:{gid_m.group(1)}"
        else:
            raise ValueError(f"unexpected id output: {raw}")
    except Exception as e:
        # Not cached: a transient failure (daemon hiccup, image pull error)
        # must not pin the fallback for this image until the server restarts —
        # the next call gets another chance at the real lookup.
        logger.warning(
            "Could not detect UID:GID from image %s: %s, falling back to 100:101",
            image,
            e,
        )
        return "100:101"
    _uid_gid_cache[image] = result
    logger.debug("Detected UID:GID for %s: %s", image, result)
    return result


def chown_recursive(
    path: str, uid: int, gid: int, client: DockerClient, image: str
) -> None:
    """Recursively chown *path* to *uid*:*gid*.

    Tries host-side ``os.chown`` first (works on Linux as root).
    On ``PermissionError`` (e.g. macOS) falls back to running
    ``chown -hR`` inside a throwaway container with *path* bind-mounted.

    The trees given away here are filestores and session directories, which
    their tenant writes to. A symbolic link in one is given away as a link and
    never followed: following it would hand whatever host file it names to the
    Odoo account. Each entry is addressed through its directory's descriptor,
    so swapping a directory for a link while the walk runs gains nothing.
    """
    top = os.path.realpath(path)
    if not os.path.isdir(top):
        # As before: a tree that is not there has nothing to give away.
        return
    try:
        os.chown(top, uid, gid)
        for _root, dirs, files, dir_fd in os.fwalk(top):
            for name in dirs + files:
                os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
    except PermissionError:
        logger.info(
            "Host chown failed (PermissionError), falling back to in-container chown for %s",
            path,
        )
        run_for_output(
            client,
            image,
            f"chown -hR {uid}:{gid} /mnt/target",
            entrypoint="",
            user="root",
            volumes={path: {"bind": "/mnt/target", "mode": "rw"}},
        )


def container_env(container: Any) -> dict[str, str]:
    """Config.Env of a container as a dict — the values it actually runs with."""
    env_vars: dict[str, str] = {}
    for entry in container.attrs.get("Config", {}).get("Env", []) or []:
        if "=" in entry:
            key, value = entry.split("=", 1)
            env_vars[key] = value
    return env_vars
