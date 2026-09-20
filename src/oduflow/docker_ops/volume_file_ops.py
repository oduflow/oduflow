"""File operations inside Docker volumes.

Uses temporary Alpine containers to read, write, search, and delete files
inside Docker volumes without requiring a running service container.
"""

from __future__ import annotations

import io
import logging
import os
import shlex
import tarfile
import time
from typing import Any

import docker
from oduflow.docker_ops.client import get_client
from oduflow.docker_ops.volume_ops import docker_volume_name
from oduflow.errors import ConflictError, NotFoundError
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_HELPER_IMAGE = "alpine:latest"
_MOUNT_POINT = "/mnt/volume"
_FILE_SIZE_LIMIT = 100 * 1024  # 100 KB
_WRITE_FILE_LIMIT = 1_000_000  # 1 MB


def _validate_volume(team: TeamSettings, name: str) -> tuple[str, docker.DockerClient]:
    """Validate that a volume exists and return its Docker name and client."""
    client = get_client()
    docker_name = docker_volume_name(team, name)
    try:
        client.volumes.get(docker_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Volume '{name}' not found.")
    return docker_name, client


def _safe_path(path: str) -> str:
    """Resolve a user-provided path to an absolute path inside the mount.

    Prevents path traversal attacks by normalising ``..`` segments and
    verifying the result stays within ``_MOUNT_POINT``.
    """
    clean = path.lstrip("/")
    if not clean:
        return _MOUNT_POINT
    full = os.path.normpath(os.path.join(_MOUNT_POINT, clean))
    if not (full == _MOUNT_POINT or full.startswith(_MOUNT_POINT + "/")):
        raise ConflictError(f"Path '{path}' escapes the volume mount point.")
    return full


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_file_in_volume(
    settings: Settings,
    team: TeamSettings,
    name: str,
    path: str,
    read_range: str = "",
    *,
    max_bytes: int = _FILE_SIZE_LIMIT,
) -> dict[str, Any]:
    """Read a text file or list a directory inside a Docker volume."""
    docker_name, client = _validate_volume(team, name)
    full_path = _safe_path(path)

    # Validate read_range early (before spinning up a container).
    start = end = 0
    if read_range:
        parts = read_range.split(":")
        if len(parts) != 2:
            return {
                "error": f"Invalid read_range format: '{read_range}'. Expected 'START:END' (e.g. '1:50')."
            }
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            return {
                "error": f"Invalid read_range format: '{read_range}'. START and END must be integers."
            }

    # Build a single shell script that handles all cases.
    if read_range:
        read_cmd = f"sed -n '{start},{end}p' \"$FILE\""
    else:
        read_cmd = 'cat "$FILE"'

    quoted = shlex.quote(full_path)
    script = f"""\
FILE={quoted}
if [ -d "$FILE" ]; then
    echo "TYPE:DIR"
    ls -la "$FILE"
elif [ -f "$FILE" ]; then
    SIZE=$(wc -c < "$FILE")
    # Binary detection: look for null bytes in first 512 bytes
    NULL_COUNT=$(head -c 512 "$FILE" | tr -cd '\\0' | wc -c)
    if [ "$NULL_COUNT" -gt 0 ]; then
        echo "TYPE:BINARY"
        echo "SIZE:$SIZE"
    elif [ {0 if read_range else 1} -eq 1 ] && [ "$SIZE" -gt {max_bytes} ]; then
        echo "TYPE:TOOLARGE"
        echo "SIZE:$SIZE"
    else
        echo "TYPE:FILE"
        echo "SIZE:$SIZE"
        echo "CONTENT_START"
        {read_cmd}
    fi
else
    echo "TYPE:NOTFOUND"
fi
"""

    logger.info("Reading file in volume %s: %s", name, path)
    try:
        output = client.containers.run(
            _HELPER_IMAGE,
            ["sh", "-c", script],
            entrypoint="",
            user="root",
            remove=True,
            volumes={docker_name: {"bind": _MOUNT_POINT, "mode": "ro"}},
        )
    except docker.errors.ContainerError as exc:
        stderr = exc.stderr or b""
        return {
            "error": stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes)
            else str(stderr)
        }

    raw = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    lines = raw.split("\n", 3)  # split header lines
    type_line = lines[0] if lines else ""

    if type_line == "TYPE:NOTFOUND":
        return {"error": f"Path not found: {path}"}

    if type_line == "TYPE:DIR":
        dir_output = "\n".join(lines[1:]) if len(lines) > 1 else ""
        return {"type": "directory", "output": dir_output}

    if type_line == "TYPE:BINARY":
        return {
            "error": "Binary file, cannot display. Use run_service_command for binary file operations."
        }

    if type_line == "TYPE:TOOLARGE":
        size_str = lines[1].replace("SIZE:", "").strip() if len(lines) > 1 else "?"
        size_kb = int(size_str) // 1024 if size_str.isdigit() else "?"
        limit_kb = max_bytes // 1024
        return {
            "error": (
                f"File is too large ({size_kb}KB, limit {limit_kb}KB). "
                'Use read_range parameter to read a specific portion, e.g. read_range="1:500".'
            )
        }

    if type_line == "TYPE:FILE":
        size_str = lines[1].replace("SIZE:", "").strip() if len(lines) > 1 else "0"
        file_size = int(size_str) if size_str.isdigit() else 0
        # Content starts after "CONTENT_START" marker
        content_start_idx = raw.find("CONTENT_START\n")
        content = (
            raw[content_start_idx + len("CONTENT_START\n") :]
            if content_start_idx >= 0
            else ""
        )
        result: dict[str, Any] = {"type": "file", "output": content, "size": file_size}
        if read_range:
            result["range"] = read_range
        return result

    return {"error": f"Unexpected output: {raw[:200]}"}


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_file_in_volume(
    settings: Settings,
    team: TeamSettings,
    name: str,
    path: str,
    content: str,
) -> dict[str, Any]:
    """Write a text file inside a Docker volume via tar stream."""
    data = content.encode("utf-8")
    if len(data) > _WRITE_FILE_LIMIT:
        raise ConflictError(
            f"Content exceeds {_WRITE_FILE_LIMIT} byte limit.",
        )

    docker_name, client = _validate_volume(team, name)
    full_path = _safe_path(path)
    parent = full_path.rsplit("/", 1)[0] if "/" in full_path else "/"
    filename = full_path.rsplit("/", 1)[-1]

    logger.info("Writing file in volume %s: %s (%d bytes)", name, path, len(data))

    container = client.containers.run(
        _HELPER_IMAGE,
        "sleep 30",
        entrypoint="",
        user="root",
        detach=True,
        remove=False,
        volumes={docker_name: {"bind": _MOUNT_POINT, "mode": "rw"}},
    )
    try:
        container.exec_run(["mkdir", "-p", parent], user="root")

        tar_stream = io.BytesIO()
        with tarfile.open(
            fileobj=tar_stream, mode="w", format=tarfile.PAX_FORMAT
        ) as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            # TarInfo defaults to epoch zero, which leaves mtime-based caches
            # stale after replacement. PAX retains fractional seconds so rapid
            # consecutive writes also invalidate file-hash caches such as Salt's.
            info.mtime = time.time()
            tar.addfile(info, io.BytesIO(data))
        tar_stream.seek(0)
        container.put_archive(parent, tar_stream)
    finally:
        container.stop(timeout=1)
        container.remove(force=True)

    return {"path": path, "size": len(data)}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_in_volume(
    settings: Settings,
    team: TeamSettings,
    name: str,
    pattern: str,
    path: str = "",
    glob: str = "*",
    max_results: int = 50,
) -> dict[str, Any]:
    """Search for a pattern in files inside a Docker volume."""
    docker_name, client = _validate_volume(team, name)
    search_path = _safe_path(path) if path else _MOUNT_POINT

    # The helper image ships busybox grep, which has no GNU `--include` (it
    # exits 2 on the unknown option, so every search failed). Stick to POSIX/
    # busybox primitives: a plain recursive grep for the default glob, and a
    # find|grep composition for a real filename filter. `--` guards against a
    # pattern that starts with a dash. No shell — args stay an exec array.
    if glob in ("", "*"):
        cmd = ["grep", "-rnH", "-F", "--", pattern, search_path]
    else:
        # `find -exec ... {} +` propagates grep's no-match exit 1, which the
        # handler below already treats as zero matches; no matching files at
        # all simply yields exit 0 with empty output.
        cmd = [
            "find",
            search_path,
            "-type",
            "f",
            "-name",
            glob,
            "-exec",
            "grep",
            "-nH",
            "-F",
            "--",
            pattern,
            "{}",
            "+",
        ]

    logger.info(
        "Searching in volume %s: pattern=%s path=%s glob=%s",
        name,
        pattern,
        search_path,
        glob,
    )

    try:
        output = client.containers.run(
            _HELPER_IMAGE,
            cmd,
            entrypoint="",
            user="root",
            remove=True,
            volumes={docker_name: {"bind": _MOUNT_POINT, "mode": "ro"}},
        )
    except docker.errors.ContainerError as exc:
        # grep returns exit code 1 when no matches — that's normal
        if exc.exit_status == 1:
            return {"matches": 0, "output": "", "truncated": False}
        raise

    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    # Strip the mount-point prefix from paths so output is relative to volume root
    output_str = output_str.replace(_MOUNT_POINT + "/", "")

    all_lines = output_str.strip().splitlines() if output_str.strip() else []
    lines = all_lines[:max_results]

    return {
        "matches": len(lines),
        "output": "\n".join(lines),
        "truncated": len(all_lines) > max_results,
    }


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def delete_file_in_volume(
    settings: Settings,
    team: TeamSettings,
    name: str,
    path: str,
) -> dict[str, Any]:
    """Delete a file or directory inside a Docker volume."""
    docker_name, client = _validate_volume(team, name)
    full_path = _safe_path(path)

    if full_path == _MOUNT_POINT:
        raise ConflictError(
            "Cannot delete the volume root. Specify a path within the volume."
        )

    quoted = shlex.quote(full_path)
    script = f"""\
if [ ! -e {quoted} ]; then
    echo "NOTFOUND"
else
    rm -rf {quoted} && echo "DELETED" || echo "FAILED"
fi
"""

    logger.info("Deleting in volume %s: %s", name, path)
    try:
        output = client.containers.run(
            _HELPER_IMAGE,
            ["sh", "-c", script],
            entrypoint="",
            user="root",
            remove=True,
            volumes={docker_name: {"bind": _MOUNT_POINT, "mode": "rw"}},
        )
    except docker.errors.ContainerError as exc:
        stderr = exc.stderr or b""
        msg = (
            stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes)
            else str(stderr)
        )
        return {"error": f"Failed to delete: {msg}"}

    result_str = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()

    if result_str == "NOTFOUND":
        return {"error": f"Path not found: {path}"}
    if result_str == "DELETED":
        return {"path": path, "status": "deleted"}
    return {"error": f"Failed to delete: {path}"}
