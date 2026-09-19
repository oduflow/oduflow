"""Unified template import engine: S3 prefix, local path, or in-place refresh.

Large databases and filestores move most efficiently as-is — a PostgreSQL
dump plus a one-to-one copy of the filestore tree, no zip packing, no Odoo
master password, no database-manager API. This module accepts that raw
layout from any of three source shapes and turns it into a template:

    <source>/
    ├── dump.pgdump          # or dump.sql / dump.sql.gz / dump.pgdump.gz
    └── filestore/           # raw Odoo filestore tree, copied file-by-file

- ``s3://bucket/prefix`` — objects are downloaded in parallel (resumable);
- a local directory with the same layout — files are hardlinked (or copied
  across filesystems), so an rsync'd drop point imports in seconds;
- a single local dump file — a database-only import, format sniffed;
- no source at all (*refresh*) — the files already sit in the template
  directory (e.g. rsync'd there directly): reload the template DB from them
  and bring the metadata back in line.

Efficiency properties, in the order they matter for multi-hundred-GB
sources: parallel downloads; resume (a staged file with the right size is
never fetched again); and incremental re-sync into an existing template
(``overwrite=True``) — unchanged files are hardlinked from the live template
filestore into staging so only changed files are fetched, and an unchanged
database dump (same S3 ETag, or same local size+mtime) skips both the fetch
and the template DB reload.

Filestore files are compared by size only: Odoo filestore paths are content
hashes, so a same-named file with the same size is the same content. Nothing
touches the live template until the staged tree is complete; the promote is
a handful of renames under the standard overlay remount guard, so live
environments keep their upper-layer deltas.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.naming import get_template_db_name, validate_template_name
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

# Same names, same precedence as TeamSettings.get_template_sql_path.
_DUMP_NAMES = ("dump.pgdump", "dump.sql", "dump.pgdump.gz", "dump.sql.gz")
_FILESTORE_PREFIX = "filestore/"
_DOWNLOAD_WORKERS = 8
# Free-disk reserve required on top of the planned fetch volume.
_DISK_RESERVE_BYTES = 1024**3


@dataclass
class _SourceDump:
    name: str  # target file name (one of _DUMP_NAMES)
    key: str  # S3 key or absolute local path
    size: int
    # Change-detection token: S3 ETag, or "local:<size>:<mtime_ns>". Stored
    # in metadata as source_dump_etag to power the next sync's skip decision.
    token: str
    last_modified: str


@dataclass
class _SourceListing:
    dump: _SourceDump
    # filestore rel path -> (S3 key or absolute local path, size)
    filestore: dict[str, tuple[str, int]]


def parse_s3_url(url: str) -> tuple[str, str]:
    """Split ``s3://bucket/some/prefix`` into ``(bucket, prefix)``."""
    rest = url[len("s3://") :]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise PrerequisiteNotMetError(
            f"Invalid S3 URL '{url}': expected s3://bucket/prefix."
        )
    return bucket, prefix.strip("/")


def _make_source_client(
    settings: Settings,
    bucket: str,
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    region: str,
) -> Any:
    """S3 client for the import source.

    Credential resolution, most explicit first: keys passed as parameters;
    the configured ``[backup]`` credentials when the bucket (and endpoint, if
    given) match; otherwise anonymous access for a publicly readable bucket.
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    if bool(access_key) != bool(secret_key):
        raise PrerequisiteNotMetError(
            "s3_access_key and s3_secret_key must be provided together."
        )
    if endpoint:
        # Same SSRF stance as the HTTP import: block loopback and the cloud
        # metadata endpoint, allow operator-managed LAN object stores.
        from oduflow.url_safety import assert_allowed_url

        assert_allowed_url(endpoint, require_https=False, allow_private=True)

    backup = settings.backup
    if (
        not access_key
        and backup is not None
        and bucket == backup.bucket
        and endpoint in ("", backup.endpoint)
    ):
        from oduflow import s3_client

        return s3_client.make_client(backup)

    config_kwargs: dict[str, Any] = {"retries": {"max_attempts": 3, "mode": "standard"}}
    if endpoint:
        config_kwargs["s3"] = {"addressing_style": "path"}
    kwargs: dict[str, Any] = {}
    if access_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    else:
        # Explicitly unsigned so boto3 does not pick up ambient host
        # credentials — "no credentials given" must mean a public bucket.
        config_kwargs["signature_version"] = UNSIGNED
    # boto3 refuses to build a client with no region at all (NoRegionError);
    # us-east-1 is the safe default the global S3 endpoint accepts.
    kwargs["region_name"] = region or "us-east-1"
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", config=Config(**config_kwargs), **kwargs)


def _list_s3_source(client: Any, bucket: str, prefix: str) -> _SourceListing:
    base = f"{prefix}/" if prefix else ""
    dumps: dict[str, _SourceDump] = {}
    filestore: dict[str, tuple[str, int]] = {}
    root_entries: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=base):
        for obj in page.get("Contents", []):
            key = str(obj["Key"])
            rel = key[len(base) :]
            if not rel or rel.endswith("/"):
                continue  # zero-byte directory markers
            size = int(obj.get("Size", 0))
            if rel in _DUMP_NAMES:
                lm = obj.get("LastModified")
                dumps[rel] = _SourceDump(
                    name=rel,
                    key=key,
                    size=size,
                    token=str(obj.get("ETag", "")).strip('"'),
                    last_modified=(
                        lm.isoformat() if hasattr(lm, "isoformat") else str(lm or "")
                    ),
                )
            elif rel.startswith(_FILESTORE_PREFIX):
                fs_rel = rel[len(_FILESTORE_PREFIX) :]
                if fs_rel:
                    filestore[fs_rel] = (key, size)
            else:
                root_entries.add(rel.split("/", 1)[0])

    source = f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
    if not dumps and not filestore:
        raise NotFoundError(
            f"No objects found under {source}. Expected layout: one of "
            f"{', '.join(_DUMP_NAMES)} at the prefix root plus an optional "
            "filestore/ tree next to it."
        )
    _require_single_dump(dumps, source, sorted(root_entries))
    return _SourceListing(dump=next(iter(dumps.values())), filestore=filestore)


def _local_dump_token(path: str) -> str:
    st = os.stat(path)
    return f"local:{st.st_size}:{st.st_mtime_ns}"


def _local_source_dump(path: str, name: str) -> _SourceDump:
    st = os.stat(path)
    return _SourceDump(
        name=name,
        key=path,
        size=st.st_size,
        token=_local_dump_token(path),
        last_modified=datetime.datetime.fromtimestamp(
            st.st_mtime, tz=datetime.timezone.utc
        ).isoformat(),
    )


def _list_local_source(source_dir: str) -> _SourceListing:
    dumps: dict[str, _SourceDump] = {}
    root_entries: list[str] = []
    for entry in sorted(os.listdir(source_dir)):
        if entry in _DUMP_NAMES:
            dumps[entry] = _local_source_dump(os.path.join(source_dir, entry), entry)
        elif entry != "filestore":
            root_entries.append(entry)
    filestore: dict[str, tuple[str, int]] = {}
    fs_root = os.path.join(source_dir, "filestore")
    if os.path.isdir(fs_root):
        for dirpath, _dirnames, filenames in os.walk(fs_root):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, fs_root)
                filestore[rel] = (path, os.path.getsize(path))
    if not dumps and not filestore:
        raise NotFoundError(
            f"No importable files found in {source_dir}. Expected layout: one "
            f"of {', '.join(_DUMP_NAMES)} plus an optional filestore/ tree."
        )
    _require_single_dump(dumps, source_dir, root_entries)
    return _SourceListing(dump=next(iter(dumps.values())), filestore=filestore)


def _require_single_dump(
    dumps: dict[str, _SourceDump], source: str, other_entries: list[str]
) -> None:
    if not dumps:
        found = ", ".join(other_entries) or "only filestore/"
        raise PrerequisiteNotMetError(
            f"No database dump found under {source}: expected exactly one of "
            f"{', '.join(_DUMP_NAMES)} at the source root (found: {found})."
        )
    if len(dumps) > 1:
        raise PrerequisiteNotMetError(
            f"Multiple database dumps found under {source}: "
            f"{', '.join(sorted(dumps))}. Keep exactly one."
        )


def _sniff_dump_name(path: str) -> str:
    """Target name (one of ``_DUMP_NAMES``) for an arbitrary dump file."""
    from oduflow.docker_ops.system_ops import _is_text_dump

    with open(path, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    base = "dump.sql" if _is_text_dump(path) else "dump.pgdump"
    return base + (".gz" if gz else "")


def _safe_join(base: str, rel: str) -> str | None:
    """Join ``rel`` under ``base``; None when it would escape (key-slip)."""
    target = os.path.normpath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


@dataclass
class _FilestorePlan:
    # (rel, key-or-path, size, target) still to fetch
    to_fetch: list[tuple[str, str, int, str]]
    kept: int  # already staged with the right size (resume)
    linked: int  # hardlinked from the live template filestore
    removed: int  # stale staged files pruned


def _plan_filestore(
    remote: dict[str, tuple[str, int]],
    staging_fs: str,
    live_fs: str,
    *,
    overwrite: bool,
) -> _FilestorePlan:
    """Diff the source filestore listing against staging (and the live
    template on overwrite), producing the minimal fetch set."""
    os.makedirs(staging_fs, exist_ok=True)
    staging_fs = os.path.normpath(staging_fs)
    live_fs = os.path.normpath(live_fs)
    to_fetch: list[tuple[str, str, int, str]] = []
    kept = linked = 0
    valid_targets: set[str] = set()

    for rel, (key, size) in remote.items():
        target = _safe_join(staging_fs, rel)
        if target is None:
            logger.warning("Skipping unsafe source member outside filestore: %s", key)
            continue
        valid_targets.add(target)
        try:
            if os.path.getsize(target) == size:
                kept += 1
                continue
        except OSError:
            pass
        if overwrite:
            live = _safe_join(live_fs, rel)
            if live is not None:
                try:
                    if os.path.getsize(live) == size:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        if os.path.exists(target):
                            os.remove(target)
                        try:
                            os.link(live, target)
                        except OSError:
                            shutil.copy2(live, target)
                        linked += 1
                        continue
                except OSError:
                    pass
        to_fetch.append((rel, key, size, target))

    # Prune staged files that are no longer part of the source (covers
    # `sync --delete` semantics and leftover .part files from crashed runs).
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(staging_fs):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            if path not in valid_targets:
                os.remove(path)
                removed += 1
    return _FilestorePlan(to_fetch=to_fetch, kept=kept, linked=linked, removed=removed)


def _download_one(client: Any, bucket: str, key: str, target: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".part"
    client.download_file(bucket, key, tmp)
    os.replace(tmp, target)


def _download_filestore(
    client: Any, bucket: str, to_fetch: list[tuple[str, str, int, str]]
) -> None:
    if not to_fetch:
        return
    with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(_download_one, client, bucket, key, target): rel
            for rel, key, _size, target in to_fetch
        }
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc is not None:
                for other in futures:
                    other.cancel()
                raise ExternalCommandError(
                    "s3 download",
                    1,
                    f"Download failed for filestore/{futures[fut]}: {exc}",
                ) from exc


def _link_one(src: str, target: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copy2(src, tmp)
    os.replace(tmp, target)


def _link_filestore(to_fetch: list[tuple[str, str, int, str]]) -> None:
    for _rel, src, _size, target in to_fetch:
        _link_one(src, target)


def _load_carried_metadata(team: TeamSettings, template_name: str) -> dict[str, Any]:
    path = team.get_template_metadata_path(template_name)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            loaded: dict[str, Any] = json.load(f)
            return loaded
    except (OSError, ValueError):
        return {}


def _check_fresh_conflicts(
    settings: Settings, team: TeamSettings, template_name: str, client: Any
) -> None:
    """Refuse to create over an existing template; gate the DB quota."""
    from oduflow.docker_ops import system_ops

    tpl_dir = team.get_template_dir(template_name)
    tpl_db = get_template_db_name(template_name, team.team_id)
    if os.path.exists(tpl_dir):
        raise ConflictError(
            f"Template directory already exists: {tpl_dir}. "
            "Pass overwrite=true to re-sync it from the source."
        )
    if system_ops._db_exists(client, settings, tpl_db):
        raise ConflictError(
            f"Template database already exists: {tpl_db}. "
            "Pass overwrite=true to re-sync it from the source."
        )
    # Replacement syncs are not gated, mirroring refresh/reload.
    system_ops.check_db_quota(client, settings, team)


def _dump_unchanged(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    dump: _SourceDump,
    carried: dict[str, Any],
    client: Any,
) -> bool:
    """An unchanged dump (same token + size as the last sync from this
    source) with a live template DB skips the fetch and the reload —
    the periodic "filestore-only" re-sync path."""
    from oduflow.docker_ops import system_ops

    tpl_db = get_template_db_name(template_name, team.team_id)
    return bool(
        dump.token
        and carried.get("source_dump_etag") == dump.token
        and int(carried.get("source_dump_bytes") or -1) == dump.size
        and os.path.isfile(team.get_template_sql_path(template_name))
        and system_ops._db_exists(client, settings, tpl_db)
    )


def _chown_filestore(client: Any, image: str, filestore_dir: str) -> None:
    from oduflow.docker_ops.client import chown_recursive, get_odoo_uid_gid

    try:
        uid_str, gid_str = get_odoo_uid_gid(client, image).split(":")
        chown_recursive(filestore_dir, int(uid_str), int(gid_str), client, image)
    except Exception as exc:  # noqa: BLE001 - chown is best-effort
        logger.warning("Could not chown template filestore: %s", exc)


def _promote_and_reload(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    *,
    client: Any,
    staging: str,
    staging_fs: str,
    syncing_filestore: bool,
    reload_db: bool,
    dump_name: str,
    metadata: dict[str, Any],
    overwrite: bool,
) -> dict[str, Any]:
    """Swap the staged tree into the live template under the remount guard,
    reload the template DB when the dump changed, and finalize metadata.

    Returns ``restore_seconds``, ``affected_envs``, ``remount_failures`` and
    the persisted metadata.
    """
    from oduflow.docker_ops import env_ops, system_ops

    tpl_dir = team.get_template_dir(template_name)
    tpl_db = get_template_db_name(template_name, team.team_id)
    live_fs = os.path.normpath(team.get_template_filestore_path(template_name))
    staged_dump = os.path.join(staging, dump_name)

    chown_image = str(metadata.get("odoo_image") or "")
    with env_ops.remount_template_overlays(
        client, settings, team, template_name
    ) as remount:
        os.makedirs(tpl_dir, exist_ok=True)
        if syncing_filestore:
            if os.path.exists(live_fs):
                shutil.rmtree(live_fs)
            os.rename(staging_fs, live_fs)
        elif not os.path.isdir(live_fs):
            os.makedirs(live_fs, exist_ok=True)
        if reload_db:
            for stale in _DUMP_NAMES:
                stale_path = os.path.join(tpl_dir, stale)
                if os.path.isfile(stale_path):
                    os.remove(stale_path)
            os.rename(staged_dump, os.path.join(tpl_dir, dump_name))
        if chown_image and syncing_filestore:
            _chown_filestore(client, chown_image, live_fs)

    restore_seconds: object = 0
    if reload_db:
        result = system_ops.reload_template(settings, team, template_name=template_name)
        restore_seconds = result.get("restore_seconds", 0)
        manifest = system_ops._read_template_manifest_from_db(
            client, settings, str(result["template_db"])
        )
        major = str(manifest.get("major_version") or "")
        metadata["odoo_version"] = manifest.get("version", "")
        metadata["pg_version"] = manifest.get("pg_version", "")
        metadata["modules"] = manifest.get("modules", {})
        if not metadata.get("odoo_image") and major:
            metadata["odoo_image"] = f"odoo:{major}"
        if not chown_image and metadata.get("odoo_image") and syncing_filestore:
            # Fresh import: the Odoo version was unknown until the dump was
            # restored, so the in-guard chown above was skipped. A template
            # that did not exist has no live overlay environments, so
            # chowning after the promote is safe.
            _chown_filestore(client, str(metadata["odoo_image"]), live_fs)

    metadata["includes_filestore"] = os.path.isdir(live_fs) and bool(
        os.listdir(live_fs)
    )
    metadata = system_ops._update_template_sizes(
        team, settings, template_name, metadata
    )
    shutil.rmtree(staging, ignore_errors=True)

    return {
        "template_db": tpl_db,
        "restore_seconds": restore_seconds,
        "metadata": metadata,
        "affected_envs": remount.affected,
        "remount_failures": remount.failures,
        "status": "synced" if overwrite else "imported",
    }


def _stage_dump_check(staging: str, dump: _SourceDump) -> tuple[str, bool]:
    """Return (staged dump path, whether it still needs fetching)."""
    staged_dump = os.path.join(staging, dump.name)
    needed = not (
        os.path.isfile(staged_dump) and os.path.getsize(staged_dump) == dump.size
    )
    if needed:
        # Drop stale staged dumps under other names so exactly one survives.
        for name in _DUMP_NAMES:
            if name == dump.name:
                continue
            for stale in (
                os.path.join(staging, name),
                os.path.join(staging, name) + ".part",
            ):
                if os.path.isfile(stale):
                    os.remove(stale)
    return staged_dump, needed


def _check_free_disk(staging: str, needed_bytes: int) -> None:
    free = shutil.disk_usage(staging).free
    if free < needed_bytes + _DISK_RESERVE_BYTES:
        raise PrerequisiteNotMetError(
            f"Not enough free disk for the import: need ~"
            f"{needed_bytes // 1024**2} MB plus reserve in {staging}, "
            f"have {free // 1024**2} MB free."
        )


def _source_metadata(
    carried: dict[str, Any],
    *,
    source_url: str,
    dump: _SourceDump,
) -> dict[str, Any]:
    # Merge over the existing metadata on overwrite so unrelated keys
    # (extra_addons, env_vars, use_overlay, …) survive a re-sync.
    return {
        **carried,
        "odoo_image": carried.get("odoo_image", ""),
        "repo_url": carried.get("repo_url", ""),
        "source_url": source_url,
        "source_db": dump.name,
        "odoo_version": carried.get("odoo_version", ""),
        "pg_version": carried.get("pg_version", ""),
        "modules": carried.get("modules", {}),
        "source_dump_etag": dump.token,
        "source_dump_bytes": dump.size,
        # The data is as old as the dump upload, not as young as this import.
        "snapshot_at": dump.last_modified,
    }


def import_from_s3_prefix(
    settings: Settings,
    team: TeamSettings,
    *,
    url: str,
    template_name: str,
    overwrite: bool = False,
    without_filestore: bool = False,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    region: str = "",
) -> dict[str, object]:
    """Import (or, with ``overwrite``, incrementally re-sync) a template
    from an S3 prefix holding a raw dump + filestore copy."""
    from oduflow.docker_ops.client import get_client

    validate_template_name(template_name)
    bucket, prefix = parse_s3_url(url)
    source_url = f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
    live_fs = os.path.normpath(team.get_template_filestore_path(template_name))

    client = get_client()
    if not overwrite:
        _check_fresh_conflicts(settings, team, template_name, client)

    s3 = _make_source_client(
        settings,
        bucket,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )
    listing = _list_s3_source(s3, bucket, prefix)
    dump = listing.dump
    remote_fs = {} if without_filestore else listing.filestore

    carried = _load_carried_metadata(team, template_name) if overwrite else {}
    staging = team.get_import_staging_dir(template_name)
    staging_fs = os.path.normpath(os.path.join(staging, "filestore"))
    os.makedirs(staging, exist_ok=True)

    reload_db = not (
        overwrite
        and _dump_unchanged(settings, team, template_name, dump, carried, client)
    )
    dump_fetch_needed = False
    if reload_db:
        staged_dump, dump_fetch_needed = _stage_dump_check(staging, dump)

    plan = _plan_filestore(remote_fs, staging_fs, live_fs, overwrite=overwrite)

    fetch_bytes = sum(size for _rel, _key, size, _t in plan.to_fetch)
    if dump_fetch_needed:
        fetch_bytes += dump.size
    _check_free_disk(staging, fetch_bytes)

    logger.info(
        "S3 import of '%s' from %s: %d filestore files to download (%.1f MB), "
        "%d staged, %d hardlinked from live template, dump %s",
        template_name,
        source_url,
        len(plan.to_fetch),
        fetch_bytes / 1024**2,
        plan.kept,
        plan.linked,
        "download"
        if dump_fetch_needed
        else ("staged" if reload_db else "unchanged (reload skipped)"),
    )

    _download_filestore(s3, bucket, plan.to_fetch)
    if dump_fetch_needed:
        _download_one(s3, bucket, dump.key, staged_dump)
        if os.path.getsize(staged_dump) != dump.size:
            raise ExternalCommandError(
                "s3 download",
                1,
                f"Dump download size mismatch for {dump.key}: expected "
                f"{dump.size} bytes, got {os.path.getsize(staged_dump)}.",
            )

    metadata = _source_metadata(carried, source_url=source_url, dump=dump)
    final = _promote_and_reload(
        settings,
        team,
        template_name,
        client=client,
        staging=staging,
        staging_fs=staging_fs,
        syncing_filestore=bool(remote_fs),
        reload_db=reload_db,
        dump_name=dump.name,
        metadata=metadata,
        overwrite=overwrite,
    )
    return _import_result(
        final,
        template_name=template_name,
        source_url=source_url,
        dump=dump,
        plan=plan,
        dump_fetched=dump_fetch_needed,
        fetch_bytes=fetch_bytes,
        reload_db=reload_db,
    )


def import_from_local_path(
    settings: Settings,
    team: TeamSettings,
    *,
    path: str,
    template_name: str,
    overwrite: bool = False,
    without_filestore: bool = False,
) -> dict[str, object]:
    """Import a template from a local directory (raw dump + filestore
    layout) or from a single dump file (database-only)."""
    from oduflow.docker_ops.client import get_client

    validate_template_name(template_name)
    source = os.path.abspath(path)
    if not os.path.exists(source):
        raise NotFoundError(f"Source path does not exist: {source}")

    client = get_client()
    if not overwrite:
        _check_fresh_conflicts(settings, team, template_name, client)

    if os.path.isfile(source):
        listing = _SourceListing(
            dump=_local_source_dump(source, _sniff_dump_name(source)), filestore={}
        )
    else:
        listing = _list_local_source(source)
    dump = listing.dump
    remote_fs = {} if without_filestore else listing.filestore

    carried = _load_carried_metadata(team, template_name) if overwrite else {}
    staging = team.get_import_staging_dir(template_name)
    staging_fs = os.path.normpath(os.path.join(staging, "filestore"))
    os.makedirs(staging, exist_ok=True)

    live_fs = os.path.normpath(team.get_template_filestore_path(template_name))
    reload_db = not (
        overwrite
        and _dump_unchanged(settings, team, template_name, dump, carried, client)
    )
    dump_fetch_needed = False
    if reload_db:
        staged_dump, dump_fetch_needed = _stage_dump_check(staging, dump)

    plan = _plan_filestore(remote_fs, staging_fs, live_fs, overwrite=overwrite)

    # Hardlinks are free; only a cross-device source actually copies bytes.
    fetch_bytes = 0
    if os.stat(source).st_dev != os.stat(staging).st_dev:
        fetch_bytes = sum(size for _rel, _key, size, _t in plan.to_fetch)
        if dump_fetch_needed:
            fetch_bytes += dump.size
        _check_free_disk(staging, fetch_bytes)

    logger.info(
        "Local import of '%s' from %s: %d filestore files to link/copy, "
        "%d staged, %d hardlinked from live template, dump %s",
        template_name,
        source,
        len(plan.to_fetch),
        plan.kept,
        plan.linked,
        "fetch"
        if dump_fetch_needed
        else ("staged" if reload_db else "unchanged (reload skipped)"),
    )

    _link_filestore(plan.to_fetch)
    if dump_fetch_needed:
        _link_one(dump.key, staged_dump)

    metadata = _source_metadata(carried, source_url=source, dump=dump)
    final = _promote_and_reload(
        settings,
        team,
        template_name,
        client=client,
        staging=staging,
        staging_fs=staging_fs,
        syncing_filestore=bool(remote_fs),
        reload_db=reload_db,
        dump_name=dump.name,
        metadata=metadata,
        overwrite=overwrite,
    )
    return _import_result(
        final,
        template_name=template_name,
        source_url=source,
        dump=dump,
        plan=plan,
        dump_fetched=dump_fetch_needed,
        fetch_bytes=fetch_bytes,
        reload_db=reload_db,
    )


def refresh_from_own_files(
    settings: Settings,
    team: TeamSettings,
    *,
    template_name: str,
) -> dict[str, object]:
    """Reload the template DB from the files already in the template
    directory and bring metadata (sizes, version, ownership) in line.

    This is the drop-point workflow: an external process (rsync, scp, a
    backup job) writes ``dump.*`` and ``filestore/`` straight into the
    template directory, then calls this to apply them. Runs under the
    overlay remount guard so live environments pick up the new lower layer
    while keeping their deltas.
    """
    from oduflow.docker_ops import env_ops, system_ops
    from oduflow.docker_ops.client import get_client

    validate_template_name(template_name)
    tpl_dir = team.get_template_dir(template_name)
    if not os.path.isdir(tpl_dir):
        raise NotFoundError(f"Template directory does not exist: {tpl_dir}")
    dump_path = team.get_template_sql_path(template_name)
    if not os.path.isfile(dump_path):
        raise NotFoundError(
            f"No dump file found in {tpl_dir} (expected one of "
            f"{', '.join(_DUMP_NAMES)})."
        )

    client = get_client()
    live_fs = os.path.normpath(team.get_template_filestore_path(template_name))
    metadata = _load_carried_metadata(team, template_name)
    dump = _local_source_dump(dump_path, os.path.basename(dump_path))
    metadata.update(
        {
            "source_db": dump.name,
            "source_dump_etag": dump.token,
            "source_dump_bytes": dump.size,
            "snapshot_at": dump.last_modified,
        }
    )

    chown_image = str(metadata.get("odoo_image") or "")
    with env_ops.remount_template_overlays(
        client, settings, team, template_name
    ) as remount:
        if chown_image and os.path.isdir(live_fs):
            _chown_filestore(client, chown_image, live_fs)

    result = system_ops.reload_template(settings, team, template_name=template_name)
    manifest = system_ops._read_template_manifest_from_db(
        client, settings, str(result["template_db"])
    )
    major = str(manifest.get("major_version") or "")
    metadata["odoo_version"] = manifest.get("version", "")
    metadata["pg_version"] = manifest.get("pg_version", "")
    metadata["modules"] = manifest.get("modules", {})
    if not metadata.get("odoo_image") and major:
        metadata["odoo_image"] = f"odoo:{major}"
        if os.path.isdir(live_fs):
            _chown_filestore(client, str(metadata["odoo_image"]), live_fs)
    metadata["includes_filestore"] = os.path.isdir(live_fs) and bool(
        os.listdir(live_fs)
    )
    metadata = system_ops._update_template_sizes(
        team, settings, template_name, metadata
    )

    return {
        "status": "refreshed",
        "template_name": template_name,
        "source_url": tpl_dir,
        "source_db": dump.name,
        "odoo_image": metadata.get("odoo_image", ""),
        "odoo_version": metadata.get("odoo_version", ""),
        "template_db": result["template_db"],
        "restore_seconds": result.get("restore_seconds", 0),
        "dump_reloaded": True,
        "includes_filestore": metadata["includes_filestore"],
        "downloaded_files": 0,
        "downloaded_mb": 0.0,
        "reused_files": 0,
        "removed_files": 0,
        "affected_envs": remount.affected,
        "remount_failures": remount.failures,
    }


def _import_result(
    final: dict[str, Any],
    *,
    template_name: str,
    source_url: str,
    dump: _SourceDump,
    plan: _FilestorePlan,
    dump_fetched: bool,
    fetch_bytes: int,
    reload_db: bool,
) -> dict[str, object]:
    metadata = final["metadata"]
    return {
        "status": final["status"],
        "template_name": template_name,
        "source_url": source_url,
        "source_db": dump.name,
        "odoo_image": metadata.get("odoo_image", ""),
        "odoo_version": metadata.get("odoo_version", ""),
        "template_db": final["template_db"],
        "restore_seconds": final["restore_seconds"],
        "dump_reloaded": reload_db,
        "includes_filestore": metadata["includes_filestore"],
        "downloaded_files": len(plan.to_fetch) + (1 if dump_fetched else 0),
        "downloaded_mb": round(fetch_bytes / 1024**2, 1),
        "reused_files": plan.kept + plan.linked,
        "removed_files": plan.removed,
        "affected_envs": final["affected_envs"],
        "remount_failures": final["remount_failures"],
    }
