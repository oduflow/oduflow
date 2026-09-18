"""Pluggable storage backends for the chunkstore.

The protocol is the minimal object-store surface the algorithm needs:
existence check (the lock-free dedup primitive), get/put, prefix listing,
rename (fossilization) and delete. Keys are ``/``-separated relative paths.

:class:`LocalStorage` backs the unit tests and local targets; the S3
backend lives in :mod:`oduflow.s3_client` (rename = CopyObject+Delete).
"""

from __future__ import annotations

import os
import tempfile
import threading
from typing import Protocol


class Storage(Protocol):
    def exists(self, key: str) -> bool: ...

    def get(self, key: str) -> bytes: ...

    def put(self, key: str, data: bytes) -> None: ...

    def list(self, prefix: str) -> list[str]:
        """All keys under *prefix* (recursive), sorted."""
        ...

    def rename(self, src: str, dst: str) -> None: ...

    def delete(self, key: str) -> None: ...


class LocalStorage:
    """Filesystem-backed storage (tests, local backup targets)."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        path = os.path.normpath(os.path.join(self.root, key))
        if not path.startswith(self.root + os.sep) and path != self.root:
            raise ValueError(f"key escapes storage root: {key!r}")
        return path

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".put-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix.rstrip("/")) if prefix else self.root
        keys: list[str] = []
        if os.path.isfile(base):
            return [prefix.rstrip("/")]
        if not os.path.isdir(base):
            return []
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                if filename.startswith(".put-"):
                    continue
                full = os.path.join(dirpath, filename)
                keys.append(os.path.relpath(full, self.root).replace(os.sep, "/"))
        return sorted(keys)

    def rename(self, src: str, dst: str) -> None:
        dst_path = self._path(dst)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        os.replace(self._path(src), dst_path)

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


class CountingStorage:
    """Wrapper counting operations — used by dedup tests ("how many PUTs did
    this incremental backup actually do?"). Thread-safe: backup uploads
    chunks from a thread pool."""

    def __init__(self, inner: Storage) -> None:
        self.inner = inner
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {
            "exists": 0,
            "get": 0,
            "put": 0,
            "list": 0,
            "rename": 0,
            "delete": 0,
        }

    def _count(self, op: str) -> None:
        with self._lock:
            self.counts[op] += 1

    def exists(self, key: str) -> bool:
        self._count("exists")
        return self.inner.exists(key)

    def get(self, key: str) -> bytes:
        self._count("get")
        return self.inner.get(key)

    def put(self, key: str, data: bytes) -> None:
        self._count("put")
        self.inner.put(key, data)

    def list(self, prefix: str) -> list[str]:
        self._count("list")
        return self.inner.list(prefix)

    def rename(self, src: str, dst: str) -> None:
        self._count("rename")
        self.inner.rename(src, dst)

    def delete(self, key: str) -> None:
        self._count("delete")
        self.inner.delete(key)
