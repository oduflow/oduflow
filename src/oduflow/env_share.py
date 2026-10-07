"""Per-environment dashboard share secrets.

An operator can hand a client a link to *one* environment's dashboard —
``https://<team-host>/env/<name>?key=<secret>`` — which opens the scoped UI
described in ``oduflow.ui_scope``. This module owns the secret behind that
link.

The secret is deliberately NOT a Docker label (the model used by
``env_tokens`` for the scoped MCP endpoint): labels cannot be added to a live
container, so environments created before this feature would never be
shareable. It lives instead in a per-team registry next to ``ports.json`` and
``activity.json``:

    {"<env>": {"secret": "...", "created_at": iso}}

That keeps sharing available for existing environments, survives container
recreation, and gives the operator per-environment revoke and rotate. The file
holds credentials, so it is written 0600 (the sibling registries are 0644).

Writes follow the same concurrency discipline as those registries: a per-path
mutex for threads plus an flock for sibling processes, and a unique temp file
per write.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from oduflow.settings import TeamSettings, secret_matches

logger = logging.getLogger("oduflow")

_FILENAME = "shares.json"

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def shares_path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, _FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_secret() -> str:
    """Generate a fresh share secret (the ``key`` in a share link)."""
    return secrets.token_urlsafe(32)


def _thread_lock(path: str) -> threading.Lock:
    with _locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[path] = lock
        return lock


@contextmanager
def _file_lock(path: str) -> Iterator[None]:
    with _thread_lock(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _load(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            # Valid JSON of the wrong shape (a truncated write leaving `null`,
            # a hand-edit). Treat as "no shares": every link then fails closed.
            logger.warning("Ignoring malformed share registry %s", path)
            return {}
        return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not load share registry %s: %s", path, e)
        return {}


def _save(path: str, records: dict[str, dict[str, Any]]) -> None:
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="shares.", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(records, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _mutate(
    team: TeamSettings, fn: Callable[[dict[str, dict[str, Any]]], bool]
) -> None:
    """Run ``fn(records)`` under the lock; save when it returns True.

    Unlike activity tracking this is not best-effort: a failed write means the
    operator would be shown a link that does not work, so OSError propagates.
    """
    if not team.data_dir:
        raise ValueError("Team data_dir is required for environment sharing.")
    path = shares_path(team)
    with _file_lock(path):
        records = _load(path)
        if fn(records):
            _save(path, records)


def get(team: TeamSettings, env_name: str) -> dict[str, Any] | None:
    """The share record for an environment, or None when it is not shared."""
    if not team.data_dir:
        return None
    record = _load(shares_path(team)).get(env_name)
    if not record or not isinstance(record.get("secret"), str):
        return None
    return record


def create_or_get(team: TeamSettings, env_name: str) -> str:
    """Return the environment's share secret, minting one on first use."""
    existing = get(team, env_name)
    if existing:
        secret: str = existing["secret"]
        return secret
    fresh = generate_secret()

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        # Re-check under the lock: two operators opening the Share modal at
        # once must end up with the same link, not overwrite each other.
        record = records.get(env_name)
        if record and isinstance(record.get("secret"), str):
            return False
        records[env_name] = {"secret": fresh, "created_at": _now_iso()}
        return True

    _mutate(team, fn)
    current = get(team, env_name)
    return current["secret"] if current else fresh


def rotate(team: TeamSettings, env_name: str) -> str:
    """Replace the share secret, invalidating the old link and live sessions."""
    fresh = generate_secret()

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        records[env_name] = {"secret": fresh, "created_at": _now_iso()}
        return True

    _mutate(team, fn)
    return fresh


def revoke(team: TeamSettings, env_name: str) -> bool:
    """Drop the share record. Returns whether the environment was shared."""
    removed = False

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        nonlocal removed
        removed = records.pop(env_name, None) is not None
        return removed

    _mutate(team, fn)
    return removed


def verify(team: TeamSettings, env_name: str, key: str) -> bool:
    """Whether ``key`` is the environment's current share secret."""
    if not key:
        return False
    record = get(team, env_name)
    if not record:
        return False
    return secret_matches(str(record["secret"]), key)


def remove(team: TeamSettings, env_name: str) -> None:
    """Drop the record of a deleted environment (best-effort cleanup)."""
    try:
        revoke(team, env_name)
    except (OSError, ValueError) as e:
        logger.warning("Could not drop share record for '%s': %s", env_name, e)


def rename(team: TeamSettings, old_name: str, new_name: str) -> None:
    """Carry a share over to a renamed environment: the link's path changes
    with the name, but the secret the operator already handed out stays
    valid."""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        record = records.pop(old_name, None)
        if record is None:
            return False
        records[new_name] = record
        return True

    try:
        _mutate(team, fn)
    except (OSError, ValueError) as e:
        logger.warning(
            "Could not move share record '%s' -> '%s': %s", old_name, new_name, e
        )
