"""Production environment lifecycle (create/update/rollback/delete).

A production environment is, at the docker_ops layer, a *namespaced*
environment: its internal env name is ``prod-{name}`` flowing through the
shared :mod:`oduflow.naming` chain, so container naming, PG roles, module
install/upgrade, logs and exec primitives are reused untouched — while a
separate metadata plane (the per-team ``productions.json`` registry, no
``oduflow.branch`` label on containers) keeps productions invisible to
every dev-side code path (dev listing, reaper, dev quotas) by construction.

Key differences from dev environments:

- databases live in the dedicated production PostgreSQL cluster
  (``settings.prod_db_container``), never the shared dev one;
- a custom domain per production (Traefik ``Host(...)`` rule) instead of
  the ``{slug}.{team.hostname}`` scheme — traefik routing mode required;
- full git clone (commit history is the point: rollback targets, deploy
  history) instead of ``--depth 1``;
- a production odoo.conf chain (``.oduflow/odoo.prod.conf`` > team
  ``odoo.prod.conf`` > bundled ``odoo-prod.conf``) with auto-tuned
  workers/limits injected on top (see :mod:`oduflow.prod_tune`);
- no ``--dev=xml``, no sanitize/neutralize, plain filestore directory
  (no overlay), no reaper.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any, Callable

import docker
from docker import DockerClient
from oduflow.docker_ops.client import chown_recursive, get_client, get_odoo_uid_gid
from oduflow.docker_ops.stats import default_env_limits
from oduflow.docker_ops.system_ops import (
    _copy_file_from_container,
    _copy_file_to_container,
    _create_pg_role,
    _db_exists,
    _drop_pg_role,
    _exec_sql,
    _resolve_conf,
    _resolve_instance_conf,
    drop_signaling_sequences,
    ensure_prod_infra,
    ensure_team_network,
    reassign_db_ownership,
)
from oduflow.env_credentials import create_credentials, load_credentials
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.naming import (
    PROD_ENV_PREFIX,
    PRODUCTION_TEMPLATE_PREFIX,
    get_db_name,
    get_filestore_paths,
    get_repo_path,
    get_resource_name,
    get_team_network_name,
    get_template_db_name,
    get_workspace_path,
    prod_env_name,
    sanitize_repo_url,
    validate_domain,
    validate_prod_name,
)
from oduflow.resource_plan import ResourcePlan
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_DEPLOYS_FILENAME = "deploys.json"
_DEPLOYS_CAP = 100

# Written into the kept workspace by delete_production(drop_database=False):
# the marker that these bytes belong to a *deleted* production. Only
# tombstoned leftovers are ever purged (by the reaper after
# [lifecycle] prod_purge_hours, or by `oduflow cleanup
# --purge-deleted-productions`); anything without a tombstone is presumed
# alive and never touched.
TOMBSTONE_FILENAME = "deleted.json"

# Hooks fired at the start of update_production (before the pull). The backup
# subsystem registers a pre-update snapshot here; failures are logged and
# never block the deploy. Signature: hook(settings, team, name) -> None.
pre_update_hooks: list[Callable[[Settings, TeamSettings, str], None]] = []

# odoo.conf [options] keys a per-production override may not touch:
# addons_path is generated from the repo + extra addon worktrees, data_dir
# anchors the filestore bind mount, and the db_* connection keys are managed
# via container env vars (a conf value would override them inside Odoo).
RESERVED_ODOO_CONF_KEYS = frozenset(
    {"addons_path", "data_dir", "db_host", "db_port", "db_user", "db_password"}
)


def prod_url(settings: Settings, team: TeamSettings, record: dict[str, Any]) -> str:
    # The domain is free-form (not necessarily under team.hostname); the owning
    # team's scheme assumes it is fronted the same way as the team's other
    # hosts (documented in docs/traefik.md). There is no per-production scheme.
    return f"{settings.public_scheme_for(team)}://{record['domain']}"


def _odoo_container_name(settings: Settings, team: TeamSettings, name: str) -> str:
    return get_resource_name(prod_env_name(name), "odoo", settings.prefix, team.team_id)


def _workspace(team: TeamSettings, name: str) -> str:
    return get_workspace_path(prod_env_name(name), team.workspaces_dir)


def prod_db_name(team: TeamSettings, name: str) -> str:
    return get_db_name(prod_env_name(name), team.team_id)


def prod_filestore_dir(team: TeamSettings, name: str) -> str:
    return os.path.join(_workspace(team, name), "filestore")


def _get_container(
    client: DockerClient, settings: Settings, team: TeamSettings, name: str
) -> Any | None:
    try:
        container = client.containers.get(_odoo_container_name(settings, team, name))
    except docker.errors.NotFound:
        return None
    label = container.labels.get(settings.team_label)
    if label is not None and label != team.team_id:
        return None
    return container


def _require_container(
    client: DockerClient, settings: Settings, team: TeamSettings, name: str
) -> Any:
    container = _get_container(client, settings, team, name)
    if container is None:
        raise NotFoundError(
            f"Production '{name}' has no container. It may need to be "
            "recreated (delete_production + create_production) or restored."
        )
    return container


# ---------------------------------------------------------------------------
# odoo.conf (production profile)
# ---------------------------------------------------------------------------


def _prod_base_conf_path(team: TeamSettings, repo_path: str) -> str:
    """Resolve the production base conf: repo > team > bundled.

    A separate chain from the dev one on purpose: a repo's dev conf
    (workers=0) must never leak into production, so the file names are
    explicit (``odoo.prod.conf`` / bundled ``odoo-prod.conf``).
    """
    repo_conf = os.path.join(repo_path, ".oduflow", "odoo.prod.conf")
    if os.path.isfile(repo_conf):
        return repo_conf
    team_conf = _resolve_instance_conf("odoo.prod.conf", team.data_dir)
    if team_conf.exists():
        return str(team_conf)
    return str(_resolve_conf("odoo-prod.conf"))


def _build_prod_odoo_conf(
    settings: Settings,
    team: TeamSettings,
    name: str,
    repo_path: str,
    extra_container_paths: list[str],
    *,
    plan: ResourcePlan | None = None,
    output_path: str | None = None,
) -> str:
    """Generate the merged production odoo.conf; return the host path."""
    from oduflow.extra_addons import generate_odoo_conf, resolve_main_addons_path
    from oduflow.pg_tune import detect_resources
    from oduflow.prod_tune import compute_odoo_worker_settings
    from oduflow.resource_plan import build_resource_plan

    if plan is None:
        res = detect_resources()
        plan = build_resource_plan(
            res["total_ram_mb"],
            res["cpu_count"],
            production_enabled=True,
        )
    overrides = compute_odoo_worker_settings(
        plan.host_cpu_count,
        plan.host_ram_mb,
        workers_cap=settings.prod_workers_cap,
        plan=plan,
    )
    # Per-production user overrides (registry record) win over auto-tuning:
    # an explicit `workers = 2` must beat the computed value. Reserved keys
    # are dropped defensively — set_production_odoo_conf refuses them, but
    # a hand-edited productions.json must not break the managed conf.
    from oduflow import production_registry

    user_conf = production_registry.get_production(team, name).get("odoo_conf") or {}
    for key, value in user_conf.items():
        if str(key) not in RESERVED_ODOO_CONF_KEYS:
            overrides[str(key)] = str(value)
    generated = output_path or os.path.join(_workspace(team, name), "odoo.conf")
    generate_odoo_conf(
        _prod_base_conf_path(team, repo_path),
        generated,
        extra_container_paths,
        resolve_main_addons_path(repo_path),
        overrides=overrides,
    )
    return generated


def stage_prod_odoo_conf(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    name: str,
    generated_path: str,
) -> str | None:
    """Copy a generated config into an existing production without restarting."""
    container = _get_container(client, settings, team, name)
    if container is None:
        return None
    _copy_file_to_container(container, generated_path, "/etc/odoo")
    return str(container.name)


def reapply_prod_odoo_conf(
    settings: Settings, team: TeamSettings, name: str, container: Any
) -> bool:
    """Rebuild and re-copy /etc/odoo/odoo.conf into a production container.

    The production counterpart of env_ops._reapply_odoo_conf (which
    delegates here based on the ``oduflow.prod`` label). Always returns
    True: the bundled odoo-prod.conf guarantees a base conf exists.
    """
    env_name = prod_env_name(name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    extra_addons_json = (container.labels or {}).get("oduflow.extra_addons", "")
    extra_paths: list[str] = []
    if extra_addons_json:
        try:
            extra_paths = [
                f"/mnt/extra-addons-{rn}" for rn in json.loads(extra_addons_json)
            ]
        except (json.JSONDecodeError, TypeError):
            extra_paths = []
    generated = _build_prod_odoo_conf(settings, team, name, repo_path, extra_paths)
    _copy_file_to_container(container, generated, "/etc/odoo")
    return True


# ---------------------------------------------------------------------------
# Deploy history
# ---------------------------------------------------------------------------


def _deploys_path(team: TeamSettings, name: str) -> str:
    return os.path.join(_workspace(team, name), _DEPLOYS_FILENAME)


def append_deploy(team: TeamSettings, name: str, record: dict[str, Any]) -> None:
    """Append a deploy record (newest last, capped at _DEPLOYS_CAP)."""
    path = _deploys_path(team, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        deploys = read_deploys(team, name, limit=0)
        deploys.append(record)
        deploys = deploys[-_DEPLOYS_CAP:]
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="deploys.", suffix=".tmp", dir=os.path.dirname(path)
        )
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(deploys, f, indent=2)
        os.replace(tmp_path, path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_deploys(
    team: TeamSettings, name: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Deploy history, newest last; ``limit=0`` returns everything."""
    path = _deploys_path(team, name)
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            deploys = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(deploys, list):
        return []
    return deploys[-limit:] if limit else deploys


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def _probe_odoo_health(container: Any) -> bool:
    """One-shot /web/health probe from inside the container.

    In-container (localhost:8069) on purpose: the external URL depends on
    DNS for the custom domain being set up, which must not fail a deploy
    verification (or trigger a rollback) on a production whose DNS is still
    propagating.
    """
    code, _ = container.exec_run(
        [
            "python3",
            "-c",
            "import urllib.request,sys;"
            "r=urllib.request.urlopen('http://localhost:8069/web/health',timeout=5);"
            "sys.exit(0 if r.status==200 else 1)",
        ]
    )
    return bool(code == 0)


def wait_production_healthy(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    name: str,
    timeout: int = 180,
) -> bool:
    """Poll the production's Odoo until healthy or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container = _get_container(client, settings, team, name)
        if container is not None:
            try:
                container.reload()
                if container.status == "running" and _probe_odoo_health(container):
                    return True
            except docker.errors.APIError:
                pass
        time.sleep(3)
    return False


# ---------------------------------------------------------------------------
# Create / delete
# ---------------------------------------------------------------------------


def _assert_domain_free(
    settings: Settings, domain: str, *, own_team: str, own_name: str
) -> None:
    """A domain maps to one global Traefik Host() rule — enforce uniqueness
    across ALL teams' productions, not just the caller's."""
    from oduflow import production_registry

    for team in settings.teams.values():
        for prod_name, record in production_registry.list_productions(team).items():
            if record.get("domain") != domain:
                continue
            if team.team_id == own_team and prod_name == own_name:
                continue
            raise ConflictError(f"Domain '{domain}' is already used by a production.")


def _copy_db_into_prod_cluster(
    client: DockerClient,
    settings: Settings,
    source_db: str,
    target_db: str,
    label: str,
) -> None:
    """Copy a dev-cluster database into the production cluster.

    ``CREATE DATABASE ... TEMPLATE`` cannot cross clusters, so this dumps the
    source from the dev instance and restores it into the production one
    (pg_dump -Fc | pg_restore --no-owner). The production image's pg_restore
    is same-or-newer than the dev dump's format, so defaults are compatible.
    """
    dev = client.containers.get(settings.shared_db_container)
    prod = client.containers.get(settings.prod_db_container)
    dump_in_container = f"/tmp/{target_db}.seed.pgdump"

    exit_code, output = dev.exec_run(
        ["pg_dump", "-U", settings.db_user, "-Fc", "-f", dump_in_container, source_db]
    )
    if exit_code != 0:
        text = output.decode("utf-8", errors="replace") if output else ""
        raise ExternalCommandError(f"pg_dump ({label})", exit_code, text[-2000:])

    with tempfile.TemporaryDirectory() as tmpdir:
        host_dump = os.path.join(tmpdir, "seed.pgdump")
        _copy_file_from_container(dev, dump_in_container, host_dump)
        dev.exec_run(["rm", "-f", dump_in_container])
        _copy_file_to_container(prod, host_dump, "/tmp")
        try:
            exit_code, output = prod.exec_run(
                [
                    "pg_restore",
                    "-U",
                    settings.db_user,
                    "--no-owner",
                    "-d",
                    target_db,
                    f"/tmp/{os.path.basename(host_dump)}",
                ]
            )
            if exit_code != 0:
                text = output.decode("utf-8", errors="replace") if output else ""
                raise ExternalCommandError(
                    f"pg_restore ({label})", exit_code, text[-2000:]
                )
        finally:
            prod.exec_run(["rm", "-f", f"/tmp/{os.path.basename(host_dump)}"])


def _seed_db_from_template(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    target_db: str,
) -> None:
    """Copy a (dev-cluster) template database into the production cluster."""
    tpl_db = get_template_db_name(template_name, team.team_id)
    if not _db_exists(client, settings, tpl_db):
        raise NotFoundError(
            f"Template database '{tpl_db}' not found. Import or create the "
            f"template '{template_name}' first."
        )
    _copy_db_into_prod_cluster(client, settings, tpl_db, target_db, "template seed")


def _seed_db_from_environment(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    source_env: str,
    target_db: str,
) -> None:
    """Copy a dev environment's database into the production cluster."""
    src_db = get_db_name(source_env, team.team_id)
    if not _db_exists(client, settings, src_db):
        raise NotFoundError(
            f"Environment database '{src_db}' not found for environment '{source_env}'."
        )
    _copy_db_into_prod_cluster(client, settings, src_db, target_db, "environment seed")


def _source_env_info(
    client: DockerClient, settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    """Resolve the source dev environment of a promotion from its container
    labels: repo/branch/image/git_user/extra_addons defaults plus the template
    provenance (to warn when the data was sanitized on its way from prod)."""
    if env_name.startswith(PROD_ENV_PREFIX):
        raise ValueError(
            f"'{env_name}' is in the production namespace, not a dev environment."
        )
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' not found (no container '{container_name}')."
        )
    labels = container.labels or {}
    team_label = labels.get(settings.team_label)
    if team_label is not None and team_label != team.team_id:
        raise NotFoundError(f"Environment '{env_name}' not found for this team.")
    try:
        extra_addons = json.loads(labels.get("oduflow.extra_addons", "{}"))
    except (json.JSONDecodeError, TypeError):
        extra_addons = {}
    return {
        "container": container,
        "repo_url": sanitize_repo_url(labels.get(settings.repo_label, "")),
        "branch": labels.get("oduflow.git_branch", env_name),
        "odoo_image": labels.get(settings.image_label, ""),
        "git_user": labels.get("oduflow.git_user", ""),
        "extra_addons": extra_addons if isinstance(extra_addons, dict) else {},
        "template": labels.get("oduflow.template", ""),
    }


def _copy_env_data_into_production(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    source_env: dict[str, Any],
    source: str,
    name: str,
    env_db: str,
) -> None:
    """Copy the source environment's database and filestore into the new
    production, with the environment's Odoo stopped over both copies for a
    consistent pair. The environment itself is left as it was (not reset)."""
    container = source_env["container"]
    was_running = getattr(container, "status", "") == "running"
    if was_running:
        container.stop(timeout=60)
    try:
        _seed_db_from_environment(client, settings, team, source, env_db)
        src_filestore = get_filestore_paths(source, team.workspaces_dir)["merged"]
        filestore_path = prod_filestore_dir(team, name)
        os.makedirs(filestore_path, mode=0o777, exist_ok=True)
        if os.path.isdir(src_filestore):
            shutil.copytree(src_filestore, filestore_path, dirs_exist_ok=True)
    finally:
        if was_running:
            try:
                container.start()
            except Exception as exc:
                logger.warning(
                    "Could not restart source environment '%s': %s", source, exc
                )


def _cleanup_partial_production(
    client: DockerClient, settings: Settings, team: TeamSettings, name: str
) -> None:
    """Best-effort teardown of a half-created production (rollback path)."""
    env_name = prod_env_name(name)
    try:
        container = _get_container(client, settings, team, name)
        if container is not None:
            container.remove(force=True)
    except Exception:
        pass
    env_db = prod_db_name(team, name)
    try:
        if _db_exists(
            client, settings, env_db, container_name=settings.prod_db_container
        ):
            _exec_sql(
                client,
                settings,
                f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);',
                container_name=settings.prod_db_container,
            )
    except Exception:
        pass
    try:
        creds = load_credentials(
            env_name, team.workspaces_dir, settings.db_user, settings.db_password
        )
        _drop_pg_role(
            client,
            settings,
            creds["pg_user"],
            container_name=settings.prod_db_container,
        )
    except Exception:
        pass
    workspace = _workspace(team, name)
    if os.path.isdir(workspace):
        shutil.rmtree(workspace, ignore_errors=True)


def _container_spec(
    settings: Settings,
    team: TeamSettings,
    name: str,
    record: dict[str, Any],
    env_creds: dict[str, str],
    extra_mount_paths: list[tuple[str, str]],
) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, str]]:
    """Env vars, volume binds and labels for a production Odoo container.

    Derived entirely from the authoritative registry record, so that
    create_production and reconfigure_production produce identical
    containers for the same record. The referenced host paths (repo,
    worktrees, filestore, sessions) must already exist.
    """
    env_name = prod_env_name(name)
    env_db = prod_db_name(team, name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    odoo_env = {
        "HOST": settings.prod_db_container,
        "USER": env_creds["pg_user"],
        "PASSWORD": env_creds["pg_password"],
    }
    odoo_volumes: dict[str, dict[str, str]] = {
        repo_path: {"bind": "/mnt/extra-addons", "mode": "rw"}
    }
    for host_path, container_path in extra_mount_paths:
        odoo_volumes[host_path] = {"bind": container_path, "mode": "ro"}
    odoo_volumes[prod_filestore_dir(team, name)] = {
        "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
        "mode": "rw",
    }
    odoo_volumes[os.path.join(_workspace(team, name), "sessions")] = {
        "bind": "/var/lib/odoo/.local/share/Odoo/sessions",
        "mode": "rw",
    }

    # Deliberately NO branch label (keeps dev listings/reaper blind) and
    # NO scoped-MCP token (productions are not agent playgrounds).
    domain = record["domain"]
    traefik_router = f"oduflow-{team.team_id}-{env_name}"
    labels = {
        settings.managed_label: "true",
        settings.team_label: team.team_id,
        settings.repo_label: record["repo_url"],
        settings.image_label: record["odoo_image"],
        "oduflow.prod": "true",
        "oduflow.prod_name": name,
        "oduflow.domain": domain,
        "oduflow.git_branch": record["branch"],
        "oduflow.created_at": record["created_at"],
        "traefik.enable": "true",
        f"traefik.http.routers.{traefik_router}.rule": f"Host(`{domain}`)",
        f"traefik.http.services.{traefik_router}.loadbalancer.server.port": "8069",
        "traefik.docker.network": get_team_network_name(team.team_id, settings.prefix),
    }
    if record.get("extra_addons"):
        labels["oduflow.extra_addons"] = json.dumps(record["extra_addons"])
    if record.get("git_user"):
        labels["oduflow.git_user"] = record["git_user"]
    if settings.routing_tls:
        labels.update(
            {
                f"traefik.http.routers.{traefik_router}.entrypoints": "websecure",
                f"traefik.http.routers.{traefik_router}.tls": "true",
                f"traefik.http.routers.{traefik_router}.tls.certresolver": (
                    "letsencrypt"
                ),
            }
        )
    else:
        labels[f"traefik.http.routers.{traefik_router}.entrypoints"] = "web"
    return odoo_env, odoo_volumes, labels


def _run_odoo_container(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    name: str,
    odoo_image: str,
    odoo_env: dict[str, str],
    odoo_volumes: dict[str, dict[str, str]],
    labels: dict[str, str],
    generated_conf: str,
    repo_path: str,
) -> tuple[Any, list[str]]:
    """Run the production Odoo container and finish its in-container setup
    (odoo.conf copy, apt/pip requirements, one restart picking both up)."""
    from oduflow.docker_ops.env_ops import (
        _install_apt_packages,
        _install_pip_requirements,
    )

    container = client.containers.run(
        image=odoo_image,
        name=_odoo_container_name(settings, team, name),
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        extra_hosts={"host.docker.internal": "host-gateway"},
        **default_env_limits(),
        environment=odoo_env,
        labels=labels,
        volumes=odoo_volumes,
        restart_policy={"Name": "unless-stopped"},
        # No --dev=xml: production serves with workers>0 and never
        # auto-reloads assets; any change requires at least a restart.
        command=f"odoo -d {prod_db_name(team, name)}",
    )
    setup_logs: list[str] = []
    _copy_file_to_container(container, generated_conf, "/etc/odoo")
    apt_log = _install_apt_packages(container, repo_path)
    if apt_log:
        setup_logs.append(apt_log)
    _, pip_log = _install_pip_requirements(container, repo_path, restart=False)
    if pip_log:
        setup_logs.append(pip_log)
    # One restart picks up both the copied odoo.conf and pip packages.
    container.restart()
    return container, setup_logs


def create_production(
    settings: Settings,
    team: TeamSettings,
    name: str,
    repo_url: str,
    branch: str,
    domain: str,
    odoo_image: str,
    *,
    git_user: str = "",
    extra_addons: dict[str, str] | None = None,
    auto_update: bool = False,
    allow_copy_to_dev_mcp: bool = True,
    template_name: str | None = None,
    from_environment: str | None = None,
) -> dict[str, Any]:
    """Provision a production environment.

    The registry record is created first (reserving the name and — on the
    team's first production — generating the webhook secret); on any failure
    the partial resources AND the record are rolled back.

    ``from_environment`` promotes a dev environment: its database and
    filestore are copied (under a briefly stopped Odoo, for consistency) and
    empty ``repo_url``/``branch``/``odoo_image``/``git_user``/``extra_addons``
    default to the environment's own. The source environment is left as it
    was — unlike the save-as-template path, nothing is reset.
    """
    from oduflow import production_registry
    from oduflow.docker_ops.env_ops import _clone_repo, _init_empty_database

    validate_prod_name(name)
    domain = validate_domain(domain)
    if settings.routing_mode != "traefik":
        raise PrerequisiteNotMetError(
            'Production hosting requires routing_mode = "traefik" (custom '
            "domains are routed via Traefik Host rules)."
        )
    if template_name is not None and from_environment:
        raise ConflictError("Pass either template_name or from_environment, not both.")
    if not from_environment and not (repo_url and branch and odoo_image):
        raise ValueError(
            "repo_url, branch and odoo_image are required "
            "(or pass from_environment to inherit them)."
        )
    _assert_domain_free(settings, domain, own_team=team.team_id, own_name=name)

    start_time = time.time()
    client = get_client()
    source_env: dict[str, Any] | None = None
    if from_environment:
        source_env = _source_env_info(client, settings, team, from_environment)
        repo_url = repo_url or source_env["repo_url"]
        branch = branch or source_env["branch"]
        odoo_image = odoo_image or source_env["odoo_image"]
        git_user = git_user or source_env["git_user"]
        if extra_addons is None and source_env["extra_addons"]:
            extra_addons = dict(source_env["extra_addons"])
        if not repo_url:
            raise PrerequisiteNotMetError(
                f"Environment '{from_environment}' has no repository URL "
                "(local-path environment?); pass repo_url explicitly."
            )
        if not odoo_image:
            raise PrerequisiteNotMetError(
                f"Environment '{from_environment}' has no image label; pass "
                "odoo_image explicitly."
            )
    env_name = prod_env_name(name)
    env_db = prod_db_name(team, name)
    container_name = _odoo_container_name(settings, team, name)
    workspace = _workspace(team, name)

    # Bring up (or verify) the production tier before touching anything else.
    ensure_prod_infra(client, settings, force=True)

    # Refuse to clobber leftovers: a previous production's database kept by
    # delete_production(drop_database=False) must be dealt with explicitly.
    if _db_exists(client, settings, env_db, container_name=settings.prod_db_container):
        raise ConflictError(
            f"Database '{env_db}' already exists in the production cluster "
            f"(left by a previous production '{name}'). Delete it first "
            "(delete_production with drop_database=true recreates cleanly) "
            "or drop it manually."
        )
    try:
        client.containers.get(container_name)
        raise ConflictError(f"Production '{name}' container already exists.")
    except docker.errors.NotFound:
        pass

    # Reserve the name in the registry (authoritative record).
    record = production_registry.create_production(
        team,
        name,
        {
            "domain": domain,
            "repo_url": sanitize_repo_url(repo_url),
            "branch": branch,
            "odoo_image": odoo_image,
            "git_user": git_user,
            "extra_addons": extra_addons or {},
            "auto_update": bool(auto_update),
            "allow_copy_to_dev_mcp": bool(allow_copy_to_dev_mcp),
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )

    try:
        ensure_team_network(client, settings, team)
        os.makedirs(workspace, exist_ok=True)
        # Re-creating a soft-deleted name revives it: drop the tombstone so
        # the purge sweep never eats the new production's bytes.
        clear_tombstone(team, name)

        # Full clone — commit history is the point for production.
        repo_path = get_repo_path(env_name, team.workspaces_dir)
        _clone_repo(
            repo_url, branch, repo_path, team, git_user=git_user, depth=0, timeout=300
        )

        extra_mount_paths: list[tuple[str, str]] = []
        if extra_addons:
            from oduflow.extra_addons import create_worktree

            extra_dir = os.path.join(workspace, "extra")
            os.makedirs(extra_dir, exist_ok=True)
            for repo_name, addon_branch in extra_addons.items():
                wt_path = os.path.join(extra_dir, repo_name)
                create_worktree(team, repo_name, addon_branch, wt_path)
                extra_mount_paths.append((wt_path, f"/mnt/extra-addons-{repo_name}"))

        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{env_db}";',
            container_name=settings.prod_db_container,
        )
        if template_name is not None:
            _seed_db_from_template(client, settings, team, template_name, env_db)
        if source_env is not None:
            assert from_environment is not None
            _copy_env_data_into_production(
                client, settings, team, source_env, from_environment, name, env_db
            )

        env_creds = create_credentials(env_name, team.team_id, team.workspaces_dir)
        _create_pg_role(
            client,
            settings,
            env_creds["pg_user"],
            env_creds["pg_password"],
            env_db,
            container_name=settings.prod_db_container,
        )
        if template_name is not None or source_env is not None:
            reassign_db_ownership(
                client,
                settings,
                env_db,
                env_creds["pg_user"],
                container_name=settings.prod_db_container,
            )
            drop_signaling_sequences(
                client,
                settings,
                env_db,
                container_name=settings.prod_db_container,
            )

        # Plain filestore directory — production is long-lived and must not
        # depend on a fuse overlay over a template.
        filestore_path = prod_filestore_dir(team, name)
        os.makedirs(filestore_path, mode=0o777, exist_ok=True)
        os.chmod(filestore_path, 0o777)
        if template_name is not None:
            tpl_filestore = team.get_template_filestore_path(template_name)
            if os.path.isdir(tpl_filestore):
                shutil.copytree(tpl_filestore, filestore_path, dirs_exist_ok=True)
        uid_str, gid_str = get_odoo_uid_gid(client, odoo_image).split(":")
        chown_recursive(filestore_path, int(uid_str), int(gid_str), client, odoo_image)

        sessions_path = os.path.join(workspace, "sessions")
        os.makedirs(sessions_path, mode=0o777, exist_ok=True)
        os.chmod(sessions_path, 0o777)
        chown_recursive(sessions_path, int(uid_str), int(gid_str), client, odoo_image)

        odoo_env, odoo_volumes, labels = _container_spec(
            settings, team, name, record, env_creds, extra_mount_paths
        )

        generated_conf = _build_prod_odoo_conf(
            settings,
            team,
            name,
            repo_path,
            [cp for _, cp in extra_mount_paths],
        )

        try:
            logger.info("Pulling image %s", odoo_image)
            client.images.pull(odoo_image)
        except Exception as exc:
            logger.warning(
                "Could not pull image %s, using local copy: %s", odoo_image, exc
            )

        setup_logs: list[str] = []
        if template_name is None and source_env is None:
            setup_logs.append(
                _init_empty_database(
                    client,
                    settings,
                    team,
                    odoo_image,
                    env_db,
                    odoo_env,
                    odoo_volumes,
                    env_name,
                )
            )

        container, run_logs = _run_odoo_container(
            client,
            settings,
            team,
            name,
            odoo_image,
            odoo_env,
            odoo_volumes,
            labels,
            generated_conf,
            repo_path,
        )
        setup_logs.extend(run_logs)

        from oduflow.git_ops import rev_parse

        head = rev_parse(repo_path)
        append_deploy(
            team,
            name,
            {
                "ts_start": record["created_at"],
                "ts_end": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "trigger": "create",
                "from_commit": "",
                "to_commit": head,
                "action": "create",
                "status": "success",
                # Extra-addon worktree HEADs at this deployed state, so a later
                # rollback to this commit can revert the addons in lockstep.
                "worktrees": _worktree_heads(team, name),
            },
        )
    except BaseException:
        logger.error(
            "create_production('%s') failed; rolling back partial resources", name
        )
        _cleanup_partial_production(client, settings, team, name)
        production_registry.delete_production(team, name)
        raise

    logger.info(
        "Production created",
        extra={"env_name": env_name, "domain": domain},
    )
    notes: list[str] = []
    if source_env is not None and str(source_env.get("template", "")).startswith(
        PRODUCTION_TEMPLATE_PREFIX
    ):
        notes.append(
            f"Environment '{from_environment}' was created from production "
            "data that was sanitized on copy; this production starts with "
            "that sanitized data."
        )
    return {
        "name": name,
        "url": prod_url(settings, team, record),
        "domain": domain,
        "notes": notes,
        "odoo_container": container_name,
        "database": env_db,
        "workspace": workspace,
        "commit": head,
        "setup_logs": setup_logs,
        "webhook_secret_hint": (
            "GitHub webhook: POST /api/webhooks/github with the team secret "
            "(see get_production_info / the dashboard Production tab)."
        ),
        "elapsed_seconds": round(time.time() - start_time, 1),
    }


_ODOO_CONF_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def set_production_odoo_conf(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    set_options: dict[str, str] | None = None,
    unset_options: list[str] | None = None,
    restart: bool = True,
) -> dict[str, Any]:
    """Update a production's odoo.conf [options] overrides and re-apply.

    Overrides live in the registry record and are merged into the managed
    conf chain (base conf > auto-tuned workers > these overrides) on every
    conf rebuild, so they survive deploys, retunes and reconfigures. The
    caller must hold the production's lock.
    """
    from oduflow import production_registry

    record = production_registry.get_production(team, name)
    cleaned = {str(k).strip(): str(v) for k, v in (set_options or {}).items()}
    unset_list = [str(k).strip() for k in (unset_options or []) if str(k).strip()]
    reserved = sorted(k for k in cleaned if k in RESERVED_ODOO_CONF_KEYS)
    if reserved:
        raise ValueError(
            f"odoo.conf keys managed by Oduflow cannot be overridden: "
            f"{', '.join(reserved)}. addons_path and data_dir are generated; "
            "db_* connection keys come from container env vars."
        )
    invalid = sorted(k for k in cleaned if not _ODOO_CONF_KEY_RE.match(k))
    if invalid:
        raise ValueError(f"Invalid odoo.conf option names: {', '.join(invalid)}.")

    conf = dict(record.get("odoo_conf") or {})
    conf.update(cleaned)
    for key in unset_list:
        conf.pop(key, None)
    production_registry.update_production(team, name, {"odoo_conf": conf})

    client = get_client()
    container = _get_container(client, settings, team, name)
    applied = False
    restarted = False
    if container is not None:
        reapply_prod_odoo_conf(settings, team, name, container)
        applied = True
        if restart and container.status == "running":
            container.restart()
            restarted = True
    return {
        "name": name,
        "odoo_conf": conf,
        "applied": applied,
        "restarted": restarted,
    }


def reconfigure_production(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    domain: str | None = None,
    odoo_image: str | None = None,
    branch: str | None = None,
    repo_url: str | None = None,
    git_user: str | None = None,
    extra_addons: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Change a production's infrastructure settings and recreate its
    container to match. Only the passed (non-None) fields change.

    The database and filestore live outside the container and are
    preserved; expect a brief downtime while the container is replaced.
    The registry record (intent) is updated first, then the workspace and
    container are converged to it — on a mid-way failure re-running the
    same call resumes the convergence. Changing ``odoo_image`` does NOT
    migrate the database: a major Odoo version bump additionally needs an
    explicit module upgrade plan. The caller must hold the production's
    lock.
    """
    from oduflow import production_registry
    from oduflow.docker_ops.env_ops import _clone_repo
    from oduflow.extra_addons import create_worktree, remove_worktree
    from oduflow.git_ops import checkout_branch, fetch_branch, rev_parse

    record = production_registry.get_production(team, name)
    start_time = time.time()
    client = get_client()
    env_name = prod_env_name(name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    workspace = _workspace(team, name)

    updates: dict[str, Any] = {}
    if domain is not None:
        new_domain = validate_domain(domain)
        if new_domain != record.get("domain"):
            _assert_domain_free(
                settings, new_domain, own_team=team.team_id, own_name=name
            )
            updates["domain"] = new_domain
    if odoo_image is not None and odoo_image != record.get("odoo_image"):
        updates["odoo_image"] = odoo_image
    if repo_url is not None:
        clean_url = sanitize_repo_url(repo_url)
        if clean_url != record.get("repo_url"):
            updates["repo_url"] = clean_url
    if branch is not None and branch != record.get("branch"):
        updates["branch"] = branch
    if git_user is not None and git_user != record.get("git_user", ""):
        updates["git_user"] = git_user
    if extra_addons is not None and dict(extra_addons) != (
        record.get("extra_addons") or {}
    ):
        updates["extra_addons"] = dict(extra_addons)

    if not updates:
        return {
            "name": name,
            "changed": [],
            "message": "No settings changed; the container was left alone.",
        }

    old_head = ""
    try:
        old_head = rev_parse(repo_path)
    except Exception:
        pass

    # Registry first: the record is the authoritative intent, and the
    # convergence below is idempotent against it.
    record = production_registry.update_production(team, name, updates)

    # --- Converge the workspace (repo checkout + extra addon worktrees). ---
    if "repo_url" in updates or "git_user" in updates:
        # A changed remote (or credential identity) invalidates the clone.
        if os.path.isdir(repo_path):
            shutil.rmtree(repo_path)
        _clone_repo(
            record["repo_url"],
            record["branch"],
            repo_path,
            team,
            git_user=record.get("git_user", ""),
            depth=0,
            timeout=300,
        )
    elif "branch" in updates:
        fetch_branch(repo_path, record["branch"], team.git_credentials_file())
        checkout_branch(repo_path, record["branch"])

    new_extras: dict[str, str] = record.get("extra_addons") or {}
    extra_dir = os.path.join(workspace, "extra")
    if "extra_addons" in updates:
        old_extras = {}
        if os.path.isdir(extra_dir):
            old_extras = {
                entry: "" for entry in os.listdir(extra_dir) if entry not in new_extras
            }
        for repo_name in old_extras:
            wt_path = os.path.join(extra_dir, repo_name)
            try:
                remove_worktree(team, repo_name, wt_path)
            except Exception as exc:
                logger.warning("Could not remove worktree %s: %s", wt_path, exc)
            shutil.rmtree(wt_path, ignore_errors=True)
    extra_mount_paths: list[tuple[str, str]] = []
    for repo_name, addon_branch in new_extras.items():
        wt_path = os.path.join(extra_dir, repo_name)
        os.makedirs(extra_dir, exist_ok=True)
        # A branch change needs a fresh worktree; recreate rather than switch.
        if "extra_addons" in updates and os.path.isdir(wt_path):
            try:
                remove_worktree(team, repo_name, wt_path)
            except Exception as exc:
                logger.warning("Could not remove worktree %s: %s", wt_path, exc)
            shutil.rmtree(wt_path, ignore_errors=True)
        if not os.path.isdir(wt_path):
            create_worktree(team, repo_name, addon_branch, wt_path)
        extra_mount_paths.append((wt_path, f"/mnt/extra-addons-{repo_name}"))

    # --- Prepare everything, then swap the container (minimal downtime). ---
    env_creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    generated_conf = _build_prod_odoo_conf(
        settings, team, name, repo_path, [cp for _, cp in extra_mount_paths]
    )
    odoo_env, odoo_volumes, labels = _container_spec(
        settings, team, name, record, env_creds, extra_mount_paths
    )
    odoo_image_new = record["odoo_image"]
    try:
        logger.info("Pulling image %s", odoo_image_new)
        client.images.pull(odoo_image_new)
    except Exception as exc:
        logger.warning(
            "Could not pull image %s, using local copy: %s", odoo_image_new, exc
        )

    container = _get_container(client, settings, team, name)
    if container is not None:
        try:
            container.stop(timeout=30)
        except Exception as exc:
            logger.warning("Stopping old container failed (removing anyway): %s", exc)
        container.remove(force=True)

    container, setup_logs = _run_odoo_container(
        client,
        settings,
        team,
        name,
        odoo_image_new,
        odoo_env,
        odoo_volumes,
        labels,
        generated_conf,
        repo_path,
    )
    healthy = wait_production_healthy(client, settings, team, name, timeout=180)
    production_registry.update_production(team, name, {"unhealthy": not healthy})

    new_head = ""
    try:
        new_head = rev_parse(repo_path)
    except Exception:
        pass
    code_changed = bool({"branch", "repo_url", "extra_addons"} & set(updates))
    if code_changed:
        append_deploy(
            team,
            name,
            {
                "ts_start": _now_iso(),
                "ts_end": _now_iso(),
                "trigger": "reconfigure",
                "from_commit": old_head,
                "to_commit": new_head,
                "action": "reconfigure",
                "status": "success" if healthy else "failed",
                "worktrees": _worktree_heads(team, name),
            },
        )

    notes: list[str] = []
    if "odoo_image" in updates:
        notes.append(
            "The database was NOT migrated to the new image's Odoo version; "
            "a major version bump needs an explicit module upgrade plan."
        )
    if code_changed:
        notes.append(
            "The new code is running but no modules were installed/upgraded; "
            "use update_production(install=..., upgrade=...) if the new code "
            "needs them."
        )
    logger.info(
        "Production reconfigured",
        extra={"env_name": env_name, "changed": sorted(updates)},
    )
    return {
        "name": name,
        "changed": sorted(updates),
        "domain": record["domain"],
        "url": prod_url(settings, team, record),
        "odoo_container": _odoo_container_name(settings, team, name),
        "commit": new_head,
        "healthy": healthy,
        "setup_logs": setup_logs,
        "notes": notes,
        "elapsed_seconds": round(time.time() - start_time, 1),
    }


def delete_production(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    drop_database: bool = False,
) -> dict[str, Any]:
    """Remove a production. The database and workspace (filestore, repo,
    deploy history) are KEPT unless ``drop_database`` — productions are
    precious, deleting bytes is opt-in."""
    from oduflow import production_registry

    production_registry.get_production(team, name)  # NotFoundError if absent
    client = get_client()
    env_name = prod_env_name(name)
    env_db = prod_db_name(team, name)
    workspace = _workspace(team, name)
    warnings: list[str] = []

    container = _get_container(client, settings, team, name)
    if container is not None:
        try:
            container.stop()
            container.remove(v=True)
        except docker.errors.APIError as exc:
            warnings.append(f"Container removal: {exc}")

    if drop_database:
        _, destroy_warnings = _destroy_leftovers(client, settings, team, name)
        warnings.extend(destroy_warnings)
    else:
        _write_tombstone(team, name, env_db)

    production_registry.delete_production(team, name)
    logger.info("Production deleted", extra={"env_name": env_name})
    return {
        "name": name,
        "database_dropped": drop_database,
        "kept": [] if drop_database else [f"database {env_db}", workspace],
        "warnings": warnings,
    }


def _tombstone_path(team: TeamSettings, name: str) -> str:
    return os.path.join(_workspace(team, name), TOMBSTONE_FILENAME)


def _write_tombstone(team: TeamSettings, name: str, env_db: str) -> None:
    """Mark the kept leftovers as deleted. The workspace is created if the
    production never had one on disk, so the tombstone (and with it the
    kept database) is always discoverable by the purge sweep."""
    workspace = _workspace(team, name)
    os.makedirs(workspace, exist_ok=True)
    payload = {
        "name": name,
        "db_name": env_db,
        "deleted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(_tombstone_path(team, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def clear_tombstone(team: TeamSettings, name: str) -> None:
    try:
        os.remove(_tombstone_path(team, name))
    except FileNotFoundError:
        pass


def _destroy_leftovers(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    require_db_drop: bool = False,
) -> tuple[bool, list[str]]:
    """Drop a production's database, PG role and workspace. Returns
    (db_dropped, warnings). With ``require_db_drop`` the workspace is kept
    when the DROP DATABASE fails, so the tombstone survives and a later
    sweep retries instead of orphaning the database silently."""
    env_name = prod_env_name(name)
    env_db = prod_db_name(team, name)
    workspace = _workspace(team, name)
    warnings: list[str] = []

    db_dropped = True
    try:
        _exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);',
            container_name=settings.prod_db_container,
        )
    except Exception as exc:
        db_dropped = False
        warnings.append(f'Failed to drop database "{env_db}": {exc}')
    if require_db_drop and not db_dropped:
        return False, warnings
    try:
        creds = load_credentials(
            env_name, team.workspaces_dir, settings.db_user, settings.db_password
        )
        _drop_pg_role(
            client,
            settings,
            creds["pg_user"],
            container_name=settings.prod_db_container,
        )
    except Exception as exc:
        warnings.append(f"Failed to drop PG role: {exc}")
    if os.path.isdir(workspace):
        extra_dir = os.path.join(workspace, "extra")
        if os.path.isdir(extra_dir):
            from oduflow.extra_addons import remove_worktree

            for repo_name in os.listdir(extra_dir):
                wt_path = os.path.join(extra_dir, repo_name)
                if os.path.isdir(wt_path):
                    remove_worktree(team, repo_name, wt_path)
        shutil.rmtree(workspace, ignore_errors=True)
    return db_dropped, warnings


def purge_deleted_productions(
    settings: Settings,
    team: TeamSettings,
    *,
    older_than_hours: float = 0,
    dry_run: bool = False,
    locks: Any | None = None,
) -> dict[str, Any]:
    """Permanently remove the leftovers (database, PG role, workspace) of
    soft-deleted productions — those whose workspace carries a tombstone
    written by delete_production(drop_database=False).

    ``older_than_hours`` keeps leftovers younger than the cutoff (the reaper
    passes ``[lifecycle] prod_purge_hours``); ``0`` purges immediately (the
    ``cleanup --purge-deleted-productions`` CLI). A tombstone on a production
    that is back in the registry is stale (the name was re-created): it is
    removed and the leftovers are NOT touched. Pass the server's LockManager
    as ``locks`` to skip productions busy with another operation.

    Returns ``{"dry_run", "purged", "pending", "warnings"}`` where purged and
    pending are lists of production names (with dry_run, "purged" means
    "would be purged").
    """
    from oduflow import production_registry
    from oduflow.locking import prod_lock_key
    from oduflow.naming import PROD_ENV_PREFIX

    registered = production_registry.list_productions(team)
    purged: list[str] = []
    pending: list[str] = []
    warnings: list[str] = []
    client: DockerClient | None = None
    now = time.time()

    if not os.path.isdir(team.workspaces_dir):
        return {"dry_run": dry_run, "purged": [], "pending": [], "warnings": []}

    for entry in sorted(os.listdir(team.workspaces_dir)):
        if not entry.startswith(PROD_ENV_PREFIX):
            continue
        name = entry[len(PROD_ENV_PREFIX) :]
        ts_path = os.path.join(team.workspaces_dir, entry, TOMBSTONE_FILENAME)
        if not os.path.isfile(ts_path):
            continue
        if name in registered:
            warnings.append(
                f"Stale tombstone on registered production '{name}' — removed."
            )
            if not dry_run:
                clear_tombstone(team, name)
            continue
        try:
            with open(ts_path, encoding="utf-8") as f:
                deleted_at = datetime.datetime.fromisoformat(
                    json.load(f)["deleted_at"]
                ).timestamp()
        except Exception:
            # Unreadable tombstone: restart its clock instead of guessing an
            # age (a purge must never fire off a corrupt timestamp).
            warnings.append(
                f"Unreadable tombstone for '{name}' — deletion clock restarted."
            )
            if not dry_run:
                _write_tombstone(team, name, prod_db_name(team, name))
            continue
        if older_than_hours > 0 and now - deleted_at < older_than_hours * 3600:
            pending.append(name)
            continue
        if dry_run:
            purged.append(name)
            continue
        if locks is not None:
            try:
                locks.acquire_env(
                    prod_lock_key(team.team_id, name), operation="prod-purge"
                )
            except Exception:
                pending.append(name)
                continue
        try:
            client = client or get_client()
            db_dropped, destroy_warnings = _destroy_leftovers(
                client, settings, team, name, require_db_drop=True
            )
            warnings.extend(destroy_warnings)
            if db_dropped:
                purged.append(name)
                logger.info(
                    "Purged leftovers of deleted production '%s'",
                    name,
                    extra={"env_name": prod_env_name(name)},
                )
            else:
                pending.append(name)
        finally:
            if locks is not None:
                locks.release_env(prod_lock_key(team.team_id, name))

    return {
        "dry_run": dry_run,
        "purged": purged,
        "pending": pending,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Start / stop / restart
# ---------------------------------------------------------------------------


def start_production(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    from oduflow import production_registry

    production_registry.get_production(team, name)
    client = get_client()
    # The production DB must be up before Odoo.
    ensure_prod_infra(client, settings, force=True)
    container = _require_container(client, settings, team, name)
    container.start()
    logger.info("Production started", extra={"env_name": prod_env_name(name)})
    return {"name": name, "odoo_container": container.name, "status": "running"}


def stop_production(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    from oduflow import production_registry

    production_registry.get_production(team, name)
    client = get_client()
    container = _require_container(client, settings, team, name)
    container.stop()
    logger.info("Production stopped", extra={"env_name": prod_env_name(name)})
    return {"name": name, "odoo_container": container.name, "status": "stopped"}


def restart_production(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    from oduflow import production_registry

    production_registry.get_production(team, name)
    client = get_client()
    container = _require_container(client, settings, team, name)
    container.restart()
    logger.info("Production restarted", extra={"env_name": prod_env_name(name)})
    return {"name": name, "odoo_container": container.name, "status": "running"}


# ---------------------------------------------------------------------------
# Update engine with automatic code rollback
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _worktree_heads(team: TeamSettings, name: str) -> dict[str, str]:
    """HEAD of every extra-addon worktree (rollback targets)."""
    from oduflow.git_ops import rev_parse

    heads: dict[str, str] = {}
    extra_dir = os.path.join(_workspace(team, name), "extra")
    if not os.path.isdir(extra_dir):
        return heads
    for repo_name in os.listdir(extra_dir):
        wt_path = os.path.join(extra_dir, repo_name)
        if os.path.isdir(os.path.join(wt_path, ".git")) or os.path.isfile(
            os.path.join(wt_path, ".git")
        ):
            try:
                heads[wt_path] = rev_parse(wt_path)
            except Exception:
                continue
    return heads


def _reset_code(repo_path: str, old_head: str, worktree_heads: dict[str, str]) -> None:
    """git reset --hard the main repo and every extra worktree."""
    from oduflow.git_ops import reset_hard

    reset_hard(repo_path, old_head)
    for wt_path, head in worktree_heads.items():
        try:
            reset_hard(wt_path, head)
        except Exception as exc:
            logger.warning("Could not reset worktree %s: %s", wt_path, exc)


def _worktrees_for_commit(
    team: TeamSettings, name: str, target: str
) -> dict[str, str] | None:
    """Extra-addon worktree HEADs recorded for the deploy that produced
    *target* (most recent match), or None when no deploy recorded them (e.g.
    a manual rollback to an arbitrary commit)."""
    for entry in reversed(read_deploys(team, name, limit=0)):
        worktrees = entry.get("worktrees")
        if entry.get("to_commit") == target and isinstance(worktrees, dict):
            return {str(k): str(v) for k, v in worktrees.items()}
    return None


def update_production(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    install: list[str] | None = None,
    upgrade: list[str] | None = None,
    restart: bool = False,
    trigger: str = "mcp",
) -> dict[str, Any]:
    """Pull the production's branch and apply the right action — with
    automatic CODE rollback on failure.

    Reuses the shared pull→classify→apply engine
    (:func:`env_ops.pull_environment`), with production semantics on top:

    - ``refresh`` is promoted to ``restart`` (no ``--dev=xml`` in prod —
      any changed file Odoo loads requires at least a container restart).
      A ``none`` outcome (Markdown-only changes) is left alone: there is
      nothing for the server to pick up;
    - the deploy is verified (module exit codes + in-container health
      poll); on failure the checkout (and extra worktrees) are reset to
      the pre-update commits, the conf is re-applied and the container
      restarted. The DATABASE is never rolled back automatically —
      restoring a snapshot is a manual, explicit operation;
    - every outcome lands in deploys.json and the registry flags.

    The caller must hold the production's lock.
    """
    from oduflow import production_registry
    from oduflow.docker_ops.env_ops import pull_environment
    from oduflow.git_ops import rev_parse

    production_registry.get_production(team, name)  # NotFoundError if absent
    client = get_client()
    env_name = prod_env_name(name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    if not os.path.isdir(repo_path):
        raise NotFoundError(
            f"Production '{name}' has no repository checkout at {repo_path}."
        )
    container = _require_container(client, settings, team, name)

    ts_start = _now_iso()
    old_head = rev_parse(repo_path)
    old_worktrees = _worktree_heads(team, name)
    production_registry.update_production(team, name, {"deploy_in_progress": True})

    deploy: dict[str, Any] = {
        "ts_start": ts_start,
        "trigger": trigger,
        "from_commit": old_head,
        "to_commit": old_head,
        "action": "none",
        "modules_installed": [],
        "modules_upgraded": [],
        "exit_code": 0,
        "status": "success",
        "error": "",
        "changed_files_count": 0,
    }

    try:
        for hook in list(pre_update_hooks):
            try:
                hook(settings, team, name)
            except Exception as exc:
                logger.warning(
                    "pre-update hook %s failed (deploy continues): %s",
                    getattr(hook, "__name__", hook),
                    exc,
                )

        result = pull_environment(
            settings,
            team,
            env_name,
            install=install,
            upgrade=upgrade,
            restart=restart,
        )
        new_head = rev_parse(repo_path)
        deploy.update(
            {
                "to_commit": new_head,
                "action": result.get("action", "none"),
                "modules_installed": result.get("modules_installed", []),
                "modules_upgraded": result.get("modules_upgraded", []),
                "exit_code": int(result.get("exit_code", 0) or 0),
                "changed_files_count": len(result.get("changed_files", []) or []),
            }
        )

        if result.get("action") == "none" and new_head == old_head:
            # Nothing pulled, nothing applied — not a deploy.
            production_registry.update_production(
                team, name, {"deploy_in_progress": False}
            )
            return {**result, "name": name, "commit": new_head}

        # Production runs without --dev=xml: a "refresh" outcome (XML/JS
        # only) still requires a restart to serve the new code.
        if result.get("action") == "refresh":
            container.restart()
            deploy["action"] = "restart"
            result["action"] = "restart"
            result["message"] = (
                "Changes applied; container restarted (production serves "
                "without --dev=xml)."
            )

        ok = deploy["exit_code"] == 0 and wait_production_healthy(
            client, settings, team, name, timeout=180
        )
        if ok:
            production_registry.update_production(
                team, name, {"deploy_in_progress": False, "unhealthy": False}
            )
            deploy["ts_end"] = _now_iso()
            # Extra-addon worktree HEADs at this deployed state (pull advanced
            # them), so a later rollback to new_head reverts them in lockstep.
            deploy["worktrees"] = _worktree_heads(team, name)
            append_deploy(team, name, deploy)
            return {
                **result,
                "name": name,
                "commit": new_head,
                "deploy": deploy,
            }

        # ------------------------- rollback (code only) -------------------
        logger.error(
            "Production '%s' deploy failed (exit_code=%s) — rolling back code %s -> %s",
            name,
            deploy["exit_code"],
            new_head[:10],
            old_head[:10],
        )
        rollback_error = ""
        try:
            _reset_code(repo_path, old_head, old_worktrees)
            reapply_prod_odoo_conf(settings, team, name, container)
            container.restart()
            recovered = wait_production_healthy(
                client, settings, team, name, timeout=120
            )
        except Exception as exc:
            recovered = False
            rollback_error = str(exc)

        deploy["ts_end"] = _now_iso()
        if recovered:
            deploy["status"] = "rolled_back"
            deploy["error"] = (
                f"Deploy failed (exit_code={deploy['exit_code']}); code "
                f"reverted to {old_head[:10]}."
            )
            production_registry.update_production(
                team, name, {"deploy_in_progress": False, "unhealthy": False}
            )
            append_deploy(team, name, deploy)
            return {
                "action": "rolled_back",
                "name": name,
                "commit": old_head,
                "failed_commit": new_head,
                "exit_code": deploy["exit_code"],
                "output": result.get("output", ""),
                "deploy": deploy,
                "message": (
                    f"Deploy of {new_head[:10]} FAILED; code was rolled back "
                    f"to {old_head[:10]} and the production is healthy again. "
                    "The DATABASE was NOT rolled back — if module upgrades "
                    "left it inconsistent, restore a snapshot manually "
                    "(restore_production)."
                ),
            }

        deploy["status"] = "rollback_failed"
        deploy["error"] = rollback_error or (
            "Rollback restart did not become healthy within 120s."
        )
        production_registry.update_production(
            team, name, {"deploy_in_progress": False, "unhealthy": True}
        )
        append_deploy(team, name, deploy)
        return {
            "action": "rollback_failed",
            "name": name,
            "commit": old_head,
            "failed_commit": new_head,
            "exit_code": deploy["exit_code"],
            "output": result.get("output", ""),
            "deploy": deploy,
            "message": (
                f"Deploy of {new_head[:10]} FAILED and the rollback to "
                f"{old_head[:10]} did not recover either — the production is "
                "marked UNHEALTHY. The container is left running for "
                "diagnosis (production_logs). The database was not touched."
            ),
        }
    except BaseException as exc:
        # Unexpected failure (network, docker, ...): record and re-flag. Only
        # mark unhealthy if the running container is actually not serving — a
        # transient pull failure (e.g. a GitHub blip on an auto_update deploy)
        # leaves the code unchanged and the site up, and must not flag a
        # healthy production (the flag would otherwise stick, since a later
        # no-op poll does not clear it).
        deploy["ts_end"] = _now_iso()
        deploy["status"] = "error"
        deploy["error"] = str(exc)
        try:
            serving = wait_production_healthy(client, settings, team, name, timeout=15)
        except Exception:
            serving = False
        production_registry.update_production(
            team, name, {"deploy_in_progress": False, "unhealthy": not serving}
        )
        append_deploy(team, name, deploy)
        raise


def rollback_production(
    settings: Settings,
    team: TeamSettings,
    name: str,
    to_commit: str = "",
    *,
    trigger: str = "mcp",
) -> dict[str, Any]:
    """Manual code-only rollback to *to_commit* (default: the previous
    deploy's starting commit). The caller must hold the production's lock."""
    from oduflow import production_registry
    from oduflow.git_ops import rev_parse

    production_registry.get_production(team, name)
    client = get_client()
    env_name = prod_env_name(name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    container = _require_container(client, settings, team, name)

    current = rev_parse(repo_path)
    target = (to_commit or "").strip()
    if not target:
        deploys = read_deploys(team, name, limit=0)
        # Latest deploy that actually moved the code forward.
        for entry in reversed(deploys):
            if entry.get("from_commit") and entry["from_commit"] != current:
                target = entry["from_commit"]
                break
    if not target:
        raise PrerequisiteNotMetError(
            f"No previous commit recorded for production '{name}'. Pass "
            "to_commit explicitly (see get_production_info commits)."
        )
    # Validate the target exists in the checkout before resetting. The
    # ^{commit} peel forces git to resolve the object (a bare 40-hex sha
    # would otherwise "parse" without existing).
    try:
        target = rev_parse(repo_path, f"{target}^{{commit}}")
    except Exception:
        raise NotFoundError(f"Commit '{target}' not found in the production checkout.")

    ts_start = _now_iso()
    # Revert extra-addon worktrees in lockstep with the main repo when the
    # target deploy recorded their HEADs; otherwise reset only the main
    # checkout (an arbitrary manual commit has no recorded worktree state).
    matched_worktrees = _worktrees_for_commit(team, name, target)
    _reset_code(repo_path, target, matched_worktrees or {})
    worktree_note = ""
    if matched_worktrees is None and _worktree_heads(team, name):
        worktree_note = (
            " Extra-addon worktrees were left at their current HEAD (no "
            "recorded worktree state for this commit)."
        )
    reapply_prod_odoo_conf(settings, team, name, container)
    container.restart()
    healthy = wait_production_healthy(client, settings, team, name, timeout=120)

    production_registry.update_production(team, name, {"unhealthy": not healthy})
    deploy = {
        "ts_start": ts_start,
        "ts_end": _now_iso(),
        "trigger": trigger,
        "from_commit": current,
        "to_commit": target,
        "action": "rollback",
        "exit_code": 0,
        "status": "success" if healthy else "rollback_failed",
        "error": "" if healthy else "Health check failed after rollback.",
    }
    append_deploy(team, name, deploy)
    return {
        "action": "rollback",
        "name": name,
        "commit": target,
        "previous_commit": current,
        "healthy": healthy,
        "message": (
            f"Code rolled back {current[:10]} -> {target[:10]}"
            + ("." if healthy else ", but the health check FAILED — check logs.")
            + worktree_note
        ),
    }


# ---------------------------------------------------------------------------
# List / info / logs
# ---------------------------------------------------------------------------


def _runtime_status(container: Any, record: dict[str, Any]) -> str:
    if record.get("deploy_in_progress"):
        return "deploying"
    if record.get("unhealthy"):
        return "unhealthy"
    if container is None:
        return "broken"
    return "running" if container.status == "running" else "stopped"


def list_productions(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    """Registry records merged with runtime facts (container status, HEAD)."""
    from oduflow import production_registry
    from oduflow.git_ops import rev_parse

    client = get_client()
    result = []
    for name, record in sorted(production_registry.list_productions(team).items()):
        container = _get_container(client, settings, team, name)
        head = ""
        repo_path = get_repo_path(prod_env_name(name), team.workspaces_dir)
        if os.path.isdir(repo_path):
            try:
                head = rev_parse(repo_path)
            except Exception:
                head = ""
        deploys = read_deploys(team, name, limit=1)
        result.append(
            {
                "name": name,
                "domain": record.get("domain", ""),
                "url": prod_url(settings, team, record),
                "status": _runtime_status(container, record),
                "repo_url": record.get("repo_url", ""),
                "branch": record.get("branch", ""),
                "odoo_image": record.get("odoo_image", ""),
                "auto_update": bool(record.get("auto_update")),
                "allow_copy_to_dev_mcp": bool(
                    record.get("allow_copy_to_dev_mcp", True)
                ),
                "commit": head,
                "commit_short": head[:10],
                "created_at": record.get("created_at", ""),
                "db_name": prod_db_name(team, name),
                "last_deploy": deploys[-1] if deploys else None,
                "backup": record.get("backup", {}),
            }
        )
    return result


def get_production_info(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    from oduflow import production_registry
    from oduflow.git_ops import log_commits, rev_parse

    record = production_registry.get_production(team, name)
    client = get_client()
    container = _get_container(client, settings, team, name)
    env_name = prod_env_name(name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)

    head = ""
    commits: list[dict[str, Any]] = []
    if os.path.isdir(repo_path):
        try:
            head = rev_parse(repo_path)
            commits = log_commits(repo_path, n=20)
        except Exception:
            pass

    healthy = None
    if container is not None and container.status == "running":
        try:
            healthy = _probe_odoo_health(container)
        except Exception:
            healthy = None

    return {
        "name": name,
        "domain": record.get("domain", ""),
        "url": prod_url(settings, team, record),
        "status": _runtime_status(container, record),
        "healthy": healthy,
        "repo_url": record.get("repo_url", ""),
        "branch": record.get("branch", ""),
        "odoo_image": record.get("odoo_image", ""),
        "git_user": record.get("git_user", ""),
        "extra_addons": record.get("extra_addons", {}),
        "auto_update": bool(record.get("auto_update")),
        "odoo_conf": record.get("odoo_conf", {}),
        "allow_copy_to_dev_mcp": bool(record.get("allow_copy_to_dev_mcp", True)),
        "unhealthy_flag": bool(record.get("unhealthy")),
        "deploy_in_progress": bool(record.get("deploy_in_progress")),
        "created_at": record.get("created_at", ""),
        "db_name": prod_db_name(team, name),
        "database_container": settings.prod_db_container,
        "workspace": _workspace(team, name),
        "odoo_container": _odoo_container_name(settings, team, name),
        "container_status": container.status if container is not None else "missing",
        "commit": head,
        "commits": commits,
        "deploys": read_deploys(team, name, limit=5),
        "backup": record.get("backup", {}),
    }


def production_logs(
    settings: Settings,
    team: TeamSettings,
    name: str,
    n_lines: int = 100,
    grep: str = "",
    level: str = "",
) -> str:
    from oduflow import production_registry
    from oduflow.docker_ops.odoo_ops import get_environment_logs

    production_registry.get_production(team, name)
    return get_environment_logs(
        settings,
        prod_env_name(name),
        n_lines=n_lines,
        grep=grep,
        level=level,
        team=team,
    )
