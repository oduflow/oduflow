from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger("oduflow")

# The registry is team-shared state mutated from many threads (parallel API
# requests, e.g. bulk delete) and potentially from more than one oduflow
# process on the same data dir. Every read-modify-write cycle runs under a
# per-path mutex (threads) plus an flock on a sidecar file (processes).
_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def _thread_lock(registry_path: str) -> threading.Lock:
    with _locks_guard:
        lock = _path_locks.get(registry_path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[registry_path] = lock
        return lock


@contextmanager
def _registry_lock(registry_path: str) -> Iterator[None]:
    """Serialize registry read-modify-write across threads and processes."""
    with _thread_lock(registry_path):
        os.makedirs(os.path.dirname(registry_path) or ".", exist_ok=True)
        fd = os.open(registry_path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _load_registry(registry_path: str) -> dict[str, int]:
    """Load port registry from disk. Returns empty dict if file doesn't exist or is corrupt."""
    if not os.path.isfile(registry_path):
        return {}
    try:
        with open(registry_path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            # Valid JSON of the wrong shape (a truncated write leaving `null`,
            # a hand-edit) would raise AttributeError past the handlers below.
            logger.warning("Ignoring malformed port registry %s", registry_path)
            return {}
        return {k: int(v) for k, v in data.items()}
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not load port registry %s: %s", registry_path, e)
        return {}


def _save_registry(registry_path: str, registry: dict[str, int]) -> None:
    """Atomically save the registry. Callers must hold ``_registry_lock``.

    The temp file name is unique per write: a fixed ``ports.json.tmp`` made
    concurrent writers rename each other's file away (ENOENT on bulk delete).
    """
    dir_name = os.path.dirname(registry_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="ports.", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(registry, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, registry_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def allocate_port(
    registry_path: str,
    env_name: str,
    port_range_start: int,
    port_range_end: int,
    used_ports: set[int] | None = None,
    *,
    preferred_port: int | None = None,
) -> int:
    """Allocate a stable port for an environment.

    If the environment already has a port in the registry AND it's not used by another
    container (checked via used_ports), reuse it. Otherwise allocate the next free port.

    Args:
        registry_path: Path to ports.json
        env_name: Environment to allocate port for
        port_range_start: Start of port range (inclusive)
        port_range_end: End of port range (exclusive)
        used_ports: Set of ports currently in use by OTHER environments' Docker containers.
                    If None, no conflict checking against Docker is done.
        preferred_port: Current Docker binding to adopt when its registry entry is missing.

    Returns:
        The allocated port number.

    Raises:
        FlowError if no free ports available.
    """
    from oduflow.errors import FlowError

    if used_ports is None:
        used_ports = set()

    with _registry_lock(registry_path):
        registry = _load_registry(registry_path)

        if env_name in registry:
            existing_port = registry[env_name]
            if (
                port_range_start <= existing_port < port_range_end
                and existing_port not in used_ports
            ):
                return existing_port

        occupied = set(registry.values()) | used_ports

        candidates = range(port_range_start, port_range_end)
        if preferred_port in candidates and preferred_port not in occupied:
            port = preferred_port
        else:
            port = next((p for p in candidates if p not in occupied), None)
        if port is not None:
            registry[env_name] = port
            _save_registry(registry_path, registry)
            logger.info("Allocated port %d for environment '%s'", port, env_name)
            return port

    raise FlowError(
        f"No free ports in range {port_range_start}-{port_range_end}. "
        f"Delete unused environments to free ports."
    )


def release_port(registry_path: str, env_name: str) -> None:
    """Remove port assignment for an environment."""
    with _registry_lock(registry_path):
        registry = _load_registry(registry_path)
        if env_name in registry:
            port = registry.pop(env_name)
            _save_registry(registry_path, registry)
            logger.info("Released port %d for environment '%s'", port, env_name)


def rename_env(registry_path: str, old_name: str, new_name: str) -> None:
    """Move a reservation to a renamed environment, keeping the same port.

    A release + allocate pair would be observably different: the environment
    could come back on another port, and a concurrent allocation could take the
    old one in between. The port is part of the environment's address, so a
    rename must preserve it.
    """
    with _registry_lock(registry_path):
        registry = _load_registry(registry_path)
        if old_name not in registry:
            return
        registry[new_name] = registry.pop(old_name)
        _save_registry(registry_path, registry)
        logger.info(
            "Moved port %d from environment '%s' to '%s'",
            registry[new_name],
            old_name,
            new_name,
        )


def get_port(registry_path: str, env_name: str) -> int | None:
    """Get the assigned port for an environment, or None if not assigned."""
    registry = _load_registry(registry_path)
    return registry.get(env_name)
