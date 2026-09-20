"""Startup data migrations.

Odoo-style upgrade mechanism for the on-disk/Docker state Oduflow manages:
the code ships an ordered, append-only registry of one-shot migration steps,
and the data directory records which steps were already applied
(``migrations.json`` under ``base_data_dir``). On server start Oduflow diffs
the registry against the recorded state and applies only the missing steps,
oldest first.

A fresh install (no state file and no ``team_*`` data yet) is stamped as
fully applied without running anything — the same way Odoo skips migration
scripts when a module is installed from scratch rather than upgraded.

Adding a migration:

- append a :class:`Migration` to ``MIGRATIONS`` with the next sequence number
  in its id (``"0001-team-scoped-container-names"``);
- never reorder, rename, or remove existing entries — recorded ids are what
  keeps reruns idempotent on existing installs;
- make the step itself idempotent where possible: state is persisted after
  each successful step, so a step that crashed halfway is retried on the next
  start.
"""

import fcntl
import glob
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_STATE_FILENAME = "migrations.json"


@dataclass(frozen=True)
class Migration:
    """One irreversible upgrade step, identified by a stable sequence id."""

    id: str
    description: str
    apply: Callable[[Settings], None]


def _migrate_team_scoped_names(settings: Settings) -> None:
    """Rename managed containers to the team-scoped naming scheme.

    ``oduflow-{env}-{type}`` → ``oduflow-{team}-{env}-{type}`` and
    ``oduflow-svc-{name}`` → ``oduflow-{team}-svc-{name}``. Docker rename
    keeps volumes, network attachment, and restart policy; Traefik routing is
    label-based (labels are immutable but reference no container names), so
    nothing else changes. Idempotent: a container whose name no longer
    matches the old scheme is skipped, so a partially-applied run resumes
    cleanly.
    """
    import docker
    from oduflow.docker_ops.client import get_client
    from oduflow.naming import get_resource_name, get_service_container_name

    client = get_client()
    for team_id in settings.teams:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    f"{settings.team_label}={team_id}",
                ]
            },
        )
        for container in containers:
            name = container.name
            if not name.startswith(settings.prefix):
                continue
            svc_name = container.labels.get("oduflow.service")
            branch = container.labels.get(settings.branch_label)
            if svc_name:
                if not name.startswith(f"{settings.prefix}svc-"):
                    continue  # already team-scoped
                new_name = get_service_container_name(
                    svc_name, settings.prefix, team_id
                )
            elif branch:
                old_prefix = f"{settings.prefix}{branch.replace('/', '-')}-"
                if not name.startswith(old_prefix):
                    continue  # already team-scoped
                resource_type = name[len(old_prefix) :]
                new_name = get_resource_name(
                    branch, resource_type, settings.prefix, team_id
                )
            else:
                continue
            if name == new_name:
                continue
            try:
                container.rename(new_name)
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"Failed to rename container {name} -> {new_name}: {exc}"
                ) from exc
            logger.info("Renamed container %s -> %s", name, new_name)


def _migrate_team_pg_tablespaces(settings: Settings) -> None:
    """Move every team's databases into a per-team PostgreSQL tablespace.

    Tablespace files live under ``base_data_dir/pg_tablespaces/team_{id}`` on
    the host, so a filesystem project quota can cover a team's databases
    together with its data dir. Steps:

    1. If the PG container lacks the ``/tablespaces`` mount, recreate it once
       (the data volume persists; seconds of downtime at startup).
    2. Ensure each team's tablespace exists.
    3. ``ALTER DATABASE ... SET TABLESPACE`` for every team database that is
       not already there, blocking reconnects during the move
       (``ALLOW_CONNECTIONS false`` + terminate). Odoo containers reconnect
       on their own afterwards. Time is proportional to database size.

    Idempotent: already-moved databases are skipped, so a partial run
    resumes where it stopped.
    """
    import docker
    from oduflow.docker_ops import system_ops
    from oduflow.docker_ops.client import get_client
    from oduflow.naming import get_tablespace_name

    client = get_client()
    try:
        pg = client.containers.get(settings.shared_db_container)
    except docker.errors.NotFound:
        # No PG container: nothing to move. Fresh infrastructure is created
        # with the mount, and databases are placed on creation.
        return

    has_mount = any(
        m.get("Destination") == system_ops._PG_TABLESPACES_MOUNT
        for m in pg.attrs.get("Mounts", [])
    )
    if not has_mount:
        logger.info(
            "Recreating %s with the tablespaces mount (data volume persists)",
            settings.shared_db_container,
        )
        pg.stop()
        pg.remove()
        system_labels = {
            settings.managed_label: "true",
            settings.system_label: "true",
        }
        system_ops._ensure_pg_container(client, settings, system_labels)
    system_ops._wait_pg_ready(client, settings)

    rows = system_ops._exec_sql(
        client,
        settings,
        "SELECT d.datname, COALESCE(t.spcname, '') FROM pg_database d "
        "LEFT JOIN pg_tablespace t ON d.dattablespace = t.oid "
        "WHERE NOT d.datistemplate;",
    )
    db_tablespaces = {}
    for line in rows.splitlines():
        name, _, spc = line.partition("|")
        if name:
            db_tablespaces[name] = spc

    for team_id, team in settings.teams.items():
        ts_name = get_tablespace_name(team_id)
        system_ops.ensure_team_tablespace(client, settings, team)
        prefixes = (f"oduflow_{team_id}_", f"oduflow_template_{team_id}_")
        for db, current in sorted(db_tablespaces.items()):
            if not db.startswith(prefixes) or current == ts_name:
                continue
            logger.info("Moving database %s to tablespace %s", db, ts_name)
            system_ops._exec_sql(
                client, settings, f'ALTER DATABASE "{db}" WITH ALLOW_CONNECTIONS false;'
            )
            system_ops._exec_sql(
                client,
                settings,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{db}';",
            )
            try:
                system_ops._exec_sql(
                    client,
                    settings,
                    f'ALTER DATABASE "{db}" SET TABLESPACE "{ts_name}";',
                )
            finally:
                system_ops._exec_sql(
                    client,
                    settings,
                    f'ALTER DATABASE "{db}" WITH ALLOW_CONNECTIONS true;',
                )


def _migrate_per_team_networks(settings: Settings) -> None:
    """Move each team's containers to the team's isolated Docker network.

    Creates ``oduflow-{team}-net`` per team (attaching the shared PostgreSQL
    container), connects every managed env/service container to it, and
    disconnects them from the shared network — live, no restarts.
    Established DB connections over the old network break once; Odoo
    reconnects via DNS on the team network. A Traefik container still
    pinning the shared backend network via ``--providers.docker.network`` is
    removed here and recreated by system init right after migrations (the
    ACME volume persists); afterwards each backend's network comes from the
    container itself. Idempotent.
    """
    import docker
    from oduflow.docker_ops import system_ops
    from oduflow.docker_ops.client import get_client

    client = get_client()

    try:
        traefik = client.containers.get(settings.traefik_container)
        cmd = traefik.attrs.get("Config", {}).get("Cmd") or []
        if any(str(arg).startswith("--providers.docker.network=") for arg in cmd):
            logger.info(
                "Removing %s (stale --providers.docker.network); system init "
                "recreates it",
                settings.traefik_container,
            )
            traefik.stop()
            traefik.remove()
    except docker.errors.NotFound:
        pass

    try:
        shared_net = client.networks.get(settings.shared_network)
    except docker.errors.NotFound:
        shared_net = None

    for team in settings.teams.values():
        net_name = system_ops.ensure_team_network(client, settings, team)
        net = client.networks.get(net_name)
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    f"{settings.team_label}={team.team_id}",
                ]
            },
        )
        for container in containers:
            if container.labels.get(settings.system_label) == "true":
                continue  # shared infra stays on the shared network
            if container.attrs.get("HostConfig", {}).get("NetworkMode") == "host":
                continue
            networks = container.attrs.get("NetworkSettings", {}).get("Networks") or {}
            if net_name not in networks:
                net.connect(container)
            if shared_net is not None and settings.shared_network in networks:
                shared_net.disconnect(container)
            logger.info("Moved container %s to %s", container.name, net_name)


def _migrate_env_resource_limits(settings: Settings) -> None:
    """Apply the default memory/pids limits to existing environment containers.

    New containers get limits at creation; ``docker update`` retrofits the
    running fleet without restarts. Best-effort per container: a failure is
    logged and skipped (the next Update/Recreate applies limits anyway).
    """
    from oduflow.docker_ops.client import get_client
    from oduflow.docker_ops.stats import default_env_limits

    client = get_client()
    limits = default_env_limits()
    for team_id in settings.teams:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    f"{settings.team_label}={team_id}",
                ]
            },
        )
        for container in containers:
            # Environments only: services are operator-sized, infra is shared.
            if not container.labels.get(settings.branch_label):
                continue
            try:
                container.update(**limits)
                logger.info("Applied resource limits to %s", container.name)
            except Exception as exc:
                logger.warning(
                    "Could not apply resource limits to %s: %s",
                    container.name,
                    exc,
                )


def _migrate_traefik_yml_config(settings: Settings) -> None:
    """Recreate Traefik if it still mounts the old ``.json`` dynamic config.

    Traefik's file provider only accepts ``.toml``/``.yaml``/``.yml`` and
    rejects ``.json`` ("unsupported file extension"), so the dynamic config is
    now written to ``oduflow.yml``. ``_ensure_traefik`` early-returns on an
    existing container and never rewrites its args, so the stale container is
    removed here (the ACME volume persists → certificates survive) and system
    init recreates it right after migrations with the corrected ``.yml`` path.
    Idempotent: once recreated the arg is ``.yml``, so a rerun finds nothing.
    """
    import docker
    from oduflow.docker_ops.client import get_client

    if settings.routing_mode != "traefik":
        return

    client = get_client()
    old_arg = "--providers.file.filename=/etc/traefik/dynamic/oduflow.json"
    try:
        traefik = client.containers.get(settings.traefik_container)
        cmd = traefik.attrs.get("Config", {}).get("Cmd") or []
        if any(str(arg) == old_arg for arg in cmd):
            logger.info(
                "Removing %s (stale .json dynamic config); system init "
                "recreates it with oduflow.yml",
                settings.traefik_container,
            )
            traefik.stop()
            traefik.remove()
    except docker.errors.NotFound:
        pass


def _chmod_private_best_effort(path: str) -> None:
    """chmod *path* to 0600, logging instead of raising on failure.

    Permission hardening is not worth blocking server startup over (a failing
    migration step aborts every start until fixed): a file the server user
    cannot chmod — e.g. root-owned after a backup restore — is reported for
    the operator to chown, like migration 0004 handles per-container failures.
    """
    try:
        os.chmod(path, 0o600)
        logger.info("Restricted %s to mode 0600", path)
    except OSError as exc:
        logger.warning("Could not restrict %s to mode 0600: %s", path, exc)


def _migrate_service_presets_permissions(settings: Settings) -> None:
    """Restrict each team's ``service_presets.json`` to owner-only access.

    Presets carry service env vars (historically including API keys) but were
    written with the default umask; the credential stores are all 0600. New
    writes already use 0600 (service_presets._save_presets); this brings the
    files that predate that change forward. Idempotent: chmod to the same mode
    is a no-op, and a missing file is skipped.
    """
    for team in settings.teams.values():
        path = os.path.join(team.data_dir, "service_presets.json")
        if os.path.isfile(path):
            _chmod_private_best_effort(path)


def _migrate_template_metadata_permissions(settings: Settings) -> None:
    """Restrict every template ``metadata.json`` to owner-only access.

    Template metadata records the source environment's env vars — the same
    data class as service presets (migration 0006) — but was written with the
    default umask. New writes are 0600; this brings existing files forward.
    The templates tree is walked directly so nested template names
    (``customer/prod``) are covered too.
    """
    for team in settings.teams.values():
        templates_dir = os.path.join(team.data_dir, "templates")
        if not os.path.isdir(templates_dir):
            continue
        for root, _dirs, files in os.walk(templates_dir):
            if "metadata.json" in files:
                _chmod_private_best_effort(os.path.join(root, "metadata.json"))


def _migrate_backfill_service_presets(settings: Settings) -> None:
    """Write a preset for every legacy service that has none.

    Presets are the authoritative service configuration — restore, the
    dashboard's edit dialog and update all read them first — but services
    created before presets existed carry theirs only on the container.
    Backfilling once here gives every reader one source of truth instead of
    each surface re-deriving the config from container inspection. Best effort
    per service, like migration 0004: a service whose image or port cannot be
    reconstructed is logged and skipped (an unrestorable preset is worse than
    none), and a rerun finds the written presets and does nothing.
    """
    from oduflow.docker_ops.client import get_client
    from oduflow.docker_ops.service_ops import backfill_service_preset

    client = get_client()
    for team in settings.teams.values():
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    f"{settings.team_label}={team.team_id}",
                    "oduflow.service",
                ]
            },
        )
        for container in containers:
            name = container.labels.get("oduflow.service")
            if name:
                backfill_service_preset(settings, team, name, container)


# Append-only registry, executed in list order. Ids are recorded in
# migrations.json once applied; reordering or renaming entries would re-run
# or skip steps on existing installs.
MIGRATIONS: list[Migration] = [
    Migration(
        id="0001-team-scoped-container-names",
        description=(
            "Rename managed containers to team-scoped names "
            "(oduflow-{team}-{env}-{type}, oduflow-{team}-svc-{name})"
        ),
        apply=_migrate_team_scoped_names,
    ),
    Migration(
        id="0002-team-pg-tablespaces",
        description=(
            "Move each team's databases into a per-team PostgreSQL tablespace "
            "under base_data_dir/pg_tablespaces/team_{id}"
        ),
        apply=_migrate_team_pg_tablespaces,
    ),
    Migration(
        id="0003-per-team-networks",
        description=(
            "Move env/service containers to isolated per-team networks "
            "(oduflow-{team}-net); shared infra attaches to every team network"
        ),
        apply=_migrate_per_team_networks,
    ),
    Migration(
        id="0004-env-resource-limits",
        description=(
            "Apply default memory/pids limits to existing environment "
            "containers via docker update"
        ),
        apply=_migrate_env_resource_limits,
    ),
    Migration(
        id="0005-traefik-yml-dynamic-config",
        description=(
            "Recreate Traefik if it still mounts the old .json dynamic config "
            "(file provider rejects .json); init recreates it with oduflow.yml"
        ),
        apply=_migrate_traefik_yml_config,
    ),
    Migration(
        id="0006-service-presets-0600",
        description=(
            "Restrict service_presets.json (holds service env vars, often "
            "API keys) to owner-only permissions like the credential stores"
        ),
        apply=_migrate_service_presets_permissions,
    ),
    Migration(
        id="0007-template-metadata-0600",
        description=(
            "Restrict template metadata.json (records source-environment env "
            "vars) to owner-only permissions like the credential stores"
        ),
        apply=_migrate_template_metadata_permissions,
    ),
    Migration(
        id="0008-backfill-service-presets",
        description=(
            "Write a service preset for every legacy service container that "
            "predates presets, so restore/update/edit read one source of truth"
        ),
        apply=_migrate_backfill_service_presets,
    ),
]


@contextmanager
def _state_lock(state_path: str) -> Iterator[None]:
    """Serialize state read-modify-write across processes (and threads:
    flock on two separate fds of the same file contends within one process
    too)."""
    fd = os.open(state_path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_state(state_path: str) -> list[str] | None:
    """Return the applied-id list, or None if no state was ever recorded."""
    if not os.path.isfile(state_path):
        return None
    with open(state_path) as f:
        data = json.load(f)
    applied = data.get("applied", [])
    return [str(mig_id) for mig_id in applied]


def _write_state(state_path: str, applied: list[str]) -> None:
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"applied": applied}, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)


def _is_fresh_install(settings: Settings) -> bool:
    """No prior per-team data means there is nothing to migrate."""
    pattern = os.path.join(settings.base_data_dir, "team_*")
    return not any(os.path.isdir(path) for path in glob.glob(pattern))


def run_pending(
    settings: Settings, registry: list[Migration] | None = None
) -> list[str]:
    """Apply not-yet-applied migrations, oldest first; return the ids run.

    Called once at server start, *before* shared-infrastructure init, so a
    migration sees the data dir and Docker resources exactly as the previous
    version left them. A failing step aborts startup (already-applied steps
    stay recorded and are not re-run on the next attempt).
    """
    migs = MIGRATIONS if registry is None else registry
    ids = [mig.id for mig in migs]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Duplicate migration ids in registry: {ids}")

    os.makedirs(settings.base_data_dir, exist_ok=True)
    state_path = os.path.join(settings.base_data_dir, _STATE_FILENAME)
    ran: list[str] = []
    with _state_lock(state_path):
        applied = _load_state(state_path)
        if applied is None:
            if _is_fresh_install(settings):
                # Fresh install: current code lays down current-shape data,
                # so historical steps have nothing to act on.
                _write_state(state_path, ids)
                if ids:
                    logger.info(
                        "Fresh install: stamped %d migration(s) as applied",
                        len(ids),
                    )
                return []
            applied = []  # pre-migrations-era install: everything is pending

        for mig in migs:
            if mig.id in applied:
                continue
            logger.info("Applying migration %s: %s", mig.id, mig.description)
            try:
                mig.apply(settings)
            except Exception as exc:
                raise PrerequisiteNotMetError(
                    f"Startup migration '{mig.id}' failed: {exc}. Fix the "
                    "cause and restart — already-applied steps will not "
                    "re-run."
                ) from exc
            applied.append(mig.id)
            # Persist after every step so a crash resumes at the failed one.
            _write_state(state_path, applied)
            ran.append(mig.id)

    if ran:
        logger.info("Applied %d migration(s): %s", len(ran), ", ".join(ran))
    return ran
