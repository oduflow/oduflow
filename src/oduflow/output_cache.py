from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field

_CACHE_TTL = 3600  # 1 hour
_MAX_ENTRIES = 50
_MAX_OUTPUT_SIZE = 10_000_000  # 10 MB


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
        self._seq = 0

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
            output = output[:_MAX_OUTPUT_SIZE]

        # A process-wide sequence guarantees a unique id even when two outputs
        # share the same 1000-char prefix within one clock tick (identical
        # prefix + equal time.time() would otherwise collide and overwrite).
        with self._lock:
            self._seq += 1
            seq = self._seq
        raw = f"{output[:1000]}{time.time()}{seq}".encode()
        output_id = hashlib.sha256(raw).hexdigest()[:8]

        lines = output.splitlines()

        error_indices = []
        for i, line in enumerate(lines):
            upper = line.upper()
            if any(m in upper for m in _ERROR_MARKERS):
                error_indices.append(i)

        entry = CachedOutput(
            output_id=output_id,
            lines=lines,
            total_chars=len(output),
            created_at=time.time(),
            source_tool=source_tool,
            source_args=source_args,
            error_line_indices=error_indices,
            team_id=team_id,
            production=production,
        )

        with self._lock:
            self._evict()
            self._store[output_id] = entry

        return entry

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
