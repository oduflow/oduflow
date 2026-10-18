"""Per-environment MCP access tokens.

Each environment gets a personal Bearer token authorizing the scoped MCP
endpoint ``/mcp/<env>`` (single-environment access). That endpoint is
Bearer-only — the OAuth flow (client_id/secret) is wired only for the
team-wide ``/mcp`` endpoint.

The token lives in a per-team registry next to ``shares.json``:

    {"<env>": {"token": "...", "created_at": iso}}

the same model as the dashboard share secret (``oduflow.env_share``), and for
the same reason: state keyed by the environment *name* survives container
recreation, while a Docker label is frozen at container creation. Tokens
originally shipped as the ``oduflow.mcp_token`` container label, which rotated
on every recreate and broke configured MCP clients; startup migration
``0012-env-mcp-token-registry`` copies the labels here, after which the label
is inert (it is still *read* as a last-resort fallback for state restored from
a pre-registry backup, but never written).

Lifecycle: minted at environment create, carried across recreate and rename,
revoked on delete. So an MCP client configured once keeps working for as long
as an environment of that name exists. The file holds credentials, so it is
written 0600, with the same concurrency discipline as the sibling registries
(per-path thread mutex + flock, unique temp file per write).

``resolve_token`` maps an incoming token to a ``(team_id, env_name)`` pair:

- a team ``auth_token`` from settings -> ``(team_id, None)``  [full access]
- a registry token -> ``(team_id, env_name)``  [scoped]

Resolution must stay cheap on the hot path: token verification runs on *every*
HTTP request. Lookups are served from an in-memory reverse map per registry
file, revalidated with one ``os.stat`` per team and rebuilt only when a file
actually changed, so an unauthenticated token flood costs stats, not reads.
The label fallback honors the same budget: tokens found in no registry are
matched against a reverse map of the ``oduflow.mcp_token`` container labels
that is rebuilt from one Docker scan at most once per TTL window, and a hit is
adopted into the registry so later requests take the fast path.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from oduflow.settings import Settings, TeamSettings, secret_matches

logger = logging.getLogger("oduflow")

# Legacy location of the token (frozen at container creation). Written by
# Oduflow < the registry migration; read only as a fallback for containers
# whose team registry has no record.
MCP_TOKEN_LABEL = "oduflow.mcp_token"

_FILENAME = "mcp_tokens.json"

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}

_cache_lock = threading.Lock()
# path -> ((st_ino, st_mtime_ns, st_size), {token: env_name})
_cache: dict[str, tuple[tuple[int, int, int], dict[str, str]]] = {}

# Label-fallback scan cache: (monotonic time, {label token: (team_id, env)}).
# Rebuilt from one Docker scan at most once per TTL, so an unknown-token flood
# is bounded by one container listing per window, not one per request.
_LABEL_SCAN_TTL = 60.0
_label_scan_lock = threading.Lock()
_label_scan: tuple[float, dict[str, tuple[str, str]]] | None = None


def tokens_path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, _FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_token() -> str:
    """Generate a fresh access token."""
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
            # a hand-edit). Treat as "no tokens": every lookup fails closed.
            logger.warning("Ignoring malformed MCP token registry %s", path)
            return {}
        return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not load MCP token registry %s: %s", path, e)
        return {}


def _save(path: str, records: dict[str, dict[str, Any]]) -> None:
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="mcp_tokens.", suffix=".tmp", dir=dir_name)
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

    Not best-effort: a lost write here means a credential the caller is about
    to hand out would never authenticate, so OSError propagates.
    """
    if not team.data_dir:
        raise ValueError("Team data_dir is required for environment MCP tokens.")
    path = tokens_path(team)
    with _file_lock(path):
        records = _load(path)
        if fn(records):
            _save(path, records)


def get(team: TeamSettings, env_name: str) -> str | None:
    """The environment's MCP token, or None when it has none."""
    if not team.data_dir:
        return None
    record = _load(tokens_path(team)).get(env_name)
    if not record or not isinstance(record.get("token"), str):
        return None
    token: str = record["token"]
    return token


def create_or_get(team: TeamSettings, env_name: str) -> str:
    """Return the environment's MCP token, minting one on first use."""
    token = ""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        # Decide under the lock: two concurrent creates must end up with the
        # same token, and the value returned must be the one stored.
        nonlocal token
        record = records.get(env_name)
        if record and isinstance(record.get("token"), str):
            token = record["token"]
            return False
        token = generate_token()
        records[env_name] = {"token": token, "created_at": _now_iso()}
        return True

    _mutate(team, fn)
    return token


def adopt(team: TeamSettings, env_name: str, token: str) -> None:
    """Record an existing (legacy label) token, unless one is already stored."""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        record = records.get(env_name)
        if record and isinstance(record.get("token"), str):
            return False
        records[env_name] = {"token": token, "created_at": _now_iso()}
        return True

    _mutate(team, fn)


def revoke(team: TeamSettings, env_name: str) -> bool:
    """Drop the token record. Returns whether the environment had one."""
    removed = False

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        nonlocal removed
        removed = records.pop(env_name, None) is not None
        return removed

    _mutate(team, fn)
    return removed


def remove(team: TeamSettings, env_name: str) -> None:
    """Drop the record of a deleted environment (best-effort cleanup)."""
    try:
        revoke(team, env_name)
    except (OSError, ValueError) as e:
        logger.warning("Could not drop MCP token record for '%s': %s", env_name, e)


def rename(team: TeamSettings, old_name: str, new_name: str) -> None:
    """Carry the token over to a renamed environment: the ``/mcp/<env>`` path
    changes with the name, but the Bearer credential stays valid."""

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
            "Could not move MCP token record '%s' -> '%s': %s", old_name, new_name, e
        )


def _reverse_map(team: TeamSettings) -> dict[str, str]:
    """``{token: env_name}`` for one team, cached until its file changes."""
    path = tokens_path(team)
    try:
        st = os.stat(path)
    except OSError:
        with _cache_lock:
            _cache.pop(path, None)
        return {}
    # os.replace swaps in a fresh inode, so this key changes on every write
    # even within one mtime granule.
    key = (st.st_ino, st.st_mtime_ns, st.st_size)
    with _cache_lock:
        cached = _cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
    records = _load(path)
    reverse = {
        record["token"]: env_name
        for env_name, record in records.items()
        if isinstance(record.get("token"), str)
    }
    with _cache_lock:
        _cache[path] = (key, reverse)
    return reverse


def _scan_labels(settings: Settings) -> dict[str, tuple[str, str]]:
    """One Docker pass over managed containers: ``{label token: (team, env)}``.

    Best effort — no Docker (unit tests, misconfiguration) means no fallback,
    never a failed registry-token resolution.
    """
    try:
        from oduflow.docker_ops.client import get_client

        containers = get_client().containers.list(
            all=True, filters={"label": [f"{settings.managed_label}=true"]}
        )
    except Exception as e:
        logger.debug("Legacy MCP token label scan unavailable: %s", e)
        return {}
    reverse: dict[str, tuple[str, str]] = {}
    for container in containers:
        labels = container.labels or {}
        token = labels.get(MCP_TOKEN_LABEL)
        env_name = labels.get(settings.branch_label)
        team_id = labels.get(settings.team_label)
        if token and env_name and team_id in settings.teams:
            reverse[token] = (team_id, env_name)
    return reverse


def _label_fallback(settings: Settings, token: str) -> tuple[str, str] | None:
    """Last-resort resolution against the legacy ``oduflow.mcp_token`` labels.

    Covers state where the registry record is gone but the label survived: a
    team data dir restored from a pre-registry backup, or a failed migration
    adopt. A hit is adopted into the registry so the next request resolves on
    the fast path.
    """
    global _label_scan
    now = time.monotonic()
    with _label_scan_lock:
        if _label_scan is None or now - _label_scan[0] >= _LABEL_SCAN_TTL:
            _label_scan = (now, _scan_labels(settings))
        found = _label_scan[1].get(token)
    if found is None:
        return None
    team_id, env_name = found
    team = settings.teams.get(team_id)
    if team is None or not team.data_dir:
        return None
    stored = get(team, env_name)
    if stored is not None:
        # The registry already holds this environment's credential; a label
        # that differs is a rotated-out token and must not authenticate.
        return (team_id, env_name) if stored == token else None
    try:
        adopt(team, env_name, token)
    except (OSError, ValueError) as e:
        logger.warning("Could not adopt legacy MCP token for '%s': %s", env_name, e)
    return (team_id, env_name)


def resolve_token(settings: Settings, token: str) -> tuple[str, str | None] | None:
    """Resolve a presented token to ``(team_id, env_name)``.

    Returns ``(team_id, None)`` for a team ``auth_token`` (full access),
    ``(team_id, env_name)`` for a per-environment token (scoped access), or
    ``None`` if the token is unknown.

    Touches settings and the per-team registry files, plus at most one
    TTL-throttled Docker scan for the legacy label fallback; async callers
    should still prefer :func:`resolve_token_async` to keep the I/O off the
    event loop.
    """
    if not token:
        return None
    # 1. Team tokens (static, from settings) — never touches disk.
    for team_id, team in settings.teams.items():
        if team.auth_token and secret_matches(team.auth_token, token):
            return (team_id, None)
    # 2. Env tokens from the per-team registries (stat-validated cache).
    for team_id, team in settings.teams.items():
        if not team.data_dir:
            continue
        env_name = _reverse_map(team).get(token)
        if env_name is not None:
            return (team_id, env_name)
    # 3. Legacy container labels, for registries lost to a backup restore.
    return _label_fallback(settings, token)


async def resolve_token_async(
    settings: Settings, token: str
) -> tuple[str, str | None] | None:
    """Async wrapper that offloads :func:`resolve_token` to a worker thread."""
    import anyio

    return await anyio.to_thread.run_sync(resolve_token, settings, token)


def invalidate_cache() -> None:
    """Drop the cached reverse maps; the next lookup re-reads the registries."""
    global _label_scan
    with _cache_lock:
        _cache.clear()
    with _label_scan_lock:
        _label_scan = None
