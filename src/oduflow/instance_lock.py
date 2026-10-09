"""Coordination between Oduflow servers that share one data directory.

Several servers may run at once: every stdio MCP client starts its own. Each
holds a shared lock for its lifetime. A startup step that must not run under
another server, such as a PostgreSQL upgrade that removes the clusters it
uses, takes the lock exclusively, which fails while any other server runs.
The kernel releases a lock when its process exits, so none is ever stale.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator

from oduflow.errors import BusyError
from oduflow.settings import Settings

_LOCK_FILE = "server.lock"
# This process's shared lock, held until it exits.
_fd: int | None = None


def _open(settings: Settings) -> int:
    os.makedirs(settings.base_data_dir, exist_ok=True)
    return os.open(
        os.path.join(settings.base_data_dir, _LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644
    )


def hold_shared(settings: Settings) -> None:
    """Register this process as a running server until it exits."""
    global _fd
    fd = _open(settings)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise BusyError(
            f"Another Oduflow server on {settings.base_data_dir} is upgrading "
            "PostgreSQL. Start this one once it has finished."
        ) from None
    _fd = fd


@contextlib.contextmanager
def exclusive(settings: Settings, purpose: str) -> Iterator[None]:
    """Run ``purpose`` only while no other server runs on the data directory."""
    fd = _fd if _fd is not None else _open(settings)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd != _fd:
            os.close(fd)
        # A failed conversion may have dropped this process's shared lock;
        # callers exit on this error rather than run on as a server.
        raise BusyError(
            f"Another Oduflow server is running on {settings.base_data_dir}. "
            f"Stop it to {purpose}, then start this one again."
        ) from None
    try:
        yield
    finally:
        if fd == _fd:
            fcntl.flock(fd, fcntl.LOCK_SH)
        else:
            os.close(fd)
