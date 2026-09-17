"""WAL-G integration for the production PostgreSQL cluster.

WAL-G provides continuous WAL archiving to S3 ("replication to S3"),
scheduled base backups, and cluster-level disaster recovery / PITR. It is
delivered as the official static binary downloaded by the Oduflow server at
bootstrap — not a custom Docker image: the binary directory and a config
directory are bind-mounted into the (official-image) production PG
container, so backups can be enabled, reconfigured, or upgraded without
recreating the container.

Container-side layout (both mounts are read-only directories, so their
contents can change while the container runs):

- ``{base_data_dir}/bin``  → ``/opt/oduflow-bin``   (wal-g-{ver} + symlink)
- ``{base_data_dir}/walg`` → ``/etc/walg``          (walg.json, 0600)

``archive_mode=on`` ships in the generated postgresql-prod.conf from day
one (toggling it requires a restart); the actual ``archive_command`` is
managed via ``ALTER SYSTEM`` + reload by :func:`apply_archive_command`, so
adding a [backup] section later needs no PG restart.

Credentials live in the wal-g config file (never in container env: env is
visible in ``docker inspect`` and requires a container recreation to
rotate). Inside the container wal-g talks to PostgreSQL over the local
unix socket (trust auth in the official image).

Note: the pinned binaries are glibc builds (ubuntu-20.04); use the default
Debian-based ``postgres:*`` images, not ``-alpine`` variants.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import tarfile
import tempfile
import time
import urllib.request
from typing import Any

from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

# Last upstream release shipping PostgreSQL builds (v3.0.5+ dropped them).
WALG_VERSION = "v3.0.3"

# Asset names differ between arches upstream (note the missing dash in the
# aarch64 one) — keep them verbatim.
_ASSETS = {
    "amd64": "wal-g-pg-ubuntu-20.04-amd64.tar.gz",
    "aarch64": "wal-g-pg-ubuntu20.04-aarch64.tar.gz",
}

_RELEASE_URL = "https://github.com/wal-g/wal-g/releases/download/{version}/{asset}"

# Container-side paths (see module docstring).
BIN_MOUNT = "/opt/oduflow-bin"
CONF_MOUNT = "/etc/walg"
WALG_BIN = f"{BIN_MOUNT}/wal-g"
WALG_CONF = f"{CONF_MOUNT}/walg.json"

_PGDATA = "/var/lib/postgresql/data"


def bin_host_dir(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, "bin")


def conf_host_dir(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, "walg")


def _walg_version(settings: Settings) -> str:
    return settings.prod_walg_version or WALG_VERSION


def _docker_arch() -> str:
    """Architecture of the Docker daemon (where the PG container runs) —
    correct under Docker Desktop on macOS, unlike host introspection."""
    from oduflow.docker_ops.client import get_client

    arch = str(get_client().info().get("Architecture", "")).lower()
    if arch in ("x86_64", "amd64"):
        return "amd64"
    if arch in ("aarch64", "arm64"):
        return "aarch64"
    raise PrerequisiteNotMetError(
        f"Unsupported Docker architecture for wal-g: {arch!r} "
        "(supported: x86_64/amd64, aarch64/arm64)."
    )


def _download(url: str, dest: str, timeout: int = 120) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "oduflow"})
    with (
        urllib.request.urlopen(request, timeout=timeout) as resp,
        open(dest, "wb") as out,
    ):
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def _fetch_expected_sha256(url: str) -> str:
    """Download the upstream ``.sha256`` sidecar and extract the hex digest."""
    with tempfile.NamedTemporaryFile() as tmp:
        _download(url, tmp.name, timeout=30)
        text = open(tmp.name).read()
    m = re.search(r"\b[0-9a-fA-F]{64}\b", text)
    if not m:
        raise PrerequisiteNotMetError(f"Malformed sha256 file at {url}: {text[:80]!r}")
    return m.group(0).lower()


def ensure_walg(settings: Settings) -> str:
    """Download the pinned wal-g binary if missing; return its host path.

    Idempotent: an existing versioned binary short-circuits. The tarball's
    integrity is verified against the upstream ``.sha256`` sidecar from the
    same release (authenticity rests on TLS to github.com). A ``wal-g``
    symlink beside the versioned binary is what the container's
    archive_command resolves, so version bumps are atomic.
    """
    version = _walg_version(settings)
    bin_dir = bin_host_dir(settings)
    os.makedirs(bin_dir, exist_ok=True)
    versioned = os.path.join(bin_dir, f"wal-g-{version}")
    link = os.path.join(bin_dir, "wal-g")

    if not os.path.isfile(versioned):
        arch = _docker_arch()
        asset = _ASSETS[arch]
        url = _RELEASE_URL.format(version=version, asset=asset)
        logger.info("Downloading wal-g %s (%s)...", version, arch)
        try:
            expected = _fetch_expected_sha256(url + ".sha256")
            with tempfile.TemporaryDirectory(dir=bin_dir) as tmpdir:
                tarball = os.path.join(tmpdir, asset)
                _download(url, tarball)
                digest = hashlib.sha256()
                with open(tarball, "rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != expected:
                    raise PrerequisiteNotMetError(
                        f"wal-g download checksum mismatch for {asset}: "
                        f"got {digest.hexdigest()}, expected {expected}"
                    )
                with tarfile.open(tarball) as tar:
                    members = [m for m in tar.getmembers() if m.isfile()]
                    if len(members) != 1:
                        raise PrerequisiteNotMetError(
                            f"Unexpected wal-g tarball layout: "
                            f"{[m.name for m in members]!r}"
                        )
                    extracted = os.path.join(tmpdir, "wal-g.bin")
                    src = tar.extractfile(members[0])
                    assert src is not None
                    with open(extracted, "wb") as out:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                os.chmod(extracted, 0o755)
                os.replace(extracted, versioned)
        except PrerequisiteNotMetError:
            raise
        except Exception as exc:
            raise PrerequisiteNotMetError(
                f"Failed to download wal-g {version} from GitHub: {exc}. "
                "Backups stay unavailable until the server can reach "
                "github.com (or place the binary manually at "
                f"{versioned})."
            ) from exc
        logger.info("wal-g %s installed at %s", version, versioned)

    # (Re)point the stable symlink at the pinned version.
    relative_target = os.path.basename(versioned)
    if os.path.islink(link):
        if os.readlink(link) != relative_target:
            os.remove(link)
            os.symlink(relative_target, link)
    elif os.path.exists(link):
        os.remove(link)
        os.symlink(relative_target, link)
    else:
        os.symlink(relative_target, link)
    return versioned


def write_walg_config(settings: Settings) -> str | None:
    """Write (or refresh) walg.json from [backup] settings; return its path.

    Returns None (and removes a stale file) when backups are unconfigured.
    Regenerated on every startup, so credential rotation in oduflow.toml
    propagates without touching the container.
    """
    conf_dir = conf_host_dir(settings)
    os.makedirs(conf_dir, exist_ok=True)
    path = os.path.join(conf_dir, "walg.json")
    backup = settings.backup
    if backup is None:
        if os.path.isfile(path):
            os.remove(path)
            logger.info("Removed stale walg.json (backups unconfigured)")
        return None

    config: dict[str, str] = {
        "WALG_S3_PREFIX": f"s3://{backup.bucket}/{backup.prefix}/walg",
        "AWS_ACCESS_KEY_ID": backup.access_key,
        "AWS_SECRET_ACCESS_KEY": backup.secret_key,
        "WALG_COMPRESSION_METHOD": "lz4",
        # Local unix socket inside the PG container (trust auth in the
        # official image); superuser needed for backup-push.
        "PGHOST": "/var/run/postgresql",
        "PGUSER": settings.db_user,
        "PGDATABASE": "postgres",
    }
    if backup.region:
        config["AWS_REGION"] = backup.region
    if backup.endpoint:
        config["AWS_ENDPOINT"] = backup.endpoint
        config["AWS_S3_FORCE_PATH_STYLE"] = "true"

    fd, tmp_path = tempfile.mkstemp(prefix="walg.", suffix=".tmp", dir=conf_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


# postgres uid:gid inside a given PG image, cached: walg.json must be readable
# by the container's postgres user, but the server writes it as a different uid.
_pg_uid_gid_cache: dict[str, tuple[int, int]] = {}


def _postgres_uid_gid(client: Any, image: str) -> tuple[int, int]:
    """Detect the ``postgres`` account's uid:gid inside *image* (cached).

    The official postgres image's default user is root, so a bare ``id`` would
    report root; ask specifically for the ``postgres`` account. Falls back to
    ``999:999`` (the official image's value) if detection fails.
    """
    if image in _pg_uid_gid_cache:
        return _pg_uid_gid_cache[image]
    uid_gid = (999, 999)
    try:
        raw = (
            client.containers.run(image, ["id", "postgres"], entrypoint="", remove=True)
            .decode()
            .strip()
        )
        uid_m = re.search(r"uid=(\d+)", raw)
        gid_m = re.search(r"gid=(\d+)", raw)
        if uid_m and gid_m:
            uid_gid = (int(uid_m.group(1)), int(gid_m.group(1)))
    except Exception as exc:
        logger.warning(
            "Could not detect postgres uid:gid from %s (%s); assuming 999:999",
            image,
            exc,
        )
    _pg_uid_gid_cache[image] = uid_gid
    return uid_gid


def apply_walg_config_ownership(settings: Settings, client: Any) -> None:
    """Make walg.json readable by the production PG container's postgres user.

    walg.json is written ``0600`` owned by the Oduflow server's uid, but wal-g
    reads it from inside the PG container as the ``postgres`` user — the
    ``archive_command`` and every ``backup-push``/``backup-list``/``delete``
    run as ``postgres``. A ``0600`` file owned by a different uid is unreadable
    there, so WAL archiving and base backups fail silently. chown only the file
    (not the directory, so the server keeps rewriting it and postgres can still
    traverse the ``0755`` dir) to the postgres uid:gid, keeping mode ``0600`` so
    the S3 credentials stay owner-only. Best-effort: failure is logged, not
    raised (the health check surfaces a broken archiver).
    """
    path = os.path.join(conf_host_dir(settings), "walg.json")
    if not os.path.isfile(path):
        return
    image = settings.prod_postgres_image or settings.postgres_image
    uid, gid = _postgres_uid_gid(client, image)
    try:
        os.chown(path, uid, gid)
        return
    except PermissionError:
        pass  # non-root host (e.g. macOS): chown in a throwaway container
    except OSError as exc:
        logger.warning("Could not chown walg.json to postgres: %s", exc)
        return
    try:
        client.containers.run(
            image,
            f"chown {uid}:{gid} /mnt/walg/walg.json",
            entrypoint="",
            user="root",
            remove=True,
            volumes={conf_host_dir(settings): {"bind": "/mnt/walg", "mode": "rw"}},
        )
    except Exception as exc:
        logger.warning("Could not chown walg.json to postgres: %s", exc)


def archive_command(enabled: bool) -> str:
    if not enabled:
        return "/bin/true"
    return f"{WALG_BIN} --config {WALG_CONF} wal-push %p"


def apply_archive_command(client: Any, settings: Settings, enabled: bool) -> None:
    """Point archive_command at wal-g (or the no-op) via ALTER SYSTEM + reload.

    Runs on every startup (idempotent); ALTER SYSTEM persists to
    postgresql.auto.conf which overrides the generated conf, so backups can
    be enabled/disabled without recreating or restarting the container.
    """
    from oduflow.docker_ops.system_ops import _exec_sql

    command = archive_command(enabled).replace("'", "''")
    _exec_sql(
        client,
        settings,
        f"ALTER SYSTEM SET archive_command TO '{command}';",
        container_name=settings.prod_db_container,
    )
    _exec_sql(
        client,
        settings,
        "SELECT pg_reload_conf();",
        container_name=settings.prod_db_container,
    )
    logger.info("Production archive_command -> %s", "wal-g" if enabled else "/bin/true")


def _exec_walg(client: Any, settings: Settings, args: list[str]) -> str:
    """Run wal-g inside the production PG container as the postgres OS user."""
    container = client.containers.get(settings.prod_db_container)
    cmd = [WALG_BIN, "--config", WALG_CONF, *args]
    exit_code, output = container.exec_run(cmd, user="postgres")
    text: str = (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else str(output)
    )
    if exit_code != 0:
        raise ExternalCommandError("wal-g " + " ".join(args), exit_code, text[-2000:])
    return text


def backup_push(client: Any, settings: Settings) -> str:
    """Take a base backup of the production cluster into S3."""
    return _exec_walg(client, settings, ["backup-push", _PGDATA])


def backup_list(client: Any, settings: Settings) -> list[dict[str, Any]]:
    """Parsed ``wal-g backup-list --detail --json`` (empty list when none)."""
    try:
        text = _exec_walg(client, settings, ["backup-list", "--detail", "--json"])
    except ExternalCommandError as exc:
        # No backups yet is not an error condition for status reporting.
        if "No backups found" in exc.output:
            return []
        raise
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def delete_retain(client: Any, settings: Settings, keep_full: int) -> str:
    """Drop base backups beyond the newest ``keep_full`` (and unneeded WAL)."""
    return _exec_walg(
        client, settings, ["delete", "retain", "FULL", str(keep_full), "--confirm"]
    )


def archiver_status(client: Any, settings: Settings) -> dict[str, Any]:
    """WAL archiver health from pg_stat_archiver (inside the prod cluster)."""
    from oduflow.docker_ops.system_ops import _exec_sql

    row = _exec_sql(
        client,
        settings,
        "SELECT archived_count, coalesce(last_archived_wal, ''), "
        "coalesce(last_archived_time::text, ''), failed_count, "
        "coalesce(last_failed_time::text, '') FROM pg_stat_archiver;",
        container_name=settings.prod_db_container,
    )
    parts = row.split("|")
    if len(parts) != 5:
        return {}
    return {
        "archived_count": int(parts[0] or 0),
        "last_archived_wal": parts[1],
        "last_archived_time": parts[2],
        "failed_count": int(parts[3] or 0),
        "last_failed_time": parts[4],
    }


# ---------------------------------------------------------------------------
# Cluster PITR (disaster recovery)
# ---------------------------------------------------------------------------

_PITR_TIMEOUT = 1800  # WAL replay can take a while


def _parse_walg_time(value: str) -> _dt.datetime | None:
    """Best-effort parse of a WAL-G / PostgreSQL timestamp to aware UTC."""
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    # Pad a bare "+00"/"-05" offset to "+00:00" (fromisoformat needs minutes).
    if re.search(r"[+-]\d{2}$", s):
        s = s + ":00"
    try:
        parsed = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def _select_pitr_base_backup(client: Any, settings: Settings, target_time: str) -> str:
    """Choose which base backup to fetch for a PITR to ``target_time``.

    Returns a specific ``backup_name`` — the newest base whose consistency point
    is at or before the target — or ``"LATEST"`` (no target given).
    ``backup-fetch LATEST`` is wrong for a target in the past: if the newest
    base is more recent than the target, PostgreSQL FATALs with "requested
    recovery stop point is before consistent recovery point" and the cluster is
    left down. So with a target, every problem that prevents picking a correct
    base — unparseable target, no readable backup inventory, target older than
    every base — raises PrerequisiteNotMetError, and the caller fails BEFORE
    the destructive restore steps.
    """
    if not target_time:
        return "LATEST"
    target = _parse_walg_time(target_time)
    if target is None:
        raise PrerequisiteNotMetError(
            f"Could not parse PITR target_time {target_time!r}. Use an ISO "
            'timestamp like "2026-07-10 12:00:00+00", or omit target_time to '
            "replay the whole archive."
        )
    best_name = ""
    best_ts: _dt.datetime | None = None
    parsed_any = False
    for entry in backup_list(client, settings):
        name = entry.get("backup_name") or entry.get("BackupName") or ""
        ts = _parse_walg_time(
            entry.get("finish_time")
            or entry.get("time")
            or entry.get("start_time")
            or ""
        )
        if not name or ts is None:
            continue
        parsed_any = True
        if ts <= target and (best_ts is None or ts > best_ts):
            best_name, best_ts = name, ts
    if best_name:
        return best_name
    if not parsed_any:
        # No usable names/times in the wal-g inventory. With an explicit
        # target we cannot know whether LATEST satisfies it, and finding out
        # only after PGDATA is displaced leaves the cluster down.
        raise PrerequisiteNotMetError(
            "Could not read base-backup names/times from wal-g backup-list, "
            "so no base backup can be matched to target_time "
            f"{target_time!r}. Verify backups exist (production_backup_status)"
            ", or omit target_time to fetch the latest base backup."
        )
    raise PrerequisiteNotMetError(
        f"No base backup exists at or before {target_time}: the earliest base "
        "backup is newer than the requested recovery target. Choose a later "
        "target_time, or restore from an older base backup."
    )


def pitr_restore_cluster(
    settings: Settings,
    *,
    target_time: str = "",
) -> dict[str, Any]:
    """Restore the WHOLE production cluster from WAL-G (base backup + WAL).

    This is the disaster-recovery path — it affects EVERY production
    database in the cluster at once (per-production restores use snapshots
    instead, see backup_ops). Also the "resurrect production elsewhere"
    path: a fresh Oduflow install pointed at the same [backup] section can
    rebuild the cluster from S3.

    Flow (the caller holds a cluster-wide lock and has stopped production
    Odoo containers):

    1. stop + remove the production PG container (its data volume stays);
    2. in a helper container on that volume: move the current PGDATA
       contents aside (``.pitr-old-{ts}/`` — nothing is destroyed),
       ``wal-g backup-fetch`` the base backup, write ``recovery.signal``
       and the restore_command (+ recovery_target_time when given);
    3. recreate the PG container and wait for recovery to finish
       (pg_is_in_recovery() = false — with no recovery target PostgreSQL
       replays the whole archive and promotes).

    The displaced data dir is left in the volume for manual cleanup.
    """
    from oduflow.docker_ops.client import get_client
    from oduflow.docker_ops.system_ops import (
        _ensure_prod_pg_container,
        _exec_sql,
        _wait_pg_ready,
    )

    if settings.backup is None:
        raise PrerequisiteNotMetError(
            "Cluster PITR requires a configured [backup] section."
        )
    client = get_client()
    ensure_walg(settings)
    write_walg_config(settings)
    apply_walg_config_ownership(settings, client)

    # Pick the base backup BEFORE any destructive step. For a target in the past
    # this selects the newest base at or before it; if none exists we raise here,
    # while the container and PGDATA are still intact.
    fetch_target = _select_pitr_base_backup(client, settings, target_time)

    image = settings.prod_postgres_image or settings.postgres_image
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    old_dir = f".pitr-old-{stamp}"

    # 1. Stop + remove the PG container (volume persists).
    try:
        container = client.containers.get(settings.prod_db_container)
        container.stop()
        container.remove()
    except Exception:
        pass

    # 2. Helper container: displace PGDATA, fetch, write recovery config.
    recovery_conf = (
        f"restore_command = '{WALG_BIN} --config {WALG_CONF} wal-fetch %f %p'\n"
    )
    if target_time:
        safe_time = target_time.replace("'", "")
        recovery_conf += (
            f"recovery_target_time = '{safe_time}'\n"
            "recovery_target_action = 'promote'\n"
        )
    script = (
        "set -e\n"
        f"mkdir -p /vol/{old_dir}\n"
        f"find /vol -mindepth 1 -maxdepth 1 ! -name '.pitr-old-*' "
        f"-exec mv {{}} /vol/{old_dir}/ \\;\n"
        f"{WALG_BIN} --config {WALG_CONF} backup-fetch /vol {fetch_target}\n"
        f'printf "%b" "{recovery_conf}" >> /vol/postgresql.auto.conf\n'
        "touch /vol/recovery.signal\n"
        "chown -R postgres:postgres /vol\n"
        "chmod 700 /vol\n"
    )
    helper = client.containers.run(
        image,
        name=f"{settings.prod_db_container}-pitr-{stamp}",
        detach=True,
        entrypoint=["sh", "-c", script],
        volumes={
            settings.prod_db_volume: {"bind": "/vol", "mode": "rw"},
            bin_host_dir(settings): {"bind": BIN_MOUNT, "mode": "ro"},
            conf_host_dir(settings): {"bind": CONF_MOUNT, "mode": "ro"},
        },
    )
    try:
        status = helper.wait(timeout=_PITR_TIMEOUT)
        exit_code = (
            status.get("StatusCode", -1) if isinstance(status, dict) else int(status)
        )
        logs = helper.logs().decode("utf-8", errors="replace")
    finally:
        try:
            helper.remove(force=True)
        except Exception:
            pass
    if exit_code != 0:
        raise ExternalCommandError("wal-g backup-fetch (PITR)", exit_code, logs[-3000:])

    # 3. Recreate the container; PostgreSQL replays WAL and promotes.
    system_labels = {settings.managed_label: "true", settings.system_label: "true"}
    _ensure_prod_pg_container(client, settings, system_labels)
    _wait_pg_ready(
        client,
        settings,
        timeout=_PITR_TIMEOUT,
        container_name=settings.prod_db_container,
    )
    deadline = time.monotonic() + _PITR_TIMEOUT
    while time.monotonic() < deadline:
        try:
            in_recovery = _exec_sql(
                client,
                settings,
                "SELECT pg_is_in_recovery();",
                container_name=settings.prod_db_container,
            )
            if in_recovery.strip() in ("f", "false"):
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        raise ExternalCommandError(
            "PITR recovery",
            1,
            f"Cluster did not finish recovery within {_PITR_TIMEOUT}s — check "
            f"docker logs {settings.prod_db_container}",
        )

    # Re-apply the archive command (postgresql.auto.conf was rebuilt).
    apply_archive_command(client, settings, enabled=True)
    logger.info("Cluster PITR complete (old data kept in volume as %s)", old_dir)
    return {
        "status": "restored",
        "target_time": target_time or "latest",
        "base_backup": fetch_target,
        "displaced_data_dir": old_dir,
    }
