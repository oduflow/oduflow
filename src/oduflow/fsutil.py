"""Shared filesystem helpers for the credential-class JSON stores.

Every file that carries secrets or credentials (secrets store, service
presets, PG credentials, agent sessions, template metadata) is written the
same way: owner-only from birth (0600 on the temp file, so not even a brief
window is world-readable), fsynced, and atomically swapped into place so a
crash mid-write can never leave a truncated file behind.
"""

from __future__ import annotations

import json
import os
from typing import Any


def atomic_write_private_text(path: str, text: str) -> None:
    """Atomically write *text* to *path* with owner-only (0600) permissions."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # O_CREAT's mode is masked by the umask and ignored for a
            # pre-existing tmp file; fchmod pins 0600 in both cases.
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def atomic_write_private_json(
    path: str, data: dict[str, Any], *, sort_keys: bool = True
) -> None:
    """Serialize *data* and :func:`atomic_write_private_text` it to *path*."""
    atomic_write_private_text(
        path, json.dumps(data, indent=2, sort_keys=sort_keys) + "\n"
    )
