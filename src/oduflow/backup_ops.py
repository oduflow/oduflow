"""Production backup orchestration: snapshots, restore, status, prune.

A **snapshot** is the per-production restorable unit: a consistent triple
of (database pg_dump, filestore chunkstore revision, deployed commit sha)
bound together by a manifest. Snapshots restore ONE production without
touching the others in the shared cluster — WAL-G (see :mod:`oduflow.walg`)
covers the complementary cluster-level disaster recovery / PITR.

Ordering inside a snapshot: database FIRST, then filestore. Odoo's DB
references filestore blobs by content hash; files written after the DB dump
are harmless extras, whereas the reverse order could reference blobs
missing from the filestore copy.

S3 layout (bucket + prefix from [backup])::

    {prefix}/walg/                                    (cluster, see walg.py)
    {prefix}/snapshots/{team}/{prod}/db/{id}.pgdump
    {prefix}/snapshots/{team}/{prod}/manifests/{id}.json
    {prefix}/filestore/{team}/                        (chunkstore, per team)

The chunkstore is per-team (not per-production): a team's productions are
often clones of each other, so cross-production dedup pays; the team
boundary preserves tenant isolation. Manifests are cached locally
({team.data_dir}/production/{prod}/snapshots/) with S3 as the source of
truth (survives cache loss / server reinstall).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from typing import Any, ContextManager

from oduflow import chunkstore, production_registry, s3_client
from oduflow.chunkstore.prune import parse_keep, select_revisions_to_keep
from oduflow.docker_ops import production_ops
from oduflow.docker_ops.client import chown_recursive, get_client, get_odoo_uid_gid
from oduflow.docker_ops.system_ops import (
    _copy_file_to_container,
    _exec_sql,
    drop_signaling_sequences,
    reassign_db_ownership,
)
from oduflow.env_credentials import load_credentials
from oduflow.errors import (
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.naming import (
    PRODUCTION_TEMPLATE_PREFIX,
    get_filestore_paths,
    prod_env_name,
)
from oduflow.settings import BackupSettings, Settings, TeamSettings

logger = logging.getLogger("oduflow")


def _require_backup(settings: Settings) -> BackupSettings:
    if settings.backup is None:
        raise PrerequisiteNotMetError(
            "Backups are not configured. Add a [backup] section (bucket, "
            "access_key, secret_key) to oduflow.toml and restart."
        )
    return settings.backup


def filestore_storage(settings: Settings, team: TeamSettings) -> s3_client.S3Storage:
    backup = _require_backup(settings)
    return s3_client.S3Storage(backup, f"{backup.prefix}/filestore/{team.team_id}")


def _snapshot_prefix(backup: BackupSettings, team: TeamSettings, name: str) -> str:
    return f"{backup.prefix}/snapshots/{team.team_id}/{name}"


def _manifest_cache_dir(team: TeamSettings, name: str) -> str:
    return os.path.join(team.data_dir, "production", name, "snapshots")


def _cache_manifest(team: TeamSettings, name: str, manifest: dict[str, Any]) -> None:
    cache_dir = _manifest_cache_dir(team, name)
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{manifest['id']}.json")
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _snapshot_id(now: datetime.datetime | None = None) -> str:
    return (now or _now_utc()).strftime("%Y%m%dT%H%M%SZ")


def _dump_stream(container: Any, db_user: str, db_name: str) -> Iterator[bytes]:
    """Yield pg_dump -Fc stdout frames from a docker exec stream.

    Uses the low-level exec API so the command's exit code can be inspected
    after the stream drains: with ``stream=True`` docker-py leaves
    ``exit_code=None`` and never raises on failure, so a pg_dump that dies
    mid-dump would otherwise stream a truncated archive that gets recorded as a
    healthy snapshot. Raising here (after the last frame, before the generator
    is exhausted) makes ``multipart_upload_stream`` abort the upload and
    ``snapshot_production`` fail before writing the manifest — no corrupt
    snapshot is ever committed.
    """
    api = container.client.api
    exec_id = api.exec_create(container.id, ["pg_dump", "-U", db_user, "-Fc", db_name])[
        "Id"
    ]
    for stdout, stderr in api.exec_start(exec_id, stream=True, demux=True):
        if stderr:
            logger.debug("pg_dump stderr: %s", stderr[-500:])
        if stdout:
            yield stdout
    exit_code = api.exec_inspect(exec_id).get("ExitCode")
    if exit_code:
        raise ExternalCommandError(
            "pg_dump", exit_code, f"pg_dump of {db_name} exited with code {exit_code}"
        )


def snapshot_production(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    trigger: str = "mcp",
    note: str = "",
) -> dict[str, Any]:
    """Take a snapshot (DB dump + filestore revision + manifest) to S3.

    The caller holds the production's lock. The manifest is uploaded LAST:
    a snapshot without a manifest does not exist (orphaned dump/chunks are
    reclaimed by prune).
    """
    from oduflow.wal_monitor import assert_writable

    assert_writable(settings)
    backup = _require_backup(settings)
    production_registry.get_production(team, name)
    client = get_client()
    env_name = prod_env_name(name)
    db_name = production_ops.prod_db_name(team, name)
    filestore_dir = production_ops.prod_filestore_dir(team, name)

    from oduflow.git_ops import rev_parse
    from oduflow.naming import get_repo_path

    commit = ""
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    if os.path.isdir(repo_path):
        try:
            commit = rev_parse(repo_path)
        except Exception:
            commit = ""

    snapshot_id = _snapshot_id()
    prefix = _snapshot_prefix(backup, team, name)
    db_key = f"{prefix}/db/{snapshot_id}.pgdump"
    started = _now_utc().isoformat()

    # 1. Database: stream pg_dump straight into a multipart upload — no
    # temp disk. -Fc is internally compressed and MVCC-consistent.
    pg = client.containers.get(settings.prod_db_container)
    s3 = s3_client.make_client(backup)
    digest = hashlib.sha256()

    def _hashing_frames() -> Any:
        for frame in _dump_stream(pg, settings.db_user, db_name):
            digest.update(frame)
            yield frame

    db_started = time.monotonic()
    db_bytes = s3_client.multipart_upload_stream(
        s3, backup.bucket, db_key, _hashing_frames()
    )
    db_seconds = round(time.monotonic() - db_started, 1)
    if db_bytes == 0:
        raise ExternalCommandError(
            "pg_dump", 1, f"pg_dump of {db_name} produced no output"
        )

    # 2. Filestore: chunkstore revision (incremental, deduplicated).
    if os.path.isdir(filestore_dir):
        fs_started = time.monotonic()
        fs_result = chunkstore.backup(
            filestore_dir,
            filestore_storage(settings, team),
            name,
            upload_threads=backup.upload_threads,
        )
        filestore_info = {
            "revision": fs_result.revision,
            "files": fs_result.files,
            "bytes": fs_result.total_bytes,
            "new_chunks": fs_result.new_chunks,
            "uploaded_bytes": fs_result.uploaded_bytes,
            "seconds": round(time.monotonic() - fs_started, 1),
        }
    else:
        filestore_info = {"revision": 0, "files": 0, "bytes": 0, "seconds": 0.0}

    # 3. Manifest (uploaded last — commits the snapshot).
    manifest = {
        "id": snapshot_id,
        "team": team.team_id,
        "production": name,
        "created_at": started,
        "finished_at": _now_utc().isoformat(),
        "trigger": trigger,
        "note": note,
        "commit_sha": commit,
        "db": {
            "key": db_key,
            "sha256": digest.hexdigest(),
            "bytes": db_bytes,
            "seconds": db_seconds,
        },
        "filestore": filestore_info,
    }
    s3.put_object(
        Bucket=backup.bucket,
        Key=f"{prefix}/manifests/{snapshot_id}.json",
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
    )
    _cache_manifest(team, name, manifest)
    production_registry.set_nested(
        team,
        name,
        "backup",
        {
            "last_snapshot_id": snapshot_id,
            "last_snapshot_at": manifest["finished_at"],
            "last_result": "success",
            "last_error": "",
        },
    )
    logger.info(
        "Snapshot %s of production '%s': db %d bytes in %.1fs, "
        "filestore rev %s in %.1fs",
        snapshot_id,
        name,
        db_bytes,
        db_seconds,
        filestore_info.get("revision"),
        filestore_info.get("seconds", 0.0),
    )
    return manifest


def list_snapshots(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Snapshot manifests, oldest first. Local cache is the fast path;
    ``refresh`` re-lists S3 (source of truth) and reconciles the cache."""
    production_registry.get_production(team, name)
    cache_dir = _manifest_cache_dir(team, name)

    if refresh:
        backup = _require_backup(settings)
        storage = s3_client.S3Storage(backup, _snapshot_prefix(backup, team, name))
        os.makedirs(cache_dir, exist_ok=True)
        seen = set()
        for key in storage.list("manifests/"):
            try:
                manifest = json.loads(storage.get(key).decode("utf-8"))
                _cache_manifest(team, name, manifest)
                seen.add(f"{manifest['id']}.json")
            except Exception:
                logger.warning("Unreadable manifest %s", key)
        for stale in os.listdir(cache_dir):
            if stale.endswith(".json") and stale not in seen:
                os.remove(os.path.join(cache_dir, stale))

    manifests = []
    if os.path.isdir(cache_dir):
        for entry in sorted(os.listdir(cache_dir)):
            if not entry.endswith(".json"):
                continue
            try:
                with open(os.path.join(cache_dir, entry)) as f:
                    manifests.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    return manifests


def _load_manifest(
    settings: Settings, team: TeamSettings, name: str, snapshot_id: str
) -> dict[str, Any]:
    path = os.path.join(_manifest_cache_dir(team, name), f"{snapshot_id}.json")
    if os.path.isfile(path):
        with open(path) as f:
            manifest: dict[str, Any] = json.load(f)
            return manifest
    backup = _require_backup(settings)
    storage = s3_client.S3Storage(backup, _snapshot_prefix(backup, team, name))
    key = f"manifests/{snapshot_id}.json"
    if not storage.exists(key):
        raise NotFoundError(
            f"Snapshot '{snapshot_id}' not found for production '{name}' "
            "(list_production_snapshots refresh=true to re-sync from S3)."
        )
    manifest = json.loads(storage.get(key).decode("utf-8"))
    _cache_manifest(team, name, manifest)
    return manifest


def _filestore_revision_from_manifest(manifest: dict[str, Any]) -> int:
    """Validated filestore revision, where zero means an empty filestore."""
    filestore = manifest.get("filestore")
    if not isinstance(filestore, dict) or "revision" not in filestore:
        raise PrerequisiteNotMetError(
            "Snapshot manifest has no filestore revision; refusing to combine "
            "its database with the current production filestore."
        )
    raw_revision = filestore["revision"]
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError):
        raise PrerequisiteNotMetError(
            "Snapshot manifest has an invalid filestore revision."
        )
    if (
        isinstance(raw_revision, bool)
        or (isinstance(raw_revision, float) and not raw_revision.is_integer())
        or revision < 0
    ):
        raise PrerequisiteNotMetError(
            "Snapshot manifest has an invalid filestore revision."
        )
    return revision


def _swap_restored_filestore(
    restore_dir: str, filestore_dir: str, old_dir: str
) -> bool:
    """Install a staged filestore, restoring the live directory on failure.

    Return whether a previous live filestore was moved to ``old_dir``. The
    caller removes that backup only after the database and filestore pair is
    fully committed.
    """
    had_previous = os.path.isdir(filestore_dir)
    if had_previous:
        os.replace(filestore_dir, old_dir)
    try:
        os.replace(restore_dir, filestore_dir)
    except BaseException:
        if had_previous and os.path.isdir(old_dir):
            os.replace(old_dir, filestore_dir)
        raise
    return had_previous


def _rollback_database_swap(
    client: Any,
    settings: Settings,
    db_name: str,
    restore_db: str,
    old_db: str,
    pg_container: str,
) -> None:
    """Put the old live DB back and move the restored DB to scratch."""
    _exec_sql(
        client,
        settings,
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db_name}';",
        container_name=pg_container,
    )
    _exec_sql(
        client,
        settings,
        f'ALTER DATABASE "{db_name}" RENAME TO "{restore_db}";',
        container_name=pg_container,
    )
    _exec_sql(
        client,
        settings,
        f'ALTER DATABASE "{old_db}" RENAME TO "{db_name}";',
        container_name=pg_container,
    )


def _commit_restored_pair(
    client: Any,
    settings: Settings,
    name: str,
    container: Any,
    was_running: bool,
    db_name: str,
    restore_db: str,
    old_db: str,
    restore_dir: str,
    filestore_dir: str,
    old_dir: str,
    pg_container: str,
) -> None:
    """Swap a fully staged (database, filestore) pair into the live production.

    Downtime starts here: the production container is stopped, the staged
    pair is renamed in, and the container is restarted only once the live
    state is provably coherent again (committed, or completely rolled back).
    On failure the previous state is back in place and the staged database —
    under ``restore_db`` again after a compensated filestore failure — is
    left for the caller's cleanup.
    """
    container_stopped = False
    live_state_safe = True
    try:
        if container is not None and was_running:
            container.stop()
            container_stopped = True
        _exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{old_db}" WITH (FORCE);',
            container_name=pg_container,
        )
        _exec_sql(
            client,
            settings,
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db_name}';",
            container_name=pg_container,
        )
        # From the first live rename until either the filestore commit or a
        # complete rollback, an exception leaves the database name uncertain.
        # Keep Odoo stopped unless one of those paths proves the live pair is
        # coherent again.
        live_state_safe = False
        _exec_sql(
            client,
            settings,
            f'ALTER DATABASE "{db_name}" RENAME TO "{old_db}";',
            container_name=pg_container,
        )
        try:
            _exec_sql(
                client,
                settings,
                f'ALTER DATABASE "{restore_db}" RENAME TO "{db_name}";',
                container_name=pg_container,
            )
        except BaseException:
            # Roll the old database back into place.
            _exec_sql(
                client,
                settings,
                f'ALTER DATABASE "{old_db}" RENAME TO "{db_name}";',
                container_name=pg_container,
            )
            live_state_safe = True
            raise

        try:
            had_previous_filestore = _swap_restored_filestore(
                restore_dir, filestore_dir, old_dir
            )
        except BaseException:
            # The filesystem helper has already put the old filestore back.
            # Compensate the successful database rename so callers never get a
            # restored DB paired with stale/missing attachment files.
            _rollback_database_swap(
                client, settings, db_name, restore_db, old_db, pg_container
            )
            live_state_safe = True
            raise

        live_state_safe = True

        if had_previous_filestore and os.path.isdir(old_dir):
            shutil.rmtree(old_dir, ignore_errors=True)

        # Old database dropped only after everything else succeeded.
        _exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{old_db}" WITH (FORCE);',
            container_name=pg_container,
        )
    finally:
        if container is not None and container_stopped and live_state_safe:
            try:
                from oduflow.wal_monitor import assert_writable

                assert_writable(settings)
                container.start()
            except Exception:
                logger.exception("Could not restart production '%s'", name)
        elif container is not None and container_stopped:
            logger.error(
                "Production '%s' remains stopped because database rollback "
                "did not complete; inspect %s and %s before restarting",
                name,
                db_name,
                old_db,
            )


def restore_production(
    settings: Settings,
    team: TeamSettings,
    name: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Restore a production's database + filestore from a snapshot.

    Swap-based, so a failed restore never leaves a half-written live state:
    the dump is restored into ``{db}__restore`` and swapped in by rename;
    the filestore is rebuilt into a sibling directory and swapped by
    rename. The caller holds the production's lock.
    """
    from oduflow.wal_monitor import assert_writable

    assert_writable(settings)
    backup = _require_backup(settings)
    record = production_registry.get_production(team, name)
    manifest = _load_manifest(settings, team, name, snapshot_id)
    fs_revision = _filestore_revision_from_manifest(manifest)
    client = get_client()
    env_name = prod_env_name(name)
    db_name = production_ops.prod_db_name(team, name)
    filestore_dir = production_ops.prod_filestore_dir(team, name)
    pg_container = settings.prod_db_container

    # Free disk pre-check for the dump download.
    tmp_root = os.path.join(team.data_dir, "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    need = int(manifest["db"]["bytes"]) * 2
    free = shutil.disk_usage(tmp_root).free
    if free < need:
        raise PrerequisiteNotMetError(
            f"Not enough free disk for restore: need ~{need // 1024**2} MB "
            f"in {tmp_root}, have {free // 1024**2} MB."
        )

    container = production_ops._get_container(client, settings, team, name)
    was_running = container is not None and container.status == "running"

    restore_db = f"{db_name}__restore"
    old_db = f"{db_name}__old"
    filestore_parent = os.path.dirname(filestore_dir)
    restore_dir = ""
    old_dir = ""
    committed = False
    try:
        os.makedirs(filestore_parent, exist_ok=True)
        restore_dir = tempfile.mkdtemp(
            dir=filestore_parent, prefix=f".restore-{snapshot_id}-"
        )
        old_dir = tempfile.mkdtemp(dir=filestore_parent, prefix=f".old-{snapshot_id}-")
        os.rmdir(old_dir)  # reserve a unique sibling path that does not yet exist

        # Build the complete target filestore before the database is swapped.
        # Revision zero is an exact empty-filestore snapshot, not an instruction
        # to retain whatever happens to be live now.
        if fs_revision > 0:
            chunkstore.restore(
                filestore_storage(settings, team), name, fs_revision, restore_dir
            )
        odoo_image = record.get("odoo_image", "")
        if odoo_image:
            uid_str, gid_str = get_odoo_uid_gid(client, odoo_image).split(":")
            chown_recursive(restore_dir, int(uid_str), int(gid_str), client, odoo_image)

        # ------------------------ database ------------------------------
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmpdir:
            dump_path = os.path.join(tmpdir, f"{snapshot_id}.pgdump")
            s3 = s3_client.make_client(backup)
            s3.download_file(backup.bucket, manifest["db"]["key"], dump_path)
            digest = hashlib.sha256()
            with open(dump_path, "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != manifest["db"]["sha256"]:
                raise ExternalCommandError(
                    "restore", 1, "Downloaded dump failed sha256 verification."
                )
            pg = client.containers.get(pg_container)
            _copy_file_to_container(pg, dump_path, "/tmp")
            in_container = f"/tmp/{os.path.basename(dump_path)}"

        _exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{restore_db}" WITH (FORCE);',
            container_name=pg_container,
        )
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{restore_db}";',
            container_name=pg_container,
        )
        exit_code, output = pg.exec_run(
            [
                "pg_restore",
                "-U",
                settings.db_user,
                "--no-owner",
                "-j",
                "2",
                "-d",
                restore_db,
                in_container,
            ]
        )
        pg.exec_run(["rm", "-f", in_container])
        if exit_code != 0:
            text = output.decode("utf-8", errors="replace") if output else ""
            raise ExternalCommandError("pg_restore", exit_code, text[-2000:])

        creds = load_credentials(
            env_name, team.workspaces_dir, settings.db_user, settings.db_password
        )
        reassign_db_ownership(
            client, settings, restore_db, creds["pg_user"], container_name=pg_container
        )
        drop_signaling_sequences(
            client, settings, restore_db, container_name=pg_container
        )

        # Swap: everything above is prepared while Odoo remains available;
        # downtime starts only for the paired database + filestore commit.
        _commit_restored_pair(
            client,
            settings,
            name,
            container,
            was_running,
            db_name,
            restore_db,
            old_db,
            restore_dir,
            filestore_dir,
            old_dir,
            pg_container,
        )
        committed = True
    finally:
        if restore_dir and os.path.isdir(restore_dir):
            shutil.rmtree(restore_dir, ignore_errors=True)
        if not committed:
            # Failed before the commit (or rolled back): clean the staged DB.
            try:
                _exec_sql(
                    client,
                    settings,
                    f'DROP DATABASE IF EXISTS "{restore_db}" WITH (FORCE);',
                    container_name=pg_container,
                )
            except Exception:
                pass

    # A production that was stopped before the restore is left stopped by
    # _commit_restored_pair, so probing it would burn the full timeout and then
    # brand a successful restore as a failed deploy. Only a production that was
    # serving before is expected to be serving after.
    healthy = True
    if was_running:
        healthy = production_ops.wait_production_healthy(
            client, settings, team, name, timeout=180
        )
        production_registry.update_production(team, name, {"unhealthy": not healthy})
    production_registry.set_nested(
        team, name, "backup", {"last_restore_id": snapshot_id}
    )

    warning = ""
    current_commit = ""
    from oduflow.git_ops import rev_parse
    from oduflow.naming import get_repo_path

    repo_path = get_repo_path(env_name, team.workspaces_dir)
    if os.path.isdir(repo_path):
        try:
            current_commit = rev_parse(repo_path)
        except Exception:
            current_commit = ""
    if (
        manifest.get("commit_sha")
        and current_commit
        and (manifest["commit_sha"] != current_commit)
    ):
        warning = (
            f"Snapshot was taken at commit {manifest['commit_sha'][:10]} but the "
            f"checkout is at {current_commit[:10]} — consider "
            f'rollback_production(to_commit="{manifest["commit_sha"][:10]}") '
            "so the code matches the data."
        )

    production_ops.append_deploy(
        team,
        name,
        {
            "ts_start": _now_utc().isoformat(),
            "ts_end": _now_utc().isoformat(),
            "trigger": "restore",
            "from_commit": current_commit,
            "to_commit": current_commit,
            "action": f"restore:{snapshot_id}",
            "status": "success" if healthy else "rollback_failed",
            "error": "" if healthy else "Health check failed after restore.",
            "note": "" if was_running else "Production was stopped; not probed.",
        },
    )
    return {
        "name": name,
        "snapshot_id": snapshot_id,
        "healthy": healthy,
        "warning": warning,
        "db_bytes": manifest["db"]["bytes"],
        "filestore_revision": fs_revision,
    }


def restore_production_from_environment(
    settings: Settings,
    team: TeamSettings,
    name: str,
    source: str,
    env_lock: Callable[[], ContextManager[None]] | None = None,
) -> dict[str, Any]:
    """Replace a production's database + filestore with a dev environment's.

    The promotion counterpart of the snapshot restore: the environment's
    database and filestore are copied while its Odoo is briefly stopped for a
    consistent pair (the environment itself is not reset), staged next to the
    production, and committed with the same swap mechanics as
    ``restore_production`` — a failed restore leaves the previous state in
    place. No backup/S3 configuration is required, and no sanitization
    happens: the data goes INTO production. The caller holds the production's
    lock; ``env_lock`` scopes the source environment's lock to the copy slice
    so dev work on the branch is not blocked by the whole restore.
    """
    from oduflow.wal_monitor import assert_writable

    assert_writable(settings)
    record = production_registry.get_production(team, name)
    client = get_client()
    source_env = production_ops._source_env_info(client, settings, team, source)
    env_name = prod_env_name(name)
    db_name = production_ops.prod_db_name(team, name)
    filestore_dir = production_ops.prod_filestore_dir(team, name)
    pg_container = settings.prod_db_container

    # Free disk pre-check for the filestore copy. The staging directory is a
    # sibling of the live filestore on the volume that also holds the running
    # production's PGDATA, so an oversized source would not merely fail the
    # copy — it could wedge the production before ENOSPC is raised. The
    # snapshot path guards its dump download the same way.
    filestore_parent = os.path.dirname(filestore_dir)
    os.makedirs(filestore_parent, exist_ok=True)
    src_filestore = get_filestore_paths(source, team.workspaces_dir)["merged"]
    if os.path.isdir(src_filestore):
        from oduflow.docker_ops.stats import _dir_size_bytes

        need = _dir_size_bytes(src_filestore)
        free = shutil.disk_usage(filestore_parent).free
        if free < need:
            raise PrerequisiteNotMetError(
                f"Not enough free disk to stage the filestore of environment "
                f"'{source}': need ~{need // 1024**2} MB in {filestore_parent}, "
                f"have {free // 1024**2} MB."
            )

    container = production_ops._get_container(client, settings, team, name)
    was_running = container is not None and container.status == "running"

    restore_db = f"{db_name}__restore"
    old_db = f"{db_name}__old"
    restore_dir = ""
    committed = False
    notes: list[str] = []
    try:
        os.makedirs(filestore_parent, exist_ok=True)
        restore_dir = tempfile.mkdtemp(dir=filestore_parent, prefix=".restore-env-")
        old_dir = tempfile.mkdtemp(dir=filestore_parent, prefix=".old-env-")
        os.rmdir(old_dir)  # reserve a unique sibling path that does not yet exist

        _exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{restore_db}" WITH (FORCE);',
            container_name=pg_container,
        )
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{restore_db}";',
            container_name=pg_container,
        )

        # Stage the environment's pair; the production stays up for this
        # multi-minute part, only the source environment stops briefly.
        with env_lock() if env_lock is not None else nullcontext():
            notes.extend(
                production_ops._copy_env_data_into_production(
                    client,
                    settings,
                    team,
                    source_env,
                    source,
                    name,
                    restore_db,
                    filestore_dest=restore_dir,
                )
            )

        odoo_image = record.get("odoo_image", "")
        if odoo_image:
            uid_str, gid_str = get_odoo_uid_gid(client, odoo_image).split(":")
            chown_recursive(restore_dir, int(uid_str), int(gid_str), client, odoo_image)

        creds = load_credentials(
            env_name, team.workspaces_dir, settings.db_user, settings.db_password
        )
        reassign_db_ownership(
            client, settings, restore_db, creds["pg_user"], container_name=pg_container
        )
        drop_signaling_sequences(
            client, settings, restore_db, container_name=pg_container
        )

        _commit_restored_pair(
            client,
            settings,
            name,
            container,
            was_running,
            db_name,
            restore_db,
            old_db,
            restore_dir,
            filestore_dir,
            old_dir,
            pg_container,
        )
        committed = True
    finally:
        if restore_dir and os.path.isdir(restore_dir):
            shutil.rmtree(restore_dir, ignore_errors=True)
        if not committed:
            # Failed before the commit (or rolled back): clean the staged DB.
            try:
                _exec_sql(
                    client,
                    settings,
                    f'DROP DATABASE IF EXISTS "{restore_db}" WITH (FORCE);',
                    container_name=pg_container,
                )
            except Exception:
                pass

    # A production that was stopped before the restore is left stopped by
    # _commit_restored_pair, so probing it would burn the full timeout and then
    # brand a successful restore as a failed deploy. Only a production that was
    # serving before is expected to be serving after.
    healthy = True
    if was_running:
        healthy = production_ops.wait_production_healthy(
            client, settings, team, name, timeout=180
        )
        production_registry.update_production(team, name, {"unhealthy": not healthy})

    if str(source_env.get("template", "")).startswith(PRODUCTION_TEMPLATE_PREFIX):
        notes.append(
            f"Environment '{source}' was created from production data that "
            "was sanitized on copy; the production now runs that sanitized "
            "data."
        )

    warning = ""
    current_commit = ""
    from oduflow.git_ops import rev_parse
    from oduflow.naming import get_repo_path

    repo_path = get_repo_path(env_name, team.workspaces_dir)
    if os.path.isdir(repo_path):
        try:
            current_commit = rev_parse(repo_path)
        except Exception:
            current_commit = ""
    env_head = str(source_env.get("head_commit", ""))
    if env_head and current_commit and env_head != current_commit:
        warning = (
            f"Environment '{source}' has commit {env_head[:10]} checked out "
            f"but the production code is at {current_commit[:10]} — the "
            "copied data may not match the deployed code (update_production "
            "or push the environment's branch if this is unexpected)."
        )

    production_ops.append_deploy(
        team,
        name,
        {
            "ts_start": _now_utc().isoformat(),
            "ts_end": _now_utc().isoformat(),
            "trigger": "restore",
            "from_commit": current_commit,
            "to_commit": current_commit,
            "action": f"restore:env:{source}",
            "status": "success" if healthy else "rollback_failed",
            "error": "" if healthy else "Health check failed after restore.",
            "note": "" if was_running else "Production was stopped; not probed.",
        },
    )
    return {
        "name": name,
        "source_environment": source,
        "healthy": healthy,
        "warning": warning,
        "notes": notes,
    }


def backup_status(settings: Settings, team: TeamSettings) -> dict[str, Any]:
    """Backup posture: per-production snapshot state + cluster WAL-G state."""
    from oduflow import walg

    status: dict[str, Any] = {
        "configured": settings.backup is not None,
        "productions": {},
        "walg": {},
        "s3": {},
    }
    for name, record in production_registry.list_productions(team).items():
        status["productions"][name] = record.get("backup", {})
    if settings.backup is None:
        return status
    try:
        client = get_client()
        status["walg"]["archiver"] = walg.archiver_status(client, settings)
    except Exception as exc:
        status["walg"]["archiver_error"] = str(exc)
    try:
        client = get_client()
        backups = walg.backup_list(client, settings)
        status["walg"].update(
            {
                "base_backups": len(backups),
                "latest_base_backup": (backups[-1] if backups else {}),
            }
        )
    except Exception as exc:
        status["walg"]["error"] = str(exc)
    status["s3"] = s3_client.check_s3(settings.backup)
    return status


def prune_backups(settings: Settings, team: TeamSettings) -> dict[str, Any]:
    """Apply retention to snapshots (manifests + dumps) and the chunkstore.

    Retention is decided ONCE at the manifest level; the chunkstore then
    keeps exactly the filestore revisions the surviving manifests
    reference (lockstep — no orphaned or dangling references).
    """
    backup = _require_backup(settings)
    keep_pairs = parse_keep(backup.keep)
    now = _now_utc()
    deleted_snapshots: list[str] = []
    keep_revisions: dict[str, set[int]] = {}

    for name in production_registry.list_productions(team):
        manifests = list_snapshots(settings, team, name, refresh=True)
        dated = []
        by_id: dict[str, dict[str, Any]] = {}
        for index, manifest in enumerate(manifests, start=1):
            by_id[manifest["id"]] = manifest
            created = datetime.datetime.fromisoformat(manifest["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            dated.append((index, created))
        ids = [m["id"] for m in manifests]
        keep_set = select_revisions_to_keep(dated, keep_pairs, now)
        keep_ids = {ids[i - 1] for i in keep_set}

        storage = s3_client.S3Storage(backup, _snapshot_prefix(backup, team, name))
        revisions: set[int] = set()
        for snapshot_id in ids:
            manifest = by_id[snapshot_id]
            if snapshot_id in keep_ids:
                rev = int(manifest.get("filestore", {}).get("revision", 0) or 0)
                if rev:
                    revisions.add(rev)
                continue
            storage.delete(f"manifests/{snapshot_id}.json")
            storage.delete(f"db/{snapshot_id}.pgdump")
            cache_path = os.path.join(
                _manifest_cache_dir(team, name), f"{snapshot_id}.json"
            )
            if os.path.isfile(cache_path):
                os.remove(cache_path)
            deleted_snapshots.append(f"{name}/{snapshot_id}")
        keep_revisions[name] = revisions

    prune_result = chunkstore.prune(
        filestore_storage(settings, team), keep_revisions=keep_revisions, now=now
    )
    return {
        "deleted_snapshots": deleted_snapshots,
        "chunkstore": {
            "deleted_revisions": prune_result.deleted_revisions,
            "fossilized_chunks": prune_result.fossilized_chunks,
            "deleted_chunks": prune_result.deleted_chunks,
            "resurrected_chunks": prune_result.resurrected_chunks,
        },
    }


def register_pre_update_hook() -> None:
    """Hook a best-effort snapshot before every production deploy."""

    def _pre_update_snapshot(settings: Settings, team: TeamSettings, name: str) -> None:
        if settings.backup is None:
            return
        snapshot_production(settings, team, name, trigger="pre-update")

    # Idempotent registration (server startup may run more than once in tests).
    names = {getattr(h, "__name__", "") for h in production_ops.pre_update_hooks}
    if "_pre_update_snapshot" not in names:
        production_ops.pre_update_hooks.append(_pre_update_snapshot)
