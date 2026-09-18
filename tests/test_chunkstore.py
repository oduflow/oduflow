"""Chunkstore tests — all against LocalStorage, no S3/Docker required."""

import datetime
import os
import random
import threading
import time
from unittest.mock import patch

import pytest

from oduflow import chunkstore
from oduflow.chunkstore.backup import list_revisions
from oduflow.chunkstore.chunker import Chunker, derive_table
from oduflow.chunkstore.format import (
    ChunkCorruptedError,
    decode_chunk,
    encode_chunk,
    ensure_config,
)
from oduflow.chunkstore.prune import parse_keep, select_revisions_to_keep
from oduflow.chunkstore.storage import CountingStorage, LocalStorage

# Small chunk parameters so tests work on kilobytes, not megabytes.
SMALL = dict(min_size=1024, avg_size=4096, max_size=16384)


def _chunk_all(chunker: Chunker, data: bytes, block: int = 8192) -> list[bytes]:
    chunks = []
    for i in range(0, len(data), block):
        chunks.extend(chunker.update(data[i : i + block]))
    chunks.extend(chunker.flush())
    return chunks


class TestChunker:
    def test_deterministic_for_same_seed(self):
        data = random.Random(1).randbytes(200_000)
        a = _chunk_all(Chunker(b"seed-a", **SMALL), data)
        b = _chunk_all(Chunker(b"seed-a", **SMALL), data, block=333)
        assert a == b

    def test_different_seed_different_boundaries(self):
        data = random.Random(2).randbytes(200_000)
        a = _chunk_all(Chunker(b"seed-a", **SMALL), data)
        b = _chunk_all(Chunker(b"seed-b", **SMALL), data)
        assert [len(c) for c in a] != [len(c) for c in b]

    def test_size_bounds(self):
        data = random.Random(3).randbytes(500_000)
        chunks = _chunk_all(Chunker(b"s", **SMALL), data)
        assert b"".join(chunks) == data
        for chunk in chunks[:-1]:
            assert SMALL["min_size"] <= len(chunk) <= SMALL["max_size"]
        assert len(chunks[-1]) <= SMALL["max_size"]

    def test_shift_resistance(self):
        # Inserting bytes at the start must not re-chunk the whole stream:
        # most chunks reappear identically (content-defined boundaries).
        data = random.Random(4).randbytes(300_000)
        original = set(_chunk_all(Chunker(b"s", **SMALL), data))
        shifted = set(_chunk_all(Chunker(b"s", **SMALL), b"XY" + data))
        common = original & shifted
        assert len(common) >= len(original) * 0.6

    def test_table_derivation_stable(self):
        assert derive_table(b"x") == derive_table(b"x")
        assert derive_table(b"x") != derive_table(b"y")
        assert len(derive_table(b"x")) == 256

    def test_avg_must_be_power_of_two(self):
        with pytest.raises(ValueError, match="power of two"):
            Chunker(b"s", min_size=100, avg_size=3000, max_size=9000)


class TestChunkFormat:
    def test_roundtrip_compressible(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        config = ensure_config(storage)
        payload = b"hello world " * 1000
        blob = encode_chunk(payload)
        assert len(blob) < len(payload)  # zstd kicked in
        assert decode_chunk(blob, config, config.chunk_hash(payload)) == payload

    def test_roundtrip_incompressible(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        config = ensure_config(storage)
        payload = random.Random(5).randbytes(10_000)
        blob = encode_chunk(payload)
        assert decode_chunk(blob, config, config.chunk_hash(payload)) == payload

    def test_corruption_detected(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        config = ensure_config(storage)
        payload = b"data" * 100
        blob = bytearray(encode_chunk(payload))
        blob[-1] ^= 0xFF
        with pytest.raises(ChunkCorruptedError):
            decode_chunk(bytes(blob), config, config.chunk_hash(payload))

    def test_config_created_once(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        a = ensure_config(storage)
        b = ensure_config(storage)
        assert a == b
        assert len(a.seed) == 32


def _make_tree(root, spec: dict[str, bytes]) -> None:
    for rel, content in spec.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)


def _read_tree(root) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out


@pytest.fixture
def small_config_storage(tmp_path, monkeypatch):
    """LocalStorage whose auto-created config uses tiny chunk sizes."""
    from oduflow.chunkstore import format as fmt

    monkeypatch.setattr(fmt, "META_MIN_SIZE", 256)
    monkeypatch.setattr(fmt, "META_AVG_SIZE", 1024)
    monkeypatch.setattr(fmt, "META_MAX_SIZE", 4096)
    # backup.py imported the names at module load; patch there too. (The
    # package __init__ re-exports the backup *function* under the same
    # name, so resolve the module via importlib.)
    import importlib

    backup_mod = importlib.import_module("oduflow.chunkstore.backup")

    monkeypatch.setattr(backup_mod, "META_MIN_SIZE", 256)
    monkeypatch.setattr(backup_mod, "META_AVG_SIZE", 1024)
    monkeypatch.setattr(backup_mod, "META_MAX_SIZE", 4096)

    storage = LocalStorage(str(tmp_path / "store"))
    ensure_config(storage)
    # Rewrite config with small data-chunk parameters.
    import json

    raw = json.loads(storage.get("config").decode())
    raw.update({"min_size": 1024, "avg_size": 4096, "max_size": 16384})
    storage.put("config", json.dumps(raw).encode())
    return storage


class _SlowStorage:
    """Delegating storage that sleeps in exists/put and records the peak
    number of concurrent chunk operations."""

    def __init__(self, inner, delay: float = 0.02) -> None:
        self.inner = inner
        self.delay = delay
        self._lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0

    def _enter(self):
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)

    def _exit(self):
        with self._lock:
            self._active -= 1

    def exists(self, key):
        self._enter()
        try:
            time.sleep(self.delay)
            return self.inner.exists(key)
        finally:
            self._exit()

    def put(self, key, data):
        self._enter()
        try:
            time.sleep(self.delay)
            self.inner.put(key, data)
        finally:
            self._exit()

    def get(self, key):
        return self.inner.get(key)

    def list(self, prefix):
        return self.inner.list(prefix)

    def rename(self, src, dst):
        self.inner.rename(src, dst)

    def delete(self, key):
        self.inner.delete(key)


class _FailingChunkPuts:
    """Delegating storage whose chunk PUTs fail (config/revision pass)."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def put(self, key, data):
        if key.startswith("chunks/"):
            raise RuntimeError("chunk upload boom")
        self.inner.put(key, data)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class TestParallelUploads:
    def test_parallel_roundtrip_uses_concurrency(self, small_config_storage, tmp_path):
        rng = random.Random(11)
        spec = {f"f{i}.bin": rng.randbytes(20_000) for i in range(12)}
        src = tmp_path / "src"
        _make_tree(str(src), spec)

        slow = _SlowStorage(small_config_storage)
        result = chunkstore.backup(str(src), slow, "erp", upload_threads=8)
        assert result.new_chunks > 4
        assert result.upload_seconds > 0
        assert result.elapsed_seconds > 0
        assert slow.max_concurrent >= 2

        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", None, str(dst))
        assert _read_tree(str(src)) == _read_tree(str(dst))

    def test_sequential_mode_unchanged(self, small_config_storage, tmp_path):
        rng = random.Random(12)
        src = tmp_path / "src"
        _make_tree(str(src), {f"f{i}.bin": rng.randbytes(10_000) for i in range(4)})

        slow = _SlowStorage(small_config_storage, delay=0.001)
        result = chunkstore.backup(str(src), slow, "erp", upload_threads=1)
        assert result.revision == 1
        assert slow.max_concurrent == 1

        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", None, str(dst))
        assert _read_tree(str(src)) == _read_tree(str(dst))

    def test_dedup_counts_deterministic_with_threads(
        self, small_config_storage, tmp_path
    ):
        # Same content twice in one tree: known_ids dedup happens on the
        # producer thread, so exists/put counts stay exact under threading.
        blob = random.Random(13).randbytes(30_000)
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": blob, "b.bin": blob})

        counting = CountingStorage(small_config_storage)
        result = chunkstore.backup(str(src), counting, "erp", upload_threads=8)
        assert counting.counts["put"] == result.new_chunks + 1  # + revision file
        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", None, str(dst))
        assert _read_tree(str(src)) == _read_tree(str(dst))

    @pytest.mark.parametrize("threads", [1, 8])
    def test_failed_upload_aborts_revision(
        self, small_config_storage, tmp_path, threads
    ):
        src = tmp_path / "src"
        _make_tree(str(src), {"f.bin": random.Random(14).randbytes(50_000)})
        failing = _FailingChunkPuts(small_config_storage)
        with pytest.raises(RuntimeError, match="chunk upload boom"):
            chunkstore.backup(str(src), failing, "erp", upload_threads=threads)
        # No revision file was committed: the snapshot does not exist.
        assert list_revisions(small_config_storage, "erp") == []


class TestBackupRestore:
    def test_roundtrip_mixed_tree(self, small_config_storage, tmp_path):
        rng = random.Random(7)
        spec = {
            "empty.txt": b"",
            "small/a.bin": rng.randbytes(100),
            "small/b.bin": rng.randbytes(300),
            "big/blob.bin": rng.randbytes(60_000),
            "nested/deep/dir/file.txt": b"hello\n" * 50,
            "unicode-файл.dat": rng.randbytes(2000),
        }
        src = tmp_path / "src"
        _make_tree(str(src), spec)
        os.makedirs(src / "empty-dir")
        os.symlink("small/a.bin", src / "link.bin")
        os.chmod(src / "small" / "a.bin", 0o600)

        result = chunkstore.backup(str(src), small_config_storage, "erp")
        assert result.revision == 1
        assert result.files == len(spec)

        dst = tmp_path / "dst"
        info = chunkstore.restore(small_config_storage, "erp", None, str(dst))
        assert info["files"] == len(spec)
        assert _read_tree(str(src)) == _read_tree(str(dst))
        assert os.path.isdir(dst / "empty-dir")
        assert os.readlink(dst / "link.bin") == "small/a.bin"
        assert os.stat(dst / "small" / "a.bin").st_mode & 0o777 == 0o600
        # mtimes preserved
        assert (
            os.stat(dst / "big" / "blob.bin").st_mtime_ns
            == os.stat(src / "big" / "blob.bin").st_mtime_ns
        )

    def test_incremental_skips_unchanged(self, small_config_storage, tmp_path):
        rng = random.Random(8)
        spec = {f"f{i}.bin": rng.randbytes(5000) for i in range(10)}
        src = tmp_path / "src"
        _make_tree(str(src), spec)

        first = chunkstore.backup(str(src), small_config_storage, "erp")

        counting = CountingStorage(small_config_storage)
        # Change ONE file.
        with open(src / "f3.bin", "wb") as f:
            f.write(rng.randbytes(5000))
        result = chunkstore.backup(str(src), counting, "erp")
        assert result.revision == 2
        assert result.unchanged_files == 9
        # Only the changed file's chunks + metadata chunks are uploaded — a
        # small fraction of the full backup's ~50 KB of incompressible data.
        # Chunk *counts* are seed-dependent (the store seed is random and
        # file mtimes shift metadata chunk boundaries), so assert on bytes.
        assert result.uploaded_bytes < first.uploaded_bytes / 3
        assert counting.counts["put"] == result.new_chunks + 1  # + revision file

        # Both revisions restore correctly.
        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", 2, str(dst))
        assert _read_tree(str(src)) == _read_tree(str(dst))

    def test_cross_snapshot_dedup(self, small_config_storage, tmp_path):
        # Two productions with identical filestores share chunks.
        rng = random.Random(9)
        spec = {f"blob{i}": rng.randbytes(20_000) for i in range(5)}
        src_a = tmp_path / "a"
        src_b = tmp_path / "b"
        _make_tree(str(src_a), spec)
        _make_tree(str(src_b), spec)

        first = chunkstore.backup(str(src_a), small_config_storage, "prod-a")
        counting = CountingStorage(small_config_storage)
        second = chunkstore.backup(str(src_b), counting, "prod-b")
        # prod-b re-reads the files (different mtimes) but uploads almost
        # nothing: all data chunks already exist.
        assert second.new_chunks < first.new_chunks / 2
        assert second.uploaded_bytes < first.uploaded_bytes / 2

    def test_deleted_files_disappear(self, small_config_storage, tmp_path):
        src = tmp_path / "src"
        _make_tree(str(src), {"keep.txt": b"keep", "drop.txt": b"drop"})
        chunkstore.backup(str(src), small_config_storage, "erp")
        os.remove(src / "drop.txt")
        chunkstore.backup(str(src), small_config_storage, "erp")

        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", 2, str(dst))
        assert _read_tree(str(dst)) == {"keep.txt": b"keep"}

    def test_property_random_trees(self, small_config_storage, tmp_path):
        rng = random.Random(11)
        src = tmp_path / "src"
        spec = {}
        for i in range(40):
            depth = rng.randint(0, 3)
            parts = [f"d{rng.randint(0, 3)}" for _ in range(depth)]
            name = "/".join(parts + [f"file{i}.bin"])
            spec[name] = rng.randbytes(rng.choice([0, 1, 100, 5000, 30_000]))
        _make_tree(str(src), spec)
        chunkstore.backup(str(src), small_config_storage, "erp")

        # Mutate: delete some, change some, add some.
        for i, name in enumerate(sorted(spec)):
            if i % 7 == 0:
                os.remove(os.path.join(str(src), name))
            elif i % 5 == 0:
                with open(os.path.join(str(src), name), "wb") as f:
                    f.write(rng.randbytes(rng.randint(1, 10_000)))
        _make_tree(str(src), {"new/extra.bin": rng.randbytes(12_345)})
        chunkstore.backup(str(src), small_config_storage, "erp")

        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", 2, str(dst))
        assert _read_tree(str(src)) == _read_tree(str(dst))


class TestRetention:
    def _revisions(self, days_ago: list[int], now):
        return [
            (i + 1, now - datetime.timedelta(days=d)) for i, d in enumerate(days_ago)
        ]

    def test_keep_all_recent(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        revs = self._revisions([0, 1, 2, 3], now)
        kept = select_revisions_to_keep(revs, parse_keep(["1:7"]), now)
        assert kept == {1, 2, 3, 4}

    def test_daily_thinning(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        # Two revisions on the same old day: only one survives the 1:7 rule.
        revs = [
            (1, now - datetime.timedelta(days=10, hours=2)),
            (2, now - datetime.timedelta(days=10, hours=1)),
            (3, now - datetime.timedelta(days=1)),
        ]
        kept = select_revisions_to_keep(revs, parse_keep(["1:7"]), now)
        assert 3 in kept
        assert len(kept & {1, 2}) == 1

    def test_zero_interval_drops(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        revs = self._revisions([400, 300, 1], now)
        kept = select_revisions_to_keep(revs, parse_keep(["0:365", "1:7"]), now)
        assert 1 not in kept  # older than 365d, dropped
        assert 3 in kept

    def test_newest_always_kept(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        revs = self._revisions([500], now)
        kept = select_revisions_to_keep(revs, parse_keep(["0:365"]), now)
        assert kept == {1}


class TestPrune:
    def _backup_days(self, storage, src, snapshot_id, monkeypatch_days):
        """Create a revision whose created_at is `monkeypatch_days` ago."""
        import json

        result = chunkstore.backup(str(src), storage, snapshot_id)
        key = f"snapshots/{snapshot_id}/{result.revision}"
        meta = json.loads(storage.get(key).decode())
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=monkeypatch_days
        )
        meta["created_at"] = created.isoformat()
        storage.put(key, json.dumps(meta).encode())
        return result

    def test_two_step_prune_and_restore_safety(self, small_config_storage, tmp_path):
        rng = random.Random(12)
        storage = small_config_storage
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": rng.randbytes(30_000)})
        self._backup_days(storage, src, "erp", 30)

        # Rev 2: different content, recent.
        with open(src / "a.bin", "wb") as f:
            f.write(rng.randbytes(30_000))
        self._backup_days(storage, src, "erp", 0)

        keep = parse_keep(["0:7"])  # drop everything older than 7 days
        result1 = chunkstore.prune(storage, keep=keep)
        assert "erp/1" in result1.deleted_revisions
        assert result1.fossilized_chunks > 0
        assert result1.deleted_chunks == 0  # step 2 hasn't run for these

        # Fossils exist but latest revision still restores fine.
        dst = tmp_path / "dst"
        chunkstore.restore(storage, "erp", None, str(dst))
        assert _read_tree(str(dst)) == _read_tree(str(src))

        # A new backup happens (moves latest revision forward)...
        with open(src / "a.bin", "ab") as f:
            f.write(b"more")
        self._backup_days(storage, src, "erp", 0)

        # ...and a prune two hours "later" permanently deletes the fossils.
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=2
        )
        result2 = chunkstore.prune(storage, keep=keep, now=later)
        assert result2.deleted_chunks > 0
        assert not [k for k in storage.list("chunks/") if k.endswith(".fsl")]

        # Everything still restores.
        dst2 = tmp_path / "dst2"
        chunkstore.restore(storage, "erp", None, str(dst2))
        assert _read_tree(str(dst2)) == _read_tree(str(src))

    def test_fossil_resurrection(self, small_config_storage, tmp_path):
        """A chunk fossilized in step 1 that a NEW revision references again
        is resurrected in step 2, not deleted."""
        rng = random.Random(13)
        storage = small_config_storage
        src = tmp_path / "src"
        original = rng.randbytes(30_000)
        _make_tree(str(src), {"a.bin": original})
        self._backup_days(storage, src, "erp", 30)

        with open(src / "a.bin", "wb") as f:
            f.write(rng.randbytes(30_000))
        self._backup_days(storage, src, "erp", 0)

        # Step 1 fossilizes rev-1-only chunks.
        chunkstore.prune(storage, keep=parse_keep(["0:7"]))
        fossils = [k for k in storage.list("chunks/") if k.endswith(".fsl")]
        assert fossils

        # The original content comes BACK (new revision references the same
        # chunks — but they're fossilized, so backup re-uploads them).
        with open(src / "a.bin", "wb") as f:
            f.write(original)
        self._backup_days(storage, src, "erp", 0)

        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=2
        )
        chunkstore.prune(storage, keep=parse_keep(["0:7"]), now=later)
        # No fossils left; latest revision restores the original content.
        assert not [k for k in storage.list("chunks/") if k.endswith(".fsl")]
        dst = tmp_path / "dst"
        chunkstore.restore(storage, "erp", None, str(dst))
        assert _read_tree(str(dst)) == {"a.bin": original}


class TestLocalStorage:
    def test_key_escape_rejected(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        with pytest.raises(ValueError, match="escapes"):
            storage.get("../outside")

    def test_list_revisions(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        storage.put("snapshots/erp/1", b"{}")
        storage.put("snapshots/erp/3", b"{}")
        storage.put("snapshots/erp/2", b"{}")
        assert list_revisions(storage, "erp") == [1, 2, 3]


class TestPruneKeepRevisions:
    def test_explicit_keep_revisions(self, small_config_storage, tmp_path):
        """Caller-driven retention keeps exactly the named revisions (plus
        the newest) — the lockstep mode used by backup_ops."""
        rng = random.Random(21)
        storage = small_config_storage
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": rng.randbytes(20_000)})
        for _ in range(3):
            with open(src / "a.bin", "wb") as f:
                f.write(rng.randbytes(20_000))
            chunkstore.backup(str(src), storage, "erp")
        assert list_revisions(storage, "erp") == [1, 2, 3]

        chunkstore.prune(storage, keep_revisions={"erp": {2}})
        assert list_revisions(storage, "erp") == [2, 3]

        # Both survivors restore.
        for rev in (2, 3):
            dst = tmp_path / f"dst{rev}"
            chunkstore.restore(storage, "erp", rev, str(dst))

    def test_prune_requires_a_policy(self, small_config_storage):
        with pytest.raises(ValueError, match="keep"):
            chunkstore.prune(small_config_storage)


class TestCountingStorage:
    """The dedup tests' instrument: if its counters lie, every "how many PUTs
    did this incremental backup do?" assertion silently passes."""

    def _counting(self, tmp_path):
        return CountingStorage(LocalStorage(str(tmp_path / "store")))

    def test_every_operation_is_counted_once(self, tmp_path):
        storage = self._counting(tmp_path)

        storage.put("k", b"v")
        storage.exists("k")
        storage.get("k")
        storage.list("")
        storage.rename("k", "k2")
        storage.delete("k2")

        assert storage.counts == {
            "exists": 1,
            "get": 1,
            "put": 1,
            "list": 1,
            "rename": 1,
            "delete": 1,
        }

    def test_counters_start_at_zero(self, tmp_path):
        assert set(self._counting(tmp_path).counts.values()) == {0}

    def test_repeated_calls_accumulate(self, tmp_path):
        storage = self._counting(tmp_path)
        storage.put("a", b"1")
        storage.put("b", b"2")

        assert storage.counts["put"] == 2

    def test_every_call_is_delegated_to_the_inner_storage(self, tmp_path):
        storage = self._counting(tmp_path)

        storage.put("k", b"payload")

        assert storage.exists("k") is True
        assert storage.get("k") == b"payload"
        assert storage.list("") == ["k"]

    def test_rename_moves_the_object_in_the_inner_storage(self, tmp_path):
        storage = self._counting(tmp_path)
        storage.put("old", b"payload")

        storage.rename("old", "new")

        assert storage.inner.exists("new") is True
        assert storage.inner.exists("old") is False

    def test_delete_removes_from_the_inner_storage(self, tmp_path):
        storage = self._counting(tmp_path)
        storage.put("k", b"v")

        storage.delete("k")

        assert storage.inner.exists("k") is False

    def test_exists_reports_the_inner_answer(self, tmp_path):
        storage = self._counting(tmp_path)

        assert storage.exists("absent") is False


class TestChunkerParameters:
    """Constructor validation and the size invariants it protects.

    Every emitted chunk must be >= min_size (except the last of a stream) and
    <= max_size: a chunker that can emit a 0-byte or oversized chunk breaks
    both dedup and the restore-side buffer bound.
    """

    def test_min_size_must_be_positive(self):
        with pytest.raises(ValueError, match="min_size"):
            Chunker(b"seed", min_size=0, avg_size=4096, max_size=16384)

    def test_min_size_of_one_is_accepted(self):
        # The bound is `0 < min_size`, not `1 < min_size`.
        Chunker(b"seed", min_size=1, avg_size=4096, max_size=16384)

    def test_min_size_may_equal_avg_size(self):
        Chunker(b"seed", min_size=4096, avg_size=4096, max_size=16384)

    def test_avg_size_may_equal_max_size(self):
        Chunker(b"seed", min_size=1024, avg_size=4096, max_size=4096)

    def test_min_size_above_avg_size_is_rejected(self):
        with pytest.raises(ValueError):
            Chunker(b"seed", min_size=8192, avg_size=4096, max_size=16384)

    def test_avg_size_above_max_size_is_rejected(self):
        with pytest.raises(ValueError):
            Chunker(b"seed", min_size=1024, avg_size=16384, max_size=4096)

    @pytest.mark.parametrize("avg", [3000, 4095, 5000, 6144])
    def test_non_power_of_two_avg_is_rejected(self, avg):
        with pytest.raises(ValueError, match="power of two"):
            Chunker(b"seed", min_size=1024, avg_size=avg, max_size=65536)

    @pytest.mark.parametrize("avg", [1, 2, 1024, 4096])
    def test_powers_of_two_are_accepted(self, avg):
        Chunker(b"seed", min_size=1, avg_size=avg, max_size=65536)

    def test_the_boundary_mask_is_avg_size_minus_one(self):
        # A wrong mask changes the average chunk size, silently degrading the
        # dedup ratio without any test failing.
        assert Chunker(b"seed", min_size=1024, avg_size=4096, max_size=16384)._mask == (
            4095
        )

    def test_no_chunk_is_smaller_than_min_size_except_the_last(self):
        data = random.Random(7).randbytes(300_000)
        chunks = _chunk_all(Chunker(b"seed", **SMALL), data)

        assert all(len(c) >= SMALL["min_size"] for c in chunks[:-1])
        assert chunks[-1]

    def test_no_chunk_exceeds_max_size(self):
        # Incompressible random data rarely hits a boundary naturally, so the
        # forced cut at max_size is what bounds it.
        data = random.Random(8).randbytes(300_000)
        chunks = _chunk_all(Chunker(b"seed", **SMALL), data)

        assert max(len(c) for c in chunks) <= SMALL["max_size"]

    def test_uniform_input_stays_within_the_size_bounds(self):
        # All-zero input gives the rolling hash nothing to vary, so the cut
        # position is whatever the fixed window hash dictates — but it must
        # still respect both bounds and lose no bytes.
        chunks = _chunk_all(Chunker(b"seed", **SMALL), b"\x00" * 100_000)

        assert all(
            SMALL["min_size"] <= len(c) <= SMALL["max_size"] for c in chunks[:-1]
        )
        assert sum(len(c) for c in chunks) == 100_000
        assert b"".join(chunks) == b"\x00" * 100_000

    def test_the_stream_is_reassembled_exactly(self):
        data = random.Random(9).randbytes(250_000)
        chunks = _chunk_all(Chunker(b"seed", **SMALL), data)

        assert b"".join(chunks) == data

    def test_an_empty_stream_emits_no_chunk(self):
        assert _chunk_all(Chunker(b"seed", **SMALL), b"") == []

    def test_input_shorter_than_min_size_is_one_chunk(self):
        chunks = _chunk_all(Chunker(b"seed", **SMALL), b"x" * 100)

        assert chunks == [b"x" * 100]

    def test_flush_cuts_the_stream_at_the_call_site(self):
        # Callers rely on this to reuse an unchanged file's chunks verbatim.
        chunker = Chunker(b"seed", **SMALL)
        first = list(chunker.update(b"a" * 500)) + list(chunker.flush())
        second = list(chunker.update(b"b" * 500)) + list(chunker.flush())

        assert first == [b"a" * 500]
        assert second == [b"b" * 500]


class TestDeriveTable:
    def test_yields_exactly_256_entries(self):
        # One entry per byte value; 255 or 257 would index out of range or
        # leave a byte unmapped.
        assert len(derive_table(b"seed")) == 256

    def test_every_entry_is_a_64_bit_value(self):
        assert all(0 <= v < 2**64 for v in derive_table(b"seed"))

    def test_entries_are_distinct(self):
        # A repeated entry would make two byte values interchangeable to the
        # rolling hash, weakening the boundary distribution.
        assert len(set(derive_table(b"seed"))) == 256

    def test_different_seeds_give_unrelated_tables(self):
        a, b = derive_table(b"seed-a"), derive_table(b"seed-b")

        assert a != b
        assert len(set(a) & set(b)) == 0

    def test_the_same_seed_always_gives_the_same_table(self):
        assert derive_table(b"seed") == derive_table(b"seed")

    def test_the_first_entry_comes_from_counter_zero(self):
        # Pins the counter start and the 8-byte little-endian slicing: any
        # drift here silently re-chunks every existing storage.
        import hashlib

        block = hashlib.blake2b(
            (0).to_bytes(8, "little"), key=b"seed", digest_size=64
        ).digest()

        table = derive_table(b"seed")
        assert table[0] == int.from_bytes(block[0:8], "little")
        assert table[7] == int.from_bytes(block[56:64], "little")

    def test_the_ninth_entry_comes_from_counter_one(self):
        import hashlib

        block = hashlib.blake2b(
            (1).to_bytes(8, "little"), key=b"seed", digest_size=64
        ).digest()

        assert derive_table(b"seed")[8] == int.from_bytes(block[0:8], "little")


class TestChunkContainer:
    def test_the_header_is_magic_version_flags(self):
        blob = encode_chunk(b"payload")

        assert blob[:4] == b"ODCK"
        assert blob[4] == 1

    def test_compressible_data_sets_the_zstd_flag(self):
        blob = encode_chunk(b"a" * 10_000)

        assert blob[5] & 0x01
        assert len(blob) < 10_000

    def test_incompressible_data_is_stored_raw(self):
        data = random.Random(3).randbytes(2048)
        blob = encode_chunk(data)

        assert blob[5] == 0
        assert blob[6:] == data

    def test_a_truncated_blob_is_corrupt(self, tmp_path):
        config = ensure_config(LocalStorage(str(tmp_path)))

        for length in (0, 3, 5):
            with pytest.raises(ChunkCorruptedError, match="magic"):
                decode_chunk(b"ODCK12"[:length], config, "deadbeef")

    def test_a_six_byte_header_is_long_enough(self, tmp_path):
        # The check is `len < 6`; a header-only chunk of an empty payload is
        # valid and must fail on the hash, not the length.
        config = ensure_config(LocalStorage(str(tmp_path)))
        blob = encode_chunk(b"")

        assert len(blob) == 6
        assert decode_chunk(blob, config, config.chunk_hash(b"")) == b""

    def test_wrong_magic_is_corrupt(self, tmp_path):
        config = ensure_config(LocalStorage(str(tmp_path)))
        blob = b"XXXX" + encode_chunk(b"payload")[4:]

        with pytest.raises(ChunkCorruptedError, match="magic"):
            decode_chunk(blob, config, "deadbeef")

    def test_an_unknown_version_is_refused(self, tmp_path):
        config = ensure_config(LocalStorage(str(tmp_path)))
        blob = bytearray(encode_chunk(b"payload"))
        blob[4] = 99

        with pytest.raises(ChunkCorruptedError, match="version"):
            decode_chunk(bytes(blob), config, "deadbeef")

    def test_the_reserved_encryption_flag_is_refused(self, tmp_path):
        config = ensure_config(LocalStorage(str(tmp_path)))
        blob = bytearray(encode_chunk(b"payload"))
        blob[5] |= 0x02

        with pytest.raises(ChunkCorruptedError, match="encrypted"):
            decode_chunk(bytes(blob), config, "deadbeef")

    def test_a_damaged_zstd_payload_is_corrupt(self, tmp_path):
        config = ensure_config(LocalStorage(str(tmp_path)))
        blob = bytearray(encode_chunk(b"a" * 10_000))
        blob[10] ^= 0xFF

        with pytest.raises(ChunkCorruptedError):
            decode_chunk(bytes(blob), config, config.chunk_hash(b"a" * 10_000))


class TestContentAddressing:
    def test_the_hash_is_keyed_by_the_storage(self, tmp_path):
        # Two storages must not produce the same chunk hash for the same
        # bytes, or a listing would confirm known plaintext.
        one = ensure_config(LocalStorage(str(tmp_path / "a")))
        two = ensure_config(LocalStorage(str(tmp_path / "b")))

        assert one.chunk_hash(b"payload") != two.chunk_hash(b"payload")

    def test_the_id_is_derived_from_the_hash_not_the_content(self, tmp_path):
        config = ensure_config(LocalStorage(str(tmp_path)))
        digest = config.chunk_hash(b"payload")

        assert config.chunk_id(digest) != digest
        assert config.chunk_id(digest) == config.chunk_id(digest)

    def test_keys_are_32_bytes(self, tmp_path):
        config = ensure_config(LocalStorage(str(tmp_path)))

        assert len(config.seed) == 32
        assert len(config.hash_key) == 32
        assert len(config.id_key) == 32

    def test_a_new_storage_gets_fresh_keys(self, tmp_path):
        one = ensure_config(LocalStorage(str(tmp_path / "a")))
        two = ensure_config(LocalStorage(str(tmp_path / "b")))

        assert one.seed != two.seed
        assert one.hash_key != two.hash_key
        assert one.id_key != two.id_key

    def test_the_config_survives_a_reload(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        first = ensure_config(storage)

        assert ensure_config(storage) == first


class TestRetentionBoundaries:
    """Retention decides what is deleted forever, so the age comparison and
    the bucket arithmetic both need pinning."""

    _NOW = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)

    def _rev(self, revision: int, days: float):
        return (revision, self._NOW - datetime.timedelta(days=days))

    def test_a_revision_exactly_at_the_age_is_still_recent(self):
        # The rule fires on `age_days > age`, so a 7-day-old revision under a
        # 1:7 policy is kept unconditionally rather than thinned.
        revs = [self._rev(1, 7), self._rev(2, 7.5), self._rev(3, 0)]

        kept = select_revisions_to_keep(revs, parse_keep(["1:7"]), self._NOW)

        assert 1 in kept

    def test_two_revisions_on_the_same_old_day_collapse_to_one(self):
        # Both are 8 days old (past the 7-day age) and fall on the same
        # calendar day, so the 1-day interval keeps exactly one.
        revs = [self._rev(1, 8.5), self._rev(2, 8.9), self._rev(3, 0)]

        kept = select_revisions_to_keep(revs, parse_keep(["1:7"]), self._NOW)

        assert len(kept & {1, 2}) == 1
        assert 3 in kept

    def test_an_empty_revision_list_keeps_nothing(self):
        assert select_revisions_to_keep([], parse_keep(["1:7"]), self._NOW) == set()

    def test_without_any_rule_everything_is_kept(self):
        revs = [self._rev(1, 900), self._rev(2, 0)]

        assert select_revisions_to_keep(revs, [], self._NOW) == {1, 2}

    def test_the_oldest_matching_rule_wins(self):
        # 0:365 must beat 1:7 for a 400-day-old revision, dropping it rather
        # than thinning it to one per day.
        revs = [self._rev(1, 400), self._rev(2, 401), self._rev(3, 0)]

        kept = select_revisions_to_keep(revs, parse_keep(["1:7", "0:365"]), self._NOW)

        assert kept == {3}

    def test_a_wider_interval_never_keeps_more_revisions(self):
        # Buckets are absolute epoch windows, so the exact survivors depend on
        # where the dates fall — but widening the interval can only thin more.
        revs = [self._rev(i, 10 + i * 1.3) for i in range(12)]

        counts = [
            len(select_revisions_to_keep(revs, parse_keep([f"{i}:7"]), self._NOW))
            for i in (1, 3, 7, 30)
        ]

        assert counts == sorted(counts, reverse=True)
        assert counts[-1] < counts[0]

    def test_thinning_only_touches_revisions_past_the_age(self):
        # Six revisions in one recent day: none is older than 7 days, so the
        # 1-day interval must not collapse them.
        revs = [self._rev(i, 1 + i * 0.1) for i in range(6)]

        kept = select_revisions_to_keep(revs, parse_keep(["1:7"]), self._NOW)

        assert len(kept) == 6


class TestParseIso:
    def test_a_naive_timestamp_is_read_as_utc(self):
        # Revision timestamps decide retention; reading a naive one as
        # host-local time would shift every deadline by the UTC offset.
        from oduflow.chunkstore.prune import _parse_iso

        parsed = _parse_iso("2026-03-01T12:00:00")

        assert parsed.tzinfo is datetime.timezone.utc
        assert parsed.hour == 12

    def test_an_explicit_offset_is_preserved(self):
        from oduflow.chunkstore.prune import _parse_iso

        parsed = _parse_iso("2026-03-01T14:00:00+02:00")

        assert parsed.astimezone(datetime.timezone.utc).hour == 12


class TestRestoreDetails:
    def _restore_roundtrip(self, storage, tmp_path, spec):
        src = tmp_path / "src"
        _make_tree(str(src), spec)
        chunkstore.backup(str(src), storage, "erp")
        dst = tmp_path / "dst"
        info = chunkstore.restore(storage, "erp", None, str(dst))
        return src, dst, info

    def test_the_byte_count_matches_the_tree(self, small_config_storage, tmp_path):
        spec = {"a.bin": b"x" * 1000, "b.bin": b"y" * 2500}

        _, _, info = self._restore_roundtrip(small_config_storage, tmp_path, spec)

        assert info["bytes"] == 3500
        assert info["files"] == 2

    def test_an_empty_file_is_restored_without_reading_a_chunk(
        self, small_config_storage, tmp_path
    ):
        # size == 0 must skip the chunk range entirely; a mutant reading
        # chunk 0 would corrupt the file with foreign bytes.
        _, dst, info = self._restore_roundtrip(
            small_config_storage, tmp_path, {"empty.txt": b"", "other.bin": b"z" * 500}
        )

        assert (dst / "empty.txt").read_bytes() == b""
        assert info["bytes"] == 500

    def test_a_missing_snapshot_raises(self, small_config_storage, tmp_path):
        with pytest.raises(FileNotFoundError, match="No revisions"):
            chunkstore.restore(
                small_config_storage, "never-backed-up", None, str(tmp_path / "dst")
            )

    def test_an_explicit_revision_is_honoured(self, small_config_storage, tmp_path):
        src = tmp_path / "src"
        _make_tree(str(src), {"a.txt": b"first"})
        chunkstore.backup(str(src), small_config_storage, "erp")
        with open(src / "a.txt", "wb") as f:
            f.write(b"second")
        chunkstore.backup(str(src), small_config_storage, "erp")

        dst = tmp_path / "dst"
        info = chunkstore.restore(small_config_storage, "erp", 1, str(dst))

        assert info["revision"] == 1
        assert (dst / "a.txt").read_bytes() == b"first"

    def test_a_path_escaping_the_target_is_refused(
        self, small_config_storage, tmp_path
    ):
        # A tampered revision must not be able to write outside target_dir.
        import importlib

        from oduflow.chunkstore.backup import load_revision
        from oduflow.chunkstore.format import ensure_config as _ensure

        # The package re-exports the restore() function, so the name
        # oduflow.chunkstore.restore resolves to that function, not to the
        # module — patch() by dotted string cannot reach the module's globals.
        restore_module = importlib.import_module("oduflow.chunkstore.restore")

        src = tmp_path / "src"
        _make_tree(str(src), {"a.txt": b"payload"})
        chunkstore.backup(str(src), small_config_storage, "erp")

        config = _ensure(small_config_storage)
        _meta, files, _hashes, _lengths = load_revision(
            small_config_storage, config, "erp", 1
        )
        files[0]["path"] = "../escaped.txt"

        with (
            patch.object(
                restore_module,
                "load_revision",
                return_value=(_meta, files, _hashes, _lengths),
            ),
            pytest.raises(ValueError, match="escapes target dir"),
        ):
            chunkstore.restore(small_config_storage, "erp", 1, str(tmp_path / "dst"))

        assert not (tmp_path / "escaped.txt").exists()


class TestBackupMetadata:
    """The revision file is the backup's index: wrong counters or chunk spans
    mean a restore that silently produces different bytes."""

    def _meta(self, storage, snapshot_id, revision):
        import json as _json

        key = f"snapshots/{snapshot_id}/{revision}"
        return _json.loads(storage.get(key).decode())

    def test_the_schema_version_is_recorded(self, small_config_storage, tmp_path):
        src = tmp_path / "src"
        _make_tree(str(src), {"a.txt": b"x"})
        chunkstore.backup(str(src), small_config_storage, "erp")

        assert self._meta(small_config_storage, "erp", 1)["version"] == 1

    def test_counters_match_the_tree(self, small_config_storage, tmp_path):
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": b"x" * 700, "b.bin": b"y" * 300})

        result = chunkstore.backup(str(src), small_config_storage, "erp")

        assert result.files == 2
        assert result.total_bytes == 1000
        assert result.unchanged_files == 0
        meta = self._meta(small_config_storage, "erp", 1)
        assert meta["files"] == 2
        assert meta["total_bytes"] == 1000

    def test_an_empty_file_gets_a_zero_length_chunk_span(
        self, small_config_storage, tmp_path
    ):
        # sc/so/ec/eo must all be 0 so restore writes nothing; any non-zero
        # value would splice in a neighbouring file's bytes.
        from oduflow.chunkstore.backup import load_revision
        from oduflow.chunkstore.format import ensure_config as _ensure

        src = tmp_path / "src"
        _make_tree(str(src), {"empty.txt": b"", "other.bin": b"z" * 5000})
        chunkstore.backup(str(src), small_config_storage, "erp")

        config = _ensure(small_config_storage)
        _meta, files, _h, _l = load_revision(small_config_storage, config, "erp", 1)
        empty = next(f for f in files if f["path"] == "empty.txt")

        assert (empty["sc"], empty["so"], empty["ec"], empty["eo"]) == (0, 0, 0, 0)
        assert empty["size"] == 0

    def test_unchanged_files_are_counted_on_the_second_revision(
        self, small_config_storage, tmp_path
    ):
        src = tmp_path / "src"
        _make_tree(str(src), {f"f{i}.bin": bytes([i]) * 5000 for i in range(4)})
        chunkstore.backup(str(src), small_config_storage, "erp")
        with open(src / "f1.bin", "wb") as f:
            f.write(b"changed" * 800)

        result = chunkstore.backup(str(src), small_config_storage, "erp")

        assert result.unchanged_files == 3
        assert result.files == 4

    def test_an_unchanged_empty_file_still_counts_as_unchanged(
        self, small_config_storage, tmp_path
    ):
        # Zero-size files take the early-return path, which has its own
        # unchanged_count increment.
        src = tmp_path / "src"
        _make_tree(str(src), {"empty.txt": b"", "a.bin": b"x" * 100})
        chunkstore.backup(str(src), small_config_storage, "erp")

        result = chunkstore.backup(str(src), small_config_storage, "erp")

        assert result.unchanged_files == 2

    def test_entries_are_sorted_by_path(self, small_config_storage, tmp_path):
        # Restore reconstructs in path order and relies on consecutive small
        # files sharing chunks; unsorted entries break the one-slot cache.
        from oduflow.chunkstore.backup import load_revision
        from oduflow.chunkstore.format import ensure_config as _ensure

        src = tmp_path / "src"
        _make_tree(str(src), {"z.bin": b"z", "a.bin": b"a", "m/n.bin": b"n"})
        chunkstore.backup(str(src), small_config_storage, "erp")

        config = _ensure(small_config_storage)
        _meta, files, _h, _l = load_revision(small_config_storage, config, "erp", 1)
        paths = [f["path"] for f in files]

        assert paths == sorted(paths)

    def test_an_explicit_prev_revision_is_used_as_the_base(
        self, small_config_storage, tmp_path
    ):
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": b"x" * 5000})
        chunkstore.backup(str(src), small_config_storage, "erp")
        chunkstore.backup(str(src), small_config_storage, "erp")

        result = chunkstore.backup(
            str(src), small_config_storage, "erp", prev_revision=1
        )

        assert result.unchanged_files == 1

    def test_an_unknown_prev_revision_falls_back_to_a_full_backup(
        self, small_config_storage, tmp_path
    ):
        # Not to "the latest": an explicitly requested base that does not
        # exist must not silently become an incremental against something else.
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": b"x" * 5000})
        chunkstore.backup(str(src), small_config_storage, "erp")

        result = chunkstore.backup(
            str(src), small_config_storage, "erp", prev_revision=99
        )

        assert result.unchanged_files == 0

    def test_revision_numbers_increment_from_one(self, small_config_storage, tmp_path):
        src = tmp_path / "src"
        _make_tree(str(src), {"a.txt": b"x"})

        first = chunkstore.backup(str(src), small_config_storage, "erp")
        second = chunkstore.backup(str(src), small_config_storage, "erp")

        assert (first.revision, second.revision) == (1, 2)
        assert list_revisions(small_config_storage, "erp") == [1, 2]

    def test_a_missing_source_directory_raises(self, small_config_storage, tmp_path):
        with pytest.raises(FileNotFoundError, match="source_dir"):
            chunkstore.backup(str(tmp_path / "nope"), small_config_storage, "erp")


class TestPruneCounters:
    """PruneResult is what the dashboard and the scheduler log; an off-by-one
    there is how a broken prune looks healthy."""

    def _aged_backup(self, storage, src, snapshot_id, days):
        import json as _json

        result = chunkstore.backup(str(src), storage, snapshot_id)
        key = f"snapshots/{snapshot_id}/{result.revision}"
        meta = _json.loads(storage.get(key).decode())
        meta["created_at"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        ).isoformat()
        storage.put(key, _json.dumps(meta).encode())
        return result

    def test_a_clean_store_reports_all_zeroes(self, small_config_storage, tmp_path):
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": b"x" * 5000})
        chunkstore.backup(str(src), small_config_storage, "erp")

        result = chunkstore.prune(small_config_storage, keep=parse_keep(["1:7"]))

        assert result.deleted_revisions == []
        assert result.deleted_chunks == 0
        assert result.resurrected_chunks == 0

    def test_dropping_one_revision_fossilizes_its_unique_chunks(
        self, small_config_storage, tmp_path
    ):
        rng = random.Random(21)
        storage = small_config_storage
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": rng.randbytes(30_000)})
        self._aged_backup(storage, src, "erp", 400)
        with open(src / "a.bin", "wb") as f:
            f.write(rng.randbytes(30_000))
        chunkstore.backup(str(src), storage, "erp")

        result = chunkstore.prune(storage, keep=parse_keep(["0:365"]))

        assert result.deleted_revisions == ["erp/1"]
        assert result.fossilized_chunks > 0
        assert result.collections_written == 1

    def _fossilize(self, storage, src, rng):
        """Leave one aged collection of fossils behind, then move the latest
        revision forward so only maturity blocks the sweep."""
        _make_tree(str(src), {"a.bin": rng.randbytes(30_000)})
        self._aged_backup(storage, src, "erp", 400)
        with open(src / "a.bin", "wb") as f:
            f.write(rng.randbytes(30_000))
        chunkstore.backup(str(src), storage, "erp")
        first = chunkstore.prune(storage, keep=parse_keep(["0:365"]))
        with open(src / "a.bin", "ab") as f:
            f.write(b"more")
        chunkstore.backup(str(src), storage, "erp")
        return first

    def test_a_fresh_collection_is_not_swept(self, small_config_storage, tmp_path):
        # Fossils must age past _COLLECTION_MIN_AGE before deletion, so a
        # concurrent restore cannot lose a chunk mid-read.
        storage = small_config_storage
        first = self._fossilize(storage, tmp_path / "src", random.Random(22))

        second = chunkstore.prune(storage, keep=parse_keep(["0:365"]))

        assert first.fossilized_chunks > 0
        assert second.deleted_chunks == 0
        assert second.collections_deleted == 0

    def test_a_matured_collection_is_swept(self, small_config_storage, tmp_path):
        storage = small_config_storage
        first = self._fossilize(storage, tmp_path / "src", random.Random(23))
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=2
        )

        second = chunkstore.prune(storage, keep=parse_keep(["0:365"]), now=later)

        assert first.fossilized_chunks > 0
        assert second.deleted_chunks == first.fossilized_chunks
        assert second.collections_deleted == 1
        assert not [k for k in storage.list("chunks/") if k.endswith(".fsl")]

    def test_prune_without_a_policy_is_refused(self, small_config_storage):
        with pytest.raises(ValueError, match="keep or keep_revisions"):
            chunkstore.prune(small_config_storage)
