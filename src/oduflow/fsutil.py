"""Shared filesystem helpers.

Every file that carries secrets or credentials (secrets store, service
presets, PG credentials, agent sessions, template metadata) is written the
same way: owner-only from birth (0600 on the temp file, so not even a brief
window is world-readable), fsynced, and atomically swapped into place so a
crash mid-write can never leave a truncated file behind.

A filestore is the other kind of tree handled here: its tenant can write to
it, so Oduflow copies it without following the links in it (copy_tenant_tree).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
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


def _copy_without_following(src: str, dst: str) -> None:
    # A file swapped for a link after the listing is copied as that link.
    shutil.copy2(src, dst, follow_symlinks=False)


def _not_plain_entries(directory: str, names: list[str]) -> list[str]:
    """The *names* in *directory* that are no file, directory or link."""
    skipped = []
    for name in names:
        try:
            mode = os.lstat(os.path.join(directory, name)).st_mode
        except OSError:
            # Gone since the listing; the copy reports it if it matters.
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            skipped.append(name)
    return skipped


def copy_tenant_tree(
    src: str,
    dst: str,
    *,
    dirs_exist_ok: bool = False,
    copy_function: Callable[[str, str], object] = _copy_without_following,
) -> None:
    """``shutil.copytree`` for a tree its tenant can write to: a filestore.

    A plain ``copytree`` follows symbolic links. Oduflow copies as the account
    it runs as, usually root, so a link a tenant left in its filestore —
    ``aa/x -> /etc/shadow``, or a link to another team's data directory —
    would be read on the host and delivered back as an ordinary file in the
    copy. Here a link is copied as a link, which inside a container resolves
    against the container's own filesystem, and device nodes, FIFOs and
    sockets are left out: an Odoo filestore holds none, and reading one blocks
    or returns a disk.
    """
    shutil.copytree(
        src,
        dst,
        symlinks=True,
        ignore=_not_plain_entries,
        copy_function=copy_function,
        dirs_exist_ok=dirs_exist_ok,
    )
