"""Prune: retention policy + two-step fossil collection.

Deleting chunks safely without locks is the hard part of a
content-addressed store. The two-step scheme (from the published
lock-free-deduplication design):

**Step 1 — collect.** Apply the retention policy, compute the chunks
referenced only by pruned revisions, and *fossilize* them: rename
``chunks/xx/yyyy`` → ``chunks/xx/yyyy.fsl`` (on S3: CopyObject+Delete).
Record a *fossil collection* file listing the fossils and, per
snapshot_id, the latest revision seen. Then delete the pruned revision
files. Nothing is permanently lost yet — a fossil can be resurrected.

**Step 2 — delete (a later prune run).** A collection's fossils may be
permanently deleted only when every snapshot_id it recorded has produced
a NEWER revision since (any backup that was in flight during step 1 has
finished — and a finished backup re-uploads any chunk it needed that had
been fossilized, because fossilized chunks fail the existence check) and
a safety slack has passed. Fossils that a kept revision still references
are resurrected instead.

Oduflow serializes backup and prune per storage (scheduler thread + team
lock), which makes the scheme conservative rather than load-bearing — but
it keeps the store correct even if that serialization is ever lost.

Collections are stored IN the storage (``fossils/<id>.json``), not on the
local machine: the server owning the store may be reinstalled; prune state
must survive with the data.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field

from oduflow.chunkstore.backup import list_revisions, load_revision
from oduflow.chunkstore.format import (
    StoreConfig,
    chunk_key,
    ensure_config,
    revision_key,
)
from oduflow.chunkstore.storage import Storage

logger = logging.getLogger("oduflow")

_FOSSIL_SUFFIX = ".fsl"
_COLLECTION_PREFIX = "fossils/"
# Minimum age of a collection before its fossils may be deleted (slack for
# eventually-consistent object stores).
_COLLECTION_MIN_AGE = datetime.timedelta(hours=1)


@dataclass
class PruneResult:
    deleted_revisions: list[str] = field(default_factory=list)
    fossilized_chunks: int = 0
    deleted_chunks: int = 0
    resurrected_chunks: int = 0
    collections_written: int = 0
    collections_deleted: int = 0


def _parse_iso(value: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def select_revisions_to_keep(
    revisions: list[tuple[int, datetime.datetime]],
    keep: list[tuple[int, int]],
    now: datetime.datetime,
) -> set[int]:
    """Apply duplicacy-style ``interval:age`` retention pairs.

    ``keep`` pairs mean: for revisions older than ``age`` days, keep one
    per ``interval`` days (``interval=0``: keep none older than ``age``).
    Pairs are evaluated most-restrictive-oldest-first (sorted by age
    descending). Revisions younger than every pair's age are all kept.
    The newest revision is always kept.
    """
    if not revisions:
        return set()
    ordered = sorted(revisions, key=lambda r: r[1])  # oldest first
    pairs = sorted(keep, key=lambda p: p[1], reverse=True)  # oldest age first
    kept: set[int] = set()
    buckets_seen: dict[tuple[int, int], bool] = {}
    for revision, created in ordered:
        age_days = (now - created).days
        rule = next((p for p in pairs if age_days > p[1]), None)
        if rule is None:
            kept.add(revision)
            continue
        interval = rule[0]
        if interval <= 0:
            continue  # drop everything older than this age
        bucket = int(created.timestamp()) // (interval * 86400)
        key = (rule[1], bucket)
        if key not in buckets_seen:
            buckets_seen[key] = True
            kept.add(revision)
    kept.add(ordered[-1][0])  # never drop the newest
    return kept


def _revision_chunk_hashes(
    storage: Storage, config: StoreConfig, snapshot_id: str, revision: int
) -> set[str]:
    """All chunk hashes a revision depends on (data + metadata chunks)."""
    meta, _files, hashes, _lengths = load_revision(
        storage, config, snapshot_id, revision
    )
    refs = set(hashes)
    refs.update(meta.get("files_meta_chunks", []))
    refs.update(meta.get("hashes_meta_chunks", []))
    refs.update(meta.get("lengths_meta_chunks", []))
    return refs


def _snapshot_ids(storage: Storage) -> list[str]:
    ids = set()
    for key in storage.list("snapshots/"):
        parts = key.split("/")
        if len(parts) >= 3:
            ids.add(parts[1])
    return sorted(ids)


def _collect(
    storage: Storage,
    config: StoreConfig,
    keep: list[tuple[int, int]],
    now: datetime.datetime,
    result: PruneResult,
    keep_revisions: dict[str, set[int]] | None = None,
) -> None:
    """Step 1: retention → fossilize newly-unreferenced chunks."""
    kept_refs: set[str] = set()
    prune_targets: list[tuple[str, int]] = []
    latest: dict[str, int] = {}

    for snapshot_id in _snapshot_ids(storage):
        revisions = list_revisions(storage, snapshot_id)
        if not revisions:
            continue
        latest[snapshot_id] = revisions[-1]
        dated = []
        for revision in revisions:
            try:
                meta = json.loads(
                    storage.get(revision_key(snapshot_id, revision)).decode("utf-8")
                )
                dated.append((revision, _parse_iso(meta["created_at"])))
            except Exception:
                # Unreadable revision: keep it (never delete blindly).
                kept_refs.update(
                    _revision_chunk_hashes(storage, config, snapshot_id, revision)
                )
        if keep_revisions is not None:
            # Caller-driven retention (Oduflow keeps filestore revisions in
            # lockstep with snapshot manifests).
            if snapshot_id in keep_revisions:
                # A snapshot the caller tracks: keep the revisions its surviving
                # manifests reference, plus the newest as an in-progress
                # backup baseline.
                keep_set = set(keep_revisions[snapshot_id])
                keep_set.add(revisions[-1])
            else:
                # Present in storage but not tracked by the caller — e.g. a
                # deleted production whose backups were intentionally kept. Its
                # manifests still reference every revision, so keep them all;
                # collapsing to the newest would strand those manifests on
                # pruned revisions and make all but the latest unrestorable.
                keep_set = set(revisions)
        else:
            keep_set = select_revisions_to_keep(dated, keep, now)
        for revision, _created in dated:
            if revision in keep_set:
                kept_refs.update(
                    _revision_chunk_hashes(storage, config, snapshot_id, revision)
                )
            else:
                prune_targets.append((snapshot_id, revision))

    if not prune_targets:
        return

    pruned_refs: set[str] = set()
    for snapshot_id, revision in prune_targets:
        try:
            pruned_refs.update(
                _revision_chunk_hashes(storage, config, snapshot_id, revision)
            )
        except Exception:
            logger.warning(
                "Could not read revision %s/%s during prune", snapshot_id, revision
            )

    fossils: list[str] = []
    for chunk_hash in sorted(pruned_refs - kept_refs):
        key = chunk_key(config.chunk_id(chunk_hash))
        if storage.exists(key):
            storage.rename(key, key + _FOSSIL_SUFFIX)
            fossils.append(key)
            result.fossilized_chunks += 1

    collection_id = now.strftime("%Y%m%dT%H%M%SZ")
    storage.put(
        f"{_COLLECTION_PREFIX}{collection_id}.json",
        json.dumps(
            {
                "created_at": now.isoformat(),
                "last_revisions": latest,
                "deleted_revisions": [f"{sid}/{rev}" for sid, rev in prune_targets],
                "fossils": fossils,
            },
            indent=2,
        ).encode("utf-8"),
    )
    result.collections_written += 1

    for snapshot_id, revision in prune_targets:
        storage.delete(revision_key(snapshot_id, revision))
        result.deleted_revisions.append(f"{snapshot_id}/{revision}")


def _sweep(
    storage: Storage,
    config: StoreConfig,
    now: datetime.datetime,
    result: PruneResult,
) -> None:
    """Step 2: delete (or resurrect) fossils of mature collections."""
    collections = storage.list(_COLLECTION_PREFIX)
    if not collections:
        return

    # Current state: per snapshot_id latest revision + all referenced hashes
    # (computed lazily only when some collection is deletable).
    latest_now: dict[str, int] = {
        sid: (list_revisions(storage, sid) or [0])[-1] for sid in _snapshot_ids(storage)
    }
    referenced_keys: set[str] | None = None

    for coll_key in collections:
        try:
            coll = json.loads(storage.get(coll_key).decode("utf-8"))
            created = _parse_iso(coll["created_at"])
        except Exception:
            logger.warning("Unreadable fossil collection %s — skipping", coll_key)
            continue
        if now - created < _COLLECTION_MIN_AGE:
            continue
        # Deletable only when every snapshot_id recorded at collect time has
        # moved past its then-latest revision (or disappeared entirely).
        deletable = all(
            sid not in latest_now or latest_now[sid] > last_rev
            for sid, last_rev in coll.get("last_revisions", {}).items()
        )
        if not deletable:
            continue

        if referenced_keys is None:
            referenced: set[str] = set()
            for sid in latest_now:
                for revision in list_revisions(storage, sid):
                    try:
                        referenced.update(
                            _revision_chunk_hashes(storage, config, sid, revision)
                        )
                    except Exception:
                        logger.warning(
                            "Unreadable revision %s/%s during sweep", sid, revision
                        )
            referenced_keys = {chunk_key(config.chunk_id(h)) for h in referenced}

        for key in coll.get("fossils", []):
            fossil = key + _FOSSIL_SUFFIX
            if not storage.exists(fossil):
                continue
            if key in referenced_keys:
                storage.rename(fossil, key)
                result.resurrected_chunks += 1
            else:
                storage.delete(fossil)
                result.deleted_chunks += 1
        storage.delete(coll_key)
        result.collections_deleted += 1


def prune(
    storage: Storage,
    *,
    keep: list[tuple[int, int]] | None = None,
    keep_revisions: dict[str, set[int]] | None = None,
    now: datetime.datetime | None = None,
) -> PruneResult:
    """Run both prune steps (sweep mature collections, then collect anew).

    Retention comes either from ``keep`` (interval:age pairs applied to
    revision timestamps) or from ``keep_revisions`` (an explicit
    {snapshot_id: revisions} map — used by Oduflow to keep filestore
    revisions in lockstep with snapshot manifests).
    """
    if keep is None and keep_revisions is None:
        raise ValueError("prune requires keep or keep_revisions")
    config = ensure_config(storage)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    result = PruneResult()
    _sweep(storage, config, now, result)
    _collect(storage, config, keep or [], now, result, keep_revisions)
    return result


def parse_keep(pairs: list[str] | tuple[str, ...]) -> list[tuple[int, int]]:
    """Parse ["7:30", ...] keep strings into (interval_days, age_days)."""
    parsed = []
    for pair in pairs:
        interval, _, age = pair.partition(":")
        parsed.append((int(interval), int(age)))
    return parsed
