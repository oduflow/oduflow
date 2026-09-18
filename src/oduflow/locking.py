"""Granular lock manager for concurrent MCP tool execution.

Provides per-resource, per-team, and system-level locks so that operations on
different environments / services / volumes / teams can run in parallel while
operations on the same resource are serialised.

``acquire_env`` is a generic keyed lock: the key is an environment name for
environment tools, and a namespaced resource key (``svc:``, ``vol:``, ``prod:``,
…) for everything else. Only keys acquired *with* a ``team_id`` take part in the
team↔environment mutual exclusion, so narrow resource keys never block — and are
never blocked by — a team-wide operation.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

from oduflow.errors import BusyError

# Keys in the production keyspace. The system lock (cluster-wide PITR) and every
# `prod:` key are mutually exclusive; see acquire_system.
PROD_KEY_PREFIX = "prod:"


def _is_prod_key(key: str) -> bool:
    return key.startswith(PROD_KEY_PREFIX)


# -- resource lock keys -------------------------------------------------------
#
# One place builds every key string, because MCP tools (server.py) and the REST
# dashboard (web_ui.py) must lock the *same* resource under the *same* key or
# they simply do not see each other. Team-scoped builders take an unused `name`
# so every builder shares one signature and can be handed to `with_key_lock`.


def prod_lock_key(team_id: str, name: str) -> str:
    """Lock key for a production — team-scoped so two teams' same-named
    productions never contend (unlike raw env keys)."""
    return f"{PROD_KEY_PREFIX}{team_id}:{name}"


def service_lock_key(team_id: str, name: str) -> str:
    """One auxiliary service — create/update/delete/restart/exec/restore."""
    return f"svc:{team_id}:{name}"


def service_database_lock_key(team_id: str, name: str) -> str:
    """One PostgreSQL database managed for auxiliary services."""
    return f"svc-db:{team_id}:{name}"


def service_database_registry_key(team_id: str) -> str:
    """The team's *set* of managed databases: quota admission.

    Distinct from :func:`service_registry_key` so creating a database never
    contends with creating a service container or deleting a volume.
    """
    return f"svc-db-registry:{team_id}"


def service_registry_key(team_id: str, name: str = "") -> str:
    """The team's *set* of services: slot admission and volume-usage checks."""
    return f"svc-registry:{team_id}"


def volume_lock_key(team_id: str, name: str) -> str:
    """One managed Docker volume, including writes to files inside it."""
    return f"vol:{team_id}:{name}"


def service_preset_lock_key(team_id: str, name: str = "") -> str:
    """The team's saved service presets."""
    return f"preset:{team_id}"


def credentials_lock_key(team_id: str, name: str = "") -> str:
    """The team's git credential store."""
    return f"creds:{team_id}"


def team_secrets_lock_key(team_id: str, name: str = "") -> str:
    """The team's named-secrets store (secrets.json read-modify-write)."""
    return f"secrets:{team_id}"


def prod_backups_lock_key(team_id: str, name: str = "") -> str:
    """The team's snapshot/chunk store — prune vs. snapshot vs. restore."""
    return f"prod-backups:{team_id}"


def image_build_lock_key(team_id: str, name: str) -> str:
    """One image build job — serialises publish against retention cleanup."""
    return f"imgbuild:{team_id}:{name}"


def image_publish_lock_key(team_id: str, name: str) -> str:
    """One destination repository — keeps tag + push atomic across builds."""
    return f"imgpublish:{team_id}:{name}"


def env_wake_key(team_id: str, env_name: str) -> str:
    """Auto-start of one environment (see :func:`keyed_mutex`)."""
    return f"wake:{team_id}:{env_name}"


_keyed_mutexes: dict[str, threading.RLock] = {}
_keyed_mutexes_guard = threading.Lock()


@contextmanager
def keyed_mutex(key: str) -> Iterator[None]:
    """Blocking, re-entrant, process-wide mutex for a short critical section.

    Unlike :class:`LockManager` keys — non-blocking, contention surfaced to the
    agent as ``BusyError`` — these guard read-then-write sections deep inside
    ``docker_ops`` that last milliseconds, where the caller has nothing better
    to do than wait, and which must hold across a call the tool layer cannot
    see (``update_service`` removing and re-creating a container).

    Lock ordering: always taken *after* the caller's ``LockManager`` key, never
    before, so the two orders cannot deadlock. Nothing under this mutex takes a
    ``LockManager`` lock. Re-entrant because ``update_service`` holds one across
    its delegation to ``create_service``.
    """
    with _keyed_mutexes_guard:
        lock = _keyed_mutexes.get(key)
        if lock is None:
            lock = threading.RLock()
            _keyed_mutexes[key] = lock
    with lock:
        yield


def _format_age(seconds: float) -> str:
    """Human-readable duration for a lock that is still held ('4m12s')."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


class _Holder:
    """Which operation took a lock, and when."""

    __slots__ = ("operation", "acquired_at")

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.acquired_at = time.monotonic()

    def describe(self) -> str:
        """' (pull_and_apply, running for 4m12s)' — empty when unknown."""
        age = _format_age(time.monotonic() - self.acquired_at)
        if self.operation:
            return f" ({self.operation}, running for {age})"
        return f" (running for {age})"


class LockManager:
    """Thread-safe lock manager with per-environment and per-team granularity."""

    def __init__(self) -> None:
        self._env_locks: dict[str, threading.Lock] = {}
        self._team_locks: dict[str, threading.Lock] = {}
        self._active_team_locks: set[str] = set()
        self._env_lock_teams: dict[str, str] = {}
        self._active_env_counts_by_team: dict[str, int] = {}
        # A rejected caller cannot tell "still running" from "stuck". Record who
        # holds each lock and since when, so BusyError can say it (issue: agents
        # read a bare "in progress" as a stale lock and reach for restarts).
        self._env_holders: dict[str, _Holder] = {}
        self._team_holders: dict[str, _Holder] = {}
        # Keys currently held in the production keyspace, for the system lock's
        # mutual exclusion (and its holder hints).
        self._active_prod_keys: set[str] = set()
        self._system_holder: _Holder | None = None
        self._map_lock = threading.Lock()  # protects dict access

    # -- environment locks --

    def _get_env_lock(self, env_name: str) -> threading.Lock:
        with self._map_lock:
            if env_name not in self._env_locks:
                self._env_locks[env_name] = threading.Lock()
            return self._env_locks[env_name]

    def _env_holder_hint(self, env_name: str) -> str:
        holder = self._env_holders.get(env_name)
        return holder.describe() if holder else ""

    def _team_holder_hint(self, team_id: str) -> str:
        holder = self._team_holders.get(team_id)
        return holder.describe() if holder else ""

    def _system_holder_hint(self) -> str:
        holder = self._system_holder
        return holder.describe() if holder else ""

    def acquire_env(
        self, env_name: str, team_id: str | None = None, operation: str = ""
    ) -> None:
        with self._map_lock:
            if team_id is not None and team_id in self._active_team_locks:
                raise BusyError(
                    f"Another team-level operation (team '{team_id}')"
                    f"{self._team_holder_hint(team_id)} is in progress. "
                    "Try again later."
                )
            if _is_prod_key(env_name) and self._system_holder is not None:
                raise BusyError(
                    f"A system-level operation{self._system_holder_hint()} is in "
                    "progress. Try again later."
                )
            if env_name not in self._env_locks:
                self._env_locks[env_name] = threading.Lock()
            lock = self._env_locks[env_name]
            if not lock.acquire(blocking=False):
                raise BusyError(
                    f"Another operation on environment '{env_name}'"
                    f"{self._env_holder_hint(env_name)} is in progress. "
                    "It is still running server-side — wait for it to finish "
                    "rather than restarting the environment."
                )
            self._env_holders[env_name] = _Holder(operation)
            if _is_prod_key(env_name):
                self._active_prod_keys.add(env_name)
            if team_id is not None:
                self._env_lock_teams[env_name] = team_id
                self._active_env_counts_by_team[team_id] = (
                    self._active_env_counts_by_team.get(team_id, 0) + 1
                )

    def acquire_env_blocking(
        self, env_name: str, timeout: float, operation: str = ""
    ) -> bool:
        """Blocking acquire with a timeout — webhook-triggered production
        deploys queue behind a running one instead of dropping the push.
        Returns False when the timeout expires (caller skips the run)."""
        lock = self._get_env_lock(env_name)
        acquired = lock.acquire(blocking=True, timeout=timeout)
        if not acquired:
            return False
        with self._map_lock:
            if _is_prod_key(env_name) and self._system_holder is not None:
                # A cluster-wide operation owns the production keyspace. Waiting
                # it out is pointless here, so the caller skips this run exactly
                # as it would on a timeout.
                lock.release()
                return False
            self._env_holders[env_name] = _Holder(operation)
            if _is_prod_key(env_name):
                self._active_prod_keys.add(env_name)
        return True

    def describe_env_holder(self, env_name: str) -> str:
        """Holder hint for callers that report contention without raising."""
        with self._map_lock:
            return self._env_holder_hint(env_name)

    @contextmanager
    def env_lock(
        self, env_name: str, team_id: str | None = None, operation: str = ""
    ) -> Iterator[None]:
        """acquire_env/release_env as a context manager, for callers that
        need a secondary lock scoped to part of an operation."""
        self.acquire_env(env_name, team_id, operation=operation)
        try:
            yield
        finally:
            self.release_env(env_name)

    def release_env(self, env_name: str) -> None:
        with self._map_lock:
            lock = self._env_locks.get(env_name)
            if lock is None:
                return
            try:
                lock.release()
            except RuntimeError:
                return
            self._env_holders.pop(env_name, None)
            self._active_prod_keys.discard(env_name)
            team_id = self._env_lock_teams.pop(env_name, None)
            if team_id is not None:
                count = self._active_env_counts_by_team.get(team_id, 0) - 1
                if count > 0:
                    self._active_env_counts_by_team[team_id] = count
                else:
                    self._active_env_counts_by_team.pop(team_id, None)

    # -- team locks --

    def _get_team_lock(self, team_id: str) -> threading.Lock:
        with self._map_lock:
            if team_id not in self._team_locks:
                self._team_locks[team_id] = threading.Lock()
            return self._team_locks[team_id]

    def acquire_team(self, team_id: str, operation: str = "") -> None:
        with self._map_lock:
            if team_id not in self._team_locks:
                self._team_locks[team_id] = threading.Lock()
            lock = self._team_locks[team_id]
            if team_id in self._active_team_locks or not lock.acquire(blocking=False):
                raise BusyError(
                    f"Another team-level operation (team '{team_id}')"
                    f"{self._team_holder_hint(team_id)} is in progress. "
                    "Try again later."
                )
            if self._active_env_counts_by_team.get(team_id, 0) > 0:
                # An environment-level operation is running under this team: a
                # team-wide operation would race it. Give the lock back.
                lock.release()
                busy = ", ".join(
                    f"'{env}'{self._env_holder_hint(env)}"
                    for env, tid in sorted(self._env_lock_teams.items())
                    if tid == team_id
                )
                raise BusyError(
                    f"An environment operation in team '{team_id}' is in "
                    f"progress: {busy}. Try again later."
                )
            self._active_team_locks.add(team_id)
            self._team_holders[team_id] = _Holder(operation)

    def release_team(self, team_id: str) -> None:
        with self._map_lock:
            lock = self._team_locks.get(team_id)
            if lock is None:
                return
            self._active_team_locks.discard(team_id)
            self._team_holders.pop(team_id, None)
            try:
                lock.release()
            except RuntimeError:
                pass

    # -- system lock --

    def acquire_system(self, operation: str = "") -> None:
        """Take the single system-wide lock (cluster PITR).

        Mutually exclusive with the whole production keyspace in both
        directions: no `prod:` key may be held when this is taken, and none may
        be taken while it is held. Environment and team locks are unaffected —
        a cluster restore does not touch development environments.
        """
        with self._map_lock:
            if self._system_holder is not None:
                raise BusyError(
                    f"A system-level operation{self._system_holder_hint()} is in "
                    "progress. Try again later."
                )
            if self._active_prod_keys:
                busy = ", ".join(
                    f"'{key}'{self._env_holder_hint(key)}"
                    for key in sorted(self._active_prod_keys)
                )
                raise BusyError(
                    f"A production operation is in progress: {busy}. Try again later."
                )
            self._system_holder = _Holder(operation)

    def release_system(self) -> None:
        with self._map_lock:
            self._system_holder = None
