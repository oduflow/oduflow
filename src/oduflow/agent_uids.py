"""Per-environment Linux identities inside the team's coding-agent container.

One agent container serves every environment of a team, but each
environment's console/chat sessions, checkout, attachments and HOME belong to
that environment's own unprivileged uid. The kernel then keeps environments
apart: a session cannot read another environment's ``/proc/<pid>/environ``
(its scoped MCP token), checkout or transcripts. See
specs/0079-per-environment-agent-identities.md.

A single JSON bag per team at ``<team.data_dir>/agent_uids.json``::

    {"next_uid": 20003, "envs": {"main": 20000, "feature/x": 20002},
     "pending_removal": {"20001": "old-feature"}}

Uids are never reused: files a deleted environment left behind (e.g. in
``/tmp``) must not become readable by a later environment that would inherit
its uid, so ``next_uid`` only grows. A rename keeps the uid — the checkout and
HOME it owns move with the environment. A deleted environment's uid waits in
``pending_removal`` (uid -> slug) until its checkout, attachments and HOME
are actually gone from the agent container, so a cleanup that could not run
at deletion time is retried before the slug is provisioned again. The server
runs as a single process, so a module-level lock guards the
read-modify-write.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from oduflow.errors import FlowError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import slugify_branch
from oduflow.settings import TeamSettings

# Well above the image's own accounts (agent = 1000) and Debian's regular
# user range, so an environment can never alias an account baked into the
# coder image.
UID_BASE = 20000

_LOCK = threading.Lock()


class AgentUidRegistryError(FlowError):
    """The registry exists but cannot be trusted."""


def _path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, "agent_uids.json")


def _load(team: TeamSettings) -> tuple[int, dict[str, int], dict[int, str]]:
    """Return ``(next_uid, envs, pending_removal)``.

    A missing file is an empty registry. An unreadable or malformed one is an
    error, never a silent reset: starting over at ``UID_BASE`` would hand an
    existing environment's uid — and with it its checkout, HOME and login — to
    a different environment.
    """
    path = _path(team)
    if not os.path.isfile(path):
        return UID_BASE, {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            data: Any = json.load(f)
        envs = {str(name): int(uid) for name, uid in data["envs"].items()}
        pending = {
            int(uid): str(slug) for uid, slug in data.get("pending_removal", {}).items()
        }
        next_uid = int(data["next_uid"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AgentUidRegistryError(
            f"Agent uid registry {path} is unreadable ({exc}); fix or restore "
            "it before using the coding agent."
        ) from exc
    # Never hand out a uid at or below one already assigned, even if the
    # counter was hand-edited backwards.
    next_uid = max(
        [next_uid, UID_BASE, *(uid + 1 for uid in [*envs.values(), *pending])]
    )
    return next_uid, envs, pending


def _save(
    team: TeamSettings,
    next_uid: int,
    envs: dict[str, int],
    pending: dict[int, str],
) -> None:
    data: dict[str, Any] = {"next_uid": next_uid, "envs": envs}
    if pending:
        data["pending_removal"] = {str(uid): slug for uid, slug in pending.items()}
    os.makedirs(team.data_dir, exist_ok=True)
    atomic_write_private_json(_path(team), data)


def get_uid(team: TeamSettings, env_name: str) -> int | None:
    """Return the environment's uid, or None if it has none yet."""
    with _LOCK:
        return _load(team)[1].get(env_name)


def allocate_uid(team: TeamSettings, env_name: str) -> int:
    """Return the environment's uid, assigning the next fresh one if needed."""
    with _LOCK:
        next_uid, envs, pending = _load(team)
        uid = envs.get(env_name)
        if uid is not None:
            return uid
        envs[env_name] = next_uid
        _save(team, next_uid + 1, envs, pending)
        return next_uid


def rename_env(team: TeamSettings, old_name: str, new_name: str) -> None:
    """Move an environment's uid to its new name.

    A renamed environment is the same environment: its checkout, attachments
    and HOME are owned by this uid and move with it. A stale entry already
    under the new name is replaced (its uid is simply never used again).
    """
    with _LOCK:
        next_uid, envs, pending = _load(team)
        uid = envs.pop(old_name, None)
        if uid is None:
            return
        envs[new_name] = uid
        _save(team, next_uid, envs, pending)


def retire_uid(team: TeamSettings, env_name: str) -> int | None:
    """Retire a deleted environment's uid; return it (None if it had none).

    The uid is never handed out again. It stays in ``pending_removal`` with
    the environment's slug until :func:`forget_removed` confirms that its
    checkout, attachments and HOME are gone from the agent container.
    """
    with _LOCK:
        next_uid, envs, pending = _load(team)
        uid = envs.pop(env_name, None)
        if uid is None:
            return None
        pending[uid] = slugify_branch(env_name)
        _save(team, next_uid, envs, pending)
        return uid


def pending_removals(team: TeamSettings) -> dict[int, str]:
    """Retired uids whose leftovers still have to be removed: ``{uid: slug}``."""
    with _LOCK:
        return _load(team)[2]


def forget_removed(team: TeamSettings, uid: int) -> None:
    """Drop a retired uid once its leftovers are gone from the agent container."""
    with _LOCK:
        next_uid, envs, pending = _load(team)
        if pending.pop(uid, None) is None:
            return
        _save(team, next_uid, envs, pending)
