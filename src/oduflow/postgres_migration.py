"""PostgreSQL major upgrades of the managed clusters, on operator request.

A cluster is upgraded when an explicit ``[database].image`` names a newer major
than its data; a new default image never upgrades anything. The development and
production clusters share that image and are upgraded together.

Development environments are not carried over and must be deleted first.
Service databases and production databases (soft-deleted ones included) are
dumped with every role and restored into the new cluster; template databases
are restored from their dumps on disk. An old cluster is removed only after all
dumps have been taken and verified. With backups configured, the production
cluster's WAL-G archive is deleted too: its base backups cannot restore into a
newer major.

A journal resumes the work after a failure without resetting a new cluster.
Until an old cluster is removed, a restart plans the upgrade again from scratch,
so setting the image back cancels it.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import logging
import os
import re
import shutil
import tarfile
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import docker
from docker import DockerClient
from oduflow import production_registry, s3_client, service_database_credentials
from oduflow.docker_ops import system_ops
from oduflow.docker_ops.client import get_client, run_for_output
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import (
    PROD_ENV_PREFIX,
    get_db_name,
    get_template_db_name,
    prod_env_name,
)
from oduflow.settings import DEFAULT_POSTGRES_IMAGE, Settings

logger = logging.getLogger("oduflow")
_JOURNAL = "postgres-upgrade.json"
# v1.85.0's one-time PG15 replacement; taken over when it was left unfinished.
_LEGACY_JOURNAL = "postgres16-migration.json"
_DUMP_DIR = "pg-upgrade"
_HELPER = "pg-upgrade-helper"
_PGDATA = "/var/lib/postgresql/data"
# Docker Hub's implicit forms of one repository name.
_DOCKER_HUB_PREFIXES = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")
# postgres:18+ images keep PGDATA under a major-specific path and refuse the
# /var/lib/postgresql/data volume Oduflow mounts.
_SUPPORTED_MAJORS = range(15, 18)
_HEARTBEAT_SECONDS = 60.0
_POLL_SECONDS = 2.0

# Every role except the bootstrap superuser, which a new cluster creates itself.
# Password hashes are copied, so credentials files stay valid.
_ROLES_SQL = r"""
SELECT format(
  'DO $r$BEGIN CREATE ROLE %1$I; EXCEPTION WHEN duplicate_object THEN NULL; END$r$; '
  'ALTER ROLE %1$I WITH %2$s %3$s %4$s %5$s %6$s %7$s %8$s '
  'CONNECTION LIMIT %9$s PASSWORD %10$L%11$s;',
  rolname,
  CASE WHEN rolsuper THEN 'SUPERUSER' ELSE 'NOSUPERUSER' END,
  CASE WHEN rolinherit THEN 'INHERIT' ELSE 'NOINHERIT' END,
  CASE WHEN rolcreaterole THEN 'CREATEROLE' ELSE 'NOCREATEROLE' END,
  CASE WHEN rolcreatedb THEN 'CREATEDB' ELSE 'NOCREATEDB' END,
  CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END,
  CASE WHEN rolreplication THEN 'REPLICATION' ELSE 'NOREPLICATION' END,
  CASE WHEN rolbypassrls THEN 'BYPASSRLS' ELSE 'NOBYPASSRLS' END,
  rolconnlimit,
  rolpassword,
  CASE WHEN rolvaliduntil IS NULL THEN ''
       ELSE format(' VALID UNTIL %L', rolvaliduntil) END)
FROM pg_authid
WHERE rolname !~ '^pg_' AND rolname <> current_user
ORDER BY rolname;
"""
_MEMBERSHIPS_SQL = r"""
SELECT format('GRANT %I TO %I%s;', r.rolname, m.rolname,
              CASE WHEN a.admin_option THEN ' WITH ADMIN OPTION' ELSE '' END)
FROM pg_auth_members a
JOIN pg_roles r ON r.oid = a.roleid
JOIN pg_roles m ON m.oid = a.member
WHERE m.rolname !~ '^pg_' AND m.rolname <> current_user
ORDER BY 1;
"""


def _repository(image: str) -> str:
    """Repository of an image reference without Docker Hub's implicit prefixes."""
    name = image.split("@", 1)[0]
    # A colon after the last slash starts the tag; one before it is a port.
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    for prefix in _DOCKER_HUB_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name.removeprefix("library/")


def _image_major(image: str) -> int | None:
    """Major from an official postgres tag; None when only the binary can tell.

    Other repositories put their own version first (pgvector:0.8.0-pg16).
    """
    if _repository(image) != "postgres":
        return None
    tag = image.split("@", 1)[0].rsplit("/", 1)[-1].partition(":")[2]
    match = re.match(r"(\d+)(?:[.\-]|$)", tag)
    return int(match[1]) if match else None


def _local_image(client: DockerClient, image: str) -> Any:
    """The image from the local store, pulled only when it is missing."""
    try:
        return client.images.get(image)
    except docker.errors.ImageNotFound:
        _download_image(client, image)
        return client.images.get(image)


def _configured_major(client: DockerClient, settings: Settings) -> int:
    image = settings.postgres_image
    major = _image_major(image)
    if major is None:
        major = _binary_major(client, _local_image(client, image).id)
    return major


def _clusters(settings: Settings) -> tuple[tuple[str, str, str], ...]:
    return (
        ("dev", settings.shared_db_container, settings.shared_db_volume),
        ("prod", settings.prod_db_container, settings.prod_db_volume),
    )


def validate_configuration(settings: Settings) -> None:
    """Reject image selections no start can honour, before anything changes.

    Runs on every start and in ``oduflow upgrade``. A newer major than a
    cluster's data requests an upgrade only when ``[database].image`` is set
    explicitly; Oduflow never downgrades a cluster.
    """
    image = settings.postgres_image
    client = get_client()
    major = _configured_major(client, settings)
    toml = settings.toml_path or "oduflow.toml"
    if major not in _SUPPORTED_MAJORS:
        raise PrerequisiteNotMetError(
            "This Oduflow version supports PostgreSQL 15 to 17, but "
            f"[database].image '{image}' is PostgreSQL {major}. Update it in "
            f"{toml}. The configuration has not been changed."
        )
    for _, name, _ in _clusters(settings):
        container = _container(client, name)
        if container is None:
            continue
        try:
            data_major = _data_major(container)
        except docker.errors.NotFound:
            continue  # not initialized yet
        if data_major > major:
            raise PrerequisiteNotMetError(
                f"{name} holds PostgreSQL {data_major} data, but [database].image "
                f"'{image}' is PostgreSQL {major}. Set [database].image in {toml} "
                f"to a PostgreSQL {data_major} image; clusters cannot be downgraded."
            )
        if data_major < major and not settings.postgres_image_explicit:
            raise PrerequisiteNotMetError(
                f"{name} holds PostgreSQL {data_major} data and {toml} does not set "
                "[database].image, whose default is now "
                f"'{DEFAULT_POSTGRES_IMAGE}'. Set it to 'postgres:{data_major}' to "
                f"keep the cluster, or to '{image}' to upgrade it (see Upgrading "
                "PostgreSQL in the installation guide)."
            )


def _container(client: DockerClient, name: str) -> Any:
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        return None


def _volume(client: DockerClient, name: str) -> Any:
    try:
        return client.volumes.get(name)
    except docker.errors.NotFound:
        return None


def _data_major(container: Any) -> int:
    # Read the data format even when the server is stopped; image tags may lie.
    stream, _ = container.get_archive(f"{_PGDATA}/PG_VERSION")
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
        member = archive.extractfile("PG_VERSION")
        if member is None:
            raise PrerequisiteNotMetError(f"Cannot read PG_VERSION in {container.name}")
        return int(member.read().decode().strip())


def _download_image(client: DockerClient, image: str) -> None:
    logger.info("Downloading PostgreSQL image %s", image)
    last_progress = 0.0
    for event in client.api.pull(image, stream=True, decode=True):
        if event.get("error"):
            raise PrerequisiteNotMetError(f"Cannot pull {image}: {event['error']}")
        now = time.monotonic()
        if event.get("status") and (
            "progressDetail" not in event or now - last_progress >= 15
        ):
            logger.info("PostgreSQL image: %s", event["status"])
            last_progress = now


def _binary_major(client: DockerClient, image: str) -> int:
    output = run_for_output(
        client,
        image,
        ["--version"],
        entrypoint="postgres",
        network_disabled=True,
    ).decode()
    match = re.search(r"PostgreSQL\) (\d+)\.", output)
    if not match:
        raise PrerequisiteNotMetError(f"Cannot determine PostgreSQL version in {image}")
    return int(match[1])


def _pull_target(client: DockerClient, image: str) -> tuple[str, int]:
    target = _local_image(client, image)
    # The binary is authoritative: tags can lie.
    major = _binary_major(client, target.id)
    if major not in _SUPPORTED_MAJORS:
        raise PrerequisiteNotMetError(
            f"A PostgreSQL upgrade needs a PostgreSQL 15 to 17 image, got {image}: "
            f"PostgreSQL {major}."
        )
    return str(target.id), major


def _templates(settings: Settings) -> list[dict[str, str]]:
    """Templates whose dump can recreate their database on the new cluster.

    Names may be nested (``customer/prod``); metadata is optional, as elsewhere.
    A directory without a dump is skipped: if the old cluster still holds its
    database, the database check refuses the upgrade instead of losing it.
    """
    result = []
    for team in settings.teams.values():
        root = Path(team.data_dir) / "templates"
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(
                d for d in dirs if d != "filestore" and not d.startswith(".")
            )
            if Path(directory) == root:
                continue
            name = Path(directory).relative_to(root).as_posix()
            try:
                dump = Path(team.get_template_sql_path(name))
            except ValueError:
                continue  # an invalid template name is never a template
            if not dump.is_file() or not dump.stat().st_size:
                if "metadata.json" in files:
                    logger.warning(
                        "Template '%s' (team %s) has no dump; its database "
                        "cannot be restored on the new PostgreSQL cluster",
                        name,
                        team.team_id,
                    )
                continue
            with dump.open("rb") as handle:
                handle.read(1)
            result.append({"team": team.team_id, "name": name})
    return result


def _is_production(container: Any) -> bool:
    return container.labels.get("oduflow.prod") == "true" or bool(
        container.labels.get("oduflow.prod_name")
    )


def _keep_hint(old_major: int) -> str:
    return (
        "Nothing has been changed; to keep the current cluster instead, set "
        f"[database].image back to 'postgres:{old_major}' (or another "
        f"PostgreSQL {old_major} image)."
    )


def _check_environments(
    client: DockerClient, settings: Settings, old_major: int
) -> None:
    containers = client.containers.list(
        all=True, filters={"label": f"{settings.managed_label}=true"}
    )
    envs = sorted(
        c.name
        for c in containers
        if c.name.startswith(settings.prefix)
        and c.labels.get(settings.branch_label)
        and not _is_production(c)
    )
    if envs:
        raise PrerequisiteNotMetError(
            "Delete these environments before upgrading PostgreSQL, stopped ones "
            f"included: {', '.join(envs)}. Their databases are not carried over. "
            + _keep_hint(old_major)
        )


def _carried_databases(settings: Settings, kind: str) -> set[str]:
    """Databases of one cluster that are dumped and restored."""
    names: set[str] = set()
    for team in settings.teams.values():
        for name in service_database_credentials.list_names(team):
            record = service_database_credentials.load(team, name)
            if (record.get("cluster") or "dev") == kind:
                names.add(record["database"])
        if kind != "prod":
            continue
        # Registered productions and those deleted with their data kept.
        productions = set(production_registry.list_productions(team))
        if os.path.isdir(team.workspaces_dir):
            productions.update(
                entry[len(PROD_ENV_PREFIX) :]
                for entry in os.listdir(team.workspaces_dir)
                if entry.startswith(PROD_ENV_PREFIX)
            )
        names.update(get_db_name(prod_env_name(p), team.team_id) for p in productions)
    return names


def _inventory(
    client: DockerClient,
    settings: Settings,
    cluster: dict[str, Any],
    templates: list[dict[str, str]],
) -> tuple[list[str], set[str], int]:
    """Databases to dump, the extensions they use and their total size.

    Refuses when the cluster holds anything that would be lost.
    """
    container = cluster["container"]
    present = system_ops._exec_sql(
        client,
        settings,
        "SELECT datname FROM pg_database WHERE datname NOT IN ('template0', 'template1');",
        container_name=container,
    ).splitlines()
    carried = _carried_databases(settings, cluster["kind"])
    bootstrap = {"postgres", settings.db_user}
    restored_from_disk = (
        {get_template_db_name(t["name"], t["team"]) for t in templates}
        if cluster["kind"] == "dev"
        else set()
    )
    unexpected = set(present) - carried - bootstrap - restored_from_disk
    if unexpected:
        raise PrerequisiteNotMetError(
            f"Cluster {container} contains databases the upgrade would lose: "
            f"{', '.join(sorted(unexpected))}. Remove leftovers of deleted "
            "environments with `oduflow cleanup --force` and drop or move anything "
            "else. " + _keep_hint(cluster["old_major"])
        )
    # The automatically created databases must be empty: operators sometimes
    # use them for services without registering a managed service database.
    for name in bootstrap.intersection(present):
        count = system_ops._exec_sql(
            client,
            settings,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f');",
            db=name,
            container_name=container,
        )
        if int(count):
            raise PrerequisiteNotMetError(
                f"Database '{name}' in {container} contains user objects that the "
                "upgrade would lose. Move them into a service database first."
            )
    databases = sorted(carried.intersection(present))
    extensions: set[str] = set()
    # Templates are restored from disk, but need the same extensions.
    for name in sorted(set(databases) | restored_from_disk.intersection(present)):
        extensions.update(
            system_ops._exec_sql(
                client,
                settings,
                "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql';",
                db=name,
                container_name=container,
            ).splitlines()
        )
    size = 0
    for name in databases:
        size += int(
            system_ops._exec_sql(
                client,
                settings,
                f"SELECT pg_database_size('{name}');",
                container_name=container,
            )
        )
    return databases, extensions, size


def _check_extensions(
    client: DockerClient, image: str, image_id: str, needed: set[str]
) -> None:
    """Refuse before removing anything when the new image lacks an extension."""
    if not needed:
        return
    output = run_for_output(
        client,
        image_id,
        ["-c", "find / -xdev -path '*/extension/*.control' 2>/dev/null; true"],
        entrypoint="sh",
        network_disabled=True,
    ).decode()
    available = {os.path.basename(p).removesuffix(".control") for p in output.split()}
    if "plpgsql" not in available:
        raise PrerequisiteNotMetError(
            f"Cannot list the extensions available in {image}. Nothing has been changed."
        )
    missing = needed - available
    if missing:
        raise PrerequisiteNotMetError(
            f"[database].image '{image}' lacks extensions that the databases use: "
            f"{', '.join(sorted(missing))}. Choose an image that provides them. "
            "Nothing has been changed."
        )


def _check_disk_space(settings: Settings, size: int) -> None:
    path = _dump_path(settings)
    usage = shutil.disk_usage(settings.base_data_dir)
    reserve = system_ops._reserve_bytes(usage.total)
    if usage.free - size < reserve:
        raise PrerequisiteNotMetError(
            f"Dumping the databases to {path} may need up to {size / 1024**3:.1f} GiB, "
            f"but {usage.free / 1024**3:.1f} GiB is free there and "
            f"{reserve / 1024**3:.1f} GiB must stay free. Nothing has been changed."
        )


@contextlib.contextmanager
def _started(
    client: DockerClient, settings: Settings, cluster: dict[str, Any]
) -> Iterator[Any]:
    """The old cluster, running meanwhile; a stopped one is stopped again."""
    container = client.containers.get(cluster["container"])
    if container.id != cluster["old_container_id"]:
        raise PrerequisiteNotMetError(
            f"{cluster['container']} changed during the PostgreSQL upgrade."
        )
    started = container.status != "running"
    if started:
        container.start()
    try:
        system_ops._wait_pg_ready(client, settings, container_name=container.name)
        yield container
    finally:
        # A deliberately stopped cluster (disabled production) stays stopped,
        # whether or not the upgrade goes ahead.
        if started:
            container.stop()


def _plan(
    client: DockerClient, settings: Settings, *, preflight: bool = False
) -> dict[str, Any] | None:
    """Check the upgrade prerequisites and describe it; nothing is changed.

    Returns None when no cluster is older than ``[database].image``.
    ``preflight`` stays read-only for ``oduflow upgrade``: stopped clusters are
    not started (startup checks them) and no image is pulled.
    """
    major = _configured_major(client, settings)
    clusters = []
    for kind, name, volume_name in _clusters(settings):
        container = _container(client, name)
        if container is None:
            continue
        try:
            data_major = _data_major(container)
        except docker.errors.NotFound:
            continue  # not initialized yet
        if data_major >= major:
            continue
        volume = _volume(client, volume_name)
        mounts = container.attrs.get("Mounts", [])
        if volume is None or not any(
            m.get("Destination") == _PGDATA
            and m.get("Type") == "volume"
            and m.get("Name") == volume_name
            for m in mounts
        ):
            raise PrerequisiteNotMetError(
                f"Unexpected PGDATA mount on {name}; refusing the PostgreSQL upgrade."
            )
        if container.labels.get(settings.managed_label) != "true":
            raise PrerequisiteNotMetError(
                f"{name} is not an Oduflow-managed container."
            )
        if kind == "dev":
            expected = os.path.realpath(system_ops._pg_tablespaces_host_dir(settings))
            if any(
                m.get("Destination") == "/tablespaces"
                and os.path.realpath(m.get("Source", "")) != expected
                for m in mounts
            ):
                raise PrerequisiteNotMetError(
                    f"The tablespace mount of {name} does not match the configured "
                    "data directory; refusing to delete PostgreSQL files."
                )
        clusters.append(
            {
                "kind": kind,
                "container": name,
                "volume": volume_name,
                "old_container_id": container.id,
                "old_image": container.attrs["Config"]["Image"],
                "old_volume_created": volume.attrs["CreatedAt"],
                "old_major": data_major,
                "was_running": container.status == "running",
                "databases": [],
                "restored_databases": [],
                "roles_restored": False,
                "phase": "dump",
            }
        )
    if not clusters:
        return None
    kinds = {c["kind"] for c in clusters}
    if "dev" in kinds:
        _check_environments(
            client,
            settings,
            next(c["old_major"] for c in clusters if c["kind"] == "dev"),
        )
    if "prod" in kinds:
        from oduflow import wal_monitor

        if wal_monitor.state(settings).get("latched"):
            raise PrerequisiteNotMetError(
                "Production disk protection is active. Recover WAL archiving and "
                "release the cluster protection before upgrading PostgreSQL."
            )
    templates = _templates(settings) if "dev" in kinds else []
    extensions: set[str] = set()
    size = 0
    for cluster in clusters:
        if preflight and not cluster["was_running"]:
            continue
        with _started(client, settings, cluster):
            databases, used, nbytes = _inventory(client, settings, cluster, templates)
        cluster["databases"] = databases
        extensions |= used
        size += nbytes
    state: dict[str, Any] = {
        "version": 2,
        "image": settings.postgres_image,
        "stopped": [],
        "clusters": clusters,
        "templates": templates,
        "restored": [],
        "skipped": [],
    }
    if preflight:
        return state
    # Pin one image for both clusters before either old cluster is removed.
    image_id, target_major = _pull_target(client, settings.postgres_image)
    if any(c["old_major"] >= target_major for c in clusters):
        raise PrerequisiteNotMetError(
            f"{settings.postgres_image} is PostgreSQL {target_major}, not newer "
            "than the clusters it would upgrade."
        )
    _check_extensions(client, settings.postgres_image, image_id, extensions)
    _check_disk_space(settings, size)
    state["image_id"] = image_id
    state["major"] = target_major
    return state


def _journal_path(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, _JOURNAL)


def _dump_path(settings: Settings, kind: str = "") -> str:
    return os.path.join(settings.base_data_dir, _DUMP_DIR, kind)


def _save(settings: Settings, state: dict[str, Any]) -> None:
    atomic_write_private_json(_journal_path(settings), state)


@contextlib.contextmanager
def _lock(settings: Settings) -> Iterator[None]:
    """Serialize upgrades across processes (``serve`` and ``stack apply``)."""
    fd = os.open(_journal_path(settings) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _run_helper(
    client: DockerClient,
    settings: Settings,
    image_id: str,
    cluster_container: str,
    command: list[str],
    directory: str,
    label: str,
    *,
    progress_path: str = "",
) -> None:
    """Run a client tool of the new image against a cluster, to completion.

    The helper shares the cluster's network namespace and connects to
    127.0.0.1, which the image's default pg_hba trusts, and reads or writes
    dumps through a bind mount, which keeps an archive seekable for a parallel
    restore. Progress is logged only after a successful container poll, so the
    startup watchdog still notices a wedged Docker daemon.
    """
    name = f"{settings.prefix}{_HELPER}"
    stale = _container(client, name)
    if stale is not None:
        stale.remove(force=True)
    helper = client.containers.run(
        image_id,
        command[1:],
        entrypoint=command[0],
        name=name,
        detach=True,
        network_mode=f"container:{cluster_container}",
        environment={"PGPASSWORD": settings.db_password},
        volumes={directory: {"bind": "/dump", "mode": "rw"}},
        log_config={"type": "json-file"},
    )
    started = last_beat = time.monotonic()
    try:
        while True:
            helper.reload()
            if helper.status not in ("created", "running", "restarting"):
                break
            now = time.monotonic()
            if now - last_beat >= _HEARTBEAT_SECONDS:
                detail = ""
                if progress_path and os.path.isfile(progress_path):
                    written = os.path.getsize(progress_path) / 1024**2
                    detail = f", {written:.0f} MiB written"
                logger.info("%s for %.0fs%s", label, now - started, detail)
                last_beat = now
            time.sleep(_POLL_SECONDS)
        code = int(helper.attrs.get("State", {}).get("ExitCode", 1))
        if code:
            output = helper.logs().decode("utf-8", errors="replace")
            raise ExternalCommandError(command[0], code, output[-2000:])
    finally:
        with contextlib.suppress(docker.errors.APIError):
            helper.remove(force=True)


def _write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


def _stop_applications(
    client: DockerClient, settings: Settings, state: dict[str, Any]
) -> None:
    """Stop services and production Odoo, so nothing changes after its dump."""
    prod = any(c["kind"] == "prod" for c in state["clusters"])
    running = client.containers.list(
        filters={"label": f"{settings.managed_label}=true"}
    )
    for container in sorted(running, key=lambda c: c.name):
        labels = container.labels
        # Another Oduflow instance on the same daemon has its own prefix.
        if not container.name.startswith(settings.prefix):
            continue
        if not (
            labels.get("oduflow.service")
            or (
                prod
                and labels.get("oduflow.prod") == "true"
                and container.name != settings.prod_db_container
            )
        ):
            continue
        # Recorded first: a crash right after the stop must still restart it.
        state["stopped"].append(container.name)
        _save(settings, state)
        logger.info("Stopping %s for the PostgreSQL upgrade", container.name)
        container.stop()


def _start(client: DockerClient, name: str) -> None:
    try:
        client.containers.get(name).start()
        logger.info("Started %s again", name)
    except docker.errors.NotFound:
        logger.warning("%s was stopped for the PostgreSQL upgrade and is gone", name)
    except docker.errors.APIError:
        logger.exception("Could not start %s again", name)


def _cancel(client: DockerClient, settings: Settings, state: dict[str, Any]) -> None:
    """Drop an upgrade that has not removed anything yet."""
    for name in state["stopped"]:
        _start(client, name)
    shutil.rmtree(_dump_path(settings), ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        os.remove(_journal_path(settings))


def _dump_cluster(
    client: DockerClient,
    settings: Settings,
    state: dict[str, Any],
    cluster: dict[str, Any],
) -> None:
    directory = _dump_path(settings, cluster["kind"])
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with _started(client, settings, cluster) as container:
        roles = [
            system_ops._exec_sql(client, settings, sql, container_name=container.name)
            for sql in (_ROLES_SQL, _MEMBERSHIPS_SQL)
        ]
        _write_private(os.path.join(directory, "roles.sql"), "\n".join(roles) + "\n")
        for name in cluster["databases"]:
            dump = f"{name}.pgdump"
            _run_helper(
                client,
                settings,
                state["image_id"],
                container.name,
                ["pg_dump", "-h", "127.0.0.1", "-U", settings.db_user, "-Fc"]
                + ["-f", f"/dump/{dump}", name],
                directory,
                f"Dumping database {name}",
                progress_path=os.path.join(directory, dump),
            )
            # Read the archive back before anything is removed.
            _run_helper(
                client,
                settings,
                state["image_id"],
                container.name,
                ["pg_restore", "--list", "-f", "/dev/null", f"/dump/{dump}"],
                directory,
                f"Verifying the dump of {name}",
            )


def _remove_old(
    client: DockerClient,
    settings: Settings,
    state: dict[str, Any],
    cluster: dict[str, Any],
) -> None:
    container = _container(client, cluster["container"])
    if container is not None:
        if container.id != cluster["old_container_id"]:
            raise PrerequisiteNotMetError(
                "PostgreSQL container changed during the upgrade; refusing to remove it."
            )
        container.stop()
    # Before anything irreversible happens locally: if S3 refuses, the stopped
    # old cluster is still intact for a retry or a rollback.
    if cluster["kind"] == "prod" and settings.backup is not None:
        _delete_walg_archive(settings)
    if container is not None:
        container.remove()
    volume = _volume(client, cluster["volume"])
    if volume is not None:
        if volume.attrs["CreatedAt"] != cluster["old_volume_created"]:
            raise PrerequisiteNotMetError(
                "PostgreSQL volume changed during the upgrade; refusing to remove it."
            )
        volume.remove()
    if cluster["kind"] == "dev":
        # Use the container's root user to remove postgres-owned physical files.
        # Leave team directories (and their XFS project IDs) in place.
        tablespaces = system_ops._pg_tablespaces_host_dir(settings)
        if os.path.isdir(tablespaces):
            run_for_output(
                client,
                state["image_id"],
                [
                    "-c",
                    "find /tablespaces -mindepth 2 -maxdepth 2 -type d "
                    f"-name 'PG_{int(cluster['old_major'])}_*' -exec rm -rf -- {{}} +",
                ],
                entrypoint="sh",
                user="root",
                network_disabled=True,
                volumes={tablespaces: {"bind": "/tablespaces", "mode": "rw"}},
            )


def _delete_walg_archive(settings: Settings) -> None:
    """Delete the stopped old cluster's WAL-G archive before the new one archives.

    The new cluster restarts at timeline 1, so its segments would overwrite old
    ones piecemeal and PITR could still pick an old base backup. Operators take
    their own backup before upgrading; this archive has no further use.
    """
    backup = settings.backup
    assert backup is not None
    client = s3_client.make_client(backup)
    # Trailing slash: WAL-G's prefix only, never a sibling such as walg-old/.
    prefix = f"{backup.prefix}/walg/"
    deleted = 0
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=backup.bucket, Prefix=prefix
    ):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if not objects:
            continue
        response = client.delete_objects(
            Bucket=backup.bucket, Delete={"Objects": objects, "Quiet": True}
        )
        if response.get("Errors"):
            error = response["Errors"][0]
            raise PrerequisiteNotMetError(
                f"Cannot delete the old WAL-G archive object {error.get('Key')}: "
                f"{error.get('Code')} {error.get('Message')}"
            )
        deleted += len(objects)
    logger.info(
        "Deleted %d objects of the old WAL-G archive at s3://%s/%s",
        deleted,
        backup.bucket,
        prefix,
    )


def _create_new(
    client: DockerClient,
    settings: Settings,
    state: dict[str, Any],
    cluster: dict[str, Any],
) -> None:
    labels = {settings.managed_label: "true", settings.system_label: "true"}
    pinned = replace(settings, postgres_image=state["image_id"])
    if _volume(client, cluster["volume"]) is None:
        client.volumes.create(cluster["volume"], labels=labels)
    if cluster["kind"] == "dev":
        system_ops._ensure_pg_container(client, pinned, labels)
    else:
        system_ops._ensure_prod_pg_container(client, pinned, labels)
    system_ops._wait_pg_ready(
        client, settings, timeout=120, container_name=cluster["container"]
    )
    if _data_major(client.containers.get(cluster["container"])) != state["major"]:
        raise PrerequisiteNotMetError(
            f"Replacement PostgreSQL cluster is not version {state['major']}."
        )
    if cluster["kind"] == "prod":
        from oduflow import walg

        # The production configuration keeps WAL until an archive command is
        # set; a large restore would fill the disk before archiving is verified.
        walg.apply_archive_command(client, settings, enabled=False)


def _restore_cluster(
    client: DockerClient,
    settings: Settings,
    state: dict[str, Any],
    cluster: dict[str, Any],
) -> None:
    container = cluster["container"]
    directory = _dump_path(settings, cluster["kind"])
    if cluster["kind"] == "dev":
        # A dump records its database's tablespace.
        for team in settings.teams.values():
            system_ops.ensure_team_tablespace(client, settings, team)
    if not cluster["roles_restored"]:
        roles = Path(directory, "roles.sql").read_text().strip()
        if roles:
            system_ops._exec_sql(client, settings, roles, container_name=container)
        cluster["roles_restored"] = True
        _save(settings, state)
    for name in cluster["databases"]:
        if name in cluster["restored_databases"]:
            continue
        logger.info("Restoring database %s into the new PostgreSQL cluster", name)
        # A failed attempt may have left part of it behind.
        system_ops._exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);',
            container_name=container,
        )
        dump = f"{name}.pgdump"
        try:
            _run_helper(
                client,
                settings,
                state["image_id"],
                container,
                ["pg_restore", "-h", "127.0.0.1", "-U", settings.db_user]
                + ["-d", "postgres", "--create", "--exit-on-error"]
                + ["-j", str(system_ops._pg_restore_jobs()), f"/dump/{dump}"],
                directory,
                f"Restoring database {name}",
            )
        except ExternalCommandError as exc:
            raise PrerequisiteNotMetError(
                f"Database {name} could not be restored into the new PostgreSQL "
                f"cluster: {exc.output}. Its dump is kept at "
                f"{os.path.join(directory, dump)}. Fix the cause and restart "
                "Oduflow to retry."
            ) from exc
        cluster["restored_databases"].append(name)
        _save(settings, state)


@contextlib.contextmanager
def _restore_heartbeat(settings: Settings, label: str, tpl_db: str) -> Iterator[None]:
    """Log restore activity so the startup watchdog sees a long restore progress.

    Each beat is a query through Docker, never a bare timer: if the daemon
    wedges, the probe blocks too, logging stops and the watchdog still fires.
    """
    done = threading.Event()
    started = time.monotonic()

    def run() -> None:
        client = get_client()
        while not done.wait(_HEARTBEAT_SECONDS):
            try:
                activity = system_ops._exec_sql(
                    client,
                    settings,
                    "SELECT left(query, 120) FROM pg_stat_activity "
                    f"WHERE datname = '{tpl_db}' AND state = 'active' LIMIT 1;",
                )
            except Exception:
                logger.debug("Template restore probe failed", exc_info=True)
                continue
            logger.info(
                "Restoring template %s for %.0fs: %s",
                label,
                time.monotonic() - started,
                activity or "copying or converting the dump",
            )

    thread = threading.Thread(
        target=run, name="oduflow-template-restore-heartbeat", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        done.set()


def _restore_templates(settings: Settings, state: dict[str, Any]) -> None:
    skipped = state.setdefault("skipped", [])
    for template in state["templates"]:
        key = [template["team"], template["name"]]
        if key in state["restored"] or key in skipped:
            continue
        team = settings.teams.get(template["team"])
        # Resolve the dump again: an operator may have fixed it under another
        # name, or deleted the template (or its team) to skip a broken restore.
        dump = team.get_template_sql_path(template["name"]) if team else ""
        if team is None or not os.path.isfile(dump):
            logger.warning(
                "Template %s/%s was removed before its restore on the new "
                "PostgreSQL cluster; skipping it",
                *key,
            )
            skipped.append(key)
            _save(settings, state)
            continue
        logger.info("Restoring template %s/%s on the new PostgreSQL cluster", *key)
        tpl_db = get_template_db_name(template["name"], template["team"])
        try:
            with _restore_heartbeat(settings, "/".join(key), tpl_db):
                system_ops.reload_template(
                    settings,
                    team,
                    template["name"],
                    dump_path=dump,
                    persist_dump=False,
                    strict=True,
                )
        except Exception as exc:
            raise PrerequisiteNotMetError(
                f"Template {key[0]}/{key[1]} could not be restored on the new "
                f"PostgreSQL cluster: {str(exc)[-2000:]}. Fix its dump and restart "
                "Oduflow to retry, or skip it by deleting it: "
                f"`oduflow delete-template {key[1]} --team {key[0]}`."
            ) from exc
        state["restored"].append(key)
        _save(settings, state)


def _finish(client: DockerClient, settings: Settings, state: dict[str, Any]) -> None:
    clusters = {c["kind"]: c for c in state["clusters"]}
    # init_system attaches the clusters to the team networks and reconciles
    # pg_hba only after this; the applications started below need both now.
    for team in settings.teams.values():
        system_ops.ensure_team_network(client, settings, team)
    if "dev" in clusters:
        system_ops._reconcile_pg_hba(
            client, settings, container_name=settings.shared_db_container
        )
    prod = clusters.get("prod")
    if prod is not None:
        if prod["was_running"] and settings.prod_enabled:
            # Production starts only once its archiving is verified.
            system_ops.ensure_prod_infra(client, settings, force=True)
        elif not prod["was_running"]:
            client.containers.get(settings.prod_db_container).stop()
        if settings.backup is not None:
            from oduflow import backup_scheduler

            # The new archive has no base backup to recover from yet.
            backup_scheduler.request_base_backup(settings)
    for name in state["stopped"]:
        _start(client, name)
    for image in {c["old_image"] for c in state["clusters"]} - {
        settings.postgres_image
    }:
        try:
            client.images.remove(image)
        except docker.errors.ImageNotFound:
            pass
        except docker.errors.APIError:
            logger.info("Keeping old PostgreSQL image %s (still referenced)", image)
    shutil.rmtree(_dump_path(settings), ignore_errors=True)
    os.remove(_journal_path(settings))
    logger.info(
        "PostgreSQL upgrade to %s complete (%d clusters, %d databases, "
        "%d templates, %d skipped)",
        settings.postgres_image,
        len(state["clusters"]),
        sum(len(c["databases"]) for c in state["clusters"]),
        len(state["restored"]),
        len(state["skipped"]),
    )


def _run(client: DockerClient, settings: Settings, state: dict[str, Any]) -> None:
    clusters = state["clusters"]
    if any(c["phase"] == "dump" for c in clusters):
        try:
            _stop_applications(client, settings, state)
            for cluster in clusters:
                _dump_cluster(client, settings, state, cluster)
        except Exception:
            # Nothing has been removed: leave everything as it was.
            _cancel(client, settings, state)
            raise
        for cluster in clusters:
            cluster["phase"] = "remove_old"
        _save(settings, state)
    for cluster in clusters:
        if cluster["phase"] == "remove_old":
            logger.info("Removing old PostgreSQL cluster %s", cluster["container"])
            _remove_old(client, settings, state, cluster)
            cluster["phase"] = "create_new"
            _save(settings, state)
        if cluster["phase"] == "create_new":
            _create_new(client, settings, state, cluster)
            cluster["new_container_id"] = client.containers.get(cluster["container"]).id
            cluster["phase"] = "restore"
            _save(settings, state)
        container = _container(client, cluster["container"])
        if container is None or container.id != cluster["new_container_id"]:
            raise PrerequisiteNotMetError(
                f"Replacement cluster {cluster['container']} changed or disappeared. "
                "Restore it before resuming the PostgreSQL upgrade."
            )
        if container.status != "running":
            container.start()
        system_ops._wait_pg_ready(client, settings, container_name=container.name)
        if cluster["phase"] == "restore":
            _restore_cluster(client, settings, state, cluster)
            cluster["phase"] = "done"
            _save(settings, state)
    _restore_templates(settings, state)
    _finish(client, settings, state)


def _legacy_finished(legacy: dict[str, Any]) -> bool:
    done = {
        tuple(key) for key in legacy.get("restored", []) + legacy.get("skipped", [])
    }
    return all(c["phase"] == "restore" for c in legacy.get("clusters", [])) and all(
        (t["team"], t["name"]) in done for t in legacy.get("templates", [])
    )


def _legacy_pending(settings: Settings) -> bool:
    path = os.path.join(settings.base_data_dir, _LEGACY_JOURNAL)
    return os.path.isfile(path) and not _legacy_finished(
        json.loads(Path(path).read_text())
    )


def _adopt_legacy(client: DockerClient, settings: Settings, path: str) -> None:
    """Take over a v1.85.0 replacement that removed an old cluster.

    Its plan only allowed clusters without databases to keep, so templates are
    all that is left to restore. A finished replacement, or one that removed
    nothing yet, needs no journal.
    """
    legacy = json.loads(Path(path).read_text())
    clusters = legacy.get("clusters", [])

    def intact(cluster: dict[str, Any]) -> bool:
        container = _container(client, cluster["container"])
        volume = _volume(client, cluster["volume"])
        return (
            cluster["phase"] == "remove_old"
            and container is not None
            and container.id == cluster["old_container_id"]
            and volume is not None
            and volume.attrs["CreatedAt"] == cluster["old_volume_created"]
        )

    if not _legacy_finished(legacy) and not all(intact(c) for c in clusters):
        _save(
            settings,
            {
                "version": 2,
                "image": clusters[0]["image"],
                "image_id": clusters[0]["image_id"],
                "major": clusters[0].get("major", 16),
                "stopped": [],
                "clusters": [
                    {
                        **c,
                        "old_major": 15,
                        "was_running": True,
                        "databases": [],
                        "restored_databases": [],
                        "roles_restored": True,
                    }
                    for c in clusters
                ],
                "templates": legacy.get("templates", []),
                "restored": legacy.get("restored", []),
                "skipped": legacy.get("skipped", []),
            },
        )
    os.remove(path)


def _resume(client: DockerClient, settings: Settings) -> dict[str, Any] | None:
    path = _journal_path(settings)
    legacy = os.path.join(settings.base_data_dir, _LEGACY_JOURNAL)
    if not os.path.isfile(path) and os.path.isfile(legacy):
        _adopt_legacy(client, settings, legacy)
    if not os.path.isfile(path):
        return None
    state: dict[str, Any] = json.loads(Path(path).read_text())
    if all(c["phase"] == "dump" for c in state["clusters"]):
        # Nothing has been removed: plan again from the current state, so a
        # changed configuration, including a reverted image, takes effect.
        _cancel(client, settings, state)
        return None
    if settings.postgres_image != state["image"]:
        raise PrerequisiteNotMetError(
            f"Keep [database].image '{state['image']}' configured until the "
            "PostgreSQL upgrade finishes: an old cluster has already been removed."
        )
    return state


def preflight(settings: Settings) -> None:
    """Refuse an ``oduflow upgrade`` whose restart could not start or upgrade.

    ``oduflow upgrade`` runs this with the new code before ``self-update``
    restarts the service, so the old server keeps serving while the operator
    resolves what is listed.
    """
    if os.path.isfile(_journal_path(settings)) or _legacy_pending(settings):
        return  # under way; the next start resumes it
    validate_configuration(settings)
    _plan(get_client(), settings, preflight=True)


def upgrade(settings: Settings) -> None:
    """Upgrade every cluster older than ``[database].image``, or resume doing so.

    Runs on every start after :func:`validate_configuration`; returns at once
    when no upgrade is requested or under way.
    """
    os.makedirs(settings.base_data_dir, exist_ok=True)
    with _lock(settings):
        client = get_client()
        state = _resume(client, settings)
        if state is None:
            state = _plan(client, settings)
            if state is None:
                return
            _save(settings, state)
            logger.info(
                "Upgrading PostgreSQL clusters %s to %s",
                ", ".join(c["container"] for c in state["clusters"]),
                settings.postgres_image,
            )
        _run(client, settings, state)
