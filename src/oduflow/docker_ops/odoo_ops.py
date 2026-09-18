from __future__ import annotations

import csv
import logging
import os
import re
import secrets
from typing import Any

import docker
from oduflow import po_tools
from oduflow.docker_ops.client import get_client
from oduflow.env_credentials import load_credentials
from oduflow.errors import (
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.naming import (
    PROD_ENV_PREFIX,
    get_db_name,
    get_repo_path,
    get_resource_name,
    slugify_branch,
    validate_env_name,
)
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

# Odoo module technical names are Python identifiers ([a-z0-9_]). Validate before
# they enter an `odoo -i/-u/-u <modules>` command string: even though exec_run
# tokenizes via shlex.split (no shell), an unvalidated token like
# "base --load=..." would be split into a *separate* argv flag and smuggle
# arbitrary Odoo CLI options into the invocation (argument injection).
_MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _validate_module_names(modules: "list[str] | tuple[str, ...]") -> None:
    for m in modules:
        if not _MODULE_NAME_RE.match(m or ""):
            raise ValueError(
                f"Invalid module name '{m}': only letters, digits and "
                "underscores are allowed."
            )


def _detect_odoo_major(container: Any, image_label: str) -> int | None:
    """Best-effort major version of the Odoo running in *container*.

    Fast path: parse the version out of the image tag stored in the
    ``oduflow.image`` label (e.g. ``odoo:15.0`` → 15). For custom-tagged images
    that carry no version (e.g. ``oduist/customer_odoo``) it falls back to asking
    the already-running binary via ``odoo --version`` — authoritative and
    independent of the image name. Returns ``None`` if the version can't be
    determined.
    """
    image = container.labels.get(image_label, "")
    match = re.search(r"odoo[:/](\d+)", image)
    if match:
        return int(match.group(1))

    try:
        _code, out = container.exec_run("odoo --version")
        text = out.decode("utf-8") if isinstance(out, bytes) else str(out)
        match = re.search(r"(\d+)\.\d+", text)
        if match:
            return int(match.group(1))
    except Exception as exc:  # noqa: BLE001 - version detection is best-effort
        logger.warning("Could not detect Odoo version from container: %s", exc)
    return None


def _longpoll_port_flag(container: Any, image_label: str) -> str:
    """CLI flag for the test server's long-polling/gevent port, per Odoo version.

    Odoo 16.0 renamed ``--longpolling-port`` to ``--gevent-port``; the new flag
    does not exist on 15.0 and earlier (Odoo aborts on the unknown option), while
    the old name still works as a deprecated alias on 16+. Pick the flag the
    running Odoo actually understands, defaulting to the modern ``--gevent-port``
    when the version can't be determined.
    """
    major = _detect_odoo_major(container, image_label)
    if major is not None and major < 16:
        return "--longpolling-port"
    return "--gevent-port"


def _validate_test_tags(test_tags: str) -> None:
    """Reject anything that is not an Odoo ``--test-tags`` expression.

    Same argument-injection concern as module names: the value lands in a
    command string that ``exec_run`` tokenizes with ``shlex.split``, so a token
    containing whitespace would become a separate argv entry and smuggle extra
    Odoo CLI flags into the run. Odoo's own grammar
    (``[-][tag][/module][:Class][.method]``, comma-separated) needs no spaces.
    """
    if not re.match(r"^[A-Za-z0-9_,:./+*-]+$", test_tags):
        raise ValueError(
            f"Invalid test_tags '{test_tags}': expected a comma-separated Odoo "
            "test-tags expression such as '/my_module:TestClass.test_method' "
            "(no spaces)."
        )


def _scope_test_tags_without_upgrade(test_tags: str, module_list: list[str]) -> str:
    """Keep an upgrade-free test run inside the requested modules.

    Without ``-u`` Odoo considers post-install tests from every initialized
    module, so ``modules`` does not scope collection by itself. Positive tag
    selectors must therefore name one of the requested modules. An
    exclusion-only expression (for example ``-slow``) is prefixed with module
    selectors so it subtracts from the requested modules rather than from the
    entire registry.
    """
    if not test_tags:
        return ",".join(f"/{module}" for module in module_list)

    selectors = test_tags.split(",")
    positive_modules: list[str] = []
    for selector in selectors:
        if selector.startswith("-"):
            continue
        body = selector[1:] if selector.startswith("+") else selector
        if "/" not in body:
            raise ValueError(
                "With upgrade=False, positive test_tags must include a module "
                "scope, for example 'slow/sale' or '/sale:TestClass'."
            )
        module = body.split("/", 1)[1].split(":", 1)[0]
        if not module or module not in module_list:
            raise ValueError(
                f"test_tags selector '{selector}' is outside the requested "
                f"modules: {','.join(module_list)}."
            )
        positive_modules.append(module)

    if positive_modules:
        return test_tags
    module_scope = [f"/{module}" for module in module_list]
    return ",".join([*module_scope, *selectors])


def run_environment_tests(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    modules: str,
    test_tags: str = "",
    upgrade: bool = True,
) -> str:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    module_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not module_list:
        # An empty list would produce a bare `-u` (no value), or with
        # upgrade=False an unscoped run over every installed module's tests.
        raise ValueError("modules is required: name the modules to test.")
    _validate_module_names(module_list)
    if test_tags:
        _validate_test_tags(test_tags)
    if not upgrade:
        # Without an upgrade nothing narrows the run to the requested modules —
        # Odoo would sweep the post-install tests of every installed module.
        # Preserve an explicit module-qualified selector, or combine an
        # exclusion-only expression with derived module filters.
        test_tags = _scope_test_tags_without_upgrade(test_tags, module_list)
    # allow_fallback=False: tests run tenant-controlled code inside the odoo
    # container, so the shared superuser password must never be exposed there
    # (cross-tenant DB access on legacy environments predating per-env
    # credentials). The per-env password itself is passed via the PGPASSWORD env
    # var (see exec_run below), not on the odoo CLI, so it stays out of the
    # container's process argv (`ps`).
    creds = load_credentials(
        env_name,
        team.workspaces_dir,
        settings.db_user,
        settings.db_password,
        allow_fallback=False,
    )
    # Use -u (upgrade), not -i (install): the module is already installed in the
    # template, and -i on an installed module is a no-op that never enters the test
    # phase (→ "0 of 0 tests"). -u re-runs the module's tests on the existing DB.
    #
    # upgrade=False drops -u for a fast re-run. Odoo then takes the test set from
    # `registry._init_modules` instead of `registry.updated_modules` and builds a
    # 'post_install' suite only (odoo/service/server.py, identical on 15–19), so
    # at_install tests — the default position for TestCase/TransactionCase — are
    # NOT collected. Keep the default (-u) unless the target class is post_install.
    #
    # --no-http has no effect under --test-enable (tests need a live HTTP server),
    # so instead of disabling HTTP we move the test server's HTTP and gevent ports
    # off the defaults (8069/8072) already held by the running Odoo container.
    # Odoo 16.0 renamed --longpolling-port to --gevent-port, so pick the flag the
    # environment's Odoo version actually understands.
    port_flag = _longpoll_port_flag(container, settings.image_label)
    upgrade_flag = f"-u {modules} " if upgrade else ""
    # --test-tags=VALUE (not a separate argv token): a leading '-' in an
    # exclusion expression like '-slow' would otherwise parse as a CLI flag.
    tags_flag = f"--test-tags={test_tags} " if test_tags else ""
    cmd = (
        f"odoo --test-enable --stop-after-init --workers 0 "
        f"--http-port 8089 {port_flag} 8090 {upgrade_flag}{tags_flag}"
        f"--db_host={settings.shared_db_container} "
        f"-r {creds['pg_user']} "
        f"--database={env_db}"
    )
    logger.info(
        "Running tests",
        extra={
            "env_name": env_name,
            "modules": modules,
            "test_tags": test_tags,
            "upgrade": upgrade,
        },
    )
    exit_code, output = container.exec_run(
        cmd, environment={"PGPASSWORD": creds["pg_password"]}
    )

    if isinstance(output, bytes):
        return output.decode("utf-8")
    return str(output)


def get_environment_logs(
    settings: Settings,
    env_name: str,
    n_lines: int = 100,
    grep: str = "",
    level: str = "",
    container_name: str = "",
    *,
    team: TeamSettings,
) -> str:
    client = get_client()
    if container_name:
        # Only allow containers that belong to this environment.
        env_prefix = get_resource_name(env_name, "", settings.prefix, team.team_id)
        if not container_name.startswith(env_prefix):
            raise NotFoundError(
                f"Container '{container_name}' does not belong to "
                f"environment '{env_name}'."
            )
        target_container_name = container_name
    else:
        target_container_name = get_resource_name(
            env_name, "odoo", settings.prefix, team.team_id
        )

    # Fetch more lines when filtering to have meaningful results
    fetch_lines = min(n_lines * 10, 10_000) if (grep or level) else n_lines

    try:
        container = client.containers.get(target_container_name)
    except docker.errors.NotFound:
        if container_name:
            raise NotFoundError(f"Container '{container_name}' does not exist.")
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    # Defence in depth on top of team-scoped names: never expose another
    # team's logs even if a name resolves unexpectedly (issue #39).
    label = container.labels.get(settings.team_label)
    if label is not None and label != team.team_id:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    logs = container.logs(tail=fetch_lines, stdout=True, stderr=True)
    logs_str = logs.decode("utf-8") if isinstance(logs, bytes) else str(logs)

    if grep or level:
        lines = logs_str.splitlines()
        filtered = []
        for line in lines:
            if level and f" {level} " not in line.upper():
                continue
            if grep and grep.lower() not in line.lower():
                continue
            filtered.append(line)
        return "\n".join(filtered[-n_lines:])

    return logs_str


def _run_odoo_module_command(
    settings: Settings, team: TeamSettings, env_name: str, flag: str, *modules: str
) -> dict[str, Any]:
    if not modules:
        raise ValueError("At least one module name is required.")
    _validate_module_names(modules)

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    if flag == "-u":
        _require_upgradeable_modules(settings, team, env_name, modules)

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d {env_db} --stop-after-init --no-http {flag} {modules_str}"

    action = "Installing" if flag == "-i" else "Upgrading"
    logger.info(
        "%s modules",
        action,
        extra={"env_name": env_name, "modules": modules_str},
    )
    exit_code, output = container.exec_run(cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    if flag == "-i" and exit_code == 0:
        _require_installed_modules(settings, team, env_name, modules)

    return {
        "modules": list(modules),
        "exit_code": exit_code,
        "output": output_str,
    }


def _get_module_states(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    modules: tuple[str, ...],
) -> dict[str, str]:
    names = ", ".join(f"'{module}'" for module in modules)
    # This is a fixed, validated SELECT, so legacy environments may safely use
    # the cluster credential that their Odoo container already runs with.
    result = _execute_db_query(
        settings,
        team,
        env_name,
        f"SELECT name, state FROM ir_module_module WHERE name IN ({names})",
        allow_fallback=True,
    )
    states: dict[str, str] = {}
    for row in result["output"].splitlines()[1:]:
        name, separator, state = row.strip().partition(",")
        if separator:
            states[name] = state
    return states


def _require_installed_modules(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    modules: tuple[str, ...],
) -> None:
    """Verify that an exit-code-0 ``odoo -i`` actually installed every module."""
    states = _get_module_states(settings, team, env_name, modules)

    unknown = [module for module in modules if module not in states]
    if unknown:
        module_list = ", ".join(unknown)
        subject = "Module" if len(unknown) == 1 else "Modules"
        verb = "was" if len(unknown) == 1 else "were"
        raise NotFoundError(
            f"{subject} {module_list} {verb} not found in environment '{env_name}' "
            "after Odoo reported a successful installation. Check the module "
            "name, addons path, and manifest validity."
        )

    not_installed = [
        f"{module} ({states[module]})"
        for module in modules
        if states[module] != "installed"
    ]
    if not_installed:
        module_list = ", ".join(not_installed)
        subject = "Module" if len(not_installed) == 1 else "Modules"
        verb = "is" if len(not_installed) == 1 else "are"
        raise PrerequisiteNotMetError(
            f"{subject} {module_list} {verb} not installed in environment "
            f"'{env_name}' after Odoo reported a successful installation."
        )


def _require_upgradeable_modules(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    modules: tuple[str, ...],
) -> None:
    """Fail before ``odoo -u`` when a requested module is not installed.

    Odoo exits successfully when ``-u`` names an unknown or uninstalled module,
    even though it does not upgrade it. Checking the database first prevents both
    direct upgrades and ``pull_and_apply(upgrade=...)`` from reporting that no-op
    as a success.
    """
    states = _get_module_states(settings, team, env_name, modules)

    unknown = [module for module in modules if module not in states]
    if unknown:
        module_list = ", ".join(unknown)
        install_arg = ",".join(unknown)
        subject = "Module" if len(unknown) == 1 else "Modules"
        verb = "is" if len(unknown) == 1 else "are"
        pronoun = "it is" if len(unknown) == 1 else "they are"
        raise NotFoundError(
            f"{subject} {module_list} {verb} unknown in environment '{env_name}' "
            f"and cannot be upgraded. If {pronoun} new, install with "
            "install_odoo_modules or "
            f"pull_and_apply(install='{install_arg}')."
        )

    not_installed = [
        f"{module} ({states[module]})"
        for module in modules
        if states[module] != "installed"
    ]
    if not_installed:
        module_list = ", ".join(not_installed)
        install_arg = ",".join(
            module for module in modules if states[module] != "installed"
        )
        subject = "Module" if len(not_installed) == 1 else "Modules"
        verb = "is" if len(not_installed) == 1 else "are"
        pronoun = "it" if len(not_installed) == 1 else "them"
        raise PrerequisiteNotMetError(
            f"{subject} {module_list} {verb} not installed and cannot be upgraded. "
            f"Install {pronoun} first with install_odoo_modules or "
            f"pull_and_apply(install='{install_arg}')."
        )


def upgrade_odoo_modules(
    settings: Settings, team: TeamSettings, env_name: str, *modules: str
) -> dict[str, Any]:
    return _run_odoo_module_command(settings, team, env_name, "-u", *modules)


def install_odoo_modules(
    settings: Settings, team: TeamSettings, env_name: str, *modules: str
) -> dict[str, Any]:
    return _run_odoo_module_command(settings, team, env_name, "-i", *modules)


_FILE_SIZE_LIMIT = 100 * 1024  # 100KB


def read_file_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    path: str,
    read_range: str = "",
) -> dict[str, Any]:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    logger.info(
        "Reading file in environment",
        extra={"env_name": env_name, "path": path},
    )

    # Check if path exists and determine its type. Use argv-form `test` calls so
    # `path` is never concatenated into a shell string (no `sh -c` injection).
    if container.exec_run(["test", "-d", path])[0] == 0:
        path_type = "DIR"
    elif container.exec_run(["test", "-f", path])[0] == 0:
        path_type = "FILE"
    else:
        path_type = "NOTFOUND"

    if path_type == "NOTFOUND":
        return {"error": f"Path not found: {path}"}

    if path_type == "DIR":
        exit_code, output = container.exec_run(["ls", "-la", path])
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
        return {"type": "directory", "output": output_str}

    # It's a file — check if binary
    exit_code, output = container.exec_run(["file", "--mime", path])
    mime_str = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    if "charset=binary" in mime_str:
        return {
            "error": f"Binary file, cannot display: {path}\nUse run_odoo_command for binary file operations."
        }

    # Check file size
    exit_code, output = container.exec_run(["stat", "-c", "%s", path])
    size_str = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    try:
        file_size = int(size_str)
    except ValueError:
        file_size = 0

    if read_range:
        # Parse "START:END" format
        parts = read_range.split(":")
        if len(parts) != 2:
            return {
                "error": f"Invalid read_range format: '{read_range}'. Expected 'START:END' (e.g. '1:50')."
            }
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            return {
                "error": f"Invalid read_range format: '{read_range}'. START and END must be integers."
            }
        exit_code, output = container.exec_run(["sed", "-n", f"{start},{end}p", path])
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
        return {
            "type": "file",
            "output": output_str,
            "size": file_size,
            "range": read_range,
        }

    # No range — enforce size limit
    if file_size > _FILE_SIZE_LIMIT:
        size_kb = file_size // 1024
        limit_kb = _FILE_SIZE_LIMIT // 1024
        return {
            "error": (
                f"File is too large ({size_kb}KB, limit {limit_kb}KB). "
                f'Use read_range parameter to read a specific portion, e.g. read_range="1:500".'
            )
        }

    exit_code, output = container.exec_run(["cat", path])
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    return {"type": "file", "output": output_str, "size": file_size}


def run_command_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    command: str,
    user: str = "odoo",
    shell: bool = True,
) -> dict[str, Any]:
    """Run *command* inside the environment's Odoo container.

    By default the command goes through ``sh -c``, so pipes, redirections,
    ``&&``, ``cd x && y`` and variable expansion behave as written. Docker's
    bare exec has no shell: it splits the string with ``shlex.split`` and runs
    argv[0] directly, which silently passes ``|``/``>``/``&&`` to the program as
    literal arguments. Pass ``shell=False`` for exact argv semantics (no
    globbing, no expansion, metacharacters stay literal).
    """
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    logger.info(
        "Executing command in environment",
        extra={
            "env_name": env_name,
            "command": command,
            "user": user,
            "shell": shell,
        },
    )
    exit_code, output = container.exec_run(
        ["sh", "-c", command] if shell else command, user=user
    )
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return {
        "exit_code": exit_code,
        "output": output_str,
    }


def reset_admin_password(
    settings: Settings, team: TeamSettings, env_name: str, new_password: str = "test"
) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    # Hash the password using passlib inside the Odoo container
    hash_cmd = [
        "python3",
        "-c",
        "from passlib.context import CryptContext; "
        f"print(CryptContext(['pbkdf2_sha512']).hash({new_password!r}))",
    ]
    exit_code, output = container.exec_run(hash_cmd)
    hashed = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    if exit_code != 0:
        raise ExternalCommandError("python3 (passlib hash)", exit_code, hashed)

    # Update the password in the database
    try:
        db_container = client.containers.get(settings.shared_db_container)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Database container '{settings.shared_db_container}' is not running. "
            "System not initialized. Restart oduflow."
        )

    sql = f"UPDATE res_users SET password = '{hashed}' WHERE login = 'admin'"
    psql_cmd = ["psql", "-U", settings.db_user, "-d", env_db, "-c", sql]
    exit_code, output = db_container.exec_run(psql_cmd)
    output_str = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    if exit_code != 0:
        raise ExternalCommandError("psql", exit_code, output_str)

    logger.info(
        "Admin password reset",
        extra={"env_name": env_name},
    )
    return {"status": "ok", "login": "admin", "psql_output": output_str}


def _execute_db_query(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    query: str,
    output_format: str = "csv",
    *,
    allow_fallback: bool,
) -> dict[str, Any]:
    """Execute SQL; legacy fallback is only safe for fixed internal queries."""
    client = get_client()
    env_db = get_db_name(env_name, team.team_id)

    try:
        db_container = client.containers.get(settings.shared_db_container)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Database container '{settings.shared_db_container}' is not running. "
            "System not initialized. Restart oduflow."
        )

    creds = load_credentials(
        env_name,
        team.workspaces_dir,
        settings.db_user,
        settings.db_password,
        allow_fallback=allow_fallback,
    )
    if output_format == "human":
        cmd = ["psql", "-U", creds["pg_user"], "-d", env_db, "-c", query]
    else:
        cmd = ["psql", "-U", creds["pg_user"], "-d", env_db, "--csv", "-c", query]

    logger.info(
        "Running DB query",
        extra={"env_name": env_name, "format": output_format},
    )
    exit_code, output = db_container.exec_run(cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    if exit_code != 0:
        raise ExternalCommandError("psql", exit_code, output_str)

    return {
        "exit_code": exit_code,
        "output": output_str,
    }


def run_db_query(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    query: str,
    output_format: str = "csv",
) -> dict[str, Any]:
    """Run arbitrary SQL using only the environment's scoped credentials."""
    return _execute_db_query(
        settings,
        team,
        env_name,
        query,
        output_format,
        allow_fallback=False,
    )


def list_installed_module_records(
    settings: Settings, team: TeamSettings, env_name: str
) -> list[dict[str, str]]:
    """Return installed module names and versions using a fixed safe query.

    The query is server-controlled, so legacy environments may use the shared
    database credential without exposing an arbitrary-query surface.
    """
    validate_env_name(env_name)
    if slugify_branch(env_name).startswith(PROD_ENV_PREFIX):
        raise ValueError(
            f"'{env_name}' is a production environment. Use the production "
            "deployment workflow instead of dev module operations."
        )
    result = _execute_db_query(
        settings,
        team,
        env_name,
        "SELECT name, latest_version FROM ir_module_module "
        "WHERE state = 'installed' ORDER BY name",
        allow_fallback=True,
    )
    modules: list[dict[str, str]] = []
    for row in csv.DictReader(result.get("output", "").splitlines()):
        name = row.get("name") or ""
        if name:
            modules.append({"name": name, "version": row.get("latest_version") or ""})
    return modules


_WRITE_FILE_LIMIT = 1_000_000  # 1 MB


def write_file_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    path: str,
    content: str,
    user: str = "odoo",
    *,
    max_bytes: int = _WRITE_FILE_LIMIT,
) -> dict[str, Any]:
    """Write a text file inside the Odoo container via tar stream."""
    import io
    import tarfile

    if len(content.encode("utf-8")) > max_bytes:
        raise ExternalCommandError(
            "write_file", 1, f"Content exceeds {max_bytes} byte limit."
        )

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    parent = path.rsplit("/", 1)[0] if "/" in path else "/"
    mkdir_code, mkdir_output = container.exec_run(["mkdir", "-p", parent], user=user)
    if mkdir_code != 0:
        # Source checkouts are mounted root-owned, so the requested user may be
        # unable to create the first missing directory (commonly ``i18n``).
        # Find the highest missing directory and chown only the tree that this
        # operation creates; existing repository content must keep its owner.
        missing_root: str | None = None
        candidate = parent
        while candidate and candidate != "/":
            exists_code, _ = container.exec_run(["test", "-d", candidate], user="root")
            if exists_code == 0:
                break
            missing_root = candidate
            candidate = (
                candidate.rsplit("/", 1)[0] if "/" in candidate else "/"
            ) or "/"

        if missing_root is None:
            raise ExternalCommandError("mkdir -p", mkdir_code, _decode(mkdir_output))

        root_mkdir_code, root_mkdir_output = container.exec_run(
            ["mkdir", "-p", parent], user="root"
        )
        if root_mkdir_code != 0:
            raise ExternalCommandError(
                "mkdir -p", root_mkdir_code, _decode(root_mkdir_output)
            )

        if user != "root":
            chown_code, chown_output = container.exec_run(
                ["chown", "-R", user, missing_root], user="root"
            )
            if chown_code != 0:
                raise ExternalCommandError(
                    "chown created directory", chown_code, _decode(chown_output)
                )

    data = content.encode("utf-8")
    tar_stream = io.BytesIO()
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    try:
        container.put_archive(parent, tar_stream)
    except docker.errors.APIError as exc:
        raise ExternalCommandError(
            "write file archive",
            1,
            f"Could not write '{path}' inside the container. "
            f"Verify that parent directory '{parent}' exists and is writable: {exc}",
        ) from exc

    if user != "root":
        chown_code, chown_output = container.exec_run(
            ["chown", user, path], user="root"
        )
        if chown_code != 0:
            raise ExternalCommandError(
                "chown written file", chown_code, _decode(chown_output)
            )

    logger.info(
        "File written in environment",
        extra={"env_name": env_name, "path": path, "size": len(data)},
    )
    return {"path": path, "size": len(data)}


def _finalize_shell_script(python_code: str, auto_commit: bool) -> str:
    """Append an explicit commit to *python_code* when *auto_commit* is set.

    ``odoo shell`` rolls back its cursor when the piped script finishes (see
    ``odoo/cli/shell.py``), so ORM writes are discarded unless the script
    commits itself. The cursor is captured under a private name *before* the
    user code runs and committed through that handle afterwards. Committing via
    ``env.cr`` directly would break for an otherwise-valid script that rebinds
    ``env`` (e.g. ``env = os.environ`` while writing through ``self.env``),
    silently rolling the successful writes back; ``__oduflow_cr__`` is captured
    up front and is unlikely to be shadowed. If the user code raises, execution
    never reaches the commit and Odoo rolls back — matching the documented
    "exception → rollback" contract. ``env`` is always in scope inside
    ``odoo shell``.
    """
    if not auto_commit:
        return python_code
    return (
        "__oduflow_cr__ = env.cr\n"
        + python_code.rstrip("\n")
        + "\n__oduflow_cr__.commit()\n"
    )


def run_odoo_shell(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    python_code: str,
    auto_commit: bool = True,
) -> dict[str, Any]:
    """Execute Python code in the Odoo shell context with full ORM access."""
    import io
    import tarfile

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    creds = load_credentials(
        env_name,
        team.workspaces_dir,
        settings.db_user,
        settings.db_password,
        allow_fallback=False,
    )

    # Write Python code to temp file via tar stream (avoids shell escaping)
    script_path = "/tmp/_oduflow_shell_script.py"
    data = _finalize_shell_script(python_code, auto_commit).encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name="_oduflow_shell_script.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive("/tmp", tar_stream)

    cmd = (
        f"odoo shell --no-http --stop-after-init "
        f"--db_host={settings.shared_db_container} "
        f"-r {creds['pg_user']} "
        f"--database={env_db} "
        f"< {script_path}"
    )

    logger.info(
        "Running Odoo shell",
        extra={"env_name": env_name, "code_size": len(data)},
    )
    # Pass the per-env DB password via PGPASSWORD (libpq reads it) rather than
    # `-w` on the odoo CLI, keeping it out of the container's process argv (`ps`).
    exit_code, output = container.exec_run(
        ["sh", "-c", cmd],
        user="odoo",
        environment={"PGPASSWORD": creds["pg_password"]},
    )
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    # Cleanup temp file
    container.exec_run(["rm", "-f", script_path])

    return {
        "exit_code": exit_code,
        "output": output_str,
    }


# Odoo's http.SESSION_LIFETIME default (7 days) — used only if the running Odoo
# does not expose the constant to the mint script.
_DEFAULT_SESSION_LIFETIME = 60 * 60 * 24 * 7

_SENTINEL_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _extract_sentinel(output: str, key: str) -> str | None:
    """Return the value framed by ``__ODUFLOW_<KEY>__…__END__`` in *output*.

    ``odoo shell`` merges its banner and logging with script ``print()`` output on
    a single stream (stdout+stderr, ``demux=False``), so a printed value cannot be
    recovered by naive last-line parsing. The mint script frames each value with
    sentinels and this helper regex-extracts it. Returns ``None`` if absent.
    """
    pat = _SENTINEL_RE_CACHE.get(key)
    if pat is None:
        pat = re.compile(r"__ODUFLOW_" + re.escape(key) + r"__(.*?)__END__", re.DOTALL)
        _SENTINEL_RE_CACHE[key] = pat
    match = pat.search(output)
    return match.group(1) if match else None


def _build_connect_as_user_script(user: str) -> str:
    """Build the in-container script used by :func:`connect_as_user`.

    An empty *user* resolves the database's admin through ``base.user_admin``
    rather than a hardcoded id: the xml-id survives an archived or renamed admin,
    where ``id = 2`` would produce a misleading "user not found".
    """
    return f"""
import traceback
try:
    _sel = {user!r}
    Users = env['res.users'].sudo()
    if not _sel:
        _admin = env.ref('base.user_admin', raise_if_not_found=False)
        u = _admin.sudo() if _admin else Users.browse()
    else:
        u = Users.search([('login', '=', _sel)], limit=1)
        if not u and _sel.isdigit():
            u = Users.search([('id', '=', int(_sel))], limit=1)
    if not u:
        print('__ODUFLOW_ERR__NOTFOUND:' + (_sel or 'admin (base.user_admin)') + '__END__')
    else:
        user_env = env(user=u.id)
        user = user_env['res.users'].browse(u.id)
        user_context = dict(user_env['res.users'].context_get() or {{}})
        user_context['uid'] = user.id
        store = odoo.http.root.session_store
        s = store.new()
        s.update({{
            'db': env.cr.dbname,
            'login': user.login,
            'uid': user.id,
            'session_token': user._compute_session_token(s.sid),
            'context': user_context,
        }})
        store.save(s)
        print('__ODUFLOW_SID__' + s.sid + '__END__')
        print('__ODUFLOW_LOGIN__' + user.login + '__END__')
        print('__ODUFLOW_UID__' + str(user.id) + '__END__')
        try:
            print('__ODUFLOW_TTL__' + str(int(odoo.http.SESSION_LIFETIME)) + '__END__')
        except Exception:
            pass
except Exception:
    print('__ODUFLOW_ERR__' + traceback.format_exc() + '__END__')
"""


def connect_as_user(
    settings: Settings, team: TeamSettings, env_name: str, user: str
) -> dict[str, Any]:
    """Mint an Odoo login session for *user* server-side (issue #78).

    Runs a small script in ``odoo shell`` that creates a session and sets the
    exact internal state a password login produces (``db``/``login``/``uid``/
    ``session_token``/``context``), then persists it to the filesystem session
    store the live HTTP server shares (same container, same data dir). The server
    honours it on the next request carrying that ``session_id`` cookie — no
    password is created, transmitted, or stored (Odoo.sh-style "Connect as user").

    *user* is a login string or a numeric user id. The session id is returned
    framed-and-parsed via :func:`_extract_sentinel` because ``odoo shell`` output
    is noisy. The mint runs inside try/except and prints a sentinel-framed
    traceback on failure so drift in the internal session API — rewritten in the
    Odoo 17.0 HTTP stack, and this tool must work across the supported 15–19 —
    surfaces as a debuggable error rather than a silent one.
    """
    import datetime
    import io
    import tarfile

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    creds = load_credentials(
        env_name,
        team.workspaces_dir,
        settings.db_user,
        settings.db_password,
        allow_fallback=False,
    )

    # `env` and `odoo` are standard odoo-shell globals across 15–19. Session state
    # is written to the filesystem store (no DB commit needed); values are printed
    # sentinel-framed so they survive the merged banner/log stream.
    mint_script = _build_connect_as_user_script(user)

    script_basename = f"_oduflow_connect_script_{secrets.token_hex(16)}.py"
    script_path = f"/tmp/{script_basename}"
    data = mint_script.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=script_basename)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    try:
        container.put_archive("/tmp", tar_stream)

        cmd = (
            f"odoo shell --no-http --stop-after-init "
            f"--db_host={settings.shared_db_container} "
            f"-r {creds['pg_user']} "
            f"--database={env_db} "
            f"< {script_path}"
        )
        logger.info("Minting session", extra={"env_name": env_name})
        # Password via PGPASSWORD (libpq reads it), never `-w` on the odoo CLI:
        # that would expose it in the container's process argv. Minting is a hot
        # path now that the odoo_* RPC tools re-mint per environment and user.
        exit_code, output = container.exec_run(
            ["sh", "-c", cmd],
            user="odoo",
            environment={"PGPASSWORD": creds["pg_password"]},
        )
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )

        err = _extract_sentinel(output_str, "ERR")
        if err is not None:
            if err.startswith("NOTFOUND:"):
                raise NotFoundError(
                    f"User '{err[len('NOTFOUND:') :]}' not found in environment "
                    f"'{env_name}'. Pass an existing login or numeric user id."
                )
            raise ExternalCommandError("odoo shell (connect_as_user)", exit_code, err)

        sid = _extract_sentinel(output_str, "SID")
        if not sid:
            # No sid and no framed error → surface the raw shell output for debugging.
            raise ExternalCommandError(
                "odoo shell (connect_as_user)", exit_code, output_str
            )

        login = _extract_sentinel(output_str, "LOGIN") or user
        uid = _extract_sentinel(output_str, "UID") or ""
        ttl_raw = _extract_sentinel(output_str, "TTL")
        try:
            ttl = int(ttl_raw) if ttl_raw else _DEFAULT_SESSION_LIFETIME
        except ValueError:
            ttl = _DEFAULT_SESSION_LIFETIME

        from oduflow.docker_ops.env_ops import get_env_base_url

        base_url, cookie_domain = get_env_base_url(settings, team, env_name, container)
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=ttl)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info("Connected as user", extra={"env_name": env_name, "login": login})
        return {
            "sid": sid,
            "login": login,
            "uid": uid,
            "base_url": base_url,
            "cookie_domain": cookie_domain,
            "url": f"{base_url}/web",
            "expires_at": expires_at,
        }
    finally:
        container.exec_run(["rm", "-f", script_path])


def list_env_users(
    settings: Settings, team: TeamSettings, env_name: str
) -> list[dict[str, Any]]:
    """Active login users of an environment (login, name, share, portal).

    Powers the dashboard's "Connect as" picker. Reads the environment database
    directly via psql on the shared DB container (like ``reset_admin_password``),
    so it works regardless of whether the Odoo HTTP server is up. ``share`` is
    True for both portal and public users; ``portal`` is True only for real
    portal users (members of ``base.group_portal``). The UI uses this to offer
    separate internal (``share`` False) and portal (``portal`` True) pickers and
    to exclude the public/anonymous user from both.
    """
    import json

    client = get_client()
    env_db = get_db_name(env_name, team.team_id)
    try:
        db_container = client.containers.get(settings.shared_db_container)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Database container '{settings.shared_db_container}' is not running. "
            "System not initialized. Restart oduflow."
        )
    # json_agg → a single JSON array so the value parses cleanly without CSV
    # quoting concerns (partner names may contain commas).
    # ``portal`` = membership in the base.group_portal user-type group, resolved
    # by xml_id via ir_model_data so it is stable across Odoo versions/DBs. It
    # is a strict subset of ``share`` (which also covers the public user).
    sql = (
        "SELECT COALESCE(json_agg(json_build_object("
        "'login', u.login, 'name', p.name, 'share', u.share, "
        "'portal', (u.id IN ("
        "SELECT r.uid FROM res_groups_users_rel r "
        "JOIN ir_model_data d ON d.model = 'res.groups' "
        "AND d.module = 'base' AND d.name = 'group_portal' "
        "WHERE r.gid = d.res_id))) "
        "ORDER BY u.share, lower(p.name)), '[]') "
        "FROM res_users u JOIN res_partner p ON p.id = u.partner_id "
        "WHERE u.active AND u.login IS NOT NULL AND u.login <> '__system__'"
    )
    exit_code, output = db_container.exec_run(
        ["psql", "-tAX", "-U", settings.db_user, "-d", env_db, "-c", sql]
    )
    out = (output.decode("utf-8") if isinstance(output, bytes) else str(output)).strip()
    if exit_code != 0:
        raise ExternalCommandError("psql", exit_code, out)
    try:
        rows: list[dict[str, Any]] = json.loads(out or "[]")
        return rows
    except json.JSONDecodeError:
        return []


def search_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    pattern: str,
    path: str = "/mnt/extra-addons",
    glob: str = "*.py",
    max_results: int = 50,
) -> dict[str, Any]:
    """Search for a pattern in files inside the Odoo container."""
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    cmd = [
        "grep",
        "-rnH",
        "-F",
        "--include",
        glob,
        pattern,
        path,
    ]

    logger.info(
        "Searching in environment",
        extra={"env_name": env_name, "pattern": pattern, "path": path},
    )
    exit_code, output = container.exec_run(cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    lines = output_str.strip().splitlines()[:max_results] if output_str.strip() else []

    return {
        "matches": len(lines),
        "output": "\n".join(lines),
        "truncated": len(output_str.strip().splitlines()) > max_results,
    }


def http_request_to_odoo(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    path: str,
    method: str = "GET",
    body: str = "",
    headers: dict[str, str] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """Make an HTTP request to the running Odoo instance."""
    import json  # noqa: F401
    import urllib.error
    import urllib.request

    from oduflow.docker_ops.env_ops import get_env_base_url, public_url_ssl_context

    # SSRF guard: `path` is appended to the environment's own base URL. Require a
    # single leading slash so it cannot rewrite the host — e.g. "@evil/..." would
    # turn base_url into userinfo (http://host:port@evil/...) and "//evil/" is a
    # protocol-relative host swap. Both would let a scoped token pivot the host
    # process to internal services / the cloud-metadata endpoint.
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(
            "path must be an absolute request path beginning with a single '/' "
            f"(got {path!r})."
        )

    # `get_env_base_url` returns scheme + host (+ port) with no path. The
    # browsable URL from `get_environment_info` must NOT be used here: it ends in
    # "/web?debug=1", so appending `path` buried it in the query string and every
    # request silently hit "/web" instead of the requested path.
    base_url, _cookie_domain = get_env_base_url(settings, team, env_name)

    url = f"{base_url}{path}"
    req_headers = dict(headers) if headers else {}
    if session_id:
        req_headers["Cookie"] = f"session_id={session_id}"

    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    logger.info(
        "HTTP request to Odoo",
        extra={"env_name": env_name, "method": method, "path": path},
    )

    try:
        with urllib.request.urlopen(
            req, timeout=30, context=public_url_ssl_context(settings)
        ) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            return {
                "status_code": resp.status,
                "headers": dict(resp.headers),
                "body": response_body[:100_000],
            }
    except urllib.error.HTTPError as e:
        response_body = e.read().decode("utf-8", errors="replace")
        return {
            "status_code": e.code,
            "headers": dict(e.headers),
            "body": response_body[:100_000],
        }
    except urllib.error.URLError as e:
        return {
            "status_code": 0,
            "headers": {},
            "body": f"Connection failed: {e.reason}",
        }


# -- Translations (i18n) --

# Locale codes reaching the `odoo -l <lang>` command line get the same treatment
# as module names above: exec_run tokenizes with shlex.split, so an unvalidated
# value could smuggle a separate Odoo CLI flag into the invocation.
_LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(?:_[A-Z]{2})?(?:@[A-Za-z][A-Za-z0-9_-]*)?$")

# A module's exported catalogue is tens of KB; the cap only exists so a runaway
# export cannot be read into the server's memory in one piece.
_PO_EXPORT_LIMIT = 5 * 1024 * 1024

# Extra-addons repos are mounted read-only (see env_ops), so a generated file can
# only be written back into the environment's own main checkout.
_MAIN_ADDONS_MOUNT = "/mnt/extra-addons"


def _validate_lang_codes(langs: "list[str] | tuple[str, ...]") -> None:
    for lang in langs:
        if not _LANG_CODE_RE.match(lang or ""):
            raise ValueError(
                f"Invalid language code '{lang}': expected a locale like "
                "'pl_PL', 'sr@latin' or 'fr'."
            )


def _get_odoo_container(settings: Settings, team: TeamSettings, env_name: str) -> Any:
    client = get_client()
    name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )


def _decode(output: Any) -> str:
    return (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else str(output)
    )


def _csv_column(output: str) -> list[str]:
    """Values of a single-column ``psql --csv`` result, header dropped."""
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    return rows[1:]


def _require_installed_module(
    settings: Settings, team: TeamSettings, env_name: str, module: str
) -> None:
    """Fail early when the module is not installed in this environment.

    Odoo's exporter filters on installed modules, so exporting an uninstalled one
    succeeds and produces an empty catalogue — indistinguishable from "this
    module has nothing to translate" unless we check.
    """
    # `module` has already passed _validate_module_names ([A-Za-z0-9_]), so it
    # cannot break out of the literal.
    result = run_db_query(
        settings,
        team,
        env_name,
        f"SELECT state FROM ir_module_module WHERE name = '{module}'",
    )
    states = _csv_column(result["output"])
    if not states:
        raise NotFoundError(
            f"Module '{module}' is unknown in environment '{env_name}'. "
            "Run pull_and_apply first so Odoo sees it."
        )
    if states[0] != "installed":
        raise PrerequisiteNotMetError(
            f"Module '{module}' is '{states[0]}', not installed. Translations are "
            "exported from installed modules only — install it first with "
            "install_odoo_modules."
        )


def _active_langs(settings: Settings, team: TeamSettings, env_name: str) -> list[str]:
    """Language codes activated in the environment's database."""
    result = run_db_query(
        settings, team, env_name, "SELECT code FROM res_lang WHERE active"
    )
    return _csv_column(result["output"])


def _i18n_basename(lang: str) -> str:
    """Odoo's own filename rule for a module's ``i18n/`` directory.

    Module loading looks for ``i18n/<get_iso_codes(lang)>.po``, which collapses a
    locale whose country repeats its language: ``pl_PL`` → ``pl``, while
    ``pt_BR`` stays as it is. Writing ``pl_PL.po`` would produce a file Odoo
    never reads.
    """
    if "_" in lang:
        base, _, country = lang.partition("_")
        if base == country.lower():
            return base
    return lang


def _export_command(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    container: Any,
    env_db: str,
    module: str,
    lang: str,
    path: str,
    token: str,
) -> tuple[Any, dict[str, Any], list[str], int | None]:
    """Build the export invocation for the environment's Odoo version.

    Odoo 19 dropped the ``--i18n-*`` server options for an ``odoo i18n``
    subcommand. That subcommand parses its own argv strictly, so it rejects the
    ``--db_*`` arguments the official image's entrypoint appends — the route
    every other module command here takes. It also accepts no connection flags
    of its own, only ``-c``, so the connection has to arrive through a config
    file; the password still travels in ``PGPASSWORD`` rather than onto disk or
    into the container's ``ps``.
    """
    major = _detect_odoo_major(container, settings.image_label)
    if major is not None and major >= 19:
        creds = load_credentials(
            env_name,
            team.workspaces_dir,
            settings.db_user,
            settings.db_password,
            allow_fallback=False,
        )
        conf = f"/tmp/oduflow-i18n-{token}.conf"
        script = (
            f'{{ cat "${{ODOO_RC:-/etc/odoo/odoo.conf}}"; '
            f'echo "db_host = {settings.shared_db_container}"; '
            f'echo "db_user = {creds["pg_user"]}"; }} > {conf} && '
            f"odoo i18n export -c {conf} -d {env_db} {module} "
            f"-l {lang or 'pot'} -o {path}"
        )
        return (
            ["sh", "-c", script],
            {"environment": {"PGPASSWORD": creds["pg_password"]}},
            [path, conf],
            major,
        )

    cmd = (
        f"/entrypoint.sh odoo -d {env_db} --stop-after-init --no-http "
        f"--i18n-export={path} --modules={module}"
    )
    if lang:
        cmd += f" -l {lang}"
    return cmd, {}, [path], major


def _export_po(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    container: Any,
    module: str,
    lang: str = "",
) -> str:
    """Run Odoo's own translation export and return the generated file.

    The export always goes through the environment's configured addons path,
    which matters for more than the database connection: the exporter finds
    ``_()``/``_lt()`` terms by walking ``addons_path`` and extracting from the
    module's Python sources. Under a partial addons path those files resolve to
    no known module and are dropped, leaving a catalogue of database terms only
    — the usual reason people conclude code messages "are not exported".
    """
    env_db = get_db_name(env_name, team.team_id)
    token = secrets.token_hex(8)
    path = f"/tmp/oduflow-i18n-{token}.{'po' if lang else 'pot'}"
    cmd, kwargs, artifacts, major = _export_command(
        settings, team, env_name, container, env_db, module, lang, path, token
    )
    label = "odoo i18n export" if major and major >= 19 else "odoo --i18n-export"

    # "module" is a reserved LogRecord attribute — passing it in `extra` raises.
    logger.info(
        "Exporting translations",
        extra={"module_name": module, "lang": lang or "template"},
    )
    export_code, export_output = container.exec_run(cmd, **kwargs)
    try:
        if export_code != 0:
            raise ExternalCommandError(label, export_code, _decode(export_output))
        size_code, size_out = container.exec_run(["stat", "-c", "%s", path])
        if size_code != 0:
            raise ExternalCommandError(
                "stat exported catalogue", size_code, _decode(size_out)
            )
        size = int(_decode(size_out).strip() or 0)
        if size > _PO_EXPORT_LIMIT:
            raise ExternalCommandError(
                label,
                export_code,
                f"Exported catalogue is {size} bytes, above the "
                f"{_PO_EXPORT_LIMIT} byte limit.",
            )
        read_code, blob = container.exec_run(["cat", path])
        if read_code != 0:
            raise ExternalCommandError(
                "read exported catalogue", read_code, _decode(blob)
            )
        return _decode(blob)
    finally:
        container.exec_run(["rm", "-f", *artifacts])


def _find_module_dir(container: Any, module: str) -> str:
    """Directory of *module* inside the container, or "" if it is not on a mount.

    Only the ``/mnt`` addons mounts are searched: core Odoo modules live in the
    image and have nothing we could usefully write back to.
    """
    _code, output = container.exec_run(
        [
            "find",
            "/mnt",
            "-maxdepth",
            "4",
            "-type",
            "f",
            "-name",
            "__manifest__.py",
            "-path",
            f"*/{module}/__manifest__.py",
        ]
    )
    marker = f"/{module}/__manifest__.py"
    for line in _decode(output).splitlines():
        line = line.strip()
        if line.endswith(marker):
            return line[: -len("/__manifest__.py")]
    return ""


def _read_translation_catalog(container: Any, path: str) -> str | None:
    """Read one bounded PO/POT file, returning ``None`` only when absent."""
    exists_code, _ = container.exec_run(["test", "-f", path])
    if exists_code != 0:
        return None

    size_code, size_out = container.exec_run(["stat", "-c", "%s", path])
    if size_code != 0:
        raise ExternalCommandError(
            "stat translation catalogue", size_code, _decode(size_out)
        )
    try:
        size = int(_decode(size_out).strip())
    except ValueError as exc:
        raise ExternalCommandError(
            "stat translation catalogue", 1, _decode(size_out)
        ) from exc
    if size > _PO_EXPORT_LIMIT:
        raise ExternalCommandError(
            "read translation catalogue",
            1,
            f"Catalogue is {size} bytes, above the {_PO_EXPORT_LIMIT} byte limit.",
        )

    read_code, output = container.exec_run(["cat", path])
    if read_code != 0:
        raise ExternalCommandError(
            "read translation catalogue", read_code, _decode(output)
        )
    return _decode(output)


def _host_path(
    settings: Settings, team: TeamSettings, env_name: str, container_path: str
) -> str:
    """Host-side path of a file under the main addons mount, or "" if elsewhere.

    In live-mount mode this is a path inside the developer's own checkout, so an
    agent on the same machine can open the generated file directly.
    """
    prefix = _MAIN_ADDONS_MOUNT + "/"
    if not container_path.startswith(prefix):
        return ""
    container = _get_odoo_container(settings, team, env_name)
    repo_path = container.labels.get("oduflow.local_path") or get_repo_path(
        env_name, team.workspaces_dir
    )
    return os.path.join(repo_path, container_path[len(prefix) :])


def export_module_translations(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    module: str,
    lang: str = "",
) -> dict[str, Any]:
    """Export a module's translation catalogue via Odoo's own exporter.

    Without *lang* this produces the ``.pot`` template (every translatable term,
    empty translations); with *lang* the msgstr values are filled from what the
    database currently holds.
    """
    _validate_module_names([module])
    if lang:
        _validate_lang_codes([lang])

    _require_installed_module(settings, team, env_name, module)
    container = _get_odoo_container(settings, team, env_name)
    content = _export_po(settings, team, env_name, container, module, lang)

    filename = f"{_i18n_basename(lang)}.po" if lang else f"{module}.pot"
    module_dir = _find_module_dir(container, module)
    written = ""
    read_only = False
    if module_dir.startswith(_MAIN_ADDONS_MOUNT + "/"):
        written = f"{module_dir}/i18n/{filename}"
        write_file_in_environment(
            settings,
            team,
            env_name,
            written,
            content,
            max_bytes=_PO_EXPORT_LIMIT,
        )
    elif module_dir:
        # An extra-addons repo: shared across environments and mounted read-only,
        # so the file can only travel out via the download link.
        read_only = True

    return {
        "module": module,
        "lang": lang,
        "filename": filename,
        "content": content,
        "summary": po_tools.summarize(po_tools.parse_po(content)),
        "module_dir": module_dir,
        "written_path": written,
        "host_path": (_host_path(settings, team, env_name, written) if written else ""),
        "read_only_mount": read_only,
    }


def translation_status(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    module: str,
    langs: "list[str] | None" = None,
) -> dict[str, Any]:
    """Compare a module's terms, its database translations and its ``.po`` files.

    The three differ more often than anyone expects, and the gap that costs the
    most is invisible: a ``.po`` whose entries carry no ``#:`` reference loads
    without a single warning and stores nothing. Reporting all three side by side
    turns that into something you see on the first call.
    """
    _validate_module_names([module])
    if langs:
        _validate_lang_codes(langs)

    _require_installed_module(settings, team, env_name, module)
    container = _get_odoo_container(settings, team, env_name)

    active = _active_langs(settings, team, env_name)
    targets = list(langs) if langs else [c for c in active if c != "en_US"]

    template_entries = po_tools.parse_po(
        _export_po(settings, team, env_name, container, module)
    )
    module_dir = _find_module_dir(container, module)
    pot_path = f"{module_dir}/i18n/{module}.pot" if module_dir else ""
    pot_content = _read_translation_catalog(container, pot_path) if pot_path else None
    committed_template = (
        po_tools.parse_po(pot_content) if pot_content is not None else None
    )

    template = po_tools.summarize(template_entries)
    per_lang: list[dict[str, Any]] = []
    for lang in targets:
        entry: dict[str, Any] = {"lang": lang, "active": lang in active}
        if entry["active"]:
            db_entries = po_tools.parse_po(
                _export_po(settings, team, env_name, container, module, lang)
            )
            entry["database"] = po_tools.summarize(db_entries)
        po_path = f"{module_dir}/i18n/{_i18n_basename(lang)}.po" if module_dir else ""
        file_content = (
            _read_translation_catalog(container, po_path) if po_path else None
        )
        if file_content is None:
            entry["file_path"] = ""
        else:
            file_entries = po_tools.parse_po(file_content)
            effective_entries = (
                po_tools.merge_with_template(file_entries, committed_template)
                if committed_template is not None
                else file_entries
            )
            entry["file_path"] = po_path
            entry["file"] = po_tools.summarize(file_entries)
            entry["import_effective"] = po_tools.summarize(effective_entries)
            entry["metadata_template_path"] = (
                pot_path if committed_template is not None else ""
            )
            entry["diff"] = po_tools.compare(template_entries, file_entries)
        # The caller wants a conclusion, not three catalogues to reconcile, so
        # the verdict is reached here where all three are already in hand.
        entry["status"] = po_tools.diagnose(
            template,
            active=entry["active"],
            database=entry.get("database"),
            file=entry.get("file"),
            effective=entry.get("import_effective"),
            missing=len(entry["diff"]["missing"]) if "diff" in entry else 0,
        )
        per_lang.append(entry)

    return {
        "module": module,
        "module_dir": module_dir,
        "template": template,
        "active_langs": active,
        "langs": per_lang,
    }
