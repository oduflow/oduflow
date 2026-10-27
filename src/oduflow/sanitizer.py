import glob as glob_mod
import logging
import os
import re
from typing import Any

import docker
from docker import DockerClient
from oduflow.docker_ops.system_ops import _exec_sql
from oduflow.env_credentials import MissingCredentialsError, load_credentials
from oduflow.naming import get_db_name, get_repo_path, get_resource_name
from oduflow.odoo_version import major_from_container_labels
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def _detect_odoo_major_from_container(
    container: object, image_label: str
) -> int | None:
    """Best-effort major version from the Odoo image label.

    Thin wrapper over the shared parser in :mod:`oduflow.odoo_version`; kept as
    a local name because the neutralize path only ever has the label to go on
    (no live exec) and callers here pass a possibly non-string label.
    """
    return major_from_container_labels(container, image_label)


def _run_scripts_from_dir(
    sanitize_dir: str,
    label: str,
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_db: str,
    env_name: str,
    *,
    runner: Any = None,
) -> list[str]:
    """Run .sql and .py sanitization scripts from a directory.

    Returns a list of human-readable log lines.
    """
    logs: list[str] = []
    if not os.path.isdir(sanitize_dir):
        return logs

    # Escape the directory part: the path contains user-chosen names (the
    # environment subfolder, team data dir, repo checkout) whose []?* would
    # otherwise be read as glob patterns and skip or mis-scope scripts.
    escaped_dir = glob_mod.escape(sanitize_dir)
    # Environment subfolders can also end in .sql or .py. Exclude directories,
    # while keeping script symlinks (including broken links, which must warn).
    sql_files = sorted(
        path
        for path in glob_mod.glob(os.path.join(escaped_dir, "*.sql"))
        if not os.path.isdir(path)
    )
    py_files = sorted(
        path
        for path in glob_mod.glob(os.path.join(escaped_dir, "*.py"))
        if not os.path.isdir(path)
    )
    if not sql_files and not py_files:
        return logs

    # Both .sql and .py scripts are repo-controlled, so they run as the
    # environment's scoped, non-superuser PostgreSQL role — never the shared
    # cluster superuser. The superuser could COPY ... TO PROGRAM (host RCE) and
    # reach every team's database; the scoped role is confined to this
    # environment's own database. If the environment predates scoped
    # credentials, skip sanitization entirely rather than fall back to the
    # superuser (recreate/update the environment to provision a role).
    try:
        creds = load_credentials(
            env_name,
            team.workspaces_dir,
            settings.db_user,
            settings.db_password,
            allow_fallback=False,
        )
    except MissingCredentialsError:
        warning = (
            f"[SANITIZE:{label}] WARNING: no scoped database credentials; "
            "skipping sanitize scripts (recreate or update the environment to "
            "provision a per-environment PostgreSQL role)"
        )
        logger.warning(warning)
        logs.append(warning)
        return logs

    # .sql scripts (run against the env database as the scoped role)
    for sql_file in sql_files:
        name = os.path.basename(sql_file)
        try:
            with open(sql_file) as f:
                sql = f.read().strip()
            if not sql:
                continue
            # The scoped role confines SQL, but psql itself interprets
            # backslash metacommands client-side: `\! cmd` runs a shell inside
            # the shared PostgreSQL container no matter which role executes the
            # script. Reject any script with a line that starts with a
            # backslash — sanitize scripts are plain SQL and never need psql
            # metacommands.
            if re.search(r"^\s*\\", sql, re.MULTILINE):
                warning = (
                    f"[SANITIZE:{label}] WARNING: {name} skipped: psql "
                    "backslash metacommands are not allowed in sanitize scripts"
                )
                logger.warning(warning)
                logs.append(warning)
                continue
            _exec_sql(client, settings, sql, db=env_db, user=creds["pg_user"])
            logger.info("[%s] Executed sanitize script %s", label, name)
            logs.append(f"[SANITIZE:{label}] Executed {name}")
        except Exception as exc:
            logger.warning("[%s] Sanitize script %s failed: %s", label, name, exc)
            logs.append(f"[SANITIZE:{label}] WARNING: {name} failed: {exc}")

    # .py scripts (executed inside the Odoo container)
    if not py_files:
        return logs

    import docker as _docker

    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        container = (
            runner if runner is not None else client.containers.get(odoo_container_name)
        )
    except _docker.errors.NotFound:
        logs.append(
            f"[SANITIZE:{label}] WARNING: container not found, skipping .py scripts"
        )
        return logs

    for py_file in py_files:
        name = os.path.basename(py_file)
        try:
            with open(py_file) as f:
                script = f.read()
            exit_code, output = container.exec_run(
                ["python3", "-c", script],
                environment={
                    "ODOO_DB": env_db,
                    "DB_HOST": settings.shared_db_container,
                    "DB_USER": creds["pg_user"],
                    "DB_PASSWORD": creds["pg_password"],
                },
            )
            output_str = (
                output.decode("utf-8") if isinstance(output, bytes) else str(output)
            )
            if exit_code != 0:
                logger.warning(
                    "[%s] Sanitize script %s failed (exit %d): %s",
                    label,
                    name,
                    exit_code,
                    output_str,
                )
                logs.append(
                    f"[SANITIZE:{label}] WARNING: {name} failed (exit {exit_code})"
                )
            else:
                logger.info("[%s] Executed sanitize script %s", label, name)
                logs.append(f"[SANITIZE:{label}] Executed {name}")
        except Exception as exc:
            logger.warning("[%s] Sanitize script %s failed: %s", label, name, exc)
            logs.append(f"[SANITIZE:{label}] WARNING: {name} failed: {exc}")

    return logs


def _run_scripts_for_scope(
    sanitize_dir: str,
    label: str,
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_db: str,
    env_name: str,
    *,
    runner: Any = None,
) -> list[str]:
    """Run a sanitize directory's scripts, then its env-named subfolder's.

    Files directly in ``sanitize_dir`` run for every environment. A
    subdirectory whose name equals the environment name *exactly* (no globs;
    ``/`` in the environment name folds to ``-``, as in every other
    filesystem identity derived from it) holds scripts for that environment
    only: its ``.sql``/``.py`` files run after the top-level files, with the
    same alphabetical .sql-then-.py order within the subfolder. Every other subdirectory is ignored — other
    environments' folders, and ``_``/``.``-prefixed folders holding shared
    assets (e.g. ``_steps/``) that env folders symlink into. Symlinked scripts
    are read through the link; the link's own filename decides ordering and
    appears in the logs.

    Returns a list of human-readable log lines.
    """
    logs = _run_scripts_from_dir(
        sanitize_dir, label, client, settings, team, env_db, env_name, runner=runner
    )

    # Branch-like environment names may contain "/"; their filesystem identity
    # folds it to "-" (exactly as get_workspace_path does), so the subfolder is
    # named after the folded form: environment "feature/x" -> "feature-x/".
    folder = env_name.replace("/", "-")
    # Underscore/dot names are reserved for shared assets, and a separator
    # would escape the sanitize directory; such names never map to a subfolder.
    if (
        not folder
        or folder.startswith(("_", "."))
        or os.sep in folder
        or (os.altsep and os.altsep in folder)
    ):
        return logs
    env_dir = os.path.join(sanitize_dir, folder)
    if not os.path.isdir(env_dir):
        return logs

    note = (
        f"[SANITIZE:{label}] Subfolder {env_dir} matches environment "
        f"'{env_name}'; running its scripts"
    )
    logger.info(note)
    logs.append(note)
    logs.extend(
        _run_scripts_from_dir(
            env_dir,
            f"{label}/{folder}",
            client,
            settings,
            team,
            env_db,
            env_name,
            runner=runner,
        )
    )
    return logs


def neutralize_environment(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    runner: Any = None,
    database: str | None = None,
) -> list[str]:
    """Neutralize the environment database using Odoo's native mechanism.

    Runs ``odoo-bin neutralize`` inside the serving container. Odoo applies
    every installed module's ``data/neutralize.sql`` in a single transaction:
    it deactivates outgoing mail servers, disables crons (except autovacuum),
    disables payment providers, scrubs third-party API credentials, neutralizes
    webhooks, and sets the ``database.is_neutralized`` flag. Because it runs in
    the serving container, Odoo sees the full ``addons_path`` (base image + repo
    + extra addons), so custom modules shipping their own ``neutralize.sql`` are
    covered too — a partial ``addons_path`` would silently skip them.

    This is the baseline sanitization layer (block anything going out / phoning
    home); per-project ``.oduflow/odoo_sanitize`` and per-team scripts (e.g.
    PII anonymization) run on top of it via :func:`sanitize_environment`.

    Note: neutralization leaves ``database.uuid`` and ``database.enterprise_code``
    untouched — it only stops transmission by disabling crons; it does not
    change the database's identity.

    Odoo 15 and earlier do not provide the native ``neutralize`` CLI command,
    so it is skipped for those versions. Custom sanitization scripts still run
    afterwards.

    Returns a list of human-readable log lines.
    """
    import docker as _docker

    env_db = database or get_db_name(env_name, team.team_id)
    logs: list[str] = []

    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        container = (
            runner if runner is not None else client.containers.get(odoo_container_name)
        )
    except _docker.errors.NotFound:
        logger.warning("[NEUTRALIZE] container not found, skipping")
        logs.append("[NEUTRALIZE] WARNING: container not found, skipping")
        return logs

    image_label = getattr(settings, "image_label", "")
    major = (
        _detect_odoo_major_from_container(container, image_label)
        if isinstance(image_label, str)
        else None
    )
    if major is not None and major <= 15:
        logger.info("[NEUTRALIZE] skipped for Odoo %s", major)
        logs.append(
            f"[NEUTRALIZE] Skipped: Odoo {major} does not provide native neutralize"
        )
        return logs

    try:
        exit_code, output = container.exec_run(
            f"/entrypoint.sh odoo neutralize -d {env_db}"
        )
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
        if exit_code != 0:
            logger.warning("[NEUTRALIZE] failed (exit %d): %s", exit_code, output_str)
            logs.append(
                f"[NEUTRALIZE] WARNING: neutralization failed (exit {exit_code}); "
                "database may still send mail or phone home"
            )
        else:
            logger.info("[NEUTRALIZE] Database %s neutralized", env_db)
            logs.append(
                "[NEUTRALIZE] Database neutralized "
                "(mail off, crons off, credentials scrubbed, is_neutralized=true)"
            )
    except Exception as exc:  # noqa: BLE001 - never let neutralize abort provisioning
        logger.warning("[NEUTRALIZE] failed: %s", exc)
        logs.append(f"[NEUTRALIZE] WARNING: neutralization failed: {exc}")

    return logs


def sanitize_environment(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    runner: Any = None,
    database: str | None = None,
) -> list[str]:
    """Sanitize environment database after provisioning.

    Runs sanitization scripts in two tiers:
    1. Team-level scripts from ``{data_dir}/odoo_sanitize/`` (managed by the
       team administrator, created during startup).
    2. Per-project scripts from the repository's
       ``.oduflow/odoo_sanitize/`` folder (managed by the developer).

    Both folders support ``.sql`` and ``.py`` files executed in alphabetical
    order. In both tiers, files directly in the folder run for every
    environment; a subfolder named exactly after the environment runs
    afterwards, for that environment only (see
    :func:`_run_scripts_for_scope`). The deprecated ``.odoo_sanitize``
    legacy folder keeps its old flat behaviour.

    Returns a list of human-readable log lines describing what was done.
    """
    env_db = database or get_db_name(env_name, team.team_id)
    logs: list[str] = []

    # --- Team-level sanitization ---
    system_dir = os.path.join(team.data_dir, "odoo_sanitize")
    logs.extend(
        _run_scripts_for_scope(
            system_dir,
            "system",
            client,
            settings,
            team,
            env_db,
            env_name,
            runner=runner,
        )
    )

    # --- Per-project sanitization from repo ---
    # Managed-clone environments keep their checkout under the Oduflow
    # workspace. Live-mount environments keep it elsewhere and record the real
    # host path on the serving container.
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        container = client.containers.get(odoo_container_name)
        labels = container.labels if isinstance(container.labels, dict) else {}
        repo_path = labels.get("oduflow.local_path") or repo_path
    except docker.errors.NotFound:
        logger.debug(
            "Could not resolve live-mount path for sanitize; using managed path",
        )

    # Compatibility for repositories created before project sanitization moved
    # under .oduflow/. Keep this runtime-only: new documentation points solely
    # at the canonical path.
    legacy_repo_dir = os.path.join(repo_path, ".odoo_sanitize")
    if os.path.isdir(legacy_repo_dir):
        # NOTICE, not WARNING: the scripts below still run. Callers treat a
        # WARNING line as a failed step (template_generations refuses to promote
        # sanitized data on one).
        notice = (
            "[SANITIZE:repo] NOTICE: Project sanitize scripts moved to "
            ".oduflow/odoo_sanitize; move scripts from .odoo_sanitize there."
        )
        logger.warning(notice)
        logs.append(notice)
        logs.extend(
            _run_scripts_from_dir(
                legacy_repo_dir,
                "repo-legacy",
                client,
                settings,
                team,
                env_db,
                env_name,
                runner=runner,
            )
        )

    repo_dir = os.path.join(repo_path, ".oduflow", "odoo_sanitize")
    logs.extend(
        _run_scripts_for_scope(
            repo_dir,
            "repo",
            client,
            settings,
            team,
            env_db,
            env_name,
            runner=runner,
        )
    )

    return logs
