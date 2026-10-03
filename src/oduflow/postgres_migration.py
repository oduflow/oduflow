"""One-time PG15 replacement for installations without retained workloads.

Environment deletion is an operator prerequisite. Only template databases are
restored; their dumps, filestores and metadata remain on disk. The journal is
written before destruction and before creating PG16, so a retry cannot reset
the replacement cluster after a failed template restore. The old production
cluster's WAL-G archive is deleted with it: its base backups cannot restore
into PG16.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
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
from oduflow.errors import PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import get_template_db_name
from oduflow.settings import DEFAULT_POSTGRES_IMAGE, Settings

logger = logging.getLogger("oduflow")
_JOURNAL = "postgres16-migration.json"
# Docker Hub's implicit forms of one repository name.
_DOCKER_HUB_PREFIXES = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")
# Only images without custom extensions: the journal pins [database].image, so a
# cluster needing extensions could not switch images after its reset.
_RESETTABLE_REPOSITORIES = {"postgres", "oduist/oduflow-postgres"}
# postgres:18+ images keep PGDATA under a major-specific path and refuse the
# /var/lib/postgresql/data volume Oduflow mounts.
_SUPPORTED_MAJORS = range(16, 18)
_HEARTBEAT_SECONDS = 60.0


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


def validate_configuration(settings: Settings) -> None:
    """Reject unusable image selections before any startup migration changes state.

    Runs on every start: an existing PG16+ cluster must keep its major version,
    because Oduflow never upgrades a cluster across majors.
    """
    image = settings.postgres_image
    client = get_client()
    major = _image_major(image)
    if major is None:
        major = _binary_major(client, _local_image(client, image).id)
    toml = settings.toml_path or "oduflow.toml"
    if major not in _SUPPORTED_MAJORS:
        raise PrerequisiteNotMetError(
            "This Oduflow version supports PostgreSQL 16 and 17, but "
            f"[database].image '{image}' is PostgreSQL {major}. Update it in "
            f"{toml} manually, e.g. to '{DEFAULT_POSTGRES_IMAGE}'. "
            "The configuration has not been changed."
        )
    for name in (settings.shared_db_container, settings.prod_db_container):
        container = _container(client, name)
        if container is None:
            continue
        try:
            data_major = _data_major(container)
        except docker.errors.NotFound:
            continue  # not initialized yet
        if data_major >= 16 and data_major != major:
            raise PrerequisiteNotMetError(
                f"{name} holds PostgreSQL {data_major} data, but [database].image "
                f"'{image}' is PostgreSQL {major}. Set [database].image in {toml} "
                f"to a PostgreSQL {data_major} image; Oduflow does not upgrade "
                "clusters across major versions."
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
    stream, _ = container.get_archive("/var/lib/postgresql/data/PG_VERSION")
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
            f"The PG15 replacement needs a PostgreSQL 16 or 17 image, got {image}: "
            f"PostgreSQL {major}."
        )
    return str(target.id), major


def _templates(settings: Settings) -> list[dict[str, str]]:
    """Templates whose dump can recreate their database on the new cluster.

    Names may be nested (``customer/prod``); metadata is optional, as elsewhere.
    A directory without a dump is skipped: if the old cluster still holds its
    database, the database check refuses the reset instead of losing it.
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


def _check_workloads(
    client: DockerClient, settings: Settings, clusters: set[str]
) -> None:
    if not clusters:
        return
    blockers = []
    containers = client.containers.list(
        all=True, filters={"label": f"{settings.managed_label}=true"}
    )
    for container in containers:
        if not container.name.startswith(settings.prefix):
            continue
        labels = container.labels
        is_prod = labels.get("oduflow.prod_name") or (
            "-prod-" in container.name and container.name != settings.prod_db_container
        )
        if (
            "dev" in clusters and labels.get(settings.branch_label) and not is_prod
        ) or ("prod" in clusters and is_prod):
            blockers.append(f"container {container.name}")
    for team in settings.teams.values():
        if "prod" in clusters:
            blockers.extend(
                f"production {team.team_id}/{name}"
                for name in production_registry.list_productions(team)
            )
        for name in service_database_credentials.list_names(team):
            record = service_database_credentials.load(team, name)
            if (record.get("cluster") or "dev") in clusters:
                blockers.append(f"service database {team.team_id}/{name}")
    if blockers:
        raise PrerequisiteNotMetError(
            "Delete these before upgrading PostgreSQL, stopped ones included: "
            f"{', '.join(blockers)}. Nothing has been changed. Delete them with "
            "the previous Oduflow release (still running if `oduflow upgrade` "
            "refused before a restart), then upgrade again."
        )


def _check_databases(
    client: DockerClient,
    settings: Settings,
    cluster: dict[str, Any],
    templates: list[dict[str, str]],
) -> None:
    container = client.containers.get(cluster["container"])
    started = container.status != "running"
    if started:
        container.start()
    try:
        _check_database_contents(client, settings, cluster, templates, container)
    finally:
        # A deliberately stopped cluster (disabled production, latched WAL
        # guard) stays stopped, whether or not the reset goes ahead.
        if started:
            container.stop()


def _check_database_contents(
    client: DockerClient,
    settings: Settings,
    cluster: dict[str, Any],
    templates: list[dict[str, str]],
    container: Any,
) -> None:
    system_ops._wait_pg_ready(client, settings, container_name=container.name)
    names = system_ops._exec_sql(
        client,
        settings,
        "SELECT datname FROM pg_database WHERE datname NOT IN ('template0', 'template1');",
        container_name=container.name,
    ).splitlines()
    allowed = {"postgres", settings.db_user}
    if cluster["kind"] == "dev":
        allowed.update(get_template_db_name(t["name"], t["team"]) for t in templates)
    unexpected = set(names) - allowed
    if unexpected:
        raise PrerequisiteNotMetError(
            f"Cluster {container.name} still contains databases that will not be "
            f"restored: {', '.join(sorted(unexpected))}. Delete environments and "
            "resolve remaining databases before upgrading PostgreSQL."
        )
    # The automatically created database must be empty too; operators sometimes
    # use it for services without registering a managed service database.
    for name in {"postgres", settings.db_user}.intersection(names):
        count = system_ops._exec_sql(
            client,
            settings,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f');",
            db=name,
            container_name=container.name,
        )
        if int(count):
            raise PrerequisiteNotMetError(
                f"Database '{name}' in {container.name} contains user objects; "
                "the PostgreSQL reset has been refused."
            )


def _plan(
    client: DockerClient, settings: Settings, *, preflight: bool = False
) -> dict[str, Any]:
    """Check the reset prerequisites and describe it; nothing is changed.

    ``preflight`` stays read-only for ``oduflow upgrade``: stopped clusters are
    not started (startup checks them) and no replacement image is pulled.
    """
    clusters = []
    stopped = set()
    image = settings.postgres_image
    for kind, name, volume_name in (
        (
            "dev",
            settings.shared_db_container,
            settings.shared_db_volume,
        ),
        (
            "prod",
            settings.prod_db_container,
            settings.prod_db_volume,
        ),
    ):
        container = _container(client, name)
        volume = _volume(client, volume_name)
        if container is None:
            if volume is not None:
                raise PrerequisiteNotMetError(
                    f"Volume '{volume_name}' exists without '{name}'. Restore the "
                    "old container to inspect its databases before the PG16 migration."
                )
            continue
        major = _data_major(container)
        if major >= 16:
            continue
        source_image = container.attrs["Config"]["Image"]
        if major != 15 or _repository(source_image) not in _RESETTABLE_REPOSITORIES:
            raise PrerequisiteNotMetError(
                f"Cannot automatically reset {name} ({source_image}, PostgreSQL {major}). "
                "Only the standard PG15 clusters support this migration."
            )
        mounts = container.attrs.get("Mounts", [])
        if volume is None or not any(
            m.get("Destination") == "/var/lib/postgresql/data"
            and m.get("Type") == "volume"
            and m.get("Name") == volume_name
            for m in mounts
        ):
            raise PrerequisiteNotMetError(
                f"Unexpected PGDATA mount on {name}; refusing reset."
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
        if container.status != "running":
            stopped.add(name)
        clusters.append(
            {
                "kind": kind,
                "container": name,
                "volume": volume_name,
                "old_container_id": container.id,
                "old_image": source_image,
                "old_volume_created": volume.attrs["CreatedAt"],
                "image": image,
                "phase": "remove_old",
            }
        )
    kinds = {c["kind"] for c in clusters}
    _check_workloads(client, settings, kinds)
    templates = _templates(settings) if "dev" in kinds else []
    for cluster in clusters:
        if not (preflight and cluster["container"] in stopped):
            _check_databases(client, settings, cluster, templates)
    # Pin one image for both clusters before either old cluster is removed.
    if clusters and not preflight:
        image_id, major = _pull_target(client, image)
        for cluster in clusters:
            cluster["image_id"] = image_id
            cluster["major"] = major
    return {"clusters": clusters, "templates": templates, "restored": []}


def _remove_old(
    client: DockerClient, settings: Settings, cluster: dict[str, Any]
) -> None:
    container = _container(client, cluster["container"])
    if container is not None:
        if container.id != cluster["old_container_id"]:
            raise PrerequisiteNotMetError(
                "PostgreSQL container changed during migration; refusing reset."
            )
        container.stop()
    # Before anything irreversible happens locally: if S3 refuses, the stopped
    # PG15 cluster is still intact for a retry or a rollback.
    if cluster["kind"] == "prod" and settings.backup is not None:
        _delete_walg_archive(settings)
    if container is not None:
        container.remove()
    volume = _volume(client, cluster["volume"])
    if volume is not None:
        if volume.attrs["CreatedAt"] != cluster["old_volume_created"]:
            raise PrerequisiteNotMetError(
                "PostgreSQL volume changed during migration; refusing reset."
            )
        volume.remove()
    if cluster["kind"] == "dev":
        # Use the container's root user to remove postgres-owned physical files.
        # Leave team directories (and their XFS project IDs) in place.
        tablespaces = system_ops._pg_tablespaces_host_dir(settings)
        if os.path.isdir(tablespaces):
            client.containers.run(
                cluster["image_id"],
                [
                    "-c",
                    "find /tablespaces -mindepth 2 -maxdepth 2 -type d "
                    "-name 'PG_15_*' -exec rm -rf -- {} +",
                ],
                entrypoint="sh",
                user="root",
                remove=True,
                network_disabled=True,
                volumes={tablespaces: {"bind": "/tablespaces", "mode": "rw"}},
            )


def _delete_walg_archive(settings: Settings) -> None:
    """Delete the stopped PG15 cluster's WAL-G archive before PG16 archives there.

    The new cluster restarts at timeline 1, so its segments would overwrite old
    ones piecemeal and PITR could still pick a PG15 base backup. Operators take
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
    client: DockerClient, settings: Settings, cluster: dict[str, Any]
) -> None:
    labels = {settings.managed_label: "true", settings.system_label: "true"}
    pinned = replace(settings, postgres_image=cluster["image_id"])
    if _volume(client, cluster["volume"]) is None:
        client.volumes.create(cluster["volume"], labels=labels)
    if cluster["kind"] == "dev":
        system_ops._ensure_pg_container(client, pinned, labels)
    else:
        system_ops._ensure_prod_pg_container(client, pinned, labels)
    system_ops._wait_pg_ready(client, settings, container_name=cluster["container"])
    expected = cluster.get("major", 16)
    if _data_major(client.containers.get(cluster["container"])) != expected:
        raise PrerequisiteNotMetError(
            f"Replacement PostgreSQL cluster is not version {expected}."
        )


def preflight(settings: Settings) -> None:
    """Refuse an upgrade whose first start would refuse the PG15 replacement.

    ``oduflow upgrade`` runs this with the new code before ``self-update``
    restarts the service, so the old server keeps serving while the operator
    deletes the listed workloads.
    """
    if os.path.isfile(os.path.join(settings.base_data_dir, _JOURNAL)):
        return  # already under way; the next start resumes it
    validate_configuration(settings)
    _plan(get_client(), settings, preflight=True)


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


def migrate(settings: Settings) -> None:
    validate_configuration(settings)
    client = get_client()
    path = os.path.join(settings.base_data_dir, _JOURNAL)
    if os.path.isfile(path):
        state = json.loads(Path(path).read_text())
    else:
        state = _plan(client, settings)
        atomic_write_private_json(path, state)
    _check_workloads(
        client,
        settings,
        {c["kind"] for c in state["clusters"] if c["phase"] == "remove_old"},
    )
    for cluster in state["clusters"]:
        if settings.postgres_image != cluster["image"]:
            raise PrerequisiteNotMetError(
                f"Keep the migration image '{cluster['image']}' configured until "
                "the PostgreSQL migration finishes."
            )
    for cluster in state["clusters"]:
        if cluster["phase"] == "remove_old":
            logger.info("Removing old PostgreSQL cluster %s", cluster["container"])
            _remove_old(client, settings, cluster)
            cluster["phase"] = "create_new"
            atomic_write_private_json(path, state)
        if cluster["phase"] == "create_new":
            _create_new(client, settings, cluster)
            cluster["new_container_id"] = client.containers.get(cluster["container"]).id
            cluster["phase"] = "restore"
            atomic_write_private_json(path, state)
        container = _container(client, cluster["container"])
        if container is None or container.id != cluster["new_container_id"]:
            raise PrerequisiteNotMetError(
                f"Replacement cluster {cluster['container']} changed or disappeared. "
                "Restore it before resuming template restoration."
            )
        if container.status != "running":
            container.start()
        system_ops._wait_pg_ready(client, settings, container_name=container.name)
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
            atomic_write_private_json(path, state)
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
        atomic_write_private_json(path, state)
    for image in {c["old_image"] for c in state["clusters"]}:
        try:
            client.images.remove(image)
        except docker.errors.ImageNotFound:
            pass
        except docker.errors.APIError:
            logger.info("Keeping old PostgreSQL image %s (still referenced)", image)
    logger.info(
        "PostgreSQL migration complete (%d clusters, %d templates, %d skipped)",
        len(state["clusters"]),
        len(state["restored"]),
        len(skipped),
    )
