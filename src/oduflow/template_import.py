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

import contextlib
import datetime
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from oduflow import secret_store
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import get_template_db_name, validate_template_name
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

# The canonical dump names Oduflow itself writes (targets, stale cleanup).
_DUMP_NAMES = ("dump.pgdump", "dump.sql", "dump.pgdump.gz", "dump.sql.gz")
# Accepted at a source root, in resolution order — the same precedence as
# TeamSettings.get_template_sql_path: canonical names outrank the hand-made
# ``db.dump`` convention, so a mirrored template directory imports the dump
# Oduflow itself persisted, not an operator leftover.
_SOURCE_DUMP_NAMES = _DUMP_NAMES + ("db.dump", "db.dump.gz")
_FILESTORE_PREFIX = "filestore/"
_DOWNLOAD_WORKERS = 8
# Free-disk reserve required on top of the planned fetch volume.
_DISK_RESERVE_BYTES = 1024**3


@dataclass
class _SourceDump:
    name: str  # file name at the source (one of _SOURCE_DUMP_NAMES)
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


# --- Remembered source credentials -------------------------------------------
#
# A private bucket has to be re-read on every ``overwrite`` re-sync, so asking
# for the secret key again each time would make the cron-able path impossible
# to automate without putting the key in a crontab. The key is therefore kept
# in the team secret store and the template metadata records only a
# ``secret:<name>`` reference, so no listing, metadata editor or MCP tool can
# echo it back. A secret Oduflow created on the operator's behalf is marked
# managed and its lifetime follows the template's.

_MANAGED_SECRET_PREFIX = "s3-template-"


def managed_secret_name(template_name: str) -> str:
    """The team-secret name Oduflow uses for a template's S3 secret key."""
    base = re.sub(r"[^a-z0-9_.-]", "-", template_name.lower().replace("/", "."))
    room = 64 - len(_MANAGED_SECRET_PREFIX)
    if base == template_name and len(base) <= room:
        return _MANAGED_SECRET_PREFIX + base
    # Lowercasing and truncation both collide; a digest keeps names apart.
    digest = hashlib.sha256(template_name.encode()).hexdigest()[:8]
    return f"{_MANAGED_SECRET_PREFIX}{base[: room - 9]}-{digest}"


def _resolve_secret_value(team: TeamSettings, ref: str) -> str:
    """The stored value behind a ``secret:<name>`` reference."""
    resolved = secret_store.resolve_env_secrets(team, {"S3_SECRET_KEY": ref}) or {}
    return resolved["S3_SECRET_KEY"]


@dataclass
class SourceCredentials:
    """Keys ready to sign with, plus what metadata should record about them."""

    access_key: str = ""
    # Resolved in memory for this operation only; never persisted as-is.
    secret_key: str = ""
    # What metadata records: a ``secret:<name>`` reference, or "" (none).
    secret_ref: str = ""
    # Whether Oduflow created that secret and therefore owns its lifetime.
    secret_managed: bool = False
    # A newly entered raw secret to write into the store; "" = nothing new.
    store_value: str = ""


def resolve_source_credentials(
    team: TeamSettings,
    template_name: str,
    *,
    endpoint: str = "",
    access_key: str = "",
    secret_key: str = "",
    carried: dict[str, Any] | None = None,
) -> SourceCredentials:
    """Work out which keys sign this import, and what to remember about them.

    Most explicit wins, as everywhere else in this module: keys passed now; a
    ``secret:<name>`` reference the operator chose; the secret remembered for
    this template. An empty secret key reuses the remembered one **only while
    the endpoint and access key are unchanged**, so a stored secret is never
    sent to sign requests for a different endpoint or key pair. With no key
    material at all the caller falls through to the ``[backup]`` credentials or
    anonymous access, exactly as before this was remembered.
    """
    carried = carried or {}
    access_key = (access_key or "").strip()
    secret_key = (secret_key or "").strip()
    stored_ref = str(carried.get("source_secret_key") or "")
    stored_access_key = str(carried.get("source_access_key") or "")

    if secret_store.is_secret_ref(secret_key):
        return SourceCredentials(
            access_key=access_key or stored_access_key,
            secret_key=_resolve_secret_value(team, secret_key),
            secret_ref=secret_key,
        )
    if secret_key:
        # Keep writing into the secret this template already owns, so rotating
        # the key does not leave the previous managed secret behind.
        if carried.get("source_secret_managed") and secret_store.is_secret_ref(
            stored_ref
        ):
            ref = stored_ref
        else:
            ref = secret_store.SECRET_REF_PREFIX + managed_secret_name(template_name)
        return SourceCredentials(
            access_key=access_key,
            secret_key=secret_key,
            secret_ref=ref,
            secret_managed=True,
            store_value=secret_key,
        )
    if not stored_ref:
        return SourceCredentials(access_key=access_key)
    if (access_key or stored_access_key) == stored_access_key and str(
        carried.get("source_endpoint") or ""
    ) == endpoint:
        return SourceCredentials(
            access_key=stored_access_key,
            secret_key=_resolve_secret_value(team, stored_ref),
            secret_ref=stored_ref,
            secret_managed=bool(carried.get("source_secret_managed")),
        )
    raise PrerequisiteNotMetError(
        "Re-enter s3_secret_key when changing the endpoint or access key: the "
        f"secret stored for template '{template_name}' signs only "
        f"{stored_access_key or 'anonymous'} access to its recorded endpoint."
    )


def credential_metadata(creds: SourceCredentials) -> dict[str, Any]:
    """The credential fields an import records in template metadata."""
    return {
        "source_access_key": creds.access_key,
        "source_secret_key": creds.secret_ref,
        "source_secret_managed": creds.secret_managed,
    }


def store_source_secret(team: TeamSettings, creds: SourceCredentials) -> None:
    """Persist a newly entered secret key under its managed name."""
    if creds.store_value:
        secret_store.set_secret(
            team,
            secret_store.secret_ref_name(creds.secret_ref),
            creds.store_value,
        )


def delete_managed_secret(team: TeamSettings, metadata: object) -> None:
    """Best-effort removal of the secret Oduflow created for a template."""
    if not isinstance(metadata, dict) or not metadata.get("source_secret_managed"):
        return
    ref = str(metadata.get("source_secret_key") or "")
    if not secret_store.is_secret_ref(ref):
        return
    with contextlib.suppress(Exception):
        secret_store.delete_secret(team, secret_store.secret_ref_name(ref))
        logger.info("Deleted managed S3 secret %s", ref)


def public_source_view(metadata: object) -> dict[str, Any] | None:
    """What a listing may show of a remembered S3 source — never the secret.

    ``None`` for anything that did not come from a bucket, so a caller can use
    it directly as "is this template re-syncable from S3".
    """
    if not isinstance(metadata, dict):
        return None
    url = str(metadata.get("source_url") or "")
    if not url.startswith("s3://"):
        return None
    return {
        "url": url,
        "endpoint": str(metadata.get("source_endpoint") or ""),
        "region": str(metadata.get("source_region") or ""),
        "access_key": str(metadata.get("source_access_key") or ""),
        # Enough for the UI to say "no key needed"; the value stays put.
        "has_secret": bool(metadata.get("source_secret_key")),
        "dump": str(metadata.get("source_db") or ""),
        "synced_at": str(metadata.get("snapshot_at") or ""),
    }


def release_replaced_secret(
    team: TeamSettings, creds: SourceCredentials, previous: dict[str, Any] | None
) -> None:
    """Drop the managed secret a completed import no longer references.

    Called only after the new metadata is on disk: until then the template
    still points at the old reference, and removing it early would leave a
    dangling one behind if the import failed.
    """
    if previous and str(previous.get("source_secret_key") or "") != creds.secret_ref:
        delete_managed_secret(team, previous)


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


# --- Bucket browser ----------------------------------------------------------
#
# Typing an object key blind is the one step of an S3 import that cannot be
# verified before it runs, and a mistyped prefix fails only after the listing
# comes back empty. The dashboard therefore walks the bucket one level at a
# time, which needs a listing that is cheap, bounded and says nothing about
# credentials.

# Loose on purpose: S3-compatible stores accept more than AWS's 3-63 lowercase
# rule, but nothing that could turn into a path or a URL component.
_BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_MAX_BROWSE_KEYS = 1000


def validate_bucket(bucket: str) -> str:
    bucket = (bucket or "").strip()
    if not _BUCKET_RE.fullmatch(bucket):
        raise ValueError(f"Invalid S3 bucket name '{bucket}'.")
    return bucket


@contextlib.contextmanager
def _s3_errors(action: str) -> Iterator[None]:
    """Translate boto errors into FlowErrors with a message fit for the UI.

    S3 error messages name the failing condition (bad key id, missing bucket,
    signature mismatch) and never echo credentials, so they are passed
    through: without them the operator cannot tell a typo from a permissions
    problem.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        yield
    except ClientError as exc:
        error = exc.response.get("Error", {}) or {}
        code = str(error.get("Code") or "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        message = str(error.get("Message") or "")
        if code in ("NoSuchBucket", "NoSuchKey", "NotFound", "404") or status == 404:
            raise NotFoundError(f"{action}: not found ({code or status}).") from exc
        raise PrerequisiteNotMetError(
            f"{action} failed: {message or code or status}."
        ) from exc
    except BotoCoreError as exc:
        raise PrerequisiteNotMetError(f"{action} failed: {exc}") from exc


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def browse_source(
    settings: Settings,
    team: TeamSettings,
    *,
    bucket: str = "",
    prefix: str = "",
    continuation_token: str = "",
    endpoint: str = "",
    region: str = "",
    access_key: str = "",
    secret_key: str = "",
    template_name: str = "",
) -> dict[str, Any]:
    """List one level of a bucket, or the buckets when *bucket* is empty.

    With ``template_name`` the secret remembered for that template signs the
    request when no new one is given — under the same endpoint/access-key rule
    as an import — so pointing an existing S3 template at a different object
    never sends its secret back to the browser.
    """
    carried: dict[str, Any] = {}
    if template_name:
        validate_template_name(template_name)
        carried = _load_carried_metadata(team, template_name)
    creds = resolve_source_credentials(
        team,
        template_name or "browse",
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        carried=carried,
    )
    client = _make_source_client(
        settings,
        bucket,
        endpoint=endpoint,
        access_key=creds.access_key,
        secret_key=creds.secret_key,
        region=region,
    )
    if not bucket:
        with _s3_errors("Listing buckets"):
            resp = client.list_buckets()
        return {
            "bucket": "",
            "prefix": "",
            "buckets": [
                str(b["Name"]) for b in resp.get("Buckets", []) if b.get("Name")
            ],
            "folders": [],
            "files": [],
            "truncated": False,
            "next_token": "",
        }

    bucket = validate_bucket(bucket)
    prefix = (prefix or "").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Prefix": prefix,
        "Delimiter": "/",
        "MaxKeys": _MAX_BROWSE_KEYS,
    }
    if continuation_token:
        kwargs["ContinuationToken"] = continuation_token
    with _s3_errors(f"Listing s3://{bucket}/{prefix}"):
        resp = client.list_objects_v2(**kwargs)
    files = [
        {
            "key": str(obj["Key"]),
            "size": int(obj.get("Size") or 0),
            "last_modified": _iso(obj.get("LastModified")),
            "etag": str(obj.get("ETag") or "").strip('"'),
        }
        for obj in resp.get("Contents", [])
        # A zero-byte "folder marker" object named after the prefix itself.
        if obj.get("Key") and obj["Key"] != prefix
    ]
    files.sort(key=lambda f: str(f["last_modified"]), reverse=True)
    return {
        "bucket": bucket,
        "prefix": prefix,
        "buckets": [],
        "folders": [
            str(p["Prefix"]) for p in resp.get("CommonPrefixes", []) if p.get("Prefix")
        ],
        "files": files,
        "truncated": bool(resp.get("IsTruncated")),
        "next_token": str(resp.get("NextContinuationToken") or ""),
    }


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
            if rel in _SOURCE_DUMP_NAMES:
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
            f"{', '.join(_SOURCE_DUMP_NAMES)} at the prefix root plus an "
            "optional filestore/ tree next to it."
        )
    return _SourceListing(
        dump=_select_dump(dumps, source, sorted(root_entries)),
        filestore=filestore,
    )


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
        if entry in _SOURCE_DUMP_NAMES:
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
            f"of {', '.join(_SOURCE_DUMP_NAMES)} plus an optional filestore/ "
            "tree."
        )
    return _SourceListing(
        dump=_select_dump(dumps, source_dir, root_entries), filestore=filestore
    )


def _select_dump(
    dumps: dict[str, _SourceDump], source: str, other_entries: list[str]
) -> _SourceDump:
    """Pick the dump to import, by the template resolver's precedence.

    Several candidates are legal — a mirrored template directory can carry a
    canonical dump next to a hand-placed ``db.dump`` — and resolve exactly
    like the template directory itself would: canonical names win.
    """
    if not dumps:
        found = ", ".join(other_entries) or "only filestore/"
        raise PrerequisiteNotMetError(
            f"No database dump found under {source}: expected one of "
            f"{', '.join(_SOURCE_DUMP_NAMES)} at the source root "
            f"(found: {found})."
        )
    for name in _SOURCE_DUMP_NAMES:
        if name in dumps:
            ignored = sorted(set(dumps) - {name})
            if ignored:
                logger.warning(
                    "Multiple dump candidates under %s: importing %s "
                    "(template-resolver precedence), ignoring %s",
                    source,
                    name,
                    ", ".join(ignored),
                )
            return dumps[name]
    raise AssertionError("unreachable: dumps keyed by _SOURCE_DUMP_NAMES")


def _canonical_dump_name(name: str) -> str:
    """Target file name for a source dump.

    ``db.dump`` names carry no format information — reload detects text vs
    binary from the content — so they install under the canonical
    custom-format name; only the meaningful ``.gz`` suffix is preserved.
    """
    if name in _DUMP_NAMES:
        return name
    return "dump.pgdump.gz" if name.endswith(".gz") else "dump.pgdump"


def _sniff_dump_name(path: str) -> str:
    """Target name (one of ``_DUMP_NAMES``) for an arbitrary dump file."""
    with open(path, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    opener = gzip.open if gz else open
    with opener(path, "rb") as f:
        base = "dump.pgdump" if f.read(5) == b"PGDMP" else "dump.sql"
    return base + (".gz" if gz else "")


def _safe_join(base: str, rel: str) -> str | None:
    """Join ``rel`` under ``base``; None when it would escape (key-slip)."""
    if os.path.isabs(rel) or any(part in (".", "..", "") for part in rel.split("/")):
        return None
    target = os.path.realpath(os.path.join(base, rel))
    base = os.path.realpath(base)
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
        os.link(os.path.realpath(src), tmp)
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
    # Fresh databases consume quota.
    system_ops.check_db_quota(client, settings, team)


def _check_overwrite_quota(
    settings: Settings, team: TeamSettings, name: str, client: Any
) -> None:
    from oduflow.docker_ops import system_ops

    if not system_ops._db_exists(
        client, settings, get_template_db_name(name, team.team_id)
    ):
        system_ops.check_db_quota(client, settings, team)


def _dump_unchanged(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    dump: _SourceDump,
    carried: dict[str, Any],
    client: Any,
    source_url: str,
    source_endpoint: str = "",
) -> bool:
    """An unchanged dump (same token + size as the last sync from this
    source) with a live template DB skips the fetch and the reload —
    the periodic "filestore-only" re-sync path."""
    from oduflow.docker_ops import system_ops

    tpl_db = get_template_db_name(template_name, team.team_id)
    return bool(
        dump.token
        and carried.get("source_url") == source_url
        and carried.get("source_endpoint", "") == source_endpoint
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

    restore_seconds: object = 0
    if reload_db:
        # Invalidate the old source fingerprint before a destructive DB reload.
        # On failure, retrying the old source must not skip a partial database.
        carried = _load_carried_metadata(team, template_name)
        carried.pop("source_dump_etag", None)
        os.makedirs(tpl_dir, exist_ok=True)
        atomic_write_private_json(
            team.get_template_metadata_path(template_name), carried
        )
        result = system_ops.reload_template(
            settings,
            team,
            template_name=template_name,
            dump_path=staged_dump,
            persist_dump=False,
        )
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

    with env_ops.remount_template_overlays(
        client, settings, team, template_name
    ) as remount:
        os.makedirs(tpl_dir, exist_ok=True)
        if syncing_filestore:
            previous_fs = os.path.join(staging, "previous-filestore")
            had_previous = os.path.exists(live_fs)
            if had_previous:
                os.replace(live_fs, previous_fs)
            try:
                os.replace(staging_fs, live_fs)
            except BaseException:
                if had_previous:
                    os.replace(previous_fs, live_fs)
                raise
        elif not os.path.isdir(live_fs):
            os.makedirs(live_fs, exist_ok=True)
        if reload_db:
            os.replace(staged_dump, os.path.join(tpl_dir, dump_name))
            for stale in _SOURCE_DUMP_NAMES:
                stale_path = os.path.join(tpl_dir, stale)
                if stale != dump_name and os.path.isfile(stale_path):
                    os.remove(stale_path)
        if metadata.get("odoo_image") and syncing_filestore:
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


def _dump_identity(dump: _SourceDump, source_id: str) -> dict[str, object]:
    return {
        "source": source_id,
        "key": dump.key,
        "token": dump.token,
        "size": dump.size,
    }


def _record_staged_dump(staging: str, dump: _SourceDump, source_id: str) -> None:
    # Record only after the atomic download/copy completed. An interrupted
    # replacement must never certify the previous same-sized dump.
    atomic_write_private_json(
        os.path.join(staging, "dump-source.json"), _dump_identity(dump, source_id)
    )


def _stage_dump_check(
    staging: str, dump: _SourceDump, source_id: str
) -> tuple[str, bool]:
    """Return (staged dump path, whether it still needs fetching).

    The dump stages under its canonical target name, so a ``db.dump``
    source never installs a non-canonical file into the template.
    """
    target_name = _canonical_dump_name(dump.name)
    staged_dump = os.path.join(staging, target_name)
    try:
        with open(os.path.join(staging, "dump-source.json")) as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = None
    needed = not (
        dump.token
        and previous == _dump_identity(dump, source_id)
        and os.path.isfile(staged_dump)
        and os.path.getsize(staged_dump) == dump.size
    )
    if needed:
        # Drop stale staged dumps under other names so exactly one survives.
        for name in _DUMP_NAMES:
            if name == target_name:
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


# --- Odoo backup archives ----------------------------------------------------
#
# The raw layout above is the efficient way to move a large database, and
# remains the recommended one. But an operator who already holds an Odoo
# backup .zip — written by Odoo's own database manager, or pulled off Odoo.sh —
# paid its packaging cost long before Oduflow was asked to read it, and asking
# them to unpack and re-upload it as a raw layout buys nothing. So a single
# .zip object is accepted as a source shape too, with its costs stated: the
# whole archive is re-fetched whenever it changes, and its filestore replaces
# the template's wholesale rather than syncing file by file.

_ZIP_DUMP_NAME = "dump.sql"
_ZIP_CHECKLIST_PREFIX = "checklist/"


def is_archive_key(key: str) -> bool:
    """Whether an S3 key names a backup archive rather than a source prefix."""
    return key.lower().endswith(".zip")


def zip_uncompressed_bytes(path: str) -> int:
    import zipfile

    with zipfile.ZipFile(path, "r") as zf:
        return sum(info.file_size for info in zf.infolist())


def extract_odoo_backup_zip(
    zip_path: str, dump_dest: str, filestore_dest: str, *, with_filestore: bool = True
) -> dict[str, Any]:
    """Unpack an Odoo database-manager backup ``.zip``; return its manifest.

    Writes ``dump.sql`` to ``dump_dest`` and replaces ``filestore_dest`` with
    the archive's ``filestore/`` tree. Members that would escape that
    directory (zip-slip) and the ``checklist/`` symlink-like entries are
    skipped. Shared by the pull-from-Odoo and the S3 archive imports, so the
    guard has one definition.

    ``with_filestore=False`` reads only the dump: a database-only import
    should not spend the disk and the minutes unpacking attachments it is
    about to throw away.
    """
    import zipfile

    manifest: dict[str, Any] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if _ZIP_DUMP_NAME not in names:
            raise PrerequisiteNotMetError(
                "The archive is not an Odoo backup: no dump.sql at its top level."
            )
        if "manifest.json" in names:
            with zf.open("manifest.json") as mf:
                loaded = json.load(mf)
            if isinstance(loaded, dict):
                manifest = loaded

        os.makedirs(os.path.dirname(dump_dest) or ".", exist_ok=True)
        with zf.open(_ZIP_DUMP_NAME) as src, open(dump_dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        logger.info("Extracted dump.sql to %s", dump_dest)

        if not with_filestore:
            return manifest

        if os.path.exists(filestore_dest):
            shutil.rmtree(filestore_dest)
        os.makedirs(filestore_dest, exist_ok=True)

        files = 0
        for member in names:
            if not member.startswith(_FILESTORE_PREFIX):
                continue
            rel = member[len(_FILESTORE_PREFIX) :]
            if not rel or rel.startswith(_ZIP_CHECKLIST_PREFIX):
                continue
            target = _safe_join(filestore_dest, rel.rstrip("/"))
            if target is None:
                logger.warning(
                    "Skipping unsafe archive member outside filestore: %s", member
                )
                continue
            if member.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files += 1
    logger.info("Extracted filestore to %s (%d files)", filestore_dest, files)
    return manifest


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
    else:
        _check_overwrite_quota(settings, team, template_name, client)

    carried = _load_carried_metadata(team, template_name) if overwrite else {}
    creds = resolve_source_credentials(
        team,
        template_name,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        carried=carried,
    )
    # Remembered like the keys: a re-sync of a region-pinned bucket must not
    # need the region typed again (a dashboard Sync has nowhere to ask).
    region = region or str(carried.get("source_region") or "")
    s3 = _make_source_client(
        settings,
        bucket,
        endpoint=endpoint,
        access_key=creds.access_key,
        secret_key=creds.secret_key,
        region=region,
    )
    listing = _list_s3_source(s3, bucket, prefix)
    dump = listing.dump
    remote_fs = {} if without_filestore else listing.filestore

    staging = team.get_import_staging_dir(template_name)
    staging_fs = os.path.normpath(os.path.join(staging, "filestore"))
    os.makedirs(staging, exist_ok=True)

    reload_db = not (
        overwrite
        and _dump_unchanged(
            settings, team, template_name, dump, carried, client, source_url, endpoint
        )
    )
    dump_fetch_needed = False
    if reload_db:
        staged_dump, dump_fetch_needed = _stage_dump_check(
            staging, dump, source_url + "|" + endpoint
        )

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

    if dump_fetch_needed:
        _record_staged_dump(staging, dump, source_url + "|" + endpoint)
    metadata = _source_metadata(carried, source_url=source_url, dump=dump)
    metadata["source_endpoint"] = endpoint
    metadata["source_region"] = region
    metadata.update(credential_metadata(creds))
    store_source_secret(team, creds)
    try:
        final = _promote_and_reload(
            settings,
            team,
            template_name,
            client=client,
            staging=staging,
            staging_fs=staging_fs,
            syncing_filestore=not without_filestore,
            reload_db=reload_db,
            dump_name=_canonical_dump_name(dump.name),
            metadata=metadata,
            overwrite=overwrite,
        )
    except BaseException:
        # A failed fresh import leaves no template behind to own the secret it
        # just stored. A re-sync keeps its own: the template still needs it.
        if creds.store_value and not overwrite:
            delete_managed_secret(team, credential_metadata(creds))
        raise
    release_replaced_secret(team, creds, carried)
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


def import_from_s3_archive(
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
    """Import (or re-sync) a template from a single Odoo backup ``.zip``.

    A zip is opaque until it is on disk, so none of the prefix layout's
    incremental machinery applies: the object is fetched whole, unpacked into
    staging and promoted by the same swap. What does carry over is the ETag
    skip — an archive unchanged since the last sync costs one HEAD.
    """
    from oduflow.docker_ops.client import get_client

    validate_template_name(template_name)
    bucket, key = parse_s3_url(url)
    source_url = f"s3://{bucket}/{key}"

    client = get_client()
    if not overwrite:
        _check_fresh_conflicts(settings, team, template_name, client)
    else:
        _check_overwrite_quota(settings, team, template_name, client)

    carried = _load_carried_metadata(team, template_name) if overwrite else {}
    creds = resolve_source_credentials(
        team,
        template_name,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        carried=carried,
    )
    region = region or str(carried.get("source_region") or "")
    s3 = _make_source_client(
        settings,
        bucket,
        endpoint=endpoint,
        access_key=creds.access_key,
        secret_key=creds.secret_key,
        region=region,
    )
    with _s3_errors(f"Reading {source_url}"):
        head = s3.head_object(Bucket=bucket, Key=key)
    dump = _SourceDump(
        name=_ZIP_DUMP_NAME,
        key=key,
        size=int(head.get("ContentLength") or 0),
        token=str(head.get("ETag") or "").strip('"'),
        last_modified=_iso(head.get("LastModified")),
    )

    if overwrite and _dump_unchanged(
        settings, team, template_name, dump, carried, client, source_url, endpoint
    ):
        logger.info("%s unchanged since the last sync; nothing to do", source_url)
        return {
            "status": "unchanged",
            "template_name": template_name,
            "source_url": source_url,
            "source_db": dump.name,
            "odoo_image": carried.get("odoo_image", ""),
            "odoo_version": carried.get("odoo_version", ""),
            "template_db": get_template_db_name(template_name, team.team_id),
            "restore_seconds": 0,
            "dump_reloaded": False,
            "includes_filestore": carried.get("includes_filestore", False),
            "downloaded_files": 0,
            "downloaded_mb": 0.0,
            "reused_files": 0,
            "removed_files": 0,
            "affected_envs": [],
            "remount_failures": [],
        }

    staging = team.get_import_staging_dir(template_name)
    staging_fs = os.path.normpath(os.path.join(staging, "filestore"))
    os.makedirs(staging, exist_ok=True)
    # The archive and everything it unpacks to live side by side until the
    # promote, so both have to fit.
    _check_free_disk(staging, dump.size * 2)

    archive = os.path.join(staging, "archive.zip")
    logger.info("Downloading %s (%d bytes)...", source_url, dump.size)
    try:
        _download_one(s3, bucket, key, archive)
        unpacked = zip_uncompressed_bytes(archive)
        _check_free_disk(staging, unpacked)
        staged_dump = os.path.join(staging, _ZIP_DUMP_NAME)
        extract_odoo_backup_zip(
            archive, staged_dump, staging_fs, with_filestore=not without_filestore
        )
        os.remove(archive)

        metadata = _source_metadata(carried, source_url=source_url, dump=dump)
        metadata["source_endpoint"] = endpoint
        metadata["source_region"] = region
        metadata.update(credential_metadata(creds))
        store_source_secret(team, creds)
        final = _promote_and_reload(
            settings,
            team,
            template_name,
            client=client,
            staging=staging,
            staging_fs=staging_fs,
            syncing_filestore=not without_filestore,
            reload_db=True,
            dump_name=_ZIP_DUMP_NAME,
            metadata=metadata,
            overwrite=overwrite,
        )
    except BaseException:
        if creds.store_value and not overwrite:
            delete_managed_secret(team, credential_metadata(creds))
        # Nothing here is resumable, so a failed attempt leaves no staging
        # behind to be mistaken for progress on the next run.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    release_replaced_secret(team, creds, carried)

    result_metadata = final["metadata"]
    return {
        "status": final["status"],
        "template_name": template_name,
        "source_url": source_url,
        "source_db": dump.name,
        "odoo_image": result_metadata.get("odoo_image", ""),
        "odoo_version": result_metadata.get("odoo_version", ""),
        "template_db": final["template_db"],
        "restore_seconds": final["restore_seconds"],
        "dump_reloaded": True,
        "includes_filestore": result_metadata["includes_filestore"],
        "downloaded_files": 1,
        "downloaded_mb": round(dump.size / 1024**2, 1),
        "reused_files": 0,
        "removed_files": 0,
        "affected_envs": final["affected_envs"],
        "remount_failures": final["remount_failures"],
    }


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
    else:
        _check_overwrite_quota(settings, team, template_name, client)

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
        and _dump_unchanged(
            settings, team, template_name, dump, carried, client, source
        )
    )
    dump_fetch_needed = False
    if reload_db:
        staged_dump, dump_fetch_needed = _stage_dump_check(staging, dump, source)

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
        _record_staged_dump(staging, dump, source)

    metadata = _source_metadata(carried, source_url=source, dump=dump)
    metadata.pop("source_endpoint", None)
    final = _promote_and_reload(
        settings,
        team,
        template_name,
        client=client,
        staging=staging,
        staging_fs=staging_fs,
        syncing_filestore=not without_filestore and os.path.isdir(source),
        reload_db=reload_db,
        dump_name=_canonical_dump_name(dump.name),
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
