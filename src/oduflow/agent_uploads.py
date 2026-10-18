"""Persistent file attachments for browser Agent Chat prompts."""

from __future__ import annotations

import io
import os
import re
import secrets
import tarfile
from typing import Any, BinaryIO
from urllib.parse import quote

import docker
from oduflow import agent_uids
from oduflow.docker_ops.client import get_client
from oduflow.docker_ops.env_ops import agent_exec_identity
from oduflow.errors import FlowError, NotFoundError
from oduflow.naming import (
    get_agent_container_name,
    get_agent_upload_dir,
    get_resource_name,
    slugify_branch,
)
from oduflow.settings import Settings, TeamSettings

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ENV_BYTES = 100 * 1024 * 1024
MAX_FILES_PER_PROMPT = 5

_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MIME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.+-]*/[a-zA-Z0-9][a-zA-Z0-9.+-]*$")


class AttachmentError(FlowError):
    """A user-visible attachment failure with an HTTP status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_filename(filename: str) -> tuple[str, str]:
    """Return safe display and storage names for an untrusted browser name."""
    display_name = os.path.basename(filename.replace("\\", "/")).strip()
    display_name = "".join(
        char for char in display_name if char >= " " and char != "\x7f"
    )
    if not display_name or display_name in {".", ".."}:
        raise AttachmentError("A valid file name is required.")
    display_name = display_name[:180]

    storage_name = re.sub(r"[^\w. -]", "_", display_name, flags=re.UNICODE)
    storage_name = re.sub(r"\s+", "_", storage_name).strip(" ._")
    if not storage_name:
        storage_name = "attachment"
    stem, suffix = os.path.splitext(storage_name)
    storage_name = (stem[:140] or "attachment") + suffix[:20]
    return display_name, storage_name


def normalize_mime_type(mime_type: str | None) -> str:
    candidate = (mime_type or "").split(";", 1)[0].strip()
    if len(candidate) <= 127 and _MIME_RE.fullmatch(candidate):
        return candidate.lower()
    return "application/octet-stream"


def _decode_output(output: Any) -> str:
    if isinstance(output, (bytes, bytearray)):
        return output.decode("utf-8", "replace")
    return str(output or "")


def _agent_container(
    settings: Settings, team: TeamSettings, env_name: str
) -> tuple[Any, int]:
    """The team's agent container and the environment's own uid.

    Attachments belong to the environment's uid (see
    specs/0079-per-environment-agent-identities.md), so every operation on
    them runs as that uid and no other environment's session can read them.
    """
    if not team.agent_enabled:
        raise AttachmentError("The coding agent is disabled for this team.", 403)
    if not slugify_branch(env_name):
        raise AttachmentError("Invalid environment name.")

    client = get_client()
    env_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        client.containers.get(env_container_name)
    except docker.errors.NotFound as exc:
        raise NotFoundError(f"Environment '{env_name}' does not exist.") from exc

    try:
        container = client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        )
    except docker.errors.NotFound as exc:
        raise AttachmentError("The coding-agent container was not found.", 503) from exc
    if container.status != "running":
        raise AttachmentError("The coding-agent container is not running.", 503)
    uid = agent_uids.get_uid(team, env_name)
    if uid is None:
        raise AttachmentError(
            "The coding agent is not set up for this environment yet; "
            "open Agent Chat first.",
            409,
        )
    return container, uid


def _exec_as_env(container: Any, cmd: list[str], uid: int) -> Any:
    user, env = agent_exec_identity(uid)
    return container.exec_run(cmd, user=user, environment=env)


def _directory_size(container: Any, path: str, uid: int) -> int:
    code, output = _exec_as_env(container, ["du", "-sb", path], uid)
    if code not in (0, None):
        return 0
    try:
        return int(_decode_output(output).split(None, 1)[0])
    except (ValueError, IndexError):
        return 0


def _attachment_archive(
    upload_id: str, storage_name: str, source: BinaryIO, size: int, uid: int
) -> bytes:
    # Docker extracts with the header ownership: the environment's uid.
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        directory = tarfile.TarInfo(upload_id)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o700
        directory.uid = uid
        directory.gid = uid
        directory.uname = f"env{uid}"
        directory.gname = f"env{uid}"
        tar.addfile(directory)

        item = tarfile.TarInfo(f"{upload_id}/{storage_name}")
        item.size = size
        item.mode = 0o600
        item.uid = uid
        item.gid = uid
        item.uname = f"env{uid}"
        item.gname = f"env{uid}"
        source.seek(0)
        tar.addfile(item, source)
    return archive.getvalue()


def store_attachment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    filename: str,
    mime_type: str | None,
    source: BinaryIO,
    size: int,
) -> dict[str, object]:
    """Copy one validated attachment into the team's agent workspace volume."""
    if size < 0 or size > MAX_FILE_BYTES:
        raise AttachmentError(
            f"Files must be no larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB.",
            413,
        )
    display_name, storage_name = normalize_filename(filename)
    mime_type = normalize_mime_type(mime_type)
    container, uid = _agent_container(settings, team, env_name)
    upload_root = get_agent_upload_dir(env_name)

    code, _output = _exec_as_env(container, ["mkdir", "-p", upload_root], uid)
    if code not in (0, None):
        raise AttachmentError("Could not prepare attachment storage.", 500)
    if _directory_size(container, upload_root, uid) + size > MAX_ENV_BYTES:
        raise AttachmentError(
            "Attachment storage for this environment is full "
            f"({MAX_ENV_BYTES // (1024 * 1024)} MiB limit).",
            413,
        )

    upload_id = secrets.token_hex(16)
    archive = _attachment_archive(upload_id, storage_name, source, size, uid)
    if not container.put_archive(upload_root, archive):
        raise AttachmentError("Could not copy the attachment to the agent.", 500)

    path = f"{upload_root}/{upload_id}/{storage_name}"
    return {
        "id": upload_id,
        "name": display_name,
        "path": path,
        "uri": "file://" + quote(path, safe="/"),
        "mimeType": mime_type,
        "size": size,
    }


def delete_attachment(
    settings: Settings, team: TeamSettings, env_name: str, upload_id: str
) -> None:
    """Delete one unsent attachment directory from the agent workspace."""
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise AttachmentError("Invalid attachment id.")
    container, uid = _agent_container(settings, team, env_name)
    path = f"{get_agent_upload_dir(env_name)}/{upload_id}"
    code, _output = _exec_as_env(container, ["rm", "-rf", "--", path], uid)
    if code not in (0, None):
        raise AttachmentError("Could not remove the attachment.", 500)
