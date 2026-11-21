from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

_CACHE_TTL = 3600  # 1 hour
_MAX_ENTRIES = 50
_MAX_OUTPUT_SIZE = 10_000_000  # 10 MB
# An oversized output keeps its first _TRUNCATED_HEAD_SIZE chars and its end:
# a failing Odoo run prints the fatal traceback last.
_TRUNCATED_HEAD_SIZE = 1_000_000
_TRUNCATION_MARKER = (
    "\n[... middle of the output omitted: over the cache size limit ...]\n"
)


@dataclass
class CachedOutput:
    output_id: str
    lines: list[str]
    total_chars: int
    created_at: float
    source_tool: str
    source_args: str
    error_line_indices: list[int] = field(default_factory=list)
    team_id: str = ""
    production: bool = False

    @property
    def total_lines(self) -> int:
        return len(self.lines)

    @property
    def has_errors(self) -> bool:
        return len(self.error_line_indices) > 0


_ERROR_MARKERS = (
    " ERROR ",
    " WARNING ",
    " CRITICAL ",
    "TRACEBACK",
    "RAISE ",
    "EXCEPTION",
)


class OutputCache:
    """Thread-safe in-memory cache for large tool outputs."""

    def __init__(self) -> None:
        self._store: dict[str, CachedOutput] = {}
        self._lock = threading.Lock()

    def store(
        self,
        output: str,
        source_tool: str,
        source_args: str,
        *,
        team_id: str = "",
        production: bool = False,
    ) -> CachedOutput:
        """Cache output, return CachedOutput with generated ID."""
        if len(output) > _MAX_OUTPUT_SIZE:
            tail_size = (
                _MAX_OUTPUT_SIZE - _TRUNCATED_HEAD_SIZE - len(_TRUNCATION_MARKER)
            )
            output = (
                output[:_TRUNCATED_HEAD_SIZE] + _TRUNCATION_MARKER + output[-tail_size:]
            )

        lines = output.splitlines()

        error_indices = []
        for i, line in enumerate(lines):
            upper = line.upper()
            if any(m in upper for m in _ERROR_MARKERS):
                error_indices.append(i)

        entry = CachedOutput(
            output_id=secrets.token_hex(4),
            lines=lines,
            total_chars=len(output),
            created_at=time.time(),
            source_tool=source_tool,
            source_args=source_args,
            error_line_indices=error_indices,
            team_id=team_id,
            production=production,
        )
        self._save(entry)
        return entry

    def _save(self, entry: CachedOutput) -> None:
        with self._lock:
            self._evict()
            while entry.output_id in self._store:
                entry.output_id = secrets.token_hex(4)
            self._store[entry.output_id] = entry

    def get(self, output_id: str) -> CachedOutput | None:
        with self._lock:
            entry = self._store.get(output_id)
            if entry and (time.time() - entry.created_at) > _CACHE_TTL:
                del self._store[output_id]
                return None
            return entry

    def _evict(self) -> None:
        """Remove expired entries + oldest if over limit."""
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.created_at > _CACHE_TTL]
        for k in expired:
            del self._store[k]
        while len(self._store) >= _MAX_ENTRIES:
            oldest = min(self._store, key=lambda k: self._store[k].created_at)
            del self._store[oldest]


class CliOutputCache(OutputCache):
    """Owner-only local CLI spool; MCP servers never open this database."""

    def __init__(self, config: str, team_id: str) -> None:
        self._scope = json.dumps([os.path.realpath(config), team_id])

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        directory = Path.home() / ".oduflow-cli-cache"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError(
                f"CLI output cache directory is not private: {directory}"
            )

        path = directory / "outputs.sqlite3"
        os.close(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA secure_delete = ON")
            db.execute(
                "CREATE TABLE IF NOT EXISTS outputs "
                "(id TEXT PRIMARY KEY, scope TEXT, created REAL, payload TEXT)"
            )
            db.execute(
                "DELETE FROM outputs WHERE created < ?", (time.time() - _CACHE_TTL,)
            )
            yield db

    def _save(self, entry: CachedOutput) -> None:
        with self._transaction() as db:
            while db.execute(
                "SELECT 1 FROM outputs WHERE id = ?", (entry.output_id,)
            ).fetchone():
                entry.output_id = secrets.token_hex(4)
            db.execute(
                "INSERT INTO outputs VALUES (?, ?, ?, ?)",
                (
                    entry.output_id,
                    self._scope,
                    entry.created_at,
                    json.dumps(vars(entry), ensure_ascii=False),
                ),
            )
            db.execute(
                "DELETE FROM outputs WHERE rowid IN "
                "(SELECT rowid FROM outputs ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                (_MAX_ENTRIES,),
            )

    def get(self, output_id: str) -> CachedOutput | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT payload FROM outputs WHERE id = ? AND scope = ?",
                (output_id, self._scope),
            ).fetchone()
        return CachedOutput(**json.loads(row[0])) if row else None
