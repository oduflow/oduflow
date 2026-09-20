"""Backup: pack the source tree into a content-defined chunk stream.

Every revision is *full* (it references all chunks its files need, so any
revision restores independently), but built *incrementally*: files whose
``(size, mtime_ns)`` match the previous revision are never read — their
chunk references are copied over; only new/changed files enter the CDC
stream. Deduplication is lock-free: a chunk is uploaded only if its
content-derived ID does not already exist in the storage (existence check),
with the previous revision's chunk IDs cached to skip even those checks.
Per-chunk storage round-trips (existence check + put) run on a bounded
thread pool (``upload_threads``) so object-store latency is not paid
serially per chunk; the plaintext queued for those uploads is capped by a
byte budget (see ``_ChunkSink``), so a large first snapshot cannot grow the
server's resident memory without bound.

Packing: changed files are concatenated into one continuous stream (small
files share chunks). The stream is cut (``Chunker.flush``) whenever an
unchanged file's preserved chunks are spliced in, so a chunk never mixes
new bytes with preserved references.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import stat as stat_module
import threading
import time
from bisect import bisect_right
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from oduflow.chunkstore.chunker import Chunker
from oduflow.chunkstore.format import (
    META_AVG_SIZE,
    META_MAX_SIZE,
    META_MIN_SIZE,
    StoreConfig,
    chunk_key,
    decode_chunk,
    dump_json_lines,
    encode_chunk,
    ensure_config,
    revision_key,
)
from oduflow.chunkstore.storage import Storage

logger = logging.getLogger("oduflow")

_READ_BLOCK = 1024 * 1024

# Floor for the in-flight plaintext budget (see _ChunkSink). Chunk count
# alone is a weak bound — chunks range from min_size to max_size — so the
# producer blocks on queued *bytes* instead. The effective budget scales
# with upload_threads, so raising the knob still keeps every worker fed.
_INFLIGHT_BYTES_FLOOR = 64 * 1024 * 1024

# Finished futures are reaped from the pending list once it grows past this
# many entries, so the list stays O(upload_threads) rather than growing with
# the number of chunks in the revision (~125k for a 500 GB first snapshot).
_FUTURE_REAP_THRESHOLD = 512


@dataclass
class BackupResult:
    snapshot_id: str
    revision: int
    files: int
    total_bytes: int
    chunks: int
    new_chunks: int
    uploaded_bytes: int
    unchanged_files: int
    scan_seconds: float = 0.0
    # Aggregate wall time spent inside storage exists+encode+put, summed
    # across upload threads (can exceed elapsed_seconds when parallel).
    upload_seconds: float = 0.0
    elapsed_seconds: float = 0.0


class _ByteBudget:
    """Blocks the producer once in-flight plaintext exceeds *limit* bytes.

    A chunk larger than the whole budget is still admitted when nothing
    else is in flight, so the producer can never deadlock on it.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._used = 0
        self._cond = threading.Condition()

    def acquire(self, size: int) -> None:
        with self._cond:
            while self._used and self._used + size > self._limit:
                self._cond.wait()
            self._used += size

    def release(self, size: int) -> None:
        with self._cond:
            self._used -= size
            self._cond.notify_all()


@dataclass
class _ChunkSink:
    """Accumulates the revision's chunk sequence and uploads new chunks.

    With ``upload_threads > 1`` the per-chunk storage round-trips
    (existence check + compress + put) run on a bounded thread pool; the
    revision's chunk *sequence* (``hashes``/``lengths``) and the dedup set
    stay producer-ordered because they are appended before submission.
    Two bounds keep memory flat over a long backup: a semaphore of
    ``2 × threads`` caps how many chunks are queued, and a byte budget of
    ``max(64 MiB, threads × avg_size)`` caps how much plaintext they hold;
    finished futures are reaped as they accumulate. Callers must ``wait()``
    (which re-raises the first upload error) before committing the revision
    file, and ``close()`` when done.
    """

    storage: Storage
    config: StoreConfig
    known_ids: set[str]
    upload_threads: int = 1
    hashes: list[str] = field(default_factory=list)
    lengths: list[int] = field(default_factory=list)
    new_chunks: int = 0
    uploaded_bytes: int = 0
    upload_seconds: float = 0.0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._futures: list[Future[None]] = []
        self._executor: ThreadPoolExecutor | None = None
        if self.upload_threads > 1:
            self._executor = ThreadPoolExecutor(
                max_workers=self.upload_threads, thread_name_prefix="chunk-upload"
            )
            self._slots = threading.BoundedSemaphore(self.upload_threads * 2)
            self._budget = _ByteBudget(
                max(_INFLIGHT_BYTES_FLOOR, self.upload_threads * self.config.avg_size)
            )

    def emit_new(self, plaintext: bytes) -> None:
        chunk_hash = self.config.chunk_hash(plaintext)
        self.hashes.append(chunk_hash)
        self.lengths.append(len(plaintext))
        chunk_id = self.config.chunk_id(chunk_hash)
        if chunk_id in self.known_ids:
            return
        self.known_ids.add(chunk_id)
        key = chunk_key(chunk_id)
        if self._executor is None:
            self._store(key, plaintext)
            return
        if self._error is not None:
            self.wait()  # fail fast: re-raises the recorded error
        if len(self._futures) >= _FUTURE_REAP_THRESHOLD:
            self._reap()
        self._slots.acquire()
        self._budget.acquire(len(plaintext))
        self._futures.append(
            self._executor.submit(self._store_in_thread, key, plaintext)
        )

    def _store_in_thread(self, key: str, plaintext: bytes) -> None:
        try:
            self._store(key, plaintext)
        except BaseException as exc:
            with self._lock:
                if self._error is None:
                    self._error = exc
            raise
        finally:
            self._budget.release(len(plaintext))
            self._slots.release()

    def _store(self, key: str, plaintext: bytes) -> None:
        start = time.monotonic()
        try:
            if self.storage.exists(key):
                return
            blob = encode_chunk(plaintext)
            self.storage.put(key, blob)
            with self._lock:
                self.new_chunks += 1
                self.uploaded_bytes += len(blob)
        finally:
            with self._lock:
                self.upload_seconds += time.monotonic() - start

    def _reap(self) -> None:
        """Drop finished futures; re-raise the first upload error.

        Called from the producer thread only, so ``_futures`` needs no lock.
        """
        pending: list[Future[None]] = []
        for future in self._futures:
            if future.done():
                future.result()  # re-raises a failed upload
            else:
                pending.append(future)
        self._futures = pending

    def wait(self) -> None:
        """Drain in-flight uploads; re-raise the first upload error."""
        futures, self._futures = self._futures, []
        for future in futures:
            future.result()

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)

    def emit_preserved(self, chunk_hash: str, length: int) -> None:
        # The chunk provably exists (it is referenced by the previous
        # revision, and prune never fossilizes chunks referenced by kept
        # revisions; backup/prune runs are serialized by the caller).
        self.hashes.append(chunk_hash)
        self.lengths.append(length)
        self.known_ids.add(self.config.chunk_id(chunk_hash))


class _Stream:
    """The continuous CDC stream for changed files, with deferred
    (start_chunk, start_offset, end_chunk, end_offset) resolution.

    Chunk boundaries only become known when the chunker emits, so each
    file records its byte span within the current stream *segment*; when
    the segment is cut (flush) the spans are resolved against the emitted
    chunk lengths.
    """

    def __init__(self, chunker: Chunker, sink: _ChunkSink) -> None:
        self.chunker = chunker
        self.sink = sink
        self._segment_reset()

    def _segment_reset(self) -> None:
        self.base_chunk = len(self.sink.hashes)
        self.pos = 0
        self.seg_lens: list[int] = []
        self.pending: list[tuple[dict[str, Any], int, int]] = []

    def _emit(self, plaintext: bytes) -> None:
        self.sink.emit_new(plaintext)
        self.seg_lens.append(len(plaintext))

    def add_file(self, entry: dict[str, Any], path: str) -> None:
        if self.pos == 0 and not self.seg_lens and not self.pending:
            # Fresh segment: preserved chunks of unchanged files may have
            # been spliced into the sink since the last flush — rebase.
            self.base_chunk = len(self.sink.hashes)
        start = self.pos
        with open(path, "rb") as f:
            while True:
                block = f.read(_READ_BLOCK)
                if not block:
                    break
                self.pos += len(block)
                for chunk in self.chunker.update(block):
                    self._emit(chunk)
        self.pending.append((entry, start, self.pos))

    def flush(self) -> None:
        for chunk in self.chunker.flush():
            self._emit(chunk)
        if self.pending:
            # Prefix sums of the segment's chunk lengths -> byte ranges.
            cums = [0]
            for length in self.seg_lens:
                cums.append(cums[-1] + length)
            for entry, start, end in self.pending:
                if end == start:  # empty file
                    entry.update({"sc": 0, "so": 0, "ec": 0, "eo": 0})
                    continue
                sc = bisect_right(cums, start) - 1
                ec = bisect_right(cums, end - 1) - 1
                entry.update(
                    {
                        "sc": self.base_chunk + sc,
                        "so": start - cums[sc],
                        "ec": self.base_chunk + ec,
                        "eo": end - cums[ec],
                    }
                )
        self._segment_reset()


def _scan_tree(source_dir: str) -> list[tuple[str, os.stat_result, str]]:
    """Sorted (relative_path, lstat, kind) for the whole tree.

    kind: "file" | "dir" | "link". Sockets/FIFOs are skipped.
    """
    root = os.path.abspath(source_dir)
    out: list[tuple[str, os.stat_result, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat_module.S_ISLNK(st.st_mode):
                out.append((rel, st, "link"))
            else:
                out.append((rel, st, "dir"))
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat_module.S_ISLNK(st.st_mode):
                out.append((rel, st, "link"))
            elif stat_module.S_ISREG(st.st_mode):
                out.append((rel, st, "file"))
    out.sort(key=lambda item: item[0])
    return out


def list_revisions(storage: Storage, snapshot_id: str) -> list[int]:
    revisions = []
    for key in storage.list(f"snapshots/{snapshot_id}/"):
        base = key.rsplit("/", 1)[-1]
        if base.isdigit():
            revisions.append(int(base))
    return sorted(revisions)


def _read_meta_sequence(
    storage: Storage, config: StoreConfig, hashes: list[str]
) -> bytes:
    parts = []
    for chunk_hash in hashes:
        blob = storage.get(chunk_key(config.chunk_id(chunk_hash)))
        parts.append(decode_chunk(blob, config, chunk_hash))
    return b"".join(parts)


def load_revision(
    storage: Storage, config: StoreConfig, snapshot_id: str, revision: int
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[int]]:
    """Load a revision: (meta, file entries, chunk hashes, chunk lengths)."""
    meta = json.loads(storage.get(revision_key(snapshot_id, revision)).decode("utf-8"))
    files = json.loads(
        _read_meta_sequence(storage, config, meta["files_meta_chunks"]).decode("utf-8")
    )
    hashes = json.loads(
        _read_meta_sequence(storage, config, meta["hashes_meta_chunks"]).decode("utf-8")
    )
    lengths = json.loads(
        _read_meta_sequence(storage, config, meta["lengths_meta_chunks"]).decode(
            "utf-8"
        )
    )
    return meta, files, hashes, lengths


def _write_meta_sequence(
    payload: bytes, config: StoreConfig, sink: _ChunkSink
) -> list[str]:
    """Chunk + upload a metadata blob; return its chunk-hash sequence.

    Metadata chunks are ordinary chunks (deduplicated like data): an
    unchanged file list across revisions costs almost nothing.
    """
    chunker = Chunker(
        config.seed,
        min_size=META_MIN_SIZE,
        avg_size=META_AVG_SIZE,
        max_size=META_MAX_SIZE,
    )
    start = len(sink.hashes)
    for chunk in chunker.update(payload):
        sink.emit_new(chunk)
    for chunk in chunker.flush():
        sink.emit_new(chunk)
    hashes = sink.hashes[start:]
    # Metadata sequences are addressed by hash directly (not by revision
    # position), so pop them off the revision's data sequence.
    del sink.hashes[start:]
    del sink.lengths[start:]
    return hashes


def backup(
    source_dir: str,
    storage: Storage,
    snapshot_id: str,
    *,
    prev_revision: int | None = None,
    upload_threads: int = 16,
) -> BackupResult:
    """Create a new revision of *source_dir* under *snapshot_id*.

    Caller contract: backups and prunes against one storage are serialized
    (Oduflow runs them under the team's `prod-backups:` lock / a single
    scheduler thread). *upload_threads* bounds concurrent chunk uploads
    (1 = sequential); it also sets the in-flight plaintext budget
    (``max(64 MiB, threads × avg chunk size)``), which is the memory this
    call adds to the server process.
    """
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"source_dir does not exist: {source_dir}")
    started = time.monotonic()
    config = ensure_config(storage)

    revisions = list_revisions(storage, snapshot_id)
    prev: int | None
    if prev_revision is not None:
        prev = prev_revision if prev_revision in revisions else None
    else:
        prev = revisions[-1] if revisions else None

    prev_files: dict[str, dict[str, Any]] = {}
    prev_hashes: list[str] = []
    prev_lengths: list[int] = []
    known_ids: set[str] = set()
    if prev is not None:
        try:
            _meta, files, prev_hashes, prev_lengths = load_revision(
                storage, config, snapshot_id, prev
            )
            prev_files = {f["path"]: f for f in files if f.get("kind") == "file"}
            known_ids = {config.chunk_id(h) for h in prev_hashes}
        except Exception:
            logger.warning(
                "Could not load previous revision %s/%s — running full backup",
                snapshot_id,
                prev,
                exc_info=True,
            )
            prev_files = {}

    sink = _ChunkSink(
        storage=storage,
        config=config,
        known_ids=known_ids,
        upload_threads=max(1, upload_threads),
    )
    stream = _Stream(
        Chunker(
            config.seed,
            min_size=config.min_size,
            avg_size=config.avg_size,
            max_size=config.max_size,
        ),
        sink,
    )

    entries: list[dict[str, Any]] = []
    # Tail of the last spliced preserved run: (prev index, new index) of its
    # final chunk. Consecutive unchanged files sharing a boundary chunk reuse
    # it; any other interleaving re-emits the full range (a chunk may appear
    # more than once in a revision's sequence — harmless, it deduplicates in
    # storage — but a FILE's chunk range must stay contiguous).
    preserved_tail: tuple[int, int] | None = None
    total_bytes = 0
    file_count = 0
    unchanged_count = 0

    scan_started = time.monotonic()
    tree = _scan_tree(source_dir)
    scan_seconds = time.monotonic() - scan_started

    try:
        for rel, st, kind in tree:
            full = os.path.join(source_dir, rel)
            if kind == "dir":
                entries.append(
                    {
                        "path": rel,
                        "kind": "dir",
                        "mode": stat_module.S_IMODE(st.st_mode),
                    }
                )
                continue
            if kind == "link":
                try:
                    target = os.readlink(full)
                except OSError:
                    continue
                entries.append({"path": rel, "kind": "link", "target": target})
                continue

            entry: dict[str, Any] = {
                "path": rel,
                "kind": "file",
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "mode": stat_module.S_IMODE(st.st_mode),
            }
            file_count += 1
            total_bytes += st.st_size

            prev_entry = prev_files.get(rel)
            unchanged = (
                prev_entry is not None
                and prev_entry.get("size") == st.st_size
                and prev_entry.get("mtime_ns") == st.st_mtime_ns
                and "sc" in prev_entry
            )
            if unchanged and st.st_size > 0:
                assert prev_entry is not None
                # Splice the previous revision's chunks in: cut the new stream
                # first so chunks never mix new bytes and preserved refs.
                stream.flush()
                sc, ec = int(prev_entry["sc"]), int(prev_entry["ec"])
                share_first = (
                    preserved_tail is not None
                    and preserved_tail[0] == sc
                    and preserved_tail[1] == len(sink.hashes) - 1
                )
                if share_first:
                    new_sc = len(sink.hashes) - 1
                    emit_from = sc + 1
                else:
                    new_sc = len(sink.hashes)
                    emit_from = sc
                for idx in range(emit_from, ec + 1):
                    sink.emit_preserved(prev_hashes[idx], prev_lengths[idx])
                new_ec = new_sc + (ec - sc)
                preserved_tail = (ec, new_ec)
                entry.update(
                    {
                        "sc": new_sc,
                        "so": prev_entry["so"],
                        "ec": new_ec,
                        "eo": prev_entry["eo"],
                    }
                )
                entries.append(entry)
                unchanged_count += 1
                continue
            if st.st_size == 0:
                entry.update({"sc": 0, "so": 0, "ec": 0, "eo": 0})
                entries.append(entry)
                if unchanged:
                    unchanged_count += 1
                continue

            entries.append(entry)
            stream.add_file(entry, full)

        stream.flush()

        files_meta = _write_meta_sequence(dump_json_lines(entries), config, sink)
        hashes_meta = _write_meta_sequence(dump_json_lines(sink.hashes), config, sink)
        lengths_meta = _write_meta_sequence(dump_json_lines(sink.lengths), config, sink)

        # All chunk uploads must have succeeded before the revision file
        # (written below) makes them reachable.
        sink.wait()
    finally:
        sink.close()

    revision = (revisions[-1] + 1) if revisions else 1
    meta = {
        "version": 1,
        "snapshot_id": snapshot_id,
        "revision": revision,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "files": file_count,
        "total_bytes": total_bytes,
        "chunks": len(sink.hashes),
        "files_meta_chunks": files_meta,
        "hashes_meta_chunks": hashes_meta,
        "lengths_meta_chunks": lengths_meta,
    }
    # The revision file is written LAST: a backup without its revision file
    # does not exist; its orphaned chunks are reclaimed by prune.
    storage.put(
        revision_key(snapshot_id, revision),
        json.dumps(meta, indent=2).encode("utf-8"),
    )
    elapsed = time.monotonic() - started
    logger.info(
        "chunkstore backup %s/%d: %d files (%d unchanged), %d chunks "
        "(%d new, %d bytes uploaded) in %.1fs "
        "(scan %.1fs, storage %.1fs across %d threads)",
        snapshot_id,
        revision,
        file_count,
        unchanged_count,
        len(sink.hashes),
        sink.new_chunks,
        sink.uploaded_bytes,
        elapsed,
        scan_seconds,
        sink.upload_seconds,
        max(1, upload_threads),
    )
    return BackupResult(
        snapshot_id=snapshot_id,
        revision=revision,
        files=file_count,
        total_bytes=total_bytes,
        chunks=len(sink.hashes),
        new_chunks=sink.new_chunks,
        uploaded_bytes=sink.uploaded_bytes,
        unchanged_files=unchanged_count,
        scan_seconds=round(scan_seconds, 3),
        upload_seconds=round(sink.upload_seconds, 3),
        elapsed_seconds=round(elapsed, 3),
    )
