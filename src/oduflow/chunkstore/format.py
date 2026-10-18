"""On-wire formats: chunk container, storage config, revision files.

Chunk container: ``ODCK`` magic, 1-byte version, 1-byte flags, payload.
Flag bit 0 = zstd compression; bit 1 is reserved for client-side
encryption (deliberately not implemented in v1 — S3 server-side encryption
covers at-rest confidentiality without a key-loss risk; chunk IDs/hashes
are keyed regardless so listings don't confirm known plaintext).

Content addressing splits identity in two:

- ``chunk_hash = blake2b(plaintext, key=hash_key)`` — stored inside
  revision metadata, verifies integrity on restore;
- ``chunk_id = blake2b(chunk_hash, key=id_key)`` — the storage filename.

Knowing an ID (visible in bucket listings) reveals nothing about the
hash/content; both keys live in the storage ``config`` object.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

import zstandard

from oduflow.chunkstore.chunker import (
    DEFAULT_AVG_SIZE,
    DEFAULT_MAX_SIZE,
    DEFAULT_MIN_SIZE,
)
from oduflow.chunkstore.storage import Storage

MAGIC = b"ODCK"
VERSION = 1
FLAG_ZSTD = 0x01
FLAG_ENCRYPTED = 0x02  # reserved

CONFIG_KEY = "config"

# Metadata chunker parameters (file lists / hash lists are much smaller
# than data, so they get a smaller average).
META_MIN_SIZE = 64 * 1024
META_AVG_SIZE = 256 * 1024
META_MAX_SIZE = 1024 * 1024


class ChunkCorruptedError(Exception):
    """A chunk failed integrity verification (bad magic, hash mismatch)."""


@dataclass(frozen=True)
class StoreConfig:
    """Per-storage parameters and keys, persisted as the ``config`` object."""

    seed: bytes
    hash_key: bytes
    id_key: bytes
    min_size: int = DEFAULT_MIN_SIZE
    avg_size: int = DEFAULT_AVG_SIZE
    max_size: int = DEFAULT_MAX_SIZE

    def chunk_hash(self, plaintext: bytes) -> str:
        return hashlib.blake2b(plaintext, key=self.hash_key, digest_size=32).hexdigest()

    def chunk_id(self, chunk_hash: str) -> str:
        return hashlib.blake2b(
            bytes.fromhex(chunk_hash), key=self.id_key, digest_size=32
        ).hexdigest()


def _parse_config(raw: dict[str, object]) -> StoreConfig:
    return StoreConfig(
        seed=bytes.fromhex(str(raw["seed"])),
        hash_key=bytes.fromhex(str(raw["hash_key"])),
        id_key=bytes.fromhex(str(raw["id_key"])),
        min_size=int(str(raw["min_size"])),
        avg_size=int(str(raw["avg_size"])),
        max_size=int(str(raw["max_size"])),
    )


def ensure_config(storage: Storage) -> StoreConfig:
    """Load the storage config, creating it (random keys) on first use.

    Creation is a conditional write: the config holds the store's chunker seed
    and hash/id keys, so two concurrent first-ever backups racing an
    exists-then-put would each mint different keys and the loser's revision
    would be permanently unrestorable. First writer wins; the loser adopts the
    winner's config.
    """
    if storage.exists(CONFIG_KEY):
        return _parse_config(json.loads(storage.get(CONFIG_KEY).decode("utf-8")))
    config = StoreConfig(
        seed=secrets.token_bytes(32),
        hash_key=secrets.token_bytes(32),
        id_key=secrets.token_bytes(32),
    )
    payload = json.dumps(
        {
            "version": VERSION,
            "seed": config.seed.hex(),
            "hash_key": config.hash_key.hex(),
            "id_key": config.id_key.hex(),
            "min_size": config.min_size,
            "avg_size": config.avg_size,
            "max_size": config.max_size,
        },
        indent=2,
    ).encode("utf-8")
    if not storage.put_if_absent(CONFIG_KEY, payload):
        return _parse_config(json.loads(storage.get(CONFIG_KEY).decode("utf-8")))
    return config


def chunk_key(chunk_id: str) -> str:
    """Storage key for a chunk: one-level fan-out (works for both local
    directories and S3 prefixes)."""
    return f"chunks/{chunk_id[:2]}/{chunk_id[2:]}"


def revision_key(snapshot_id: str, revision: int) -> str:
    return f"snapshots/{snapshot_id}/{revision}"


def encode_chunk(plaintext: bytes) -> bytes:
    """Wrap plaintext into the chunk container (zstd if it helps)."""
    compressed = zstandard.ZstdCompressor(level=3).compress(plaintext)
    if len(compressed) < len(plaintext):
        return MAGIC + bytes((VERSION, FLAG_ZSTD)) + compressed
    return MAGIC + bytes((VERSION, 0)) + plaintext


def decode_chunk(blob: bytes, config: StoreConfig, expected_hash: str) -> bytes:
    """Unwrap a chunk container and verify its keyed hash."""
    if len(blob) < 6 or blob[:4] != MAGIC:
        raise ChunkCorruptedError("bad chunk magic")
    version, flags = blob[4], blob[5]
    if version != VERSION:
        raise ChunkCorruptedError(f"unsupported chunk version {version}")
    if flags & FLAG_ENCRYPTED:
        raise ChunkCorruptedError("encrypted chunks are not supported")
    payload = blob[6:]
    if flags & FLAG_ZSTD:
        try:
            payload = zstandard.ZstdDecompressor().decompress(
                payload, max_output_size=DEFAULT_MAX_SIZE * 4
            )
        except zstandard.ZstdError as exc:
            raise ChunkCorruptedError(f"chunk decompression failed: {exc}") from exc
    if config.chunk_hash(payload) != expected_hash:
        raise ChunkCorruptedError("chunk hash mismatch")
    return payload


def dump_json_lines(value: Any) -> bytes:
    """Stable JSON serialization for metadata lists."""
    return json.dumps(value, separators=(",", ":"), sort_keys=False).encode("utf-8")
