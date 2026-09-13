from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import pathlib
import re
import shlex
import sys
import warnings
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ParamSpec, TypeVar, cast

# Suppress a third-party deprecation warning emitted at import time by fastmcp's
# JWT auth provider (it imports the deprecated authlib.jose module). This keeps
# CLI output (e.g. `oduflow --version`) clean.
try:
    from authlib.deprecate import AuthlibDeprecationWarning

    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except Exception:  # pragma: no cover - authlib internals may change
    pass

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_http_request

from oduflow import (
    activity,
    artifact_tokens,
    bundled_upgrade,
    git_ops,
    image_builds,
    migrations,
    po_tools,
    production_registry,
    quotas,
    reaper,
    secret_store,
)
from oduflow import settings as settings_module
from oduflow.docker_ops import (
    env_ops,
    odoo_ops,
    odoo_rpc,
    production_ops,
    service_database_ops,
    service_ops,
    service_presets,
    system_ops,
    volume_file_ops,
    volume_ops,
)
from oduflow.errors import FlowError, NotFoundError, PrerequisiteNotMetError
from oduflow.locking import (
    PROD_KEY_PREFIX,
    LockManager,
    credentials_lock_key,
    prod_backups_lock_key,
    service_database_lock_key,
    service_lock_key,
    service_preset_lock_key,
    volume_lock_key,
)
from oduflow.naming import (
    normalize_env_vars,
    parse_env_vars,
    parse_service_command,
    redact_url_credentials,
)
from oduflow.output_cache import CachedOutput, OutputCache
from oduflow.po_tools import PoEntry
from oduflow.settings import ImageRegistrySettings, Settings, TeamSettings, find_toml
from oduflow.stack_loader import StackValidationError

logger = logging.getLogger("oduflow")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_CACHE_THRESHOLD = 5_000  # chars — outputs above this are cached + summarized
_SUMMARY_HEAD_LINES = 200
_SUMMARY_TAIL_LINES = 100
_SUMMARY_ERROR_CONTEXT = 5
_ODOO_TEST_SUMMARY_RE = re.compile(
    r"\b\d+ failed, \d+ error\(s\) of \d+ tests\b", re.IGNORECASE
)

_output_cache = OutputCache()

_MCP_INSTRUCTIONS = """
Once at the start of the session, call get_agent_instructions to load the
current Oduflow workflow guide and active code-delivery mode. Use that guide
for the rest of the session; do not call it again before each tool.

Before writing or refactoring Odoo module code, call
get_odoo_development_guide(version="<major>") for the target Odoo version.
Determine the version from the user request, existing environment info, or the
odoo_image value; for example, odoo:18.0 means version="18".

After create_environment returns an instruction to call
get_odoo_development_guide(version="..."), follow it immediately before
editing code.
""".strip()

# mask_error_details=True: only messages from explicitly-raised ToolError (our
# handle_errors wraps FlowError into one) reach the client. Any other exception
# is returned as a generic "Error calling tool" without its text, so internal
# paths, container/DB names and stack detail are not disclosed to MCP callers.
mcp = FastMCP("Oduflow", instructions=_MCP_INSTRUCTIONS, mask_error_details=True)
_locks = LockManager()
_settings: Settings | None = None
_instance_id: str = ""
# Where the dashboard is reachable, recorded when the HTTP transport starts.
# Stays None under stdio, where no web server is mounted and therefore no
# artifact download URL can be offered.
_web_bind: tuple[str, int] | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        toml_path = find_toml()
        _settings = Settings.from_toml(toml_path)
        _settings.validate()
    return _settings


def _resolve_team(ctx: Context | None) -> TeamSettings:
    """Resolve the team for the current MCP request.

    Priority: token/OAuth client_id → Host header → single-team → team "1".
    """
    settings = _get_settings()
    # 1. Context.client_id comes from caller-controlled MCP request metadata,
    # not the verified credential. Only trust the access token established by auth.
    access_token = get_access_token()
    team_id = access_token.client_id if access_token else None
    if team_id and team_id in settings.teams:
        return settings.teams[team_id]
    # 2. Hostname-based: match Host header against team hostnames
    try:
        request = get_http_request()
        host = request.headers.get("host", "")
        team = settings.get_team_by_hostname(host)
        if team:
            return team
    except Exception:
        # No HTTP request in scope (e.g. stdio transport) — fall through to
        # the single-team / default-team resolution below. Logged, not
        # silently swallowed, so misrouting is traceable.
        logger.debug("Host-header team resolution unavailable", exc_info=True)
    # 3. Fallback: single team or default team "1". Only for stdio (implicit
    # local single user) and explicitly-unauthenticated HTTP: in a hosted
    # multi-client deployment a request that matches no token and no hostname
    # must never silently land in another team's context.
    if settings_module.TRANSPORT == "http" and not settings.allow_insecure_http:
        raise NotFoundError(
            "Cannot resolve a team for this request: no team auth token "
            "matched and the Host header matches no [team.*] hostname."
        )
    if len(settings.teams) == 1:
        return next(iter(settings.teams.values()))
    return settings.get_team("1")


# -- Output cache helpers --


def _make_summary(cached: CachedOutput) -> str:
    """Build a smart summary from cached output: head + errors + tail + metadata."""
    lines = cached.lines
    total = cached.total_lines
    parts: list[str] = []

    # Head
    parts.extend(lines[:_SUMMARY_HEAD_LINES])

    # Errors with context (deduplicated)
    if cached.error_line_indices:
        parts.append(
            f"\n--- Errors/Warnings ({len(cached.error_line_indices)} occurrences) ---"
        )
        seen: set[int] = set()
        for idx in cached.error_line_indices:
            context_end = min(idx + _SUMMARY_ERROR_CONTEXT + 1, total)
            for i in range(idx, context_end):
                if i not in seen:
                    parts.append(lines[i])
                    seen.add(i)
            parts.append("")

    # Skipped count
    skip_start = _SUMMARY_HEAD_LINES
    skip_end = total - _SUMMARY_TAIL_LINES
    if skip_end > skip_start:
        parts.append(f"--- Skipped {skip_end - skip_start} lines of output ---")

    # Tail
    tail_start = max(total - _SUMMARY_TAIL_LINES, _SUMMARY_HEAD_LINES)
    parts.extend(lines[tail_start:])

    # Metadata footer
    parts.append("")
    parts.append(
        f"[Cached output: id={cached.output_id}, {cached.total_lines} lines, {cached.total_chars} chars]"
    )
    parts.append(
        f'[Use read_output(output_id="{cached.output_id}", ...) to search, read ranges, or get full output]'
    )

    return "\n".join(parts)


def _maybe_cache(output: str, header: str, source_tool: str, source_args: str) -> str:
    """If output exceeds threshold, cache it and return header + summary. Otherwise return as-is."""
    if len(output) > _CACHE_THRESHOLD:
        cached = _output_cache.store(
            output, source_tool=source_tool, source_args=source_args
        )
        return f"{header}\n\n{_make_summary(cached)}"
    return f"{header}\n\nOutput:\n{output}"


def _cache_output(output: str, source_tool: str, source_args: str) -> CachedOutput:
    """Store command output when the caller explicitly requested a terse response."""
    return _output_cache.store(output, source_tool=source_tool, source_args=source_args)


def _odoo_test_summary(output: str) -> str | None:
    """Return Odoo's final aggregate test result without its log prefix."""
    matches = list(_ODOO_TEST_SUMMARY_RE.finditer(output))
    return matches[-1].group(0) if matches else None


def _compact_pull_result(
    result: dict[str, Any], woke_note: str, output: str, env_name: str
) -> str:
    """Build a one-line apply result while keeping raw command output server-side."""
    parts = [" ".join((woke_note + str(result["message"])).split())]
    if result.get("modules_installed"):
        parts.append(f"Installed: {', '.join(result['modules_installed'])}")
    if result.get("modules_upgraded"):
        parts.append(f"Upgraded: {', '.join(result['modules_upgraded'])}")
    parts.append(f"Changed files: {len(result.get('changed_files', []))}")

    exit_code = int(result.get("exit_code", 0) or 0)
    parts.append(f"Status: {'failed' if exit_code else 'ok'} (exit_code={exit_code})")

    warnings = result.get("warnings") or []
    if warnings:
        compact_warnings = [" ".join(str(warning).split()) for warning in warnings]
        parts.append(f"Guardrail warnings: {' | '.join(compact_warnings)}")

    if output:
        cached = _cache_output(
            output,
            source_tool="pull_and_apply",
            source_args=f"env={env_name}, summary_only=True",
        )
        parts.append(f"output_id={cached.output_id}")
    return "; ".join(parts)


def _artifact_url(settings: Settings, team: TeamSettings, token: str) -> str | None:
    """Public URL for a one-time artifact download, or None if unavailable.

    Under stdio there is no web server to serve it from — the caller instead
    returns a checkout path or materializes a local temporary artifact.
    """
    if _web_bind is None:
        return None
    if settings.routing_mode == "traefik":
        base = f"{settings.public_scheme_for(team)}://{team.hostname}"
    else:
        bind_host, port = _web_bind
        # The bind address controls where the listener accepts connections; it
        # is not necessarily a client-reachable name. Port mode already uses
        # the team's configured hostname for every other public URL.
        host = team.hostname or (
            "localhost" if bind_host in ("0.0.0.0", "::") else bind_host
        )
        base = f"{settings.public_scheme_for(team)}://{host}:{port}"
    return f"{base}/oduflow-artifact?token={token}"


# -- Decorators --

P = ParamSpec("P")
R = TypeVar("R")


def handle_errors(fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        import anyio

        def _run() -> R:
            try:
                result = fn(*args, **kwargs)
                preview = (
                    (result[:200] + "...")
                    if isinstance(result, str) and len(result) > 200
                    else result
                )
                logger.info("[%s] -> %s", fn.__name__, preview)
                return result
            except FlowError as e:
                logger.error("[%s] Error: %s", fn.__name__, e)
                raise ToolError(str(e))
            except ValueError as e:
                # Intentional, developer-authored input validation (invalid
                # env/module/template names, bad request path, …). These messages
                # are safe to surface and helpful; every OTHER exception stays
                # masked by mask_error_details=True so internal detail never leaks.
                logger.error("[%s] Invalid input: %s", fn.__name__, e)
                raise ToolError(str(e))

        return await anyio.to_thread.run_sync(_run)

    return wrapper


def with_env_lock(fn: Callable[P, R]) -> Callable[P, R]:
    """Acquire a per-environment lock before executing the tool function."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # Under ParamSpec, values pulled from *args/**kwargs are typed `object`;
        # narrow them back to the concrete types these helpers expect.
        raw_env_name = kwargs.get("env_name") or (args[0] if args else None)
        if not raw_env_name:
            raise ToolError("env_name is required")
        env_name = cast(str, raw_env_name)
        ctx = cast("Context | None", kwargs.get("ctx"))
        team = _resolve_team(ctx)
        # Dev environment tools must never operate on the production
        # namespace (their name-derived container/DB chains would resolve to
        # production resources). Productions have their own tool stack.
        from oduflow.naming import PROD_ENV_PREFIX

        if env_name.startswith(PROD_ENV_PREFIX):
            raise ToolError(
                f"'{env_name}' is a production environment. Use the "
                "*_production tools instead of the dev environment tools."
            )
        _locks.acquire_env(env_name, team.team_id, operation=fn.__name__)
        try:
            try:
                activity.touch(team, env_name)
            except Exception:
                pass  # activity tracking is best-effort
            return fn(*args, **kwargs)
        finally:
            _locks.release_env(env_name)

    return wrapper


def _wake_for_work(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    purpose: str = "for this call",
) -> str:
    """Container-level tools start a stopped environment instead of failing:
    with auto-stop, 'stopped' is a routine state, not an error. Returns the
    one-line note to prepend to the tool response ('' if already running)."""
    if env_ops.ensure_running(settings, env_name, team):
        activity.mark_started(team, env_name)
        return f"Note: environment was stopped; started it {purpose}.\n"
    return ""


def _wake_for_rpc(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """Wake helper for the lock-free ``odoo_*`` tools.

    They take no environment lock (XML-RPC against a live Odoo is
    transactional), so they record activity themselves — otherwise an
    environment driven purely through these tools would look idle to the reaper
    and be auto-stopped mid-session.
    """
    try:
        activity.touch(team, env_name)
    except Exception:
        pass  # activity tracking is best-effort
    return _wake_for_work(settings, team, env_name)


def with_team_lock(fn: Callable[P, R]) -> Callable[P, R]:
    """Acquire a per-team lock before executing the tool function."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        ctx = cast("Context | None", kwargs.get("ctx"))
        team = _resolve_team(ctx)
        _locks.acquire_team(team.team_id, operation=fn.__name__)
        try:
            return fn(*args, **kwargs)
        finally:
            _locks.release_team(team.team_id)

    return wrapper


def with_key_lock(
    key_fn: Callable[[str, str], str], require_name: bool = True
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Serialise a tool on a narrow resource key instead of the whole team.

    ``key_fn(team_id, name)`` builds the key from the caller's team and the
    tool's first argument (``name``); pass ``require_name=False`` for tools
    whose key is team-scoped only — their first positional argument is not a
    resource name and is ignored.

    These keys are deliberately acquired *without* a ``team_id``, so they stay
    outside the team↔environment mutual exclusion: deleting a service must not
    wait for a ten-minute ``create_environment``, and vice versa.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            name = ""
            if require_name:
                raw_name = kwargs.get("name") or (args[0] if args else None)
                if not raw_name:
                    raise ToolError("name is required")
                name = cast(str, raw_name)
            ctx = cast("Context | None", kwargs.get("ctx"))
            team = _resolve_team(ctx)
            key = key_fn(team.team_id, name)
            _locks.acquire_env(key, operation=fn.__name__)
            try:
                return fn(*args, **kwargs)
            finally:
                _locks.release_env(key)

        return wrapper

    return decorator


def prod_lock_key(team_id: str, name: str) -> str:
    """Lock key for a production — team-scoped so two teams' same-named
    productions never contend (unlike raw env keys)."""
    return f"{PROD_KEY_PREFIX}{team_id}:{name}"


_PRODUCTION_DISABLED_MESSAGE = (
    "Production hosting is disabled. Set enabled = true in the [production] "
    "section of oduflow.toml and restart Oduflow."
)


def production_enabled(fn: Callable[P, R]) -> Callable[P, R]:
    """Reject production MCP calls before they acquire locks or touch state."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if not _get_settings().prod_enabled:
            raise PrerequisiteNotMetError(_PRODUCTION_DISABLED_MESSAGE)
        return fn(*args, **kwargs)

    return wrapper


def with_prod_lock(fn: Callable[P, R]) -> Callable[P, R]:
    """Acquire the production's lock before executing the tool function."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        raw_name = kwargs.get("name") or (args[0] if args else None)
        if not raw_name:
            raise ToolError("name is required")
        name = cast(str, raw_name)
        ctx = cast("Context | None", kwargs.get("ctx"))
        team = _resolve_team(ctx)
        key = prod_lock_key(team.team_id, name)
        _locks.acquire_env(key, operation=fn.__name__)
        try:
            return fn(*args, **kwargs)
        finally:
            _locks.release_env(key)

    return wrapper


# =============================================================================
# MCP Tools — Git credentials
# =============================================================================


@mcp.tool()
@handle_errors
@with_key_lock(credentials_lock_key, require_name=False)
def setup_repo_auth(
    repo_url: str = "",
    token: str = "",
    username: str = "",
    host: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Cache git credentials for a private git host.

    Stores a personal access token in the team's git credential store and
    verifies it. Git matches credentials by host (and username), so one entry
    covers every repository on that host; after this, create_environment and
    add_extra_repo can clone with a plain https:// URL.

    Preferred form: pass the token itself, e.g.
    setup_repo_auth(repo_url="https://github.com/owner/repo.git", token="ghp_...").
    The host is taken from repo_url (or from `host`), and access is verified with
    `git ls-remote` against repo_url when one is given, otherwise against the
    provider's API (GitHub, GitLab, Bitbucket).

    Legacy form: a repo_url with inline credentials
    (https://user:PAT@github.com/owner/repo.git) and no `token`.

    Args:
        repo_url: Repository HTTPS URL (used to derive the host and to verify access).
        token: Personal access token / app password.
        username: Account name to store with the token. Optional for GitHub,
            GitLab and Azure DevOps (defaults to "x-access-token"); required for
            Bitbucket app passwords. Use distinct usernames to keep several
            tokens for the same host.
        host: Git host such as github.com or git.example.com:8443. Only needed
            when repo_url is omitted.
    """
    team = _resolve_team(ctx)
    if token:
        result = git_ops.store_credential(
            host=host or repo_url,
            token=token,
            username=username,
            verify_repo_url=repo_url,
            cred_file=team.git_credentials_file(),
        )
        return (
            f"Repository authentication configured.\n"
            f"Host: {result['host']}\n"
            f"Username: {result['username']}\n"
            f"Status: {result['status']}\n\n"
            f"You can now use create_environment with a plain https:// URL "
            f"on this host."
        )
    if not repo_url:
        raise ValueError(
            "Pass token (and repo_url or host), or a repo_url with inline credentials."
        )
    result = git_ops.setup_repo_auth(repo_url, cred_file=team.git_credentials_file())
    return (
        f"Repository authentication configured.\n"
        f"Host: {result['host']}\n"
        f"Repo URL (clean): {result['repo_url']}\n"
        f"Status: {result['status']}\n\n"
        f"You can now use create_environment with the clean URL (without credentials)."
    )


# =============================================================================
# MCP Tools — Extra addons repos
#
# No tool lock here: extra_addons.py already serialises every mutator on a
# per-repo RLock, and delete refuses while any managed container references the
# repo. A team lock only added false contention with unrelated work.
# =============================================================================


@mcp.tool()
@handle_errors
def add_extra_repo(name: str, repo_url: str, ctx: Context | None = None) -> str:
    """
    Clone an extra addons repository for use with environments.

    The repository is cloned as a shallow bare repo (only the latest commit of
    each branch, no history) to the shared repos directory, so large repos like
    Odoo Enterprise clone quickly. All branches are kept, so one repo serves any
    Odoo version. When creating an environment, reference it by name to mount it
    as additional addons (e.g., Odoo Enterprise).

    Args:
        name: Short name for the repo (e.g. "enterprise", "custom-themes").
        repo_url: HTTPS URL of the repository (e.g. https://github.com/owner/repo.git).
    """
    from oduflow.extra_addons import clone_extra_repo

    git_ops.validate_repo_url(repo_url)
    team = _resolve_team(ctx)
    result = clone_extra_repo(team, name, repo_url)
    return f"Extra repo '{result['name']}' cloned successfully.\nPath: {result['path']}"


@mcp.tool()
@handle_errors
def list_extra_repos(ctx: Context | None = None) -> str:
    """List all cloned extra addons repositories."""
    from oduflow.extra_addons import list_extra_repos as _list

    team = _resolve_team(ctx)
    repos = _list(team)
    if not repos:
        return "No extra addons repositories found."
    lines = ["Extra addons repositories:"]
    for r in repos:
        branches = ", ".join(r["branches"][:10]) if r["branches"] else "(no branches)"
        lines.append(f"- {r['name']}: {r['repo_url']} [{branches}]")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def delete_extra_repo(name: str, ctx: Context | None = None) -> str:
    """
    Delete a cloned extra addons repository.

    Args:
        name: Name of the extra repo to delete.
    """
    from oduflow.extra_addons import delete_extra_repo as _delete

    settings = _get_settings()
    team = _resolve_team(ctx)
    _delete(settings, team, name)
    return f"Extra repo '{name}' deleted."


@mcp.tool()
@handle_errors
def update_extra_repo(name: str, ctx: Context | None = None) -> str:
    """
    Pull latest changes from the remote for an extra addons repository.

    Fetches all branches and prunes deleted remote refs.

    Args:
        name: Name of the extra repo to update (e.g. "enterprise").
    """
    from oduflow.extra_addons import fetch_extra_repo

    team = _resolve_team(ctx)
    summary = fetch_extra_repo(team, name)
    return _format_fetch_summary(summary)


def _format_fetch_summary(summary: dict[str, Any]) -> str:
    name = summary["name"]
    if summary.get("local"):
        return f"Extra repo '{name}' is local (no remote) — nothing to pull."
    if summary["up_to_date"]:
        return f"Extra repo '{name}': already up to date."
    parts = [f"Extra repo '{name}' updated:"]
    for b in summary["updated_branches"]:
        parts.append(f"  {b['branch']}: {b['new_commits']} new commit(s)")
    for b in summary["new_branches"]:
        parts.append(f"  {b}: new branch")
    for b in summary["deleted_branches"]:
        parts.append(f"  {b}: deleted")
    return "\n".join(parts)


def _parse_extra_addons(raw: str) -> dict[str, str]:
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, branch = item.split(":", 1)
            result[name.strip()] = branch.strip()
        else:
            raise ValueError(
                f"Extra addon '{item}' must include a branch (e.g. '{item}:19.0')."
            )
    return result


def _odoo_guide_reminder(odoo_image: str) -> str:
    """The "load the version guide before writing code" line, if the image says
    which Odoo version this is."""
    import re

    match = re.search(r"odoo[:/](\d+)(?:\.0)?", odoo_image)
    if not match:
        return ""
    version = match.group(1)
    return (
        f"\n⚠️ Immediately call "
        f'get_odoo_development_guide(version="{version}") to load Odoo {version} '
        "development standards and constraints. Do not wait for the user to ask "
        "— these guidelines must be loaded before writing any code."
    )


def _existing_environment_message(
    existing: dict[str, Any],
    *,
    requested_image: str,
    requested_template: str,
) -> str:
    """Report an environment that create_environment found already provisioned."""
    lines = [
        "Environment already exists — reusing it. Nothing was recreated.",
        f"Environment: {existing['env_name']}",
        f"URL: {existing['url']}",
        f"Git Branch: {existing['git_branch']}",
        f"Hostname: {existing['hostname']}",
        f"Odoo Container: {existing['odoo_container']}",
        f"Database: {existing['database']}",
        f"Workspace: {existing['workspace']}",
        f"Odoo Image: {existing['odoo_image']}",
        f"Template: {existing['template_name']}",
    ]
    if existing.get("local_path"):
        lines.append(
            f"Live-mount: {existing['local_path']} "
            "(edit files directly; call pull_and_apply to apply)"
        )
    if existing.get("started"):
        lines.append("It was stopped and has been started for you.")
    if requested_image and requested_image != existing["odoo_image"]:
        lines.append(
            f"Note: you asked for image '{requested_image}', but this "
            f"environment runs '{existing['odoo_image']}'. Change it with "
            "update_environment, or delete and create it again."
        )
    if (
        requested_template
        and requested_template.lower() != "none"
        and requested_template != existing["template_name"]
    ):
        lines.append(
            f"Note: you asked for template '{requested_template}', but this "
            f"environment was created from '{existing['template_name']}'. Its "
            "database is the one that already exists; delete and create the "
            "environment again to start from another template."
        )
    lines.append("Apply your code with pull_and_apply.")
    reminder = _odoo_guide_reminder(existing["odoo_image"])
    if reminder:
        lines.append(reminder)
    return "\n".join(lines)


# =============================================================================
# MCP Tools — Environments
# =============================================================================


@mcp.tool()
@handle_errors
def create_environment(
    branch: str,
    env_name: str = "",
    template_name: str = "",
    repo_url: str = "",
    odoo_image: str = "",
    extra_addons: str = "",
    sanitize: bool = True,
    auto_install_modules: str = "",
    env_vars: str = "",
    local_path: str = "",
    hostname: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Provision a new ephemeral Odoo environment.

    Safe to call first, without listing environments: if one already exists
    under this name it is returned as is — with its URL, and started when it
    was stopped — and nothing is recreated. The single refusal is a branch
    mismatch: an environment tracking another branch is left alone, since its
    database and URL are in use. Move it with switch_branch, pass a different
    env_name, or delete it first. When the team runs out of environment slots,
    switch_branch onto a finished (merged) environment is the way forward.

    Args:
        branch: The git branch to clone (e.g. "19.0", "feature/my-feature").
        env_name: Optional environment name. If empty, defaults to the branch name. Use this to create multiple environments from the same branch (e.g. env_name="client-a" with branch="19.0").
        hostname: Optional short Traefik hostname (for example "qa"). By default public hostnames remain environment-derived (feature.dev.example.com). Teams that explicitly set environment_hostname_mode="slots" reuse dev1.example.com through devN.example.com. An explicit value replaces the team prefix in either mode, so hostname="qa" produces qa.example.com.
        template_name: Name of the template profile to use as database template. Pass "none" to skip template and initialise Odoo from scratch with -i base. When a template is specified, repo_url and odoo_image are loaded from template metadata (but can be overridden). A template saved from a live-mounted environment supplies local_path instead of repo_url and recreates the live-mount when allow_local_path is enabled.
        repo_url: URL of the git repository to clone. Optional when template_name is specified (loaded from template metadata).
        odoo_image: Full Docker image name with tag (e.g. "odoo:19.0"). Optional when template_name is specified (loaded from template metadata).
        extra_addons: Comma-separated list of extra addon repo names with branches (e.g. "enterprise:19.0,custom-themes:main"). Each entry must include a branch after a colon.
        sanitize: Sanitize the database after provisioning (default: True). Runs Odoo's native neutralization (deactivates outgoing mail servers and crons, disables payment providers, scrubs third-party API credentials, sets database.is_neutralized) and then any custom scripts from the .oduflow/odoo_sanitize/ folder in the repository. Only applies to environments created from a template.
        auto_install_modules: Comma-separated list of Odoo modules to install automatically after the environment is provisioned (e.g. "sale,purchase,stock"). When a template is specified and this is empty, the value is loaded from template metadata.
        env_vars: Comma- or newline-separated KEY=VALUE pairs injected as environment variables into the Odoo container (e.g. "WORKERS=2,LIMIT_TIME_CPU=600"). Commas inside values are preserved unless what follows the comma looks like another KEY=; put one pair per line when in doubt. These are added on top of the database connection variables (HOST/USER/PASSWORD). When a template records env_vars, the two sets are merged per key and the values passed here win. A value "secret:<name>" references a team secret (see list_secrets): the real value is injected only inside the container and is never readable back.
        local_path: LOCAL FAST-PATH. Absolute path to a checkout on THIS host. When set, Oduflow skips git clone and bind-mounts the directory live into the container — your file edits are visible instantly, no git push/pull needed. After editing, call pull_and_apply with explicit install/upgrade/restart to apply. repo_url is not required in this mode. Gated by allow_local_path (default: true).
    """
    import json

    from oduflow.naming import validate_env_name

    resolved_env_name = validate_env_name(env_name or branch)
    settings = _get_settings()
    team = _resolve_team(ctx)
    _locks.acquire_env(resolved_env_name, team.team_id, operation="create_environment")
    try:
        # Creating an environment that already exists is not a mistake worth an
        # error: the agent wanted one for this branch and there is one. Answer
        # with its URL before doing any of the expensive work below, so calling
        # create first — without a list_environments round-trip — is the cheap,
        # correct default. A branch mismatch still raises (see the helper).
        existing = env_ops.adopt_existing_environment(
            settings, team, resolved_env_name, branch=branch
        )
        if existing is not None:
            return _existing_environment_message(
                existing, requested_image=odoo_image, requested_template=template_name
            )

        resolved_template: str | None
        if not template_name or template_name.lower() == "none":
            resolved_template = None
        else:
            resolved_template = template_name

        # Load metadata from template if available
        effective_repo_url = repo_url
        effective_odoo_image = odoo_image
        effective_git_user = ""
        template_env_vars: dict[str, str] = {}
        local_path = (local_path or "").strip()
        local_path_from_template = False
        if resolved_template:
            metadata_path = team.get_template_metadata_path(resolved_template)
            if os.path.isfile(metadata_path):
                with open(metadata_path) as f:
                    metadata = json.load(f)
                if not effective_repo_url:
                    effective_repo_url = metadata.get("repo_url", "")
                if not effective_odoo_image:
                    effective_odoo_image = metadata.get("odoo_image", "")
                if not effective_git_user:
                    effective_git_user = metadata.get("git_user", "")
                if not extra_addons:
                    raw_extra = metadata.get("extra_addons")
                    if raw_extra:
                        from oduflow.docker_ops.env_ops import _normalize_extra_addons

                        _metadata_extra = _normalize_extra_addons(raw_extra)
                        if _metadata_extra:
                            extra_addons = ",".join(
                                f"{name}:{b}" for name, b in _metadata_extra.items()
                            )
                if not auto_install_modules:
                    auto_install_modules = metadata.get("auto_install_modules", "")
                template_env_vars = system_ops._template_env_vars(
                    metadata, resolved_template
                )
                # The template's live-mount path applies only when the caller
                # gave no code source of their own (explicit repo_url wins, so
                # http clients can still clone from a real remote).
                if not local_path and not repo_url and metadata.get("local_path"):
                    local_path = metadata["local_path"]
                    local_path_from_template = True

        if local_path:
            if not settings.allow_local_path:
                if local_path_from_template:
                    raise ValueError(
                        f"Template '{resolved_template}' was saved from a "
                        "live-mounted environment, so it provides a local_path "
                        "instead of a repo_url. Set allow_local_path = true "
                        "in oduflow.toml [server] to enable live-mount, or "
                        "pass repo_url= explicitly."
                    )
                raise ValueError(
                    "local_path (live-mount) is disabled. Set "
                    "allow_local_path = true in oduflow.toml [server] "
                    "to enable it."
                )
            local_path = os.path.abspath(os.path.expanduser(local_path))
            if not os.path.isdir(local_path):
                raise ValueError(
                    f"local_path does not exist or is not a directory: {local_path}"
                )
            # Live-mount: no remote URL required; label the env with the path.
            if not effective_repo_url:
                effective_repo_url = local_path
        else:
            if not effective_repo_url:
                sources = [
                    "repo_url",
                    "template_name (which supplies repo_url from its metadata)",
                ]
                if settings.allow_local_path:
                    sources.append("local_path=<abs path> (live-mount fast-path)")
                raise ValueError(
                    "No code source for the environment — provide one of: "
                    + "; ".join(sources)
                    + "."
                )
            git_ops.validate_repo_url(effective_repo_url)

        if not effective_odoo_image:
            raise ValueError(
                "odoo_image is required (not found in template metadata either)."
            )

        extra_dict = _parse_extra_addons(extra_addons) if extra_addons else {}
        auto_install_list = (
            [m.strip() for m in auto_install_modules.split(",") if m.strip()]
            if auto_install_modules
            else []
        )
        # Per-key merge: the template supplies the baseline and an explicit
        # argument overrides just the keys it names, so a caller can bump
        # WORKERS without having to restate the template's whole set.
        parsed_env = {**template_env_vars, **normalize_env_vars(env_vars)} or None
        result = env_ops.create_environment(
            settings,
            team,
            branch,
            effective_repo_url,
            effective_odoo_image,
            env_name=resolved_env_name,
            template_name=resolved_template,
            extra_addons=extra_dict or None,
            git_user=effective_git_user,
            sanitize=sanitize,
            auto_install_modules=auto_install_list or None,
            env_vars=parsed_env,
            local_path=local_path,
            hostname=hostname,
        )

        from oduflow.telemetry import record_env_created

        record_env_created(_instance_id, _get_version(), settings.disable_telemetry)

        display_template = (
            resolved_template
            if resolved_template is not None
            else "none (init from scratch)"
        )
        lines = [
            "Environment provisioned successfully!",
            f"Environment: {resolved_env_name}",
            f"URL: {result['url']}",
            f"Hostname: {result.get('hostname', '')}",
            f"Odoo Container: {result['odoo_container']}",
            f"Database: {result['database']}",
            f"Workspace: {result['workspace']}",
            f"Template: {display_template}",
            f"Creation time: {result.get('elapsed_seconds', '?')}s",
        ]
        if resolved_env_name != branch:
            lines.insert(2, f"Git Branch: {branch}")
        if result.get("local_path"):
            lines.append(
                f"Live-mount: {result['local_path']} "
                "(edit files directly; call pull_and_apply to apply)"
            )
        if extra_dict:
            extras_display = ", ".join(
                f"{name} ({b})" for name, b in extra_dict.items()
            )
            lines.append(f"Extra Addons: {extras_display}")
        if auto_install_list:
            lines.append(f"Auto-install modules: {', '.join(auto_install_list)}")
        if parsed_env:
            lines.append(
                "Env vars: " + ", ".join(f"{k}={v}" for k, v in parsed_env.items())
            )
        lineage = result.get("template_lineage") or {}
        if lineage.get("message"):
            label = (
                "Code is behind the template database"
                if lineage.get("status") == "diverged"
                else "Code is ahead of the template database"
            )
            lines.append(f"\n⚠️ {label} — {lineage['message']}")
        setup_logs: list[str] = result.get("setup_logs", [])
        if setup_logs:
            lines.append("\n--- Setup Log ---")
            lines.extend(setup_logs)
        reminder = _odoo_guide_reminder(effective_odoo_image)
        if reminder:
            lines.append(reminder)
        return "\n".join(lines)
    finally:
        _locks.release_env(resolved_env_name)


@mcp.tool()
@handle_errors
@with_team_lock
def save_as_template(
    env_name: str,
    template_name: str,
    reset_env_changes: bool = False,
    overwrite: bool = False,
    ctx: Context | None = None,
) -> str:
    """
    Save an environment as a template (DB + filestore).

    By default this creates a NEW template and REFUSES to overwrite an existing
    one (raises an error) — pick a fresh template_name. Set overwrite=True to
    deliberately re-baseline an existing template: its database and filestore are
    replaced with the data from the specified environment, and other environments
    that use this template with overlay-mounted filestores are remounted against
    the new baseline. On re-baseline their filestore changes (the overlay upper
    layer) are PRESERVED by default — non-destructive; set reset_env_changes=True
    to discard those changes and reset every affected environment to the new
    baseline. The source environment itself is always reset (its data just became
    the new template).

    Requires EXPLICIT user permission and confirmation before execution.
    If the user has not clearly and unambiguously asked you to save
    a specific environment as template, DO NOT call this tool. Both
    overwrite=True (re-baselines an existing template) and reset_env_changes=True
    (destructive for other environments) require an explicit user request.

    Args:
        env_name: The name of the environment whose DB and filestore will become the new template.
        template_name: Name of the template profile to publish into.
        reset_env_changes: If True, discard other environments' filestore deltas (destructive). Default False (preserve).
        overwrite: If True, allow re-baselining an existing template. Default False (refuse if the template already exists).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.publish_env_as_template(
        settings,
        team,
        env_name,
        template_name=template_name,
        reset_env_changes=reset_env_changes,
        overwrite=overwrite,
    )
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    lines = [
        f"Environment '{result['env_name']}' saved as template '{template_name}'.",
        f"Template DB: {result['template_db']}",
        f"Dump: {result['dump']}",
        f"Filestore: {result['filestore']}",
    ]
    if affected:
        verb = "Reset" if reset_env_changes else "Remounted (changes preserved)"
        lines.append(f"{verb} filestore overlays for: {', '.join(affected)}")
    else:
        lines.append("No other environments were affected.")
    if failures:
        lines.append(
            "⚠️ Remount issues:\n"
            + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def list_templates(ctx: Context | None = None) -> str:
    """List available template profiles (database + filestore snapshots)."""
    settings = _get_settings()
    team = _resolve_team(ctx)
    templates = system_ops.list_templates(settings, team)
    if not templates:
        return "No template profiles found."
    output = "Template profiles:\n"
    for r in templates:
        db_status = "loaded" if r["db_loaded"] else "not loaded"
        overlay_status = "overlay" if r.get("use_overlay") else "copy"
        fs_size = r.get("filestore_size_mb")
        dump_size = r.get("dump_size_mb")
        size_info = ""
        if fs_size is not None or dump_size is not None:
            fs_str = f"{fs_size:.0f} MB" if fs_size is not None else "?"
            dump_str = f"{dump_size:.0f} MB" if dump_size is not None else "?"
            size_info = f", Filestore size={fs_str}, Dump size={dump_str}"
        auto_install = r.get("auto_install_modules", "")
        auto_info = f", Auto-install={auto_install}" if auto_install else ""
        # Names only — a template's env vars routinely carry API keys, and this
        # listing is the one template view an agent reads unprompted.
        env_names = sorted(r.get("env_vars") or {})
        env_info = f", Env={','.join(env_names)}" if env_names else ""
        # Provenance: which code this database snapshot came from. A branch that
        # does not contain that commit will hit upgrade failures against newer data.
        origin_parts = []
        if r.get("source_branch"):
            origin_parts.append(str(r["source_branch"]))
        if r.get("source_commit"):
            origin_parts.append(str(r["source_commit"])[:8])
        if r.get("snapshot_at"):
            origin_parts.append(f"snapshot {str(r['snapshot_at'])[:10]}")
        origin_info = f", Source={' @ '.join(origin_parts)}" if origin_parts else ""
        output += f"- {r['template_name']}: DB={db_status}, SQL={r['has_sql']}, Filestore={r['has_filestore']}, Mode={overlay_status}{size_info}{auto_info}{env_info}{origin_info}\n"
    return output


@mcp.tool()
@handle_errors
@with_team_lock
def import_template_from_odoo(
    odoo_url: str,
    master_pwd: str,
    db_name: str = "",
    template_name: str = "default",
    without_filestore: bool = False,
    ctx: Context | None = None,
) -> str:
    """
    Import a template from a running Odoo instance via its database manager API.

    Downloads a full ZIP backup or database-only PostgreSQL custom dump and
    loads it into PostgreSQL as a template database.

    Args:
        odoo_url: Base URL of the Odoo instance (e.g. "https://my-odoo.example.com").
        master_pwd: Odoo master password (database manager password).
        db_name: Name of the database to back up. If empty, auto-detected (fails if multiple DBs exist).
        template_name: Name of the template profile to create.
        without_filestore: If true, request a database-only PostgreSQL custom dump.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.import_from_odoo(
        settings,
        team,
        odoo_url=odoo_url,
        master_pwd=master_pwd,
        db_name=db_name,
        template_name=template_name,
        without_filestore=without_filestore,
    )
    lines = [
        f"Template '{result['template_name']}' imported successfully!",
        f"Source: {result['source_url']} (db: {result['source_db']})",
        f"Odoo version: {result['odoo_version']}",
        f"Odoo image: {result['odoo_image']}",
        f"Template DB: {result['template_db']}",
        "Filestore: "
        + ("included" if result.get("includes_filestore") else "not included"),
        f"Backup size: {result['zip_size_mb']} MB",
        f"DB restore time: {result['restore_seconds']}s",
    ]
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    if affected:
        lines.append(
            "Remounted (changes preserved) filestore overlays for: "
            + ", ".join(affected)
        )
    if failures:
        lines.append(
            "⚠️ Remount issues:\n"
            + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_team_lock
def refresh_template(
    template_name: str,
    reset_env_changes: bool = False,
    ctx: Context | None = None,
) -> str:
    """
    Re-apply a template's current filestore to live overlay environments.

    Unmounts and remounts every overlay-mounted environment that uses this
    template against the template's current on-disk filestore. By default each
    environment's filestore changes (the overlay upper layer) are PRESERVED —
    non-destructive. Set reset_env_changes=True to discard those changes and
    reset every affected environment to the template baseline (destructive).

    Use this after the template filestore was changed on disk, or to re-sync an
    environment that was busy/skipped during an import or save.

    Requires EXPLICIT user permission, especially with reset_env_changes=True.

    Args:
        template_name: Name of the template profile to re-apply.
        reset_env_changes: If True, discard environments' filestore deltas (destructive). Default False (preserve).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.refresh_template(
        settings, team, template_name, reset_env_changes=reset_env_changes
    )
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    if affected:
        verb = "Reset" if reset_env_changes else "Remounted (changes preserved)"
        lines = [f"{verb} filestore overlays for: {', '.join(affected)}"]
    else:
        lines = [
            f"No live overlay environments use template '{template_name}'; "
            "nothing to do."
        ]
    if failures:
        lines.append(
            "⚠️ Remount issues:\n"
            + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_team_lock
def attach_filestore(
    template_name: str,
    source: str,
    reset_env_changes: bool = False,
    strip_prefix: str = "auto",
    ctx: Context | None = None,
) -> str:
    """
    Attach or replace a template filestore from a directory, rsync/ssh source, or archive.

    The source may be a local directory, a local .zip/.tar/.tar.gz archive, an
    rsync:// URL, or an SSH-style rsync source such as user@host:/path. Archive
    and directory sources are normalized to the Odoo filestore layout
    (XX/<sha1>). strip_prefix="auto" detects a wrapper directory such as the
    database name; pass an explicit prefix when auto-detection is ambiguous.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.attach_filestore(
        settings,
        team,
        template_name,
        source,
        reset_env_changes=reset_env_changes,
        strip_prefix=strip_prefix,
    )
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    lines = [
        f"Filestore attached to template '{result['template_name']}'.",
        f"Source: {result['source']} ({result['source_kind']})",
        f"Strip prefix: {result.get('strip_prefix') or '<none>'}",
        f"Files: {result['filestore_files']}",
        f"Filestore size: {result['filestore_size_mb']} MB",
        f"Mode: {'overlay' if result.get('use_overlay') else 'copy'}",
    ]
    if affected:
        verb = "Reset" if reset_env_changes else "Remounted (changes preserved)"
        lines.append(f"{verb} filestore overlays for: {', '.join(affected)}")
    if failures:
        lines.append(
            "⚠️ Remount issues:\n"
            + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    return "\n".join(lines)


# =============================================================================
# MCP Tools — Agent guides
# =============================================================================


@mcp.tool()
@handle_errors
def get_agent_instructions(ctx: Context | None = None) -> str:
    """Load the Oduflow agent workflow once at the start of a session."""
    import pathlib

    def _mode_preface() -> str:
        settings = _get_settings()
        try:
            envs = env_ops.list_environments(settings, team)
        except Exception:
            envs = []

        local_envs = [env for env in envs if env.get("local_path")]
        if local_envs:
            names = ", ".join(
                f"{env['env_name']} ({env['local_path']})" for env in local_envs
            )
            return (
                "## Current Code Delivery Mode\n\n"
                f"Live-mount/local_path mode is active for: {names}\n\n"
                "Edit those mounted folders directly; no push is required. "
                "After edits, call `pull_and_apply` with the explicit action "
                "required by the change.\n\n"
                "---\n\n"
            )

        if not envs:
            return (
                "## Current Code Delivery Mode\n\n"
                "No environment was detected yet. Choose the delivery mode "
                "before calling `create_environment`. For `repo_url`, first "
                "publish the current branch with `git push -u origin HEAD`; "
                "Oduflow cannot clone a local-only branch. For `local_path`, "
                "pass the absolute checkout path and do not push.\n\n"
                "---\n\n"
            )

        return (
            "## Current Code Delivery Mode\n\n"
            "No live-mount/local_path environment was detected. Use the "
            "`repo_url` workflow. Before the first `create_environment` for "
            "the current branch, publish that branch with "
            "`git push -u origin HEAD`; Oduflow cannot clone a local-only "
            "branch. Then edit locally, commit, push, and call "
            "`pull_and_apply`.\n\n"
            "---\n\n"
        )

    def _guide_body() -> str:
        for name in ("agent_instructions.md", "agent_skill.md", "agent_guide.md"):
            skill_path = os.path.join(team.data_dir, "agent_guides", name)
            if os.path.isfile(skill_path):
                with open(skill_path, "r", encoding="utf-8") as f:
                    return _mode_preface() + f.read()
        for name in ("agent_instructions.md", "agent_skill.md", "agent_guide.md"):
            bundled = (
                pathlib.Path(__file__).resolve().parent
                / "templates"
                / "agent_guides"
                / name
            )
            if bundled.is_file():
                return _mode_preface() + bundled.read_text(encoding="utf-8")
        return "Agent skill not found."

    team = _resolve_team(ctx)
    guide = _guide_body()
    # The feedback section ships nowhere on disk — it exists only while the
    # (undocumented) [server] agent_feedback option is on.
    if _get_settings().agent_feedback:
        from oduflow import agent_feedback as feedback_mod

        guide = guide.rstrip() + "\n\n" + feedback_mod.INSTRUCTIONS_SECTION
    return guide


@mcp.tool()
@handle_errors
def get_odoo_development_guide(version: str, ctx: Context | None = None) -> str:
    """
    Get Odoo development standards and constraints guide for a specific Odoo version.

    Args:
        version: Odoo version number (e.g. "18", "18.0", "19", "19.0"). Both "19" and "19.0" formats are accepted.
    """
    import pathlib

    normalized = version.split(".")[0]
    filename = f"odoo_{normalized}_guide.md"
    team = _resolve_team(ctx)
    guide_path = os.path.join(team.data_dir, "agent_guides", filename)
    if os.path.isfile(guide_path):
        with open(guide_path, "r", encoding="utf-8") as f:
            return f.read()
    bundled = (
        pathlib.Path(__file__).resolve().parent
        / "templates"
        / "agent_guides"
        / filename
    )
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    available = []
    guides_dir = os.path.join(team.data_dir, "agent_guides")
    if os.path.isdir(guides_dir):
        available = [
            f.replace("odoo_", "").replace("_guide.md", "")
            for f in os.listdir(guides_dir)
            if f.startswith("odoo_") and f.endswith("_guide.md")
        ]
    if available:
        return f"No development guide found for Odoo {version}. Available versions: {', '.join(sorted(available))}"
    return f"No development guide found for Odoo {version}."


@mcp.tool(enabled=False)
@handle_errors
def submit_agent_feedback(
    category: str,
    tools: str,
    suggestion: str,
    ctx: Context | None = None,
) -> str:
    """
    Report anonymous feedback about the Oduflow MCP tools once a task is finished.

    Call this at most once per task, and only when the tools themselves caused
    friction or something was missing. Never include names, paths, hostnames,
    repositories, credentials or any of the user's business data — the report
    leaves the machine.

    Args:
        category: One of "friction", "missing_tool", "unclear_error", "docs".
        tools: Oduflow tool names involved, comma-separated.
        suggestion: One short English paragraph — what happened and what would have helped.
    """
    from oduflow import agent_feedback as feedback_mod

    normalized = category.strip().lower()
    if normalized not in feedback_mod.CATEGORIES:
        raise ToolError(
            f"category must be one of: {', '.join(feedback_mod.CATEGORIES)}"
        )

    settings = _get_settings()
    team = _resolve_team(ctx)

    # Identifiers that look like ordinary words and so cannot be caught by a
    # generic pattern — redact them by name.
    known: set[str] = {team.team_id, team.hostname}
    try:
        known.update(
            str(env.get("env_name", ""))
            for env in env_ops.list_environments(settings, team)
        )
    except Exception:
        pass

    # This field is structurally separate from the scrubbed suggestion, so
    # restrict it to registered MCP names rather than letting arbitrary
    # identifiers leave the instance under the guise of tool names.
    registered_tools = set(
        getattr(getattr(mcp, "_tool_manager", None), "_tools", {}) or {}
    )
    payload = feedback_mod.build_payload(
        category=normalized,
        tools=feedback_mod.normalize_tools(tools, registered_tools),
        suggestion=feedback_mod.scrub(suggestion, tuple(n for n in known if n)),
        version=_get_version(),
        instance_id=_instance_id,
    )
    if not payload["suggestion"]:
        raise ToolError("suggestion is required")

    feedback_mod.send(payload)
    return "Feedback submitted anonymously. Thanks."


# =============================================================================
# MCP Tools — Feedback
# =============================================================================


@mcp.tool()
@handle_errors
def report_issue(
    details: str,
    kind: str = "feedback",
    title: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Build a link that lets the user file an issue about Oduflow itself on GitHub.

    Use this when the user hits a bug in Oduflow, wants a feature, or wants to
    send feedback about the tool — not for problems in their own Odoo code.
    The tool does NOT create the issue: it returns a prefilled link to the
    oduist/oduflow issue form. Show the link to the user and let them submit it
    from their own GitHub account, so the report is attributable to them and
    they can edit it first.

    Oduflow version, Python version, platform, transport and routing mode are
    attached automatically. Never put hostnames, repository URLs, branch or
    database names, credentials, or customer data into the text.

    Args:
        details: The report body — what happened, what was expected, or the feedback.
        kind: One of "bug", "feature", "feedback" (default). Selects the issue form and its labels.
        title: Optional one-line summary used as the issue title.
    """
    from oduflow import feedback

    normalized = kind.strip().lower()
    if normalized not in feedback.KINDS:
        raise ToolError(
            f"Unknown kind '{kind}'. Use one of: {', '.join(sorted(feedback.KINDS))}."
        )
    if not details.strip():
        raise ToolError("details is required — describe the issue or the feedback.")

    url = feedback.build_issue_url(
        kind=normalized,
        title=title,
        details=details,
        settings=_get_settings(),
    )
    return feedback.report_issue_message(url, normalized)


# =============================================================================
# MCP Tools — Template management
# =============================================================================


@mcp.tool()
@handle_errors
@with_team_lock
def delete_template(template_name: str, ctx: Context | None = None) -> str:
    """
    DANGEROUS: Delete a template profile — permanently removes its template database and files from disk.

    This is a destructive, irreversible operation. All environments that depend on this
    template will lose their baseline and cannot be recreated until a new template is set up.

    NEVER call this tool on your own initiative. Requires EXPLICIT user permission
    and confirmation before execution. If the user has not clearly and unambiguously
    asked you to delete a specific template, DO NOT call this tool.

    Args:
        template_name: Name of the template profile to delete.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.delete_template(settings, team, template_name)
    return f"Template '{result['template_name']}' deleted. Template DB '{result['template_db']}' removed."


@mcp.tool()
@handle_errors
@with_team_lock
def rename_template(
    template_name: str, new_name: str, ctx: Context | None = None
) -> str:
    """
    Rename a template profile — renames its directory and PostgreSQL template DB.

    Refused if any environment was created from this template (its template
    reference is fixed at creation time and cannot be updated on a running
    environment): delete those environments first, or leave the template as is.

    Args:
        template_name: Current name of the template.
        new_name: New name for the template.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.rename_template(settings, team, template_name, new_name)
    return (
        f"Template '{result['old_name']}' renamed to '{result['template_name']}' "
        f"(DB '{result['template_db']}')."
    )


# =============================================================================
# MCP Tools — Environment lifecycle
# =============================================================================


@mcp.tool()
@handle_errors
@with_env_lock
def delete_environment(env_name: str, ctx: Context | None = None) -> str:
    """
    Stop and remove all resources associated with an Odoo environment.

    Args:
        env_name: The name of the environment to tear down.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    warnings = env_ops.delete_environment(settings, team, env_name)
    result = f"Environment '{env_name}' has been torn down."
    if warnings:
        result += "\n\n⚠️ Warnings:\n" + "\n".join(f"- {w}" for w in warnings)
    return result


@mcp.tool()
@handle_errors
def list_environments(ctx: Context | None = None) -> str:
    """
    List all managed Odoo environments.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    envs = env_ops.list_environments(settings, team)
    if not envs:
        return "No active Flow environments found."

    output = "Active Environments:\n"
    for env in envs:
        status_line = f"- {env['env_name']} (Status: {env['status']})"
        if env.get("url"):
            status_line += f" - {env['url']}"
        output += status_line + "\n"
        output += f"  Git Branch: {env.get('git_branch') or env['env_name']}\n"
        if env.get("created_at"):
            output += f"  Created: {env['created_at']}\n"
        output += f"  Last Activity: {env.get('last_activity') or 'unknown'}\n"
        if env["status"] == "stopped" or env.get("stopped_at"):
            stopped_at = env.get("stopped_at") or "unknown"
            stopped_by = env.get("stopped_by") or "unknown"
            output += f"  Stopped At: {stopped_at} ({stopped_by})\n"
        output += f"  Protected: {'yes' if env.get('protected') else 'no'}\n"
        if env.get("stack"):
            output += f"  Stack: {env['stack']}\n"
        note = str(env.get("note") or "").strip()
        if note:
            note = note.replace("\n", "\n        ")
            output += f"  Note: {note}\n"
        if env.get("db_name"):
            output += f"  Database: {env['db_name']}\n"
        if env.get("odoo_image"):
            output += f"  Image: {env['odoo_image']}\n"
        if env.get("repo_url") and not env.get("local_path"):
            output += f"  Repo: {env['repo_url']}\n"
        if env.get("local_path"):
            output += f"  Live-mount: {env['local_path']}\n"
        if env.get("template_name"):
            output += f"  Template: {env['template_name']}\n"
        for container in env["containers"]:
            output += f"  * {container['name']} [{container['status']}] ({container['image']})\n"
    return output


@mcp.tool()
@handle_errors
@with_env_lock
def run_odoo_tests(
    env_name: str,
    modules: str,
    test_tags: str = "",
    upgrade: bool = True,
    summary_only: bool = False,
    ctx: Context | None = None,
) -> str:
    """
    Run Odoo tests for specific modules in an environment.

    The module must already be installed in the environment (by default it runs the
    tests via an upgrade, `-u`). Install it first with install_odoo_modules or
    pull_and_apply if it is not present; testing an uninstalled module yields
    "0 of 0 tests".

    Narrow a run to a single class or method with test_tags — a full module upgrade
    to re-run one test is usually wasted minutes.

    Args:
        env_name: The name of the environment.
        modules: Comma-separated list of already-installed modules to test.
        test_tags: Odoo `--test-tags` expression narrowing which tests run, e.g.
            "/my_module:TestInvoice" (one class), "/my_module:TestInvoice.test_total"
            (one method), or "-slow" (exclude a tag). Comma-separated, no spaces.
            Empty (default) runs every test of the listed modules. With
            upgrade=False, positive selectors must include one of the requested
            modules (for example "slow/my_module"); exclusion-only selectors such
            as "-slow" are automatically scoped to the requested modules.
        upgrade: Upgrade the modules before testing (default True, `odoo -u`). Set
            False to skip the upgrade for a much faster re-run when the code under
            test is already loaded in the database — but note that without an
            upgrade Odoo only collects **post_install** tests, so classes at the
            default at_install position (plain TransactionCase/TestCase) will report
            "0 tests". If a class you expect does not run, re-run with upgrade=True.
        summary_only: Return only Odoo's aggregate test result and an output_id
            instead of command logs. The complete output remains available through
            read_output. Default False preserves the verbose response.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    output = odoo_ops.run_environment_tests(
        settings, team, env_name, modules, test_tags=test_tags, upgrade=upgrade
    )
    source_args = (
        f"env={env_name}, modules={modules}, test_tags={test_tags}, upgrade={upgrade}"
    )
    if summary_only:
        cached = _cache_output(
            output, "run_odoo_tests", f"{source_args}, summary_only=True"
        )
        summary = _odoo_test_summary(output) or "Test summary not found"
        # Keep the wake note (whitespace-collapsed, so the response stays one
        # line): the caller still has to know the environment was stopped and
        # started by this call.
        note = " ".join(woke.split())
        prefix = f"{note} " if note else ""
        return f"{prefix}{summary} [output_id={cached.output_id}]"

    header = woke + f"Test Results for {env_name}:"
    return _maybe_cache(
        output,
        header,
        "run_odoo_tests",
        source_args,
    )


@mcp.tool()
@handle_errors
def get_environment_logs(
    env_name: str,
    n_lines: int = 100,
    grep: str = "",
    level: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Get the last N lines of logs from the Odoo container for a specific environment.

    Args:
        env_name: The name of the environment.
        n_lines: The number of recent log lines to retrieve (default 100).
        grep: Filter logs to only show lines matching this pattern (case-insensitive substring search). Useful to find specific errors, modules, or messages.
        level: Filter by Odoo log level. One of: "ERROR", "WARNING", "CRITICAL". Returns only lines containing the specified level marker. Can be combined with grep.
    """
    output = odoo_ops.get_environment_logs(
        _get_settings(),
        env_name,
        n_lines,
        grep=grep,
        level=level,
        team=_resolve_team(ctx),
    )
    return f"Recent logs for {env_name}:\n\n{_ANSI_RE.sub('', output)}"


@mcp.tool()
@handle_errors
@with_env_lock
def restart_environment(
    env_name: str, wait: bool = True, ctx: Context | None = None
) -> str:
    """
    Restart the Odoo container for a specific environment.

    Args:
        env_name: The name of the environment to restart.
        wait: Wait for Odoo to become ready after restart (default True). Polls /web/health every 2 seconds for up to 120 seconds.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.restart_environment(settings, env_name, team)
    activity.mark_started(team, env_name)
    lines = [
        "Environment restarted successfully!",
        f"Odoo Container: {result['odoo_container']}",
    ]
    if wait:
        team = _resolve_team(ctx)
        ready = env_ops.wait_for_odoo_ready(settings, team, env_name)
        if ready:
            lines.append("Odoo is ready.")
        else:
            lines.append(
                "Warning: Odoo did not become ready within 120 seconds. Check logs with get_environment_logs."
            )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_env_lock
def update_environment(
    env_name: str,
    env_vars: str = "",
    odoo_image: str = "",
    new_name: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Re-create the Odoo container for an environment without losing the database
    or filestore. Pulls the target image and re-creates the container.

    With no arguments this simply rebuilds the container from its current image
    and configuration — use it when the container is broken (e.g. packages were
    accidentally removed, system files corrupted) and you need a fresh container
    reconnected to the existing database and filestore.

    Pass odoo_image to switch to a different image, and/or env_vars to change the
    environment variables. The database and filestore are always preserved.

    Pass new_name to rename the environment — useful when its name no longer
    describes what it holds. The container has to be re-created for a rename
    anyway (it carries the name in its own name, its labels and its mounts), so
    the rename rides on this same recreate. The database, filestore, ports and
    credentials move with it. A pooled or explicit hostname is kept, so the URL
    stays the same; a hostname derived from the environment name follows the
    new name. The scoped MCP endpoint moves from /mcp/<old name> to
    /mcp/<new name>, so an MCP client configured against the old path has to
    be re-pointed. Production environments and stack members cannot be renamed
    here: their names are recorded elsewhere. Use
    switch_branch instead when the environment should also move onto another
    git branch — it renames along the way.

    Args:
        env_name: The name of the environment to update.
        env_vars: Comma- or newline-separated KEY=VALUE pairs that fully replace the current user-supplied env vars (e.g. "WORKERS=4,LIMIT_TIME_CPU=900"). Commas inside values are preserved unless what follows the comma looks like another KEY=; put one pair per line when in doubt. Leave empty to keep the current env vars. The database connection variables (HOST/USER/PASSWORD) are always preserved. A value "secret:<name>" references a team secret (see list_secrets): the real value is injected only inside the container and is never readable back.
        odoo_image: New Docker image with tag to pull and run (e.g. "odoo:19.0"). Leave empty to keep the current image.
        new_name: Optional new name for the environment. Leave empty to keep the current name.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    parsed_env = parse_env_vars(env_vars) or None
    rename_to = (new_name or "").strip()
    if rename_to and rename_to != env_name:
        # The tool decorator locks env_name only; hold the target name too, so a
        # concurrent create_environment cannot claim it mid-rename.
        _locks.acquire_env(rename_to, team.team_id, operation="update_environment")
    else:
        rename_to = ""
    try:
        result = env_ops.update_environment(
            settings,
            team,
            env_name,
            env_override=parsed_env,
            image_override=odoo_image or None,
            rename_to=rename_to or None,
        )
    finally:
        if rename_to:
            _locks.release_env(rename_to)
    renamed_from = result.get("renamed_from") or ""
    final_env_name = str(result.get("env_name") or env_name)
    lines = [
        (
            f"Environment renamed from '{renamed_from}' to '{final_env_name}' "
            "and updated successfully!"
            if renamed_from
            else "Environment updated successfully!"
        ),
        f"URL: {result['url']}",
        f"Hostname: {result.get('hostname', '')}",
        f"Odoo Container: {result['odoo_container']}",
        f"Database: {result['database']}",
        f"Workspace: {result['workspace']}",
        f"Image: {result.get('image', '')}"
        + (" (updated)" if result.get("image_updated") else ""),
    ]
    env_result = result.get("env_vars") or {}
    if env_result:
        lines.append(
            "Env vars: " + ", ".join(f"{k}={v}" for k, v in env_result.items())
        )
    if renamed_from:
        lines.append(
            f"Use env_name='{final_env_name}' from now on; its scoped MCP endpoint "
            f"is /mcp/{final_env_name}."
        )
    setup_logs = result.get("setup_logs", [])
    if setup_logs:
        lines.append("\n--- Setup Log ---")
        lines.extend(setup_logs)
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def get_environment_info(env_name: str, ctx: Context | None = None) -> str:
    """
    Get comprehensive information about an environment.

    Returns database name, URL, repository, image, template, extra addons,
    workspace path, container status, and CPU/RAM stats.

    Args:
        env_name: The name of the environment to check.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    info = env_ops.get_environment_info(settings, team, env_name)
    overall = (
        "All containers running"
        if info["all_running"]
        else "Some containers not running"
    )
    lines = [f"Environment Info for '{env_name}': {overall}"]
    lines.append(f"Git Branch: {info.get('git_branch') or env_name}")
    if info.get("created_at"):
        lines.append(f"Created: {info['created_at']}")
    lines.append(f"Last Activity: {info.get('last_activity') or 'unknown'}")
    odoo_status = info.get("odoo", {})
    if info.get("stopped_at") or (
        not odoo_status.get("running") and odoo_status.get("status") != "not found"
    ):
        stopped_at = info.get("stopped_at") or "unknown"
        stopped_by = info.get("stopped_by") or "unknown"
        lines.append(f"Stopped At: {stopped_at} ({stopped_by})")
    lines.append(f"Protected: {'yes' if info.get('protected') else 'no'}")
    if info.get("stack"):
        lines.append(f"Stack: {info['stack']}")
    note = str(info.get("note") or "").strip()
    if note:
        note = note.replace("\n", "\n      ")
        lines.append(f"Note: {note}")
    lines.append(f"Database: {info['db_name']}")
    if info.get("url"):
        lines.append(f"URL: {info['url']}")
    if info.get("hostname"):
        lines.append(f"Hostname: {info['hostname']}")
    if info.get("repo_url") and not info.get("local_path"):
        lines.append(f"Repo: {info['repo_url']}")
    if info.get("local_path"):
        lines.append("Code delivery: live-mount")
        lines.append(f"Live-mount: {info['local_path']}")
    if info.get("odoo_image"):
        lines.append(f"Image: {info['odoo_image']}")
    if info.get("template_name"):
        lines.append(f"Template: {info['template_name']}")
    if info.get("extra_addons"):
        addons = ", ".join(f"{k}:{v}" for k, v in info["extra_addons"].items())
        lines.append(f"Extra addons: {addons}")
    if info.get("env_vars"):
        env_display = ", ".join(f"{k}={v}" for k, v in info["env_vars"].items())
        lines.append(f"Env vars: {env_display}")
    if info.get("workspace"):
        lines.append(f"Workspace: {info['workspace']}")
    for key in ("odoo", "db"):
        cinfo = info[key]
        label = "Odoo" if key == "odoo" else "DB (shared)"
        line = f"{label}: {cinfo['status']}"
        if "cpu_percent" in cinfo:
            line += f" | CPU: {cinfo['cpu_percent']}% | RAM: {cinfo['mem_usage_mb']} MB ({cinfo['mem_percent']}%)"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_env_lock
def stop_environment(env_name: str, ctx: Context | None = None) -> str:
    """
    Stop the Odoo container for a specific environment.

    Args:
        env_name: The name of the environment to stop.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    activity.touch(team, env_name)
    result = env_ops.stop_environment(settings, team, env_name)
    return (
        f"Environment stopped successfully!\n"
        f"Stopped containers: {', '.join(result['stopped'])}"
    )


@mcp.tool()
@handle_errors
@with_env_lock
def start_environment(
    env_name: str, wait: bool = True, ctx: Context | None = None
) -> str:
    """
    Start all containers for a specific environment.

    Args:
        env_name: The name of the environment to start.
        wait: Wait for Odoo to become ready after start (default True). Polls /web/health every 2 seconds for up to 120 seconds.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.start_environment(settings, env_name, team)
    activity.mark_started(team, env_name)
    lines = [
        "Environment started successfully!",
        f"Started containers: {', '.join(result['started'])}",
    ]
    if wait:
        ready = env_ops.wait_for_odoo_ready(settings, team, env_name)
        if ready:
            lines.append("Odoo is ready.")
        else:
            lines.append(
                "Warning: Odoo did not become ready within 120 seconds. Check logs with get_environment_logs."
            )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_env_lock
def pull_and_apply(
    env_name: str,
    install: str = "",
    upgrade: str = "",
    restart: bool = False,
    strict: bool = False,
    summary_only: bool = False,
    ctx: Context | None = None,
) -> str:
    """
    Sync the latest code into an environment and apply the right Odoo action.

    Works for both code-delivery modes (chosen automatically per environment):
    - git: pulls the branch and resolves extra-addons to shared immutable
      SHA checkouts before applying changes.
    - live-mount (from create_environment(local_path=...)): your
      edits are already live on disk; this just applies them — no git needed.

    Two ways to drive it:
    - EXPLICIT (recommended — you know what you changed): pass `install` /
      `upgrade` (comma-separated module names) and/or `restart=True`. A guardrail
      compares your request against the detected changes and appends non-blocking
      warnings if something looks missing (e.g. you only restarted but a module's
      data/schema changed and needs -u). With `strict=True` it refuses instead of
      warning.
    - AUTO (leave install/upgrade/restart empty): Oduflow analyzes changed files
      and decides install/upgrade/restart/refresh itself. Best when pulling
      commits you did not author.

    Decision rules — what to pass explicitly:
    - Only view/QWeb XML, JS, CSS changed → nothing; refresh the browser.
    - Python logic/methods changed (no new fields/models) → restart=True.
    - A field/model, security or data records, ir.cron, mail templates, or
      manifest data/depends changed → upgrade="module" (-u): these live in the
      database and a restart won't load them.
    - A brand-new module was added → install="module" (-i).
    - Dependency files (`requirements.txt`, `.oduflow/requirements.txt`,
      `.oduflow/apt_packages.txt`) changed → Oduflow reinstalls dependencies into
      the running container and restarts automatically; no action needed. (Packages
      removed from the file are not uninstalled until the container is rebuilt via
      update_environment.)

    Errors and tracebacks are returned directly in this response — do NOT call
    get_environment_logs to check for them. With summary_only=True they are not
    inlined: the response reports the exit status and an `output_id`, and the
    full log is read with read_output.

    Args:
        env_name: The environment to apply changes to.
        install: Comma-separated modules to install (-i).
        upgrade: Comma-separated modules to upgrade (-u).
        restart: Restart the Odoo container (for Python-only changes).
        strict: If True, refuse to apply when the guardrail finds a likely
            missing action (default False: warn but apply anyway).
        summary_only: Return a compact one-line action/status summary instead of
            command logs and changed-file names. Raw output is cached and remains
            available through read_output. Default False preserves the verbose
            response.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke_note = _wake_for_work(settings, team, env_name, "to apply the changes")
    install_list = [m.strip() for m in install.split(",") if m.strip()]
    upgrade_list = [m.strip() for m in upgrade.split(",") if m.strip()]
    result = env_ops.pull_environment(
        settings,
        team,
        env_name,
        install=install_list or None,
        upgrade=upgrade_list or None,
        restart=restart,
        strict=strict,
    )
    action = result["action"]
    warnings = result.get("warnings") or []

    if action == "blocked":
        lines = [woke_note + "BLOCKED by guardrail (strict mode):"]
        lines.extend(f"  ⚠ {w}" for w in warnings)
        lines.append("")
        lines.append(result["message"])
        return "\n".join(lines)

    if action == "none":
        return cast(str, woke_note + result["message"])

    header_lines = [woke_note + result["message"]]
    if result.get("modules_installed"):
        header_lines.append(f"Installed: {', '.join(result['modules_installed'])}")
    if result.get("modules_upgraded"):
        header_lines.append(f"Upgraded: {', '.join(result['modules_upgraded'])}")
    header_lines.append(f"Changed files ({len(result.get('changed_files', []))}):")
    for f in result.get("changed_files", [])[:20]:
        header_lines.append(f"  - {f}")
    if len(result.get("changed_files", [])) > 20:
        header_lines.append(f"  ... and {len(result['changed_files']) - 20} more")
    if warnings:
        header_lines.append("Guardrail warnings (applied anyway):")
        header_lines.extend(f"  ⚠ {w}" for w in warnings)

    header = "\n".join(header_lines)
    output = result.get("output", "")
    if summary_only:
        return _compact_pull_result(result, woke_note, output, env_name)
    if output:
        return _maybe_cache(output, header, "pull_and_apply", f"env={env_name}")
    return header


@mcp.tool()
@handle_errors
@with_env_lock
def switch_branch(
    env_name: str,
    branch: str,
    install: str = "",
    upgrade: str = "",
    restart: bool = False,
    strict: bool = False,
    extra_addons: str = "",
    new_name: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Move an existing environment onto another git branch.

    Everything except the code stays: the database, filestore, URL / hostname,
    ports, database credentials and the scoped MCP token. Reach for this when
    the team has no free environment slots left and a previous branch is
    finished (merged), or when that database and URL are worth keeping — it
    replaces "delete the old environment, create a fresh one", which re-clones
    the repository and re-copies the template database. With slots still free,
    create_environment is the simpler path.

    The branch must already exist on origin, so push it first. Oduflow then
    diffs the old and new tips and applies exactly the logic of pull_and_apply:
    pass install / upgrade / restart explicitly when you know what changed, or
    leave them empty to let Oduflow classify the difference itself.

    Pass new_name to rename the environment in the same operation — useful when
    its name still echoes the finished branch. The URL is kept, but the scoped
    MCP endpoint moves from /mcp/<old name> to /mcp/<new name>, so an MCP client
    configured against the old path has to be re-pointed.

    Switching does not inspect which modules are installed in the retained
    database. If the target code is incompatible with that database, the apply
    command returns the real failure or Odoo reports it at runtime. Live-mounted
    environments are rejected — there the checkout is yours, so switch the
    branch in it and call pull_and_apply.

    Errors and tracebacks are returned directly in this response — do NOT call
    get_environment_logs to check for them.

    Args:
        env_name: The environment to move onto another branch.
        branch: Target git branch; it must already exist on origin.
        install: Comma-separated modules to install (-i). Leave empty for automatic classification.
        upgrade: Comma-separated modules to upgrade (-u). Leave empty for automatic classification.
        restart: Restart the Odoo container (for Python-only differences).
        strict: Refuse instead of warning when the requested action looks incomplete for the diff.
        extra_addons: Optional comma-separated extra addon repos with branches (e.g. "enterprise:19.0,custom-themes:main") to switch along with the main repo. Leave empty to keep the current ones.
        new_name: Optional new name for the environment. Leave empty to keep the current name.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke_note = _wake_for_work(settings, team, env_name, "to switch the branch")
    rename_to = (new_name or "").strip()
    if rename_to and rename_to != env_name:
        # The tool decorator locks env_name only; hold the target name too, so a
        # concurrent create_environment cannot claim it mid-rename.
        _locks.acquire_env(rename_to, team.team_id, operation="switch_branch")
    else:
        rename_to = ""
    try:
        result = env_ops.switch_environment_branch(
            settings,
            team,
            env_name,
            branch,
            install=[m.strip() for m in install.split(",") if m.strip()] or None,
            upgrade=[m.strip() for m in upgrade.split(",") if m.strip()] or None,
            restart=restart,
            strict=strict,
            extra_addons=_parse_extra_addons(extra_addons) if extra_addons else None,
            new_name=rename_to or None,
        )
    finally:
        if rename_to:
            _locks.release_env(rename_to)
    action = result["action"]
    warnings = result.get("warnings") or []

    if action == "blocked":
        lines = [woke_note + "BLOCKED (strict mode):"]
        lines.extend(f"  ⚠ {w}" for w in warnings)
        lines.append("")
        lines.append(result["message"])
        return "\n".join(lines)

    final_env_name = str(result.get("env_name") or env_name)
    header_lines = [woke_note + result["message"]]
    if result.get("renamed_from"):
        header_lines.append(
            f"Use env_name='{final_env_name}' from now on; its scoped MCP endpoint "
            f"is /mcp/{final_env_name}."
        )
    if result.get("modules_installed"):
        header_lines.append(f"Installed: {', '.join(result['modules_installed'])}")
    if result.get("modules_upgraded"):
        header_lines.append(f"Upgraded: {', '.join(result['modules_upgraded'])}")
    changed = result.get("changed_files", [])
    if changed:
        header_lines.append(f"Changed files ({len(changed)}):")
        header_lines.extend(f"  - {f}" for f in changed[:20])
        if len(changed) > 20:
            header_lines.append(f"  ... and {len(changed) - 20} more")
    if warnings:
        header_lines.append("Warnings (switched anyway):")
        header_lines.extend(f"  ⚠ {w}" for w in warnings)

    header = "\n".join(header_lines)
    output = result.get("output", "")
    if output:
        return _maybe_cache(
            output, header, "switch_branch", f"env={final_env_name}, branch={branch}"
        )
    return header


# =============================================================================
# MCP Tools — Output cache drill-down
# =============================================================================


@mcp.tool()
@handle_errors
def read_output(
    output_id: str,
    mode: str = "lines",
    start: int = 1,
    end: int = 0,
    grep: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Read from a cached tool output by its ID.

    After calling tools like install_odoo_modules, upgrade_odoo_modules,
    run_odoo_tests, etc., large outputs are cached on the server. The tool
    response includes an output_id and metadata. Use this tool to explore
    the cached output interactively.

    Modes:
    - "lines" (default): Return a range of lines. Use start/end for pagination
      (1-indexed). Default: first 200 lines. Example: start=100, end=200.
    - "errors": Return only ERROR/WARNING/CRITICAL lines with ±5 lines of context.
    - "grep": Search for a pattern (case-insensitive substring). Returns matching
      lines with line numbers. Combine with start/end to paginate results.
    - "info": Return metadata only — line count, char count, error count, source tool.
    - "tail": Return last 100 lines.

    Args:
        output_id: The cached output ID (e.g. "a3f7c012"), returned by the original tool.
        mode: Read mode — "lines", "errors", "grep", "info", "tail".
        start: First line number to return (1-indexed, default 1). Used with mode="lines" and "grep".
        end: Last line number to return (0 = start+200 for "lines", all results for "grep").
        grep: Search pattern for mode="grep". Case-insensitive substring match.
    """
    import time as _time

    cached = _output_cache.get(output_id)
    if cached is None:
        return f"Output '{output_id}' not found or expired (TTL: 1 hour)."

    lines = cached.lines
    total = cached.total_lines

    if mode == "info":
        return (
            f"Cached output: {output_id}\n"
            f"Source: {cached.source_tool}({cached.source_args})\n"
            f"Lines: {total}\n"
            f"Characters: {cached.total_chars}\n"
            f"Errors/Warnings: {len(cached.error_line_indices)} lines\n"
            f"Age: {int(_time.time() - cached.created_at)}s"
        )

    if mode == "errors":
        if not cached.error_line_indices:
            return "No errors or warnings found in cached output."
        result_lines: list[str] = []
        seen: set[int] = set()
        for idx in cached.error_line_indices:
            ctx_start = max(0, idx - 2)
            ctx_end = min(total, idx + 6)
            for i in range(ctx_start, ctx_end):
                if i not in seen:
                    result_lines.append(f"{i + 1:>6}| {lines[i]}")
                    seen.add(i)
            result_lines.append("")
        return "\n".join(result_lines)

    if mode == "grep":
        if not grep:
            return "Error: grep parameter is required for mode='grep'."
        pattern = grep.lower()
        matches = []
        for i, line in enumerate(lines):
            if pattern in line.lower():
                matches.append(f"{i + 1:>6}| {line}")
        if not matches:
            return f"No matches for '{grep}' in {total} lines."
        s = max(start - 1, 0)
        e = end if end > 0 else s + 200
        page = matches[s:e]
        header = f"Matches for '{grep}': {len(matches)} total (showing {s + 1}-{min(e, len(matches))})"
        return header + "\n" + "\n".join(page)

    if mode == "tail":
        tail = lines[-100:]
        start_num = total - len(tail) + 1
        numbered = [f"{start_num + i:>6}| {ln}" for i, ln in enumerate(tail)]
        return f"Last {len(tail)} lines (of {total}):\n" + "\n".join(numbered)

    # mode == "lines" (default)
    s = max(start - 1, 0)
    e = end if end > 0 else s + 200
    e = min(e, total)
    page = lines[s:e]
    numbered = [f"{s + i + 1:>6}| {ln}" for i, ln in enumerate(page)]
    header = f"Lines {s + 1}-{e} of {total}:"
    return header + "\n" + "\n".join(numbered)


# =============================================================================
# MCP Tools — Container image builds
#
# Available only to teams with a [team.X.image_registry] section: for everyone
# else the tools raise a clear prerequisite error (same pattern as production
# tools behind [production].enabled). The agent gets no Docker socket and no
# credentials — Oduflow builds and pushes on its behalf, constrained to the
# team's configured registry namespace. See specs/0054.
# =============================================================================


def _image_registry_settings(team: TeamSettings) -> ImageRegistrySettings:
    registry = team.image_registry
    if registry is None:
        raise PrerequisiteNotMetError(
            "Container image building is not configured for this team. An "
            f"operator must add a [team.{team.team_id}.image_registry] section "
            "(repository_prefix, optional credentials) to oduflow.toml and "
            "restart Oduflow."
        )
    return registry


@mcp.tool()
@handle_errors
@with_env_lock
def start_image_build(
    env_name: str,
    dockerfile: str = "Dockerfile",
    context: str = ".",
    target: str = "",
    build_args: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Build a container image from the environment's current git revision.

    The build source is the exact HEAD commit of the environment's managed
    checkout at call time (push your commits and pull_and_apply first). The
    build runs asynchronously server-side; this call returns a build_id
    immediately. Poll progress with get_image_build, then publish a succeeded
    build to the team's configured registry namespace with publish_image_build.

    Build arguments are non-secret: their values reach the Dockerfile and the
    image history. Never pass credentials.

    Args:
        env_name: The environment whose checkout to build from.
        dockerfile: Dockerfile path relative to the context (default "Dockerfile").
        context: Build context directory relative to the repository root (default ".").
        target: Optional multi-stage build target.
        build_args: Comma-separated KEY=VALUE Docker build arguments (non-secret).
    """
    from oduflow.docker_ops import build_ops

    settings = _get_settings()
    team = _resolve_team(ctx)
    registry_cfg = _image_registry_settings(team)
    context_path = image_builds.validate_relative_path(context, "context")
    dockerfile_path = image_builds.validate_relative_path(dockerfile, "dockerfile")
    if dockerfile_path == ".":
        raise ToolError("dockerfile must be a file path, not '.'")
    args = parse_env_vars(build_args) if build_args.strip() else {}
    repo_path, branch, owner_id = build_ops.env_checkout_for_build(
        settings, team, env_name
    )
    job = image_builds.store.create(
        team,
        env_name,
        branch,
        dockerfile_path,
        context_path,
        target=target.strip(),
        build_arg_names=list(args),
        owner_id=owner_id,
        max_concurrent_builds=registry_cfg.max_concurrent_builds,
    )
    try:
        # The tool's environment lock is held through this snapshot, so the
        # sealed context is exactly one commit; the background build then runs
        # lock-free against the immutable copy.
        build_ops.snapshot_context(team, registry_cfg, job, repo_path)
    except Exception as exc:
        image_builds.store.finish(
            team,
            job,
            image_builds.STATUS_FAILED,
            error=redact_url_credentials(str(exc)),
        )
        raise
    build_ops.run_build_async(team, registry_cfg, job, args)
    return (
        f"Image build started.\n"
        f"build_id: {job.build_id}\n"
        f"Source: branch '{branch}' @ {job.commit[:12]} "
        f"(dockerfile {dockerfile_path}, context {context_path})\n"
        f"Timeout: {registry_cfg.build_timeout_seconds}s\n\n"
        f'Poll with get_image_build(build_id="{job.build_id}"). Once succeeded, '
        f'publish with publish_image_build(build_id="{job.build_id}", '
        f'repository="<name>", tags="1.0.0,latest") — the destination is '
        f"{registry_cfg.host}/{registry_cfg.repository_prefix}/<name>."
    )


@mcp.tool()
@handle_errors
def get_image_build(
    env_name: str,
    build_id: str,
    tail_lines: int = 100,
    ctx: Context | None = None,
) -> str:
    """
    Status and log tail of an image build started with start_image_build.

    Args:
        env_name: The environment the build belongs to.
        build_id: The build to inspect.
        tail_lines: Number of trailing build log lines to include (default 100).
    """
    from oduflow.docker_ops import build_ops

    team = _resolve_team(ctx)
    _image_registry_settings(team)
    settings = _get_settings()
    owner_id = build_ops.environment_owner_id(settings, team, env_name)
    job = image_builds.store.load(team, env_name, owner_id, build_id)
    lines = [
        f"Build {job.build_id}: {job.status}",
        f"Source: branch '{job.branch}' @ {job.commit[:12]}",
        f"Created: {job.created_at}"
        + (f", finished: {job.finished_at}" if job.finished_at else ""),
    ]
    if job.error:
        lines.append(f"Error: {job.error}")
    if job.status == image_builds.STATUS_SUCCEEDED:
        lines.append(
            f'Ready to publish: publish_image_build(build_id="{job.build_id}", '
            'repository="<name>", tags="...").'
        )
    for pub in job.publications:
        for res in pub.get("results", []):
            outcome = res.get("error") or res.get("digest") or "pushed"
            lines.append(
                f"Published {pub.get('repository', '')}:{res.get('tag', '')} "
                f"({pub.get('at', '')}): {outcome}"
            )
    tail = image_builds.store.read_log_tail(team, job.build_id, tail_lines)
    if tail:
        lines.append("")
        lines.append(f"--- build log (last {tail_lines} lines) ---")
        lines.append(tail.rstrip("\n"))
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def publish_image_build(
    env_name: str,
    build_id: str,
    repository: str,
    tags: str,
    ctx: Context | None = None,
) -> str:
    """
    Publish a succeeded image build to the team's configured registry.

    Copies the exact built image (never a rebuild) to every requested tag under
    the team's repository prefix. Any syntactically valid tag is accepted,
    including `latest` and semver; overwriting an existing tag is intentional
    and last-writer-wins. Tags are pushed one by one — partial success is
    possible and each tag reports its own outcome.

    Args:
        env_name: The environment the build belongs to.
        build_id: A build in status "succeeded".
        repository: Destination repository below the configured prefix (e.g. "app").
        tags: Comma-separated tags to publish, e.g. "1.4.0,latest".
    """
    from oduflow.docker_ops import build_ops
    from oduflow.locking import image_publish_lock_key

    settings = _get_settings()
    team = _resolve_team(ctx)
    registry_cfg = _image_registry_settings(team)
    repo = image_builds.validate_repository(repository)
    tag_list = image_builds.validate_tags(tags)
    owner_id = build_ops.environment_owner_id(settings, team, env_name)
    job = image_builds.store.load(team, env_name, owner_id, build_id)
    if job.status != image_builds.STATUS_SUCCEEDED:
        raise PrerequisiteNotMetError(
            f"Build '{job.build_id}' is '{job.status}', not 'succeeded'; only a "
            "succeeded build can be published."
        )
    destination = f"{registry_cfg.host}/{registry_cfg.repository_prefix}/{repo}"
    key = image_publish_lock_key(team.team_id, destination)
    _locks.acquire_env(key, operation="publish_image_build")
    try:
        publication = build_ops.publish(team, registry_cfg, job, repo, tag_list)
    finally:
        _locks.release_env(key)
    lines = [
        f"Publish results for build {job.build_id} "
        f"({registry_cfg.host}/{publication['repository']}):"
    ]
    failures = 0
    for res in publication["results"]:
        if res["error"]:
            failures += 1
            lines.append(f"  ✗ {res['tag']}: {res['error']}")
        else:
            lines.append(f"  ✓ {res['tag']}: {res['digest'] or 'pushed'}")
    if failures:
        lines.append(
            f"{failures} of {len(publication['results'])} tags failed; the "
            "call is retryable per tag."
        )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def cancel_image_build(env_name: str, build_id: str, ctx: Context | None = None) -> str:
    """
    Cancel a running image build.

    The build worker and its Docker connection are terminated even when the
    current Dockerfile step is not producing output; the job then moves to
    "cancelled".

    Args:
        env_name: The environment the build belongs to.
        build_id: The build to cancel.
    """
    team = _resolve_team(ctx)
    _image_registry_settings(team)
    settings = _get_settings()
    from oduflow.docker_ops import build_ops

    owner_id = build_ops.environment_owner_id(settings, team, env_name)
    job = image_builds.store.load(team, env_name, owner_id, build_id)
    if job.status in image_builds.TERMINAL_STATUSES:
        return f"Build {job.build_id} is already '{job.status}'; nothing to cancel."
    event = image_builds.store.cancel_event(job.build_id)
    if event is None:
        return f"Build {job.build_id} is no longer running."
    event.set()
    return (
        f"Cancellation requested for build {job.build_id}. Check "
        f'get_image_build(build_id="{job.build_id}") for the final status.'
    )


# =============================================================================
# MCP Tools — Odoo operations
# =============================================================================


@mcp.tool()
@handle_errors
@with_env_lock
def install_odoo_modules(
    env_name: str, modules: str, ctx: Context | None = None
) -> str:
    """
    Install Odoo modules in an environment.

    Args:
        env_name: The name of the environment.
        modules: Comma-separated list of modules to install (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.install_odoo_modules(settings, team, env_name, *modules_list)
    exit_code = result["exit_code"]
    modules_str = ", ".join(result["modules"])
    output = result.get("output", "")
    if exit_code == 0:
        env_ops.restart_environment(settings, env_name, team)
        header = f"{woke}Success. Modules installed: {modules_str}. Container restarted. Exit code: 0."
    else:
        header = f"{woke}Error. Modules: {modules_str}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "install_odoo_modules",
        f"env={env_name}, modules={modules}",
    )


@mcp.tool()
@handle_errors
@with_env_lock
def upgrade_odoo_modules(
    env_name: str, modules: str, ctx: Context | None = None
) -> str:
    """
    Upgrade installed Odoo modules in an environment.

    Unknown or uninstalled modules are rejected with a suggestion to use
    install_odoo_modules or pull_and_apply(install=...) instead.

    Args:
        env_name: The name of the environment.
        modules: Comma-separated list of modules to upgrade (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.upgrade_odoo_modules(settings, team, env_name, *modules_list)
    exit_code = result["exit_code"]
    modules_str = ", ".join(result["modules"])
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    header = f"{woke}{status}. Modules: {modules_str}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "upgrade_odoo_modules",
        f"env={env_name}, modules={modules}",
    )


_TERM_KINDS = {
    "model": "field labels, help, selection values, model names",
    "model_terms": "view arch, action help",
    "code": "Python _() / _lt(), JS _t()",
    "other": "unrecognised reference kind",
}
# A list this short is a to-do list; a longer one is a statement about the file
# as a whole, which the counts already make. The full set is in the exported
# catalogue either way, so nothing is lost by not printing it.
_TERM_LIST_MAX = 20
# Enough of the source string to recognise the term next to its reference.
_TERM_SNIPPET = 60


def _format_term_counts(by_type: dict[str, int]) -> list[str]:
    return [
        f"  {kind:<12} {count:>5}  {_TERM_KINDS.get(kind, '')}"
        for kind, count in by_type.items()
    ]


def _format_term(entry: PoEntry) -> str:
    """One term on one line: where it lives, then what it says.

    A ``model_terms`` msgid is a whole view or report body, newlines included,
    so printing it verbatim buries the report. The ``#:`` reference is the part
    that says which field or view to open.
    """
    text = " ".join(entry.msgid.split())
    if len(text) > _TERM_SNIPPET:
        text = text[: _TERM_SNIPPET - 1].rstrip() + "…"
    reference = entry.occurrences[0] if entry.occurrences else ""
    return f'{reference}  "{text}"' if reference else f'"{text}"'


_VERDICT_LABEL = {
    po_tools.OK: "OK",
    po_tools.PARTIAL: "PARTIAL",
    po_tools.NOT_LOADED: "NOT LOADED",
    po_tools.NOT_TRANSLATED: "NOT TRANSLATED",
    po_tools.IMPORT_DROPPED: "IMPORT SILENTLY DROPPED",
    po_tools.IMPORT_ABORTS: "IMPORT ABORTS",
    po_tools.NO_FILE: "NO FILE",
    po_tools.NOT_ACTIVATED: "NOT ACTIVATED",
}


def _format_next_step(
    status: po_tools.LangStatus, env_name: str, module: str, lang: str
) -> list[str]:
    """The one action that moves this language forward, as a callable line.

    The verdict is only half an answer; the caller ran this tool to find out
    what to do, and the fix differs per failure — a catalogue that was never
    loaded and one that Odoo read and discarded look identical in the counts.
    """
    export = f'export_module_translations("{env_name}", "{module}", "{lang}")'
    export_pot = f'export_module_translations("{env_name}", "{module}")'
    upgrade = f'upgrade_odoo_modules("{env_name}", "{module}")'
    fill_in = f"Next: {export}, fill in the msgstr values, then {upgrade}."

    if status.code == po_tools.OK:
        return []
    if status.code == po_tools.NOT_ACTIVATED:
        return [
            f"Next: activate {lang} in Odoo (Settings → Translations → "
            f"Languages), then {upgrade}."
        ]
    if status.code in (po_tools.NO_FILE, po_tools.NOT_TRANSLATED):
        return [fill_in]
    if status.code in (po_tools.IMPORT_DROPPED, po_tools.IMPORT_ABORTS):
        return [
            f"Next: {export_pot} writes the sibling .pot that Odoo merges for "
            f"this metadata — commit it beside the .po, then {upgrade}.",
        ]
    if status.code == po_tools.NOT_LOADED:
        return [f"Next: {upgrade} loads the file into the database."]
    if status.not_applied:
        return [f"Next: {upgrade} re-reads the file into the database."]
    return [f"Next: {export} merges the module's new terms into the file."]


def _format_term_list(label: str, terms: list[PoEntry]) -> list[str]:
    """A bullet list, nested one level under its own label's indent.

    Beyond :data:`_TERM_LIST_MAX` only the count is printed: a truncated list of
    alphabetically-first terms is not a to-do list, and the reader still has to
    export the catalogue to act on it.
    """
    if not terms:
        return []
    indent = " " * (len(label) - len(label.lstrip()) + 2)
    if len(terms) > _TERM_LIST_MAX:
        return [f"{label} ({len(terms)}) — too many to list; export the catalogue."]
    return [f"{label} ({len(terms)}):"] + [
        f"{indent}- {_format_term(term)}" for term in terms
    ]


@mcp.tool()
@handle_errors
@with_env_lock
def export_module_translations(
    env_name: str, module: str, lang: str = "", ctx: Context | None = None
) -> str:
    """
    Export a module's translation catalogue using Odoo's own exporter.

    Without `lang` this produces the `.pot` template: every translatable term
    with an empty translation, including the `_()` / `_lt()` messages from the
    module's Python sources. With `lang` it produces a `.po` whose translations
    are filled from what the database currently holds — useful for seeing what
    actually got applied.

    The file is written to the module's own `i18n/` directory inside the
    container, which is a read-write mount of the environment's checkout, so in
    live-mount mode it lands directly in your working tree. The response carries
    only a summary plus a one-time download URL, never the file body — a
    template for a mid-sized module is tens of kilobytes.

    Common use cases:
    - Get the authoritative term list before writing translations
    - Check that `_()` messages are being picked up (look at the `code` count)
    - Snapshot what the database holds for a language

    Args:
        env_name: The name of the environment.
        module: A single installed module's technical name (e.g. "sale_custom").
        lang: Optional locale to fill translations from (e.g. "pl_PL"). Omit for
              a `.pot` template.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.export_module_translations(settings, team, env_name, module, lang)
    summary = result["summary"]
    kind = f"'{lang}' translations" if lang else "template"

    lines = [f"Terms: {summary.entries}"]
    lines += _format_term_counts(summary.by_type)
    if lang:
        lines.append(
            f"Translated: {summary.translated} / {summary.entries} "
            f"({summary.untranslated} missing)"
        )
        lines += [""] + _format_term_list("Untranslated", summary.untranslated_terms)
    if not summary.by_type.get("code"):
        # The exporter finds these by walking addons_path and reading the
        # module's sources, so an empty count usually means the module's
        # directory is not itself an addons-path entry.
        lines += [
            "",
            "Note: no `code:` terms were exported. If this module has _() or "
            "_lt() messages, its directory is probably not directly on "
            "addons_path, so the exporter could not attribute its sources.",
        ]

    lines.append("")
    if result["written_path"]:
        lines.append(f"Written:   {result['written_path']}")
        if result["host_path"]:
            lines.append(f"Host path: {result['host_path']}")
    elif result["read_only_mount"]:
        lines.append(
            f"Not written: {result['module_dir']} is a shared extra-addons "
            "checkout, mounted read-only."
        )
    else:
        lines.append(
            f"Not written: no directory for '{module}' found under /mnt. "
            "Core Odoo modules ship their own translations."
        )

    content = str(result["content"]).encode("utf-8")
    if _web_bind is None:
        if not result["host_path"]:
            local_path = artifact_tokens.materialize(str(result["filename"]), content)
            lines.append(
                f"Host path: {local_path}  "
                "(temporary; removed when this Oduflow process stops)"
            )
    else:
        token = artifact_tokens.issue(str(result["filename"]), content)
        url = _artifact_url(settings, team, token)
        assert url is not None
        lines.append(f"Download:  {url}  (one-time, expires in 10 minutes)")

    header = f"{woke}Exported {module} ({kind}) from environment '{env_name}'."
    return _maybe_cache(
        "\n".join(lines),
        header,
        "export_module_translations",
        f"env={env_name}, module={module}, lang={lang}",
    )


@mcp.tool()
@handle_errors
@with_env_lock
def translation_status(
    env_name: str, module: str, langs: str = "", ctx: Context | None = None
) -> str:
    """
    Report whether a module's translations actually landed, and what to do next.

    Compares the term template Odoo derives from the module, the translations
    actually stored in the database, and the committed `i18n/<lang>.po` files,
    and returns a verdict per language rather than three catalogues to
    reconcile. Use this after loading translations: Odoo's importer is silent
    about the two ways a `.po` fails, and this is what makes them visible.

    - Entries with no `#:` reference line import as ZERO translations, with no
      warning at all, unless a sibling `<module>.pot` supplies the metadata.
    - Entries with no `#. module:` comment abort the import outright unless that
      sibling template supplies it.

    Each language gets a verdict (OK, PARTIAL, NOT LOADED, NOT TRANSLATED,
    IMPORT SILENTLY DROPPED, IMPORT ABORTS, NO FILE, NOT ACTIVATED), the
    coverage behind it, and the call that fixes it. Terms missing from the file
    or left stale in it are listed only when few enough to act on — otherwise
    export the catalogue, which holds them all.

    Args:
        env_name: The name of the environment.
        module: A single installed module's technical name.
        langs: Comma-separated locales to check (e.g. "pl_PL,ru_RU"). Omit to
               check every language activated in the database except en_US.
    """
    lang_list = [s.strip() for s in langs.split(",") if s.strip()]

    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.translation_status(
        settings, team, env_name, module, lang_list or None
    )

    template = result["template"]
    lines = [f"Module terms (template): {template.entries}"]
    lines += _format_term_counts(template.by_type)

    if not result["langs"]:
        lines += [
            "",
            "No languages to check. Activate one in Odoo (Settings → "
            "Translations → Languages) or pass `langs` explicitly.",
        ]

    for entry in result["langs"]:
        status = entry["status"]
        lines += ["", f"--- {entry['lang']} ---  {_VERDICT_LABEL[status.code]}"]
        lines.append(
            f"Coverage:  file {status.covered_terms}/{status.template_terms} "
            f"module terms, database {status.db_terms}/{status.template_terms} "
            f"translated ({status.coverage:.0%})."
        )
        if not entry["active"]:
            lines.append(
                "Language is NOT activated in this database, so translations "
                "have nowhere to load. Activate it first."
            )
        elif entry["file_path"]:
            po = entry["file"]
            effective = entry.get("import_effective", po)
            lines.append(f"File:      {entry['file_path']} — {po.entries} entries")
            if entry.get("metadata_template_path"):
                lines.append(
                    f"  Import metadata: merged from {entry['metadata_template_path']}"
                )
            if effective.no_reference:
                lines.append(
                    f"  ! {effective.no_reference} entries have no '#:' reference — "
                    "Odoo imports these as ZERO translations, silently."
                )
            if effective.no_module_comment:
                lines.append(
                    f"  ! {effective.no_module_comment} entries have no '#. module:' "
                    "comment — these abort the import with an error."
                )
            if status.not_applied:
                lines.append(
                    f"  ! {status.not_applied} translated entries in the file are "
                    "not in the database."
                )
            diff = entry["diff"]
            lines += _format_term_list("  Missing from the file", diff["missing"])
            lines += _format_term_list(
                "  Stale (no longer in the module)", diff["stale"]
            )
        else:
            lines.append(f"File:      no i18n/{entry['lang']}.po in the module.")
        lines += _format_next_step(status, env_name, module, entry["lang"])

    header = f"{woke}Translation status for {module} in environment '{env_name}'."
    return _maybe_cache(
        "\n".join(lines),
        header,
        "translation_status",
        f"env={env_name}, module={module}, langs={langs}",
    )


@mcp.tool()
@handle_errors
def read_file_in_odoo(
    env_name: str, path: str, read_range: str = "", ctx: Context | None = None
) -> str:
    """
    Read a text file or list a directory inside the Odoo container for a specific environment.

    Use this tool to inspect files in the container without constructing shell commands.
    Common use cases:
    - Read Odoo source code (e.g. /usr/lib/python3/dist-packages/odoo/addons/sale/models/sale_order.py)
    - Inspect addon structure (e.g. /mnt/extra-addons/)
    - Check config files (e.g. /etc/odoo/odoo.conf)
    - Verify file presence after pull_and_apply

    If the path is a directory, returns a listing (like `ls -la`).
    If the path is a text file, returns its contents (first 100KB by default).
    Binary files are not supported — use run_odoo_command for binary operations.

    Prefer this tool over run_odoo_command with `cat` or `ls` commands.

    Args:
        env_name: The name of the environment.
        path: Absolute path inside the container (e.g. "/mnt/extra-addons/my_module/__manifest__.py").
        read_range: Optional line range in format "START:END" (e.g. "1:50", "100:200").
                    If omitted, returns the entire file (up to 100KB).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.read_file_in_environment(
        settings, team, env_name, path, read_range
    )
    if "error" in result:
        return f"{woke}Error: {result['error']}"
    return cast(str, woke + result["output"])


@mcp.tool()
@handle_errors
@with_env_lock
def write_file_in_odoo(
    env_name: str,
    path: str,
    content: str,
    user: str = "odoo",
    ctx: Context | None = None,
) -> str:
    """
    Write a text file inside the Odoo container.

    Creates parent directories if they don't exist. Overwrites the file if it
    already exists. Content is transferred via container stdin to avoid
    shell escaping issues.

    Common use cases:
    - Write CSV files for data import
    - Create/modify odoo.conf settings
    - Write one-off Python scripts for odoo shell execution
    - Place test fixture files (demo data, config)

    Do NOT use this to edit source code in the repository — all code changes
    must go through git commit → git push → pull_and_apply.

    Args:
        env_name: The name of the environment.
        path: Absolute path inside the container (e.g. "/tmp/import_data.csv").
        content: Text content to write to the file.
        user: OS user to own the file (default "odoo"). Use "root" for system paths.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.write_file_in_environment(
        settings, team, env_name, path, content, user
    )
    return f"{woke}File written: {result['path']} ({result['size']} bytes)"


@mcp.tool()
@handle_errors
@with_env_lock
def run_odoo_shell(
    env_name: str,
    python_code: str,
    auto_commit: bool = True,
    ctx: Context | None = None,
) -> str:
    """
    Execute Python code in the Odoo shell context with full ORM access.

    The code runs inside `odoo shell` with access to `self.env`, all Odoo
    models, and the environment's database. Use `print()` to produce output
    that will be returned to you.

    Common use cases:
    - Test computed fields: print(self.env['sale.order'].search([]).mapped('amount_total'))
    - Create test records: self.env['res.partner'].create({'name': 'Test'})
    - Inspect models: print(self.env['ir.model.fields'].search([('model','=','sale.order')]).mapped('name'))
    - Debug business logic: check workflow transitions, access rights
    - Run data-fix scripts

    Transaction handling: `odoo shell` rolls back its cursor when the piped
    script finishes, so ORM writes would otherwise be discarded. With
    `auto_commit=True` (the default) the transaction is committed after your
    code runs, so a successful run is persisted; if the code raises an
    exception, the commit is never reached and the transaction is rolled back
    with the traceback returned. Pass `auto_commit=False` for a read-only /
    dry-run inspection where nothing should persist (Odoo rolls everything back
    at the end).

    Args:
        env_name: The name of the environment.
        python_code: Python code to execute. Use print() for output.
        auto_commit: Commit the transaction after a successful run (default
            True). Set to False to inspect without persisting any changes.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.run_odoo_shell(
        settings, team, env_name, python_code, auto_commit=auto_commit
    )
    exit_code = result["exit_code"]
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    header = f"{woke}{status}. Exit code: {exit_code}."
    return _maybe_cache(output, header, "run_odoo_shell", f"env={env_name}")


@mcp.tool()
@handle_errors
def http_request_to_odoo(
    env_name: str,
    path: str,
    method: str = "GET",
    body: str = "",
    headers: str = "",
    session_id: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Make an HTTP request to the running Odoo instance for a specific environment.

    Useful for testing web controllers, JSON-RPC API, REST endpoints, and
    verifying that Odoo responds correctly. The request is made from the
    host to the container's mapped port.

    Common use cases:
    - Health check: GET /web/health
    - JSON-RPC call: POST /jsonrpc with JSON body
    - Test a custom controller: GET /my/custom/endpoint
    - Verify access rights: check 200 vs 403 responses
    - Test REST API endpoints

    Args:
        env_name: The name of the environment.
        path: URL path (e.g. "/web/health", "/jsonrpc", "/my/invoices").
        method: HTTP method (default "GET"). One of GET, POST, PUT, DELETE.
        body: Request body as a string (typically JSON). Empty for GET requests.
        headers: Comma-separated KEY:VALUE pairs (e.g. "Content-Type:application/json,Accept:text/html").
        session_id: Odoo session ID for authenticated requests. Obtain by calling POST /web/session/authenticate first.
    """
    import json

    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    parsed_headers = None
    if headers:
        parsed_headers = dict(
            item.split(":", 1) for item in headers.split(",") if ":" in item
        )
    result = odoo_ops.http_request_to_odoo(
        settings, team, env_name, path, method, body, parsed_headers, session_id
    )
    status_code = result["status_code"]
    resp_headers = result.get("headers", {})
    resp_body = result.get("body", "")

    lines = [woke + f"HTTP {status_code}"]
    header_items = list(resp_headers.items())[:20]
    if header_items:
        lines.append(f"Headers: {json.dumps(dict(header_items), indent=2)}")
    if resp_body:
        lines.append(f"\nBody:\n{resp_body[:50000]}")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def search_in_odoo(
    env_name: str,
    pattern: str,
    path: str = "/mnt/extra-addons",
    glob: str = "*.py",
    max_results: int = 50,
    ctx: Context | None = None,
) -> str:
    """
    Search for a pattern in files inside the Odoo container.

    Runs a recursive grep inside the container and returns matching lines
    with file paths and line numbers. Useful for finding field definitions,
    method implementations, model usage across addons.

    Common use cases:
    - Find where a field is defined: pattern="x_custom_field", path="/mnt/extra-addons"
    - Find model usage: pattern="class SaleOrder", path="/usr/lib/python3/dist-packages/odoo/addons"
    - Find all imports of a module: pattern="from odoo.addons.sale"
    - Find XML record: pattern='id="action_sale_order"', glob="*.xml"

    Args:
        env_name: The name of the environment.
        pattern: Search pattern (fixed string, case-sensitive). Regex is not supported to avoid escaping issues.
        path: Directory to search in (default "/mnt/extra-addons"). Use "/usr/lib/python3/dist-packages/odoo/addons" to search Odoo core.
        glob: File glob pattern (default "*.py"). Use "*.xml" for views/data, "*.js" for frontend, "*" for all files.
        max_results: Maximum number of matching lines to return (default 50).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.search_in_environment(
        settings, team, env_name, pattern, path, glob, max_results
    )
    output = result["output"]
    if not output:
        return f"{woke}No matches for '{pattern}' in {path} ({glob})."
    header = f"{woke}Matches: {result['matches']}"
    if result["truncated"]:
        header += f" (truncated to {max_results})"
    return f"{header}\n\n{output}"


@mcp.tool()
@handle_errors
def list_installed_modules(
    env_name: str,
    name_filter: str = "",
    state_filter: str = "installed",
    ctx: Context | None = None,
) -> str:
    """
    List Odoo modules and their states in an environment.

    Returns a table of module name, state, and installed version. By default
    shows only installed modules. Use state_filter="" to show all modules.

    Args:
        env_name: The name of the environment.
        name_filter: Filter modules by name (substring match, e.g. "sale" matches "sale", "sale_management", "pos_sale").
        state_filter: Filter by module state (default "installed"). Common values: "installed", "uninstalled", "to upgrade", "to install". Pass empty string to show all states.
    """
    import re as _re

    # Sanitize inputs: only allow alphanumeric, underscore, space, dot
    _safe = _re.compile(r"^[a-zA-Z0-9_ .]*$")
    if name_filter and not _safe.match(name_filter):
        return "Error: name_filter contains invalid characters."
    if state_filter and not _safe.match(state_filter):
        return "Error: state_filter contains invalid characters."

    query = "SELECT name, state, latest_version FROM ir_module_module"
    conditions = []
    if state_filter:
        conditions.append(f"state = '{state_filter}'")
    if name_filter:
        conditions.append(f"name ILIKE '%{name_filter}%'")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY name"

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.run_db_query(settings, team, env_name, query, "csv")
    return cast(str, result["output"])


@mcp.tool()
@handle_errors
@with_env_lock
def run_odoo_command(
    env_name: str,
    command: str,
    user: str = "odoo",
    shell: bool = True,
    ctx: Context | None = None,
) -> str:
    """
    Execute an arbitrary shell command inside the Odoo container for a specific environment.

    The command runs through `sh -c`, so pipes, redirections, `&&`, `cd x && y`,
    `$VAR` and quoting all behave as written — one call, one shell line.

    Args:
        env_name: The name of the environment.
        command: The shell command to execute (e.g. "ls /mnt/extra-addons | head", "python3 -c 'print(1)'").
        user: The OS user to run the command as (default "odoo"). Use "root" for privileged operations.
        shell: Run via `sh -c` (default True). Pass False for exact argv semantics —
            the string is then split on whitespace and executed directly, so `|`, `>`,
            `&&`, `*` and `$VAR` reach the program as literal arguments instead of
            being interpreted.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.run_command_in_environment(
        settings, team, env_name, command, user, shell=shell
    )
    exit_code = result["exit_code"]
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    header = f"{woke}{status}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "run_odoo_command",
        f"env={env_name}, command={command[:80]}",
    )


@mcp.tool()
@handle_errors
@with_env_lock
def reset_admin_password(
    env_name: str, new_password: str = "test", ctx: Context | None = None
) -> str:
    """
    Reset the admin user password in the environment's Odoo database.

    Hashes the password using passlib (pbkdf2_sha512) inside the Odoo container
    and updates the res_users record where login = 'admin'.

    Args:
        env_name: The name of the environment.
        new_password: The new password for the admin user (default: "test").
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.reset_admin_password(settings, team, env_name, new_password)
    # Odoo derives a session's token from the user's password hash, so every
    # cached session for this environment is now rejected server-side.
    odoo_rpc.invalidate_sessions(team.team_id, env_name)
    return f"{woke}Admin password has been reset successfully.\nLogin: {result['login']}\nNew password: {new_password}"


@mcp.tool()
@handle_errors
@with_env_lock
def connect_as_user(env_name: str, user: str, ctx: Context | None = None) -> str:
    """
    Mint a passwordless Odoo login session for a user and return its cookie.

    Creates a server-side Odoo session for `user` (by login or numeric id) — the
    same authenticated state a password login produces — without setting or
    transmitting any password, and returns the `session_id` cookie plus a URL.
    Hand the cookie to a browser automation tool (e.g. Playwright
    `context.add_cookies([...])` then `page.goto(url)`) to land directly in an
    authenticated session as that user, skipping the login form. Mint a fresh
    session per user to exercise a feature across roles (admin, sales manager,
    portal — portal users are supported; they land on `/web` and Odoo redirects
    them to their portal) in a single test run.

    Whoever can call this can already `run_odoo_shell`, so it grants no new
    privilege. The returned session id is a live credential shown in this tool's
    output (and this transcript) — treat it like a password.

    Args:
        env_name: The name of the environment.
        user: The target user's login (e.g. "jane@acme.com") or numeric id.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    woke = _wake_for_work(settings, team, env_name)
    result = odoo_ops.connect_as_user(settings, team, env_name, user)
    sid = result["sid"]
    domain = result["cookie_domain"]
    return (
        f"{woke}Connected as {result['login']} (uid {result['uid']}) in "
        f"'{env_name}'.\n"
        f"Session cookie — add it to your browser / Playwright context:\n"
        f"  name:    session_id\n"
        f"  value:   {sid}\n"
        f"  domain:  {domain}\n"
        f"  path:    /\n"
        f"URL:        {result['url']}\n"
        f"Expires at: {result['expires_at']}\n"
        f"Playwright: context.add_cookies([{{'name': 'session_id', "
        f"'value': '{sid}', 'domain': '{domain}', 'path': '/'}}]) "
        f"then page.goto('{result['url']}')"
    )


@mcp.tool()
@handle_errors
@with_env_lock
def run_db_query(
    env_name: str,
    query: str,
    output_format: str = "csv",
    max_rows: int = 100,
    ctx: Context | None = None,
) -> str:
    """
    Execute a SQL query against the environment's PostgreSQL database.

    Args:
        env_name: The name of the environment.
        query: SQL query to execute (e.g. "SELECT id, name FROM res_partner LIMIT 10").
        output_format: "csv" (default, compact for agent consumption) or "human" (pretty table — use when relaying results to the user).
        max_rows: Maximum rows to return (default 100). The query itself is not
                  modified — truncation happens on the output. If more rows are
                  available, a note is appended suggesting to add LIMIT to the query.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.run_db_query(settings, team, env_name, query, output_format)
    output = result["output"]

    # Truncate by row count
    output_lines = output.splitlines()
    # First line is the header row
    if len(output_lines) > max_rows + 1:
        truncated = output_lines[: max_rows + 1]
        remaining = len(output_lines) - max_rows - 1
        truncated.append(f"... ({remaining} more rows, add LIMIT to your query)")
        output = "\n".join(truncated)

    return _maybe_cache(output, "", "run_db_query", f"env={env_name}")


# =============================================================================
# MCP Tools — Odoo ORM (JSON-RPC)
# =============================================================================
#
# execute_kw-equivalent access to the *running* Odoo server. Shared shape:
#   - `as_user` (login or numeric id, empty = the environment's admin) makes the
#     call run in a real session for that user, so ACLs and record rules apply.
#   - JSON string arguments accept a Python literal too, because models emit one
#     about as often as they emit JSON.
#   - Odoo-side failures (AccessError, ValidationError, ...) are returned as
#     text with the server traceback — they are answers, not tool failures.
#   - Every call is its own committed transaction.
#
# These tools take NO environment lock: PostgreSQL, not Oduflow, arbitrates
# concurrent ORM writes, and the neighbouring http_request_to_odoo /
# search_in_odoo already reach the same live server lock-free. Holding the
# environment lock here only made an agent's reads bounce off its own long
# install/test run.


def _rpc_user(as_user: str) -> str:
    """Validate `as_user`, rejecting the superuser."""
    value = (as_user or "").strip()
    if value in {"1", "__system__"}:
        raise ValueError(
            "as_user cannot be the superuser (uid 1 / __system__): a web session "
            "for it is not a real state. Use run_odoo_shell for sudo() access."
        )
    return value


_ODOO_CALL_DEDICATED_MUTATIONS = frozenset({"create", "write", "unlink"})


def _rpc_generic_method(method: str) -> str:
    """Keep policy-visible CRUD mutations on their dedicated MCP tools."""
    value = (method or "").strip()
    if value in _ODOO_CALL_DEDICATED_MUTATIONS:
        raise ValueError(
            f"Method {value!r} must use its dedicated odoo_{value} tool so "
            "tool-level mutation policy cannot be bypassed through odoo_call."
        )
    return value


def _rpc_rows(rows: Any) -> str:
    """Render ORM records as one compact JSON object per line."""
    if not isinstance(rows, list) or not rows:
        return json.dumps(rows, ensure_ascii=False, default=str)
    inner = ",\n".join(
        json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":"))
        for row in rows
    )
    return f"[\n{inner}\n]"


def _rpc_response(
    woke: str,
    result: odoo_rpc.RpcResult,
    header: str,
    body: str,
    tool: str,
    source_args: str,
) -> str:
    """Format an RpcResult, prepending wake-up and session-mint notes."""
    notes = woke
    if result.minted:
        notes += (
            f"Note: minted a new Odoo session for {result.login} "
            "(first call for this user in this environment).\n"
        )
    actor = result.login or "admin"
    if not result.ok:
        return _maybe_cache(
            result.error_text(), f"{notes}Error (as {actor}).", tool, source_args
        )
    return _maybe_cache(body, f"{notes}{header}", tool, source_args)


@mcp.tool()
@handle_errors
def odoo_search_read(
    env_name: str,
    model: str,
    domain: str = "[]",
    fields: str = "",
    limit: int = 80,
    offset: int = 0,
    order: str = "",
    count_only: bool = False,
    as_user: str = "",
    context: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Search and read Odoo records — the ORM equivalent of XML-RPC `search_read`.

    Prefer this over run_odoo_shell for reading data: it is far faster, returns
    JSON you can parse, and enforces the target user's access rights.

    Common use cases:
    - List records: model="res.partner", domain='[["customer_rank",">",0]]'
    - Count only: count_only=True (runs search_count, ignores fields/limit)
    - Check a user's visibility: as_user="portal@example.com"
    - Include archived records: context='{"active_test": false}'

    Always pass `fields` — reading every field pulls binary columns and blows up
    the response.

    These `odoo_*` tools talk to the live Odoo HTTP server, exactly like an
    external RPC client. So edited Python code stays invisible until the
    environment is restarted (`pull_and_apply` / `restart_environment`; XML views
    do reload), and every call is its own committed transaction. Use
    run_odoo_shell when you need a fresh registry, `sudo()`, private methods, a
    rollback, or several steps in one transaction.

    Args:
        env_name: The name of the environment.
        model: Technical model name (e.g. "res.partner").
        domain: Odoo search domain as JSON (e.g. '[["state","=","sale"]]'). A
            single bare leaf is accepted and wrapped for you.
        fields: Comma-separated field names, or a JSON array. Empty reads every
            field — avoid it.
        limit: Maximum rows to return (default 80). Applied server-side.
        offset: Rows to skip, for paging.
        order: SQL-style ordering (e.g. "date_order desc, id").
        count_only: Return only the number of matching records (search_count).
        as_user: Login or numeric user id to run as. Empty = the environment's
            admin. Access rights and record rules apply to that user.
        context: JSON object added to the call context (e.g.
            '{"lang": "fr_FR", "active_test": false}').
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    user = _rpc_user(as_user)
    parsed_domain = odoo_rpc.parse_domain(domain)
    parsed_context = odoo_rpc.parse_context(context)
    woke = _wake_for_rpc(settings, team, env_name)

    if count_only:
        args: list[Any] = [parsed_domain]
        kwargs: dict[str, Any] = {}
        method = "search_count"
    else:
        parsed_fields = odoo_rpc.parse_fields(fields)
        args = [parsed_domain, parsed_fields or []]
        kwargs = {"limit": limit, "offset": offset}
        if order:
            kwargs["order"] = order
        method = "search_read"
    if parsed_context is not None:
        kwargs["context"] = parsed_context

    result = odoo_rpc.call_kw(
        settings, team, env_name, model, method, args, kwargs, as_user=user
    )
    actor = result.login or "admin"
    if not result.ok:
        body, header = "", ""
    elif count_only:
        header = f"{model}: {result.value} records match (as {actor})."
        body = str(result.value)
    else:
        rows = result.value if isinstance(result.value, list) else []
        header = f"{model}: {len(rows)} rows (as {actor}, limit {limit})."
        if limit and len(rows) == limit:
            header += (
                f" Exactly {limit} rows came back — there may be more; "
                "raise limit or page with offset."
            )
        body = _rpc_rows(rows)
    return _rpc_response(
        woke,
        result,
        header,
        body,
        "odoo_search_read",
        f"env={env_name}, model={model}",
    )


@mcp.tool()
@handle_errors
def odoo_create(
    env_name: str,
    model: str,
    values: str,
    as_user: str = "",
    context: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Create Odoo records — the ORM equivalent of XML-RPC `create`.

    `values` is a JSON object of field values, or a JSON array of such objects to
    create several records in one call. Returns the new ids.

    Committed on success: there is no dry run. For a rollback-on-purpose run, or
    for several steps that must succeed or fail together, use run_odoo_shell.
    If the call times out, verify with a read before retrying — a repeat can
    create duplicates.

    Args:
        env_name: The name of the environment.
        model: Technical model name (e.g. "res.partner").
        values: JSON object of field values, or a JSON array of such objects.
        as_user: Login or numeric user id to run as. Empty = the environment's
            admin. Access rights and record rules apply to that user.
        context: JSON object added to the call context.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    user = _rpc_user(as_user)
    parsed_values = odoo_rpc.parse_values(values, allow_list=True)
    parsed_context = odoo_rpc.parse_context(context)
    woke = _wake_for_rpc(settings, team, env_name)

    kwargs: dict[str, Any] = {}
    if parsed_context is not None:
        kwargs["context"] = parsed_context
    result = odoo_rpc.call_kw(
        settings, team, env_name, model, "create", [parsed_values], kwargs, as_user=user
    )
    created = result.value if isinstance(result.value, list) else [result.value]
    actor = result.login or "admin"
    return _rpc_response(
        woke,
        result,
        f"Created {model}: {len(created)} record(s) (as {actor}). Committed.",
        json.dumps(result.value, ensure_ascii=False, default=str),
        "odoo_create",
        f"env={env_name}, model={model}",
    )


@mcp.tool()
@handle_errors
def odoo_write(
    env_name: str,
    model: str,
    ids: str,
    values: str,
    as_user: str = "",
    context: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Update Odoo records — the ORM equivalent of XML-RPC `write`.

    `ids` accepts "42", "1,2,3" or "[1,2,3]"; `values` is a JSON object of field
    values. Committed on success, with no dry run — use run_odoo_shell when you
    need a rollback or one transaction across several steps.

    Args:
        env_name: The name of the environment.
        model: Technical model name (e.g. "res.partner").
        ids: Record ids: "42", "1,2,3" or "[1,2,3]".
        values: JSON object of field values to set.
        as_user: Login or numeric user id to run as. Empty = the environment's
            admin. Access rights and record rules apply to that user.
        context: JSON object added to the call context.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    user = _rpc_user(as_user)
    parsed_ids = odoo_rpc.parse_ids(ids)
    parsed_values = odoo_rpc.parse_values(values)
    parsed_context = odoo_rpc.parse_context(context)
    woke = _wake_for_rpc(settings, team, env_name)

    kwargs: dict[str, Any] = {}
    if parsed_context is not None:
        kwargs["context"] = parsed_context
    result = odoo_rpc.call_kw(
        settings,
        team,
        env_name,
        model,
        "write",
        [parsed_ids, parsed_values],
        kwargs,
        as_user=user,
    )
    actor = result.login or "admin"
    return _rpc_response(
        woke,
        result,
        f"Wrote {model} ids {parsed_ids} (as {actor}). Committed.",
        json.dumps(result.value, ensure_ascii=False, default=str),
        "odoo_write",
        f"env={env_name}, model={model}",
    )


@mcp.tool()
@handle_errors
def odoo_unlink(
    env_name: str,
    model: str,
    ids: str,
    as_user: str = "",
    context: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Delete Odoo records — the ORM equivalent of XML-RPC `unlink`.

    Destructive and committed immediately: the records are gone when this
    returns, and there is no rollback. Confirm the target set with
    odoo_search_read first. Archiving (`active = false` via odoo_write) is
    usually what is actually wanted.

    Args:
        env_name: The name of the environment.
        model: Technical model name (e.g. "res.partner").
        ids: Record ids to delete: "42", "1,2,3" or "[1,2,3]".
        as_user: Login or numeric user id to run as. Empty = the environment's
            admin. Access rights and record rules apply to that user.
        context: JSON object added to the call context.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    user = _rpc_user(as_user)
    parsed_ids = odoo_rpc.parse_ids(ids)
    parsed_context = odoo_rpc.parse_context(context)
    woke = _wake_for_rpc(settings, team, env_name)

    kwargs: dict[str, Any] = {}
    if parsed_context is not None:
        kwargs["context"] = parsed_context
    result = odoo_rpc.call_kw(
        settings, team, env_name, model, "unlink", [parsed_ids], kwargs, as_user=user
    )
    actor = result.login or "admin"
    return _rpc_response(
        woke,
        result,
        f"Unlinked {model} ids {parsed_ids} (as {actor}). Committed — not recoverable.",
        json.dumps(result.value, ensure_ascii=False, default=str),
        "odoo_unlink",
        f"env={env_name}, model={model}",
    )


@mcp.tool()
@handle_errors
def odoo_call(
    env_name: str,
    model: str,
    method: str,
    ids: str = "",
    args: str = "[]",
    kwargs: str = "{}",
    as_user: str = "",
    context: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Call a public Odoo model method — the XML-RPC `execute_kw` escape hatch.

    Use it for everything the dedicated tools do not cover: read_group,
    name_search, default_get, copy, message_post, action_confirm, and any method
    a custom addon exposes. The CRUD mutations create/write/unlink are rejected
    here so tool-level policy can control their dedicated odoo_* tools.

    `ids` is prepended as the first positional argument, so
    model="sale.order", method="action_confirm", ids="42" sends args=[[42]].
    Leave `ids` empty for model-level methods (`@api.model`).

    Private methods (leading underscore) are rejected — Odoo 19 refuses them
    server-side as well. Use run_odoo_shell for those.

    Common use cases:
    - Group and aggregate: method="read_group",
      args='[[], ["amount_total:sum"], ["partner_id"]]'
    - Autocomplete lookup: method="name_search", kwargs='{"name": "Acme"}'
    - Trigger business logic: method="action_confirm", ids="42"

    Args:
        env_name: The name of the environment.
        model: Technical model name (e.g. "sale.order").
        method: Public method name (e.g. "read_group", "action_confirm"). Use
            odoo_create, odoo_write or odoo_unlink for those CRUD mutations.
        ids: Record ids prepended as the first positional argument. Empty for
            model-level methods.
        args: JSON array of the remaining positional arguments.
        kwargs: JSON object of keyword arguments.
        as_user: Login or numeric user id to run as. Empty = the environment's
            admin. Access rights and record rules apply to that user.
        context: JSON object added to the call context.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    user = _rpc_user(as_user)
    method = _rpc_generic_method(method)
    parsed_args = odoo_rpc.parse_json_arg(args, "args", [])
    if not isinstance(parsed_args, list):
        raise ValueError("args must be a JSON array of positional arguments.")
    parsed_kwargs = odoo_rpc.parse_json_arg(kwargs, "kwargs", {})
    if not isinstance(parsed_kwargs, dict):
        raise ValueError("kwargs must be a JSON object of keyword arguments.")
    if ids.strip():
        parsed_args = [odoo_rpc.parse_ids(ids)] + parsed_args
    parsed_context = odoo_rpc.parse_context(context)
    if parsed_context is not None:
        parsed_kwargs["context"] = parsed_context
    woke = _wake_for_rpc(settings, team, env_name)

    result = odoo_rpc.call_kw(
        settings,
        team,
        env_name,
        model,
        method,
        parsed_args,
        parsed_kwargs,
        as_user=user,
    )
    actor = result.login or "admin"
    body = result.value
    return _rpc_response(
        woke,
        result,
        f"{model}.{method} returned (as {actor}). Committed.",
        _rpc_rows(body)
        if isinstance(body, list)
        else json.dumps(body, ensure_ascii=False, default=str),
        "odoo_call",
        f"env={env_name}, model={model}, method={method}",
    )


@mcp.tool()
@handle_errors
def odoo_schema(
    env_name: str,
    model: str = "",
    name_filter: str = "",
    attributes: str = "string,type,relation,required,readonly,selection",
    as_user: str = "",
    limit: int = 200,
    offset: int = 0,
    ctx: Context | None = None,
) -> str:
    """
    Inspect the Odoo schema: list models, or describe one model's fields.

    Leave `model` empty to list models whose technical or display name matches
    `name_filter`. Set `model` to get its fields (XML-RPC `fields_get`),
    optionally narrowed to field names matching `name_filter`.

    Call this before writing a domain or a values dict — guessing field names is
    the most common cause of an empty result set or a confusing error.

    Args:
        env_name: The name of the environment.
        model: Technical model name (e.g. "sale.order"). Empty lists models.
        name_filter: Substring filter — on model names when listing models, on
            field names when describing a model.
        attributes: Comma-separated field attributes to return. Odoo prunes them
            server-side, so keeping this short keeps the response small. Pass an
            empty string for every attribute.
        as_user: Login or numeric id to inspect as (empty = admin). Field
            visibility can differ per user.
        limit: Maximum models to return when listing models (default 200; 0 = no
            limit). Ignored when describing one model.
        offset: Models to skip when listing models, for paging.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    user = _rpc_user(as_user)
    woke = _wake_for_rpc(settings, team, env_name)

    if not model:
        domain: list[Any] = []
        if name_filter:
            domain = [
                "|",
                ["model", "ilike", name_filter],
                ["name", "ilike", name_filter],
            ]
        result = odoo_rpc.call_kw(
            settings,
            team,
            env_name,
            "ir.model",
            "search_read",
            [domain, ["model", "name", "transient"]],
            {"limit": limit, "offset": offset, "order": "model"},
            as_user=user,
        )
        rows = result.value if isinstance(result.value, list) else []
        actor = result.login or "admin"
        header = f"Models matching '{name_filter}': {len(rows)} (as {actor})."
        if limit and len(rows) == limit:
            header += (
                f" Exactly {limit} models came back — there may be more; "
                f"narrow name_filter or page with offset={offset + limit}."
            )
        return _rpc_response(
            woke,
            result,
            header,
            _rpc_rows(
                [{k: r.get(k) for k in ("model", "name", "transient")} for r in rows]
            ),
            "odoo_schema",
            f"env={env_name}, name_filter={name_filter}, limit={limit}, offset={offset}",
        )

    attribute_list = [a.strip() for a in attributes.split(",") if a.strip()]
    result = odoo_rpc.call_kw(
        settings,
        team,
        env_name,
        model,
        "fields_get",
        [[], attribute_list],
        {},
        as_user=user,
    )
    actor = result.login or "admin"
    fields_map = result.value if isinstance(result.value, dict) else {}
    if name_filter:
        needle = name_filter.lower()
        fields_map = {k: v for k, v in fields_map.items() if needle in k.lower()}
    return _rpc_response(
        woke,
        result,
        f"{model}: {len(fields_map)} fields (as {actor}).",
        json.dumps(
            fields_map, ensure_ascii=False, default=str, indent=1, sort_keys=True
        ),
        "odoo_schema",
        f"env={env_name}, model={model}",
    )


# =============================================================================
# MCP Tools — PostgreSQL databases for auxiliary services
# =============================================================================


def _service_database_connection_text(result: dict[str, Any]) -> str:
    lines = [
        f"Name: {result['name']}",
        f"Status: {result['status']}",
        f"Host: {result['host']}",
        f"Port: {result['port']}",
        f"Database: {result['database']}",
        f"Username: {result['username']}",
    ]
    if result.get("password"):
        lines.extend(
            [
                f"Password: {result['password']}",
                f"DATABASE_URL: {result['url']}",
                "Environment variables:",
                f"PGHOST={result['host']}",
                f"PGPORT={result['port']}",
                f"PGDATABASE={result['database']}",
                f"PGUSER={result['username']}",
                f"PGPASSWORD={result['password']}",
            ]
        )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_key_lock(service_database_lock_key)
def create_service_database(name: str, ctx: Context | None = None) -> str:
    """Create a persistent PostgreSQL database for auxiliary services.

    The database belongs to the current team, uses a dedicated non-superuser
    role, and survives service updates and deletion. It is reachable from
    bridge-mode team services at the returned host and port.

    Args:
        name: Stable lowercase resource name using letters, digits, '-' or '_'.
    """
    result = service_database_ops.create_database(
        _get_settings(), _resolve_team(ctx), name
    )
    return (
        "Service database created successfully!\n"
        + _service_database_connection_text(result)
    )


@mcp.tool()
@handle_errors
def list_service_databases(ctx: Context | None = None) -> str:
    """List managed PostgreSQL databases for auxiliary services without secrets."""
    rows = service_database_ops.list_databases(_get_settings(), _resolve_team(ctx))
    if not rows:
        return "No service databases found."
    lines = ["Service databases:"]
    for row in rows:
        size_mb = int(row.get("size_bytes", 0)) / 1024**2
        lines.append(
            f"- {row['name']}: {row['status']}, database={row.get('database', 'unknown')}, "
            f"size={size_mb:.1f} MB, connections={row.get('connections', 0)}"
        )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_key_lock(service_database_lock_key)
def get_service_database(name: str, ctx: Context | None = None) -> str:
    """Get connection credentials for a managed auxiliary-service database.

    The response contains a plaintext password and DATABASE_URL; treat it as a
    secret and pass only the variables the target service needs.
    """
    result = service_database_ops.get_database(
        _get_settings(), _resolve_team(ctx), name, reveal_password=True
    )
    return _service_database_connection_text(result)


@mcp.tool()
@handle_errors
@with_key_lock(service_database_lock_key)
def rotate_service_database_password(name: str, ctx: Context | None = None) -> str:
    """Rotate a service database password and return the new credentials.

    Existing containers keep the old value until their environment variables
    are updated and the containers are recreated or restarted.
    """
    result = service_database_ops.rotate_password(
        _get_settings(), _resolve_team(ctx), name
    )
    return "Service database password rotated.\n" + _service_database_connection_text(
        result
    )


@mcp.tool()
@handle_errors
@with_key_lock(service_database_lock_key)
def delete_service_database(name: str, ctx: Context | None = None) -> str:
    """Permanently delete an auxiliary-service database and its login role.

    Active PostgreSQL connections are terminated. Service containers are not
    modified and will fail to reconnect until reconfigured.
    """
    result = service_database_ops.delete_database(
        _get_settings(), _resolve_team(ctx), name
    )
    return (
        f"Service database '{result['name']}' deleted permanently. "
        f"Database '{result['database']}' and role '{result['username']}' removed."
    )


# =============================================================================
# MCP Tools — Auxiliary services
# =============================================================================


def _service_internal_host_line(container_name: str, host_mode: bool) -> str:
    """The hostname other team containers (Odoo included) use to reach a service.

    On the team's Docker network the resolvable DNS name is exactly the
    container name (``{prefix}{team}-svc-{name}``) — there is no short alias.
    Host-mode services are not on that network, so they are reached through
    ``host.docker.internal`` instead. Stated explicitly because the ``URL:``
    line is the *external* Traefik/host address, not the internal one.
    """
    if host_mode:
        return (
            "Internal hostname: host network — reach it via host.docker.internal "
            "(host-mode services are not resolvable by container name)"
        )
    return f"Internal hostname (from Odoo & other team services): {container_name}"


@mcp.tool()
@handle_errors
@with_key_lock(service_lock_key)
def create_service(
    name: str,
    image: str,
    port: int = 0,
    hostname: str = "",
    env_vars: str = "",
    host_mode: bool = False,
    volumes: str = "",
    privileged: bool = False,
    net_admin: bool = False,
    routes: list[dict[str, object]] | None = None,
    command: str = "",
    runtime: dict[str, object] | None = None,
    ctx: Context | None = None,
) -> str:
    """
    Create a managed auxiliary service container (e.g. Redis, Meilisearch).

    Args:
        name: Short name for the service (e.g. "redis", "meilisearch").
        runtime: Explicit Docker tmpfs, private cgroupns, stop_signal and stop_timeout settings.
        image: Docker image with tag (e.g. "redis:7", "getmeili/meilisearch:v1.6").
        port: Catch-all exposure mode: forward every path to this one container port. Required outside Traefik. Mutually exclusive with routes.
        hostname: Custom hostname for traefik routing (optional, traefik mode only).
        env_vars: Comma- or newline-separated KEY=VALUE pairs (e.g. "MEILI_MASTER_KEY=abc,MEILI_ENV=production"). Commas inside values are preserved unless what follows the comma looks like another KEY=; put one pair per line when in doubt. So "CONNECT_MCP_TOOL_GROUPS=write,collaboration,documents" is one variable. A value "secret:<name>" references a team secret (see list_secrets): the real value is injected only inside the container and is never readable back.
        host_mode: Run the container in host network mode instead of the shared Docker network. Use when the service needs direct host network access. Traefik routing still works.
        volumes: Comma-separated volume mounts (e.g. "mydata:/data,config:/etc/app:ro"). Each entry is volume_name:/container/path[:ro|rw]. Volumes must be created first via create_volume. In Traefik TLS mode the system ACME volume is mounted automatically at /etc/traefik:ro; do not include it here.
        privileged: Run the container in privileged mode (full host access). Use with care — implies all Linux capabilities. Mutually exclusive with net_admin (privileged already grants NET_ADMIN).
        net_admin: Add the NET_ADMIN Linux capability. Required for VPN/WireGuard, tun/tap devices, and iptables manipulation inside the container.
        routes: Alternative Traefik exposure mode. Each object has path, backend port, and optional strip_prefix. Routes target this same service and unlisted paths return Traefik 404. Mutually exclusive with the top-level port.
        command: Start command overriding the image CMD, written as a shell-quoted string (e.g. "server /data --console-address :9001" for minio/minio). Leave empty to use the image default. The image ENTRYPOINT is not affected.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    parsed_env = None
    if env_vars:
        parsed_env = parse_env_vars(env_vars)
    parsed_volumes = volume_ops.parse_volume_mounts(volumes) if volumes else None
    cap_add = ["NET_ADMIN"] if net_admin else None
    parsed_command = parse_service_command(command)
    result = service_ops.create_service(
        settings,
        team,
        name,
        image,
        port or None,
        hostname=hostname or None,
        env_vars=parsed_env,
        host_mode=host_mode,
        volumes=parsed_volumes,
        cap_add=cap_add,
        privileged=privileged,
        routes=routes,
        runtime=runtime,
        command=parsed_command or None,
    )
    vol_info = ""
    if parsed_volumes:
        vol_info = "\nVolumes: " + ", ".join(
            f"{v['volume']}:{v['mount_path']}:{v['mode']}" for v in parsed_volumes
        )
    route_info = ""
    if result.get("routes"):
        route_info = "\nRoutes:\n" + "\n".join(
            f"- {route['path']} -> {route['port']}"
            f"{' (strip prefix)' if route.get('strip_prefix') else ''}"
            for route in result["routes"]
        )
    cmd_info = ""
    if result.get("command"):
        cmd_info = "\nCommand: " + shlex.join(result["command"])
    return (
        f"Service created successfully!\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"{_service_internal_host_line(result['container_name'], host_mode)}\n"
        f"Image: {result['image']}\n"
        f"URL: {result['url']}"
        f"{cmd_info}"
        f"{vol_info}"
        f"{route_info}"
    )


@mcp.tool()
@handle_errors
@with_key_lock(service_lock_key)
def update_service(
    name: str,
    env_vars: str = "",
    image: str = "",
    port: int = 0,
    hostname: str = "",
    host_mode: bool | None = None,
    volumes: str = "",
    privileged: bool | None = None,
    net_admin: bool | None = None,
    routes: list[dict[str, object]] | None = None,
    command: str | None = None,
    runtime: dict[str, object] | None = None,
    ctx: Context | None = None,
) -> str:
    """
    Update a managed auxiliary service container. Pulls the latest image and
    optionally changes any setting (env vars, image, port, hostname, host_mode,
    volumes, privileged, net_admin, routes, command). The container is recreated when the image or
    any setting changes; settings that are not overridden are preserved. This is
    the preferred way to change a service — you do not need to delete and
    recreate it manually.

    Args:
        name: The name of the service to update (e.g. "redis", "meilisearch").
        env_vars: Comma- or newline-separated KEY=VALUE pairs that fully replace existing env vars (e.g. "MEILI_MASTER_KEY=abc,MEILI_ENV=production"). Commas inside values are preserved unless what follows the comma looks like another KEY=; put one pair per line when in doubt. Leave empty to keep current env vars. A value "secret:<name>" references a team secret (see list_secrets): the real value is injected only inside the container and is never readable back.
        runtime: Replace lifecycle settings; omit to preserve, pass {} to clear.
        image: New Docker image with tag (e.g. "redis:8"). Leave empty to keep current image.
        port: New container port. Pass 0 to keep current port.
        hostname: New hostname for traefik routing. Leave empty to keep current hostname.
        host_mode: Run in host network mode. Leave unset (null) to keep current mode.
        volumes: Comma-separated volume mounts that fully replace existing user volumes (e.g. "mydata:/data,config:/etc/app:ro"). Leave empty to keep current volumes. The implicit Traefik TLS mount at /etc/traefik:ro is preserved separately.
        privileged: Run the container in privileged mode (full host access). Leave unset (null) to keep current mode. Mutually exclusive with net_admin (privileged already grants NET_ADMIN).
        net_admin: Add (True) or remove (False) the NET_ADMIN Linux capability — required for VPN/WireGuard, tun/tap, and iptables. Leave unset (null) to keep current capabilities.
        routes: Full replacement HTTP route list. Leave unset to preserve it. Pass [] together with port to return to a single catch-all port.
        command: New start command overriding the image CMD, as a shell-quoted string (e.g. "server /data"). Leave unset (null) to keep the current command; pass an empty string to drop the override and fall back to the image CMD. Note this differs from env_vars/image, where an empty string means "keep".
    """
    settings = _get_settings()
    team = _resolve_team(ctx)

    # Parse optional overrides
    parsed_env = None
    if env_vars:
        parsed_env = parse_env_vars(env_vars)

    parsed_volumes = None
    if volumes:
        parsed_volumes = volume_ops.parse_volume_mounts(volumes)

    cap_add_override = None
    if net_admin is not None:
        cap_add_override = ["NET_ADMIN"] if net_admin else []

    command_override = parse_service_command(command) if command is not None else None

    result = service_ops.update_service(
        settings,
        team,
        name,
        env_override=parsed_env,
        image_override=image or None,
        port_override=port or None,
        hostname_override=hostname or None,
        host_mode_override=host_mode,
        volume_override=parsed_volumes,
        cap_add_override=cap_add_override,
        privileged_override=privileged,
        routes_override=routes,
        command_override=command_override,
        runtime_override=runtime,
    )

    if result.get("image_updated"):
        status = "Image updated (new image pulled, container recreated)"
    elif result.get("config_updated"):
        status = "Config updated (container recreated)"
    else:
        status = "Already up-to-date (no changes)"

    digest_short = (result.get("new_digest") or "")[:19]
    cmd_info = ""
    if result.get("command"):
        cmd_info = "\nCommand: " + shlex.join(result["command"])
    return (
        f"Service updated successfully!\n"
        f"Status: {status}\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"{_service_internal_host_line(result['container_name'], bool(result.get('host_mode')))}\n"
        f"Image: {result['image']}\n"
        f"Digest: {digest_short}\n"
        f"URL: {result['url']}"
        f"{cmd_info}"
    )


@mcp.tool()
@handle_errors
@with_key_lock(service_lock_key)
def delete_service(name: str, ctx: Context | None = None) -> str:
    """
    Stop and remove a managed auxiliary service container.

    Args:
        name: The name of the service to delete.
    """
    result = service_ops.delete_service(_get_settings(), _resolve_team(ctx), name)
    return f"Service '{result['name']}' deleted. Container '{result['container_name']}' removed."


@mcp.tool()
@handle_errors
@with_key_lock(service_lock_key)
def restart_service(name: str, ctx: Context | None = None) -> str:
    """
    Restart a managed auxiliary service container.

    Args:
        name: The name of the service to restart (e.g. "redis", "meilisearch").
    """
    result = service_ops.restart_service(_get_settings(), _resolve_team(ctx), name)
    return f"Service '{result['name']}' restarted."


@mcp.tool()
@handle_errors
def get_service_info(name: str, ctx: Context | None = None) -> str:
    """
    Get full state and configuration of a managed auxiliary service.

    Returns image (with digest), runtime status, port, hostname, URL, host_mode,
    start command, volumes, environment variables, capabilities, privileged flag,
    restart count, started_at, and whether a saved preset exists. Use this before
    recreating or updating a service so all current options (volumes, host_mode,
    cap_add, command, env) are preserved.

    Args:
        name: The name of the service to inspect (e.g. "redis", "fs").
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    info = service_ops.get_service_info(settings, team, name)

    digest = info.get("image_digest") or ""
    digest_short = digest[:19] if digest else ""

    lines = [f"Service '{info['name']}': {info['status']}"]
    lines.append(f"Container: {info['container_name']}")
    lines.append(
        _service_internal_host_line(info["container_name"], bool(info.get("host_mode")))
    )
    lines.append(f"Image: {info['image']}")
    if digest_short:
        lines.append(f"Digest: {digest_short}")
    if info.get("port") is not None:
        lines.append(f"Port: {info['port']}")
    if info.get("hostname"):
        lines.append(f"Hostname: {info['hostname']}")
    if info.get("url"):
        lines.append(f"URL: {info['url']}")
    if info.get("routes"):
        lines.append("Routes:")
        for route in info["routes"]:
            suffix = " (strip prefix)" if route.get("strip_prefix") else ""
            lines.append(
                f"  {route['path']} -> {route['port']}{suffix} [{route.get('url', '')}]"
            )
    lines.append(f"Host mode: {'true' if info.get('host_mode') else 'false'}")
    if info.get("privileged"):
        lines.append("Privileged: true")
    if info.get("cap_add"):
        lines.append(f"Capabilities: {','.join(info['cap_add'])}")
    if info.get("command"):
        lines.append(f"Command: {shlex.join(info['command'])}")
    elif info.get("image_command"):
        lines.append(
            f"Command: {shlex.join(info['image_command'])} (image default, not overridden)"
        )
    user_volumes = []
    system_volumes = []
    for volume in info.get("volumes") or []:
        if (
            volume.get("volume") == settings.traefik_acme_volume
            and volume.get("mount_path") == "/etc/traefik"
        ):
            system_volumes.append(volume)
        else:
            user_volumes.append(volume)
    if user_volumes:
        vol_str = ", ".join(
            f"{v['volume']}:{v['mount_path']}:{v.get('mode', 'rw')}"
            for v in user_volumes
        )
        lines.append(f"Volumes: {vol_str}")
    if system_volumes:
        system_str = ", ".join(
            f"{v['volume']}:{v['mount_path']}:{v.get('mode', 'rw')}"
            for v in system_volumes
        )
        lines.append(f"System mounts: {system_str} (implicit; do not pass as volumes)")
    if info.get("env_vars"):
        env_str = ", ".join(f"{k}={v}" for k, v in info["env_vars"].items())
        lines.append(f"Env: {env_str}")
    if info.get("started_at"):
        lines.append(
            f"Started: {info['started_at']} (restart_count={info.get('restart_count', 0)})"
        )
    lines.append(f"Preset: {'yes' if info.get('has_preset') else 'no'}")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def list_service_presets(ctx: Context | None = None) -> str:
    """List saved service presets (configurations that can be restored)."""
    team = _resolve_team(ctx)
    presets = service_presets.list_presets(team)
    if not presets:
        return "No service presets saved."
    output = "Saved Service Presets:\n"
    for p in presets:
        env_str = (
            ", ".join(f"{k}={v}" for k, v in p["env_vars"].items())
            if p.get("env_vars")
            else ""
        )
        output += f"- {p['name']}: image={p['image']}"
        if not p.get("routes"):
            output += f", port={p['port']}"
        if p.get("hostname"):
            output += f", hostname={p['hostname']}"
        if env_str:
            output += f", env=[{env_str}]"
        if p.get("host_mode"):
            output += ", host_mode=true"
        if p.get("privileged"):
            output += ", privileged=true"
        if p.get("cap_add"):
            output += f", cap_add=[{','.join(p['cap_add'])}]"
        if p.get("command"):
            output += f", command={shlex.join(p['command'])!r}"
        if p.get("volumes"):
            vol_str = ",".join(
                f"{v['volume']}:{v['mount_path']}:{v.get('mode', 'rw')}"
                for v in p["volumes"]
            )
            output += f", volumes=[{vol_str}]"
        if p.get("routes"):
            route_str = ",".join(
                f"{route['path']}->{route['port']}" for route in p["routes"]
            )
            output += f", routes=[{route_str}]"
        output += "\n"
    return output


@mcp.tool()
@handle_errors
def list_secrets(ctx: Context | None = None) -> str:
    """
    List the names of the team's named secrets. Values are write-only: they are
    set by a human in the Oduflow dashboard and can never be read through MCP.

    Reference a secret in any env_vars argument (create_environment,
    update_environment, create_service, update_service) as
    KEY=secret:<name> — the real value is substituted only inside the
    container, while every stored or displayed configuration keeps the
    reference.
    """
    team = _resolve_team(ctx)
    records = secret_store.list_secrets(team)
    if not records:
        return (
            "No secrets defined. A human operator can add them in the "
            "dashboard (Credentials tab, Secrets section)."
        )
    lines = ["Team secrets (values are not readable over MCP):"]
    for record in records:
        line = f"- {record['name']}"
        if record.get("updated_at"):
            line += f" (updated {record['updated_at']})"
        lines.append(line)
    lines.append("Use in env_vars as KEY=secret:<name>.")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_key_lock(service_lock_key)
def restore_service(name: str, ctx: Context | None = None) -> str:
    """
    Restore a service from a saved preset. Recreates the service container with the same configuration.

    Args:
        name: The name of the saved service preset to restore.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    preset = service_presets.get_preset(team, name)
    preset_volumes = preset.get("volumes") or None
    preset_cap_add = preset.get("cap_add") or None
    preset_privileged = preset.get("privileged", False)
    preset_command = preset.get("command") or None
    result = service_ops.create_service(
        settings,
        team,
        name=preset["name"],
        image=preset["image"],
        port=preset["port"],
        hostname=preset.get("hostname") or None,
        env_vars=preset.get("env_vars") or None,
        host_mode=preset.get("host_mode", False),
        volumes=preset_volumes,
        cap_add=preset_cap_add,
        privileged=preset_privileged,
        routes=preset.get("routes") or None,
        command=preset_command,
        runtime=preset.get("runtime"),
    )
    extra = ""
    if preset_volumes:
        extra += "\nVolumes: " + ", ".join(
            f"{v['volume']}:{v['mount_path']}:{v.get('mode', 'rw')}"
            for v in preset_volumes
        )
    if preset_privileged:
        extra += "\nPrivileged: true"
    elif preset_cap_add:
        extra += f"\nCapabilities: {','.join(preset_cap_add)}"
    if preset_command:
        extra += f"\nCommand: {shlex.join(preset_command)}"
    if result.get("routes"):
        extra += "\nRoutes: " + ", ".join(
            f"{route['path']}->{route['port']}" for route in result["routes"]
        )
    return (
        f"Service restored from preset!\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"{_service_internal_host_line(result['container_name'], bool(result.get('host_mode')))}\n"
        f"Image: {result['image']}\n"
        f"URL: {result['url']}"
        f"{extra}"
    )


@mcp.tool()
@handle_errors
@with_key_lock(service_preset_lock_key)
def delete_service_preset(name: str, ctx: Context | None = None) -> str:
    """
    Remove a saved service preset.

    Args:
        name: The name of the service preset to delete.
    """
    team = _resolve_team(ctx)
    service_presets.delete_preset(team, name)
    return f"Service preset '{name}' deleted."


@mcp.tool()
@handle_errors
def list_services(ctx: Context | None = None) -> str:
    """List all managed auxiliary service containers."""
    settings = _get_settings()
    team = _resolve_team(ctx)
    services = service_ops.list_services(settings, team)
    if not services:
        return "No active services found."
    output = "Active Services:\n"
    for svc in services:
        output += f"- {svc['name']} ({svc['container_name']}): {svc['status']}\n"
        output += f"  Image: {svc['image']}\n"
        if svc.get("port"):
            output += f"  Port: {svc['port']}\n"
        if svc.get("url"):
            output += f"  URL: {svc['url']}\n"
        if svc.get("routes"):
            for route in svc["routes"]:
                output += f"  Route: {route['path']} -> {route['port']}\n"
        if svc.get("env_vars"):
            env_str = ", ".join(f"{k}={v}" for k, v in svc["env_vars"].items())
            output += f"  Env: {env_str}\n"
    return output


@mcp.tool()
@handle_errors
def get_service_logs(name: str, n_lines: int = 100, ctx: Context | None = None) -> str:
    """
    Get logs from a managed auxiliary service container.

    Args:
        name: The name of the service.
        n_lines: Number of recent log lines to retrieve (default 100).
    """
    output = service_ops.get_service_logs(
        _get_settings(), _resolve_team(ctx), name, n_lines
    )
    return f"Recent logs for service '{name}':\n\n{_ANSI_RE.sub('', output)}"


@mcp.tool()
@handle_errors
@with_key_lock(service_lock_key)
def run_service_command(
    name: str,
    command: str,
    user: str = "root",
    shell: bool = True,
    ctx: Context | None = None,
) -> str:
    """
    Execute an arbitrary shell command inside a managed auxiliary service container.

    The command runs through `sh -c`, so pipes, redirections and `&&` work as written.

    Args:
        name: The name of the service (e.g. "redis", "meilisearch").
        command: The shell command to execute (e.g. "redis-cli ping", "ls /data | wc -l").
        user: The OS user to run the command as (default "root").
        shell: Run via `sh -c` (default True). Pass False for exact argv semantics, or
            when the image ships no shell at all (scratch/distroless).
    """
    result = service_ops.run_command_in_service(
        _get_settings(), _resolve_team(ctx), name, command, user, shell=shell
    )
    exit_code = result["exit_code"]
    output = cast(str, result.get("output", ""))
    status = "Success" if exit_code == 0 else "Error"
    header = f"{status}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "run_service_command",
        f"service={name}, command={command[:80]}",
    )


# =============================================================================
# Volume management
# =============================================================================


@mcp.tool()
@handle_errors
@with_key_lock(volume_lock_key)
def create_volume(
    name: str,
    description: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Create a named Docker volume for use with services.

    Args:
        name: Short name for the volume (e.g. "redis-data", "meilisearch-data").
        description: Optional description of what this volume is for.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_ops.create_volume(settings, team, name, description=description)
    desc_info = (
        f"\nDescription: {result['description']}" if result["description"] else ""
    )
    return (
        f"Volume created successfully!\n"
        f"Name: {result['name']}\n"
        f"Docker name: {result['docker_name']}"
        f"{desc_info}"
    )


@mcp.tool()
@handle_errors
def list_volumes(ctx: Context | None = None) -> str:
    """List all managed Docker volumes and their usage by services."""
    settings = _get_settings()
    team = _resolve_team(ctx)
    vols = volume_ops.list_volumes(settings, team)
    if not vols:
        return "No volumes found. Create one with create_volume."
    output = "Managed Volumes:\n"
    for vol in vols:
        used = ", ".join(vol["used_by"]) if vol["used_by"] else "not in use"
        desc = f" — {vol['description']}" if vol.get("description") else ""
        output += f"- {vol['name']}{desc}\n"
        output += f"  Docker name: {vol['docker_name']}\n"
        output += f"  Used by: {used}\n"
    return output


@mcp.tool()
@handle_errors
def inspect_volume(name: str, ctx: Context | None = None) -> str:
    """
    Get detailed information about a specific volume, including which services use it.

    Args:
        name: The name of the volume to inspect.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    vol = volume_ops.inspect_volume(settings, team, name)
    used = ", ".join(vol["used_by"]) if vol["used_by"] else "not in use"
    desc = f"\nDescription: {vol['description']}" if vol.get("description") else ""
    return (
        f"Volume: {vol['name']}\n"
        f"Docker name: {vol['docker_name']}"
        f"{desc}\n"
        f"Driver: {vol['driver']}\n"
        f"Created: {vol['created_at']}\n"
        f"Mountpoint: {vol['mountpoint']}\n"
        f"Used by: {used}"
    )


@mcp.tool()
@handle_errors
@with_key_lock(volume_lock_key)
def delete_volume(name: str, ctx: Context | None = None) -> str:
    """
    Delete a managed Docker volume. Fails if the volume is in use by any service.

    Args:
        name: The name of the volume to delete.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_ops.delete_volume(settings, team, name)
    return f"Volume '{result['name']}' deleted."


# =============================================================================
# Volume file operations
# =============================================================================


@mcp.tool()
@handle_errors
def read_file_in_volume(
    name: str,
    path: str,
    read_range: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Read a text file or list a directory inside a Docker volume.

    Spins up a temporary helper container to access the volume contents.
    If the path is a directory, returns a listing (like ``ls -la``).
    If the path is a text file, returns its contents (up to 100KB by default).
    Binary files are detected and rejected.

    Args:
        name: The name of the volume (e.g. "redis-data").
        path: Path inside the volume (e.g. "data/dump.rdb", "config/redis.conf").
              Leading "/" is optional — paths are relative to the volume root.
        read_range: Optional line range "START:END" (e.g. "1:50", "100:200").
                    If omitted, returns the full file (up to 100KB).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.read_file_in_volume(settings, team, name, path, read_range)
    if "error" in result:
        return f"Error: {result['error']}"
    return cast(str, result["output"])


@mcp.tool()
@handle_errors
@with_key_lock(volume_lock_key)
def write_file_in_volume(
    name: str,
    path: str,
    content: str,
    ctx: Context | None = None,
) -> str:
    """
    Write a text file inside a Docker volume.

    Creates parent directories if they don't exist. Overwrites the file if it
    already exists. Uses a temporary helper container to access the volume.

    Args:
        name: The name of the volume (e.g. "redis-data").
        path: Path inside the volume (e.g. "config/my.conf").
              Leading "/" is optional — paths are relative to the volume root.
        content: Text content to write to the file.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.write_file_in_volume(settings, team, name, path, content)
    return f"File written: {result['path']} ({result['size']} bytes)"


@mcp.tool()
@handle_errors
def search_in_volume(
    name: str,
    pattern: str,
    path: str = "",
    glob: str = "*",
    max_results: int = 50,
    ctx: Context | None = None,
) -> str:
    """
    Search for a pattern in files inside a Docker volume.

    Runs a recursive fixed-string grep inside a temporary helper container
    and returns matching lines with file paths and line numbers.

    Args:
        name: The name of the volume (e.g. "redis-data").
        pattern: Search pattern (fixed string, case-sensitive).
        path: Directory to search in, relative to volume root (default: entire volume).
        glob: File glob pattern (default "*"). Use "*.py", "*.xml", "*.conf", etc.
        max_results: Maximum number of matching lines to return (default 50).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.search_in_volume(
        settings, team, name, pattern, path, glob, max_results
    )
    output = result["output"]
    if not output:
        return f"No matches for '{pattern}' in volume '{name}' ({glob})."
    header = f"Matches: {result['matches']}"
    if result["truncated"]:
        header += f" (truncated to {max_results})"
    return f"{header}\n\n{output}"


@mcp.tool()
@handle_errors
@with_key_lock(volume_lock_key)
def delete_file_in_volume(
    name: str,
    path: str,
    ctx: Context | None = None,
) -> str:
    """
    Delete a file or directory inside a Docker volume.

    Uses a temporary helper container to access the volume.
    Cannot delete the volume root — use delete_volume to remove the entire volume.

    Args:
        name: The name of the volume (e.g. "redis-data").
        path: Path inside the volume to delete (e.g. "data/old-dump.rdb").
              Leading "/" is optional — paths are relative to the volume root.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.delete_file_in_volume(settings, team, name, path)
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Deleted: {result['path']}"


# =============================================================================
# MCP Tools — Production hosting
# =============================================================================


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def create_production(
    name: str,
    repo_url: str,
    branch: str,
    domain: str,
    odoo_image: str,
    git_user: str = "",
    extra_addons: dict[str, str] | None = None,
    auto_update: bool = False,
    template_name: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Create a PRODUCTION Odoo environment (long-lived, own domain, dedicated
    production PostgreSQL cluster, auto-tuned workers, no sanitization).

    Productions are rarely created and rarely deleted — they live on and get
    updated (update_production). Requires routing_mode = "traefik".

    Args:
        name: Production name, e.g. "erp" (lowercase letters/digits/dashes).
        repo_url: HTTPS git repository URL.
        branch: Git branch to deploy (full history is kept).
        domain: The production's public domain, e.g. "erp.customer.com"
                (DNS must point at this server; TLS via Let's Encrypt).
        odoo_image: Docker image, e.g. "odoo:18.0".
        git_user: Optional git username for credential matching.
        extra_addons: Optional {repo_name: branch} extra addon repos.
        auto_update: Deploy automatically on GitHub push webhooks.
        template_name: Optional template to seed the database and filestore
                from (e.g. an import of the customer's existing production).
                Empty = fresh database (odoo -i base).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    git_ops.validate_repo_url(repo_url)
    result = production_ops.create_production(
        settings,
        team,
        name,
        repo_url,
        branch,
        domain,
        odoo_image,
        git_user=git_user,
        extra_addons=env_ops._normalize_extra_addons(extra_addons),
        auto_update=auto_update,
        template_name=template_name or None,
    )
    lines = [
        f"Production '{name}' created in {result['elapsed_seconds']}s.",
        f"URL: {result['url']}",
        f"Database: {result['database']} (cluster: oduflow-prod-db)",
        f"Deployed commit: {result['commit'][:10]}",
        f"Container: {result['odoo_container']}",
    ]
    if result.get("setup_logs"):
        lines.append("\nSetup:\n" + "\n".join(result["setup_logs"]))
    lines.append(
        "\nNote: point the domain's DNS at this server. Use update_production "
        "to deploy new commits (failed updates roll the code back "
        "automatically)."
    )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@production_enabled
def list_productions(ctx: Context | None = None) -> str:
    """
    List the team's production environments with status, domain, deployed
    commit, and last deploy result.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    prods = production_ops.list_productions(settings, team)
    if not prods:
        return "No productions found. Use create_production to provision one."
    import json as _json

    return _json.dumps(prods, indent=2)


@mcp.tool()
@handle_errors
@production_enabled
def get_production_info(name: str, ctx: Context | None = None) -> str:
    """
    Detailed information about a production: status, health, deployed commit,
    recent branch commits, deploy history, and backup state.

    Args:
        name: The production name.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    info = production_ops.get_production_info(settings, team, name)
    import json as _json

    return _json.dumps(info, indent=2)


@mcp.tool()
@handle_errors
@production_enabled
def production_logs(
    name: str,
    n_lines: int = 100,
    grep: str = "",
    level: str = "",
    ctx: Context | None = None,
) -> str:
    """
    Fetch logs from a production's Odoo container.

    Args:
        name: The production name.
        n_lines: Number of log lines to return (default 100).
        grep: Optional case-insensitive substring filter.
        level: Optional log level filter (e.g. "ERROR", "WARNING").
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    logs = production_ops.production_logs(
        settings, team, name, n_lines=n_lines, grep=grep, level=level
    )
    return _maybe_cache(
        logs,
        f"Logs for production '{name}':",
        "production_logs",
        f"name={name}",
    )


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def start_production(name: str, ctx: Context | None = None) -> str:
    """
    Start a stopped production environment.

    Args:
        name: The production name.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = production_ops.start_production(settings, team, name)
    return f"Production '{name}' started ({result['odoo_container']})."


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def stop_production(name: str, ctx: Context | None = None) -> str:
    """
    Stop a production environment. WARNING: takes the production offline.

    Args:
        name: The production name.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = production_ops.stop_production(settings, team, name)
    return f"Production '{name}' stopped ({result['odoo_container']})."


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def restart_production(name: str, ctx: Context | None = None) -> str:
    """
    Restart a production's Odoo container (brief downtime).

    Args:
        name: The production name.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = production_ops.restart_production(settings, team, name)
    return f"Production '{name}' restarted ({result['odoo_container']})."


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def set_production_auto_update(
    name: str, enabled: bool, ctx: Context | None = None
) -> str:
    """
    Enable/disable automatic deployment of GitHub push webhooks for a
    production. When enabled, a push to the production's branch triggers
    update_production in the background (with automatic code rollback on
    failure).

    Args:
        name: The production name.
        enabled: True to deploy automatically on push.
    """
    team = _resolve_team(ctx)
    production_registry.get_production(team, name)
    production_registry.update_production(team, name, {"auto_update": bool(enabled)})
    state = "enabled" if enabled else "disabled"
    return f"Auto-update {state} for production '{name}'."


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def update_production(
    name: str,
    install: str = "",
    upgrade: str = "",
    restart: bool = False,
    ctx: Context | None = None,
) -> str:
    """
    Deploy the latest commits of a production's branch — with AUTOMATIC CODE
    ROLLBACK on failure.

    Pulls the branch (and extra-addon worktrees), decides or applies the
    Odoo action, then verifies the deploy (module exit codes + health
    check). If the deploy fails, the checkout is reset to the previous
    commit, the config is re-applied and the container restarted. The
    DATABASE is never rolled back automatically — restore a snapshot
    manually if module upgrades left it inconsistent.

    Drive it like pull_and_apply:
    - EXPLICIT: pass install/upgrade (comma-separated modules) or restart=True.
    - AUTO (all empty): changed files are classified automatically. Note:
      in production a "refresh"-class change (XML/JS) still restarts the
      container (no --dev=xml in production).

    Args:
        name: The production name.
        install: Comma-separated modules to install (-i).
        upgrade: Comma-separated modules to upgrade (-u).
        restart: Restart the container (for Python-only changes).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    install_list = [m.strip() for m in install.split(",") if m.strip()]
    upgrade_list = [m.strip() for m in upgrade.split(",") if m.strip()]
    result = production_ops.update_production(
        settings,
        team,
        name,
        install=install_list or None,
        upgrade=upgrade_list or None,
        restart=restart,
        trigger="mcp",
    )
    header = f"[{result.get('action', 'none')}] {result.get('message', '')}".strip()
    output = result.get("output", "")
    commit = result.get("commit", "")
    lines = [header, f"Deployed commit: {commit[:10]}" if commit else ""]
    body = "\n".join(line for line in lines if line)
    if output:
        return _maybe_cache(output, body, "update_production", f"name={name}")
    return body


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def rollback_production(
    name: str, to_commit: str = "", ctx: Context | None = None
) -> str:
    """
    Manually roll a production's CODE back to a previous commit and restart.
    (The database is not touched — restore a snapshot for data rollback.)

    Args:
        name: The production name.
        to_commit: Target commit sha (or any git ref present in the checkout).
                Empty = the previous deploy's starting commit.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = production_ops.rollback_production(
        settings, team, name, to_commit, trigger="mcp"
    )
    return str(result["message"])


@mcp.tool()
@handle_errors
@production_enabled
def production_deploys(name: str, limit: int = 20, ctx: Context | None = None) -> str:
    """
    Deploy history of a production (newest last): commits, actions, modules,
    status (success / rolled_back / rollback_failed), errors.

    Args:
        name: The production name.
        limit: Max number of records (default 20).
    """
    team = _resolve_team(ctx)
    production_registry.get_production(team, name)
    deploys = production_ops.read_deploys(team, name, limit=limit)
    if not deploys:
        return f"No deploys recorded for production '{name}'."
    import json as _json

    return _json.dumps(deploys, indent=2)


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
# Lock order everywhere: the production first, then the team's backup store.
# Prune takes the store key alone, so it can never run mid-snapshot.
@with_key_lock(prod_backups_lock_key, require_name=False)
def snapshot_production(name: str, note: str = "", ctx: Context | None = None) -> str:
    """
    Take a snapshot of a production to S3: database dump + deduplicated
    filestore revision + manifest (with the deployed commit sha). Snapshots
    are the per-production restore unit (restore_production).

    Requires a [backup] section in oduflow.toml.

    Args:
        name: The production name.
        note: Optional free-form note stored in the manifest.
    """
    from oduflow import backup_ops

    settings = _get_settings()
    team = _resolve_team(ctx)
    manifest = backup_ops.snapshot_production(
        settings, team, name, trigger="mcp", note=note
    )
    return (
        f"Snapshot {manifest['id']} of production '{name}' completed.\n"
        f"Database: {manifest['db']['bytes']} bytes (sha256 "
        f"{manifest['db']['sha256'][:12]}…)\n"
        f"Filestore: revision {manifest['filestore'].get('revision')} "
        f"({manifest['filestore'].get('files', 0)} files, "
        f"{manifest['filestore'].get('uploaded_bytes', 0)} bytes uploaded)\n"
        f"Commit: {manifest.get('commit_sha', '')[:10]}"
    )


@mcp.tool()
@handle_errors
@production_enabled
def list_production_snapshots(
    name: str, refresh: bool = False, ctx: Context | None = None
) -> str:
    """
    List a production's snapshots (oldest first): id, created_at, sizes,
    commit sha.

    Args:
        name: The production name.
        refresh: Re-list S3 (source of truth) instead of the local cache.
    """
    from oduflow import backup_ops

    settings = _get_settings()
    team = _resolve_team(ctx)
    manifests = backup_ops.list_snapshots(settings, team, name, refresh=refresh)
    if not manifests:
        return (
            f"No snapshots for production '{name}'. Use snapshot_production "
            "or wait for the scheduled backup."
        )
    import json as _json

    return _json.dumps(manifests, indent=2)


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
# Same lock order as snapshot_production: production first, then backup store.
@with_key_lock(prod_backups_lock_key, require_name=False)
def restore_production(
    name: str, snapshot_id: str, confirm: str = "", ctx: Context | None = None
) -> str:
    """
    Restore a production's DATABASE and FILESTORE from a snapshot.
    DESTRUCTIVE for current data (swap-based: a failed restore leaves the
    previous state in place). The code checkout is NOT touched — a warning
    is returned if it does not match the snapshot's commit.

    Args:
        name: The production name.
        snapshot_id: Snapshot to restore (see list_production_snapshots).
        confirm: Must equal the production name (safety check).
    """
    if confirm != name:
        raise ToolError(
            f'Confirmation failed: pass confirm="{name}" to restore this production.'
        )
    from oduflow import backup_ops

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = backup_ops.restore_production(settings, team, name, snapshot_id)
    lines = [
        f"Production '{name}' restored from snapshot {snapshot_id}.",
        f"Healthy: {result['healthy']}",
    ]
    if result.get("warning"):
        lines.append(f"WARNING: {result['warning']}")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@production_enabled
def production_backup_status(ctx: Context | None = None) -> str:
    """
    Backup posture for the team: per-production snapshot state (schedule,
    last snapshot, last error) and cluster WAL-G state (base backups, WAL
    archiver health, S3 reachability).
    """
    from oduflow import backup_ops

    settings = _get_settings()
    team = _resolve_team(ctx)
    status = backup_ops.backup_status(settings, team)
    import json as _json

    return _json.dumps(status, indent=2)


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def set_production_backup_schedule(
    name: str, schedule: str, ctx: Context | None = None
) -> str:
    """
    Override a production's daily snapshot time.

    Args:
        name: The production name.
        schedule: "HH:MM" (server-local time) or "off" to disable scheduled
                snapshots for this production. Default (unset) follows
                [backup] snapshot_time.
    """
    schedule = schedule.strip().lower()
    if schedule != "off" and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", schedule):
        raise ToolError('schedule must be "HH:MM" (24h) or "off"')
    team = _resolve_team(ctx)
    production_registry.get_production(team, name)
    production_registry.set_nested(team, name, "backup", {"schedule": schedule})
    return f"Snapshot schedule for production '{name}' set to {schedule}."


@mcp.tool()
@handle_errors
@production_enabled
@with_key_lock(prod_backups_lock_key, require_name=False)
def prune_production_backups(ctx: Context | None = None) -> str:
    """
    Apply the retention policy ([backup] keep) to the team's snapshots and
    filestore chunk store now (runs weekly on schedule anyway). Uses safe
    two-step fossil collection — chunks are only permanently deleted on a
    later prune after every production has produced a newer revision.
    """
    from oduflow import backup_ops

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = backup_ops.prune_backups(settings, team)
    import json as _json

    return _json.dumps(result, indent=2)


@mcp.tool()
@handle_errors
@production_enabled
def restore_cluster_pitr(
    target_time: str = "", confirm: str = "", ctx: Context | None = None
) -> str:
    """
    DISASTER RECOVERY: restore the WHOLE production PostgreSQL cluster from
    WAL-G (base backup + WAL replay). Affects EVERY production database at
    once — for restoring a single production use restore_production.

    Also the "resurrect production elsewhere" path: a fresh Oduflow server
    with the same [backup] section can rebuild the cluster from S3.

    The current data directory is displaced inside the volume (not
    destroyed); production Odoo containers are stopped and restarted.

    Args:
        target_time: Optional PITR target, e.g. "2026-07-10 12:00:00+00"
                (empty = replay the whole archive to the latest state).
        confirm: Must equal "RESTORE-CLUSTER" (safety check).
    """
    if confirm != "RESTORE-CLUSTER":
        raise ToolError(
            'Confirmation failed: pass confirm="RESTORE-CLUSTER" to restore '
            "the whole production cluster."
        )
    from oduflow import walg

    settings = _get_settings()
    _resolve_team(ctx)  # authenticate the caller; PITR spans all teams
    # The one true system-level operation: it rewrites the cluster every team's
    # productions live in, so it excludes the whole `prod:` keyspace at once
    # (per-production locks would each have to be enumerated and could still be
    # taken behind our back).
    _locks.acquire_system(operation="restore_cluster_pitr")
    try:
        from oduflow.docker_ops.client import get_client

        client = get_client()
        # Stop every production Odoo container (all teams share the cluster).
        stopped: list[str] = []
        for team_cfg in settings.teams.values():
            for prod_name in production_registry.list_productions(team_cfg):
                container = production_ops._get_container(
                    client, settings, team_cfg, prod_name
                )
                if container is not None and container.status == "running":
                    container.stop()
                    stopped.append(f"{team_cfg.team_id}/{prod_name}")
        result = walg.pitr_restore_cluster(settings, target_time=target_time)
        for entry in stopped:
            team_id, prod_name = entry.split("/", 1)
            try:
                production_ops.start_production(
                    settings, settings.teams[team_id], prod_name
                )
            except Exception as exc:
                logger.warning("Could not restart production %s: %s", entry, exc)
    finally:
        _locks.release_system()
    return (
        f"Cluster restored to {result['target_time']}. Previous data "
        f"directory kept inside the volume as {result['displaced_data_dir']} "
        f"(remove manually after verifying). Restarted productions: "
        f"{', '.join(stopped) or 'none'}."
    )


@mcp.tool()
@handle_errors
@production_enabled
@with_prod_lock
def delete_production(
    name: str,
    confirm: str = "",
    drop_database: bool = False,
    ctx: Context | None = None,
) -> str:
    """
    Delete a production environment. The container and registry record are
    removed; the DATABASE and workspace (filestore, repo, deploy history)
    are KEPT unless drop_database=true.

    Args:
        name: The production name.
        confirm: Must equal the production name (safety check).
        drop_database: Also drop the database and delete the workspace.
    """
    if confirm != name:
        raise ToolError(
            f'Confirmation failed: pass confirm="{name}" to delete this production.'
        )
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = production_ops.delete_production(
        settings, team, name, drop_database=drop_database
    )
    lines = [f"Production '{name}' deleted."]
    if result["kept"]:
        lines.append(
            "Kept (pass drop_database=true to remove): " + ", ".join(result["kept"])
        )
    if result["warnings"]:
        lines.append("Warnings: " + "; ".join(result["warnings"]))
    return "\n".join(lines)


# =============================================================================
# CLI helpers
# =============================================================================


def _managed_bundled_files(
    bundled_dir: pathlib.Path, team_id: str, team: TeamSettings
) -> list[bundled_upgrade.ManagedFile]:
    """Return the bundled files reconciled for one team."""
    label = f"[team.{team_id}]"
    state_root = pathlib.Path(team.data_dir) / ".bundled_upgrade"
    entries: list[tuple[pathlib.Path, pathlib.Path]] = [
        (bundled_dir / "odoo.conf", pathlib.Path("odoo.conf")),
        (
            bundled_dir / "01_disable_mail.sql",
            pathlib.Path("odoo_sanitize") / "01_disable_mail.sql",
        ),
    ]

    guides_dir = bundled_dir / "agent_guides"
    if guides_dir.is_dir():
        entries.extend(
            (guide, pathlib.Path("agent_guides") / guide.name)
            for guide in sorted(guides_dir.iterdir())
            if guide.is_file() and guide.suffix == ".md"
        )

    return [
        bundled_upgrade.ManagedFile(
            label=label,
            source=source,
            destination=pathlib.Path(team.data_dir) / relative,
            state_root=state_root,
            relative_path=relative,
        )
        for source, relative in entries
        if source.is_file()
    ]


def _apply_agent_feedback(settings: Settings) -> None:
    """Expose ``submit_agent_feedback`` only while the hidden option is on.

    Registered disabled, so by default the tool is absent from ``list_tools``
    and calling it answers "Unknown tool". Enabling also appends a one-liner to
    the MCP instructions every client receives in the initialize handshake.
    """
    if not settings.agent_feedback:
        return
    from oduflow import agent_feedback as feedback_mod

    submit_agent_feedback.enable()
    if feedback_mod.MCP_HINT not in (mcp.instructions or ""):
        mcp.instructions = f"{mcp.instructions}\n\n{feedback_mod.MCP_HINT}".strip()
    logger.info("Agent feedback enabled (submit_agent_feedback exposed)")


def _ensure_initialized(settings: Settings) -> None:
    """Ensure shared infrastructure and per-team directories exist (idempotent)."""
    from oduflow import prereqs

    _apply_agent_feedback(settings)
    _copy_bundled_configs()
    prereqs.ensure_fuse_overlayfs()
    prereqs.ensure_rsync()
    system_ops.init_system(settings)

    # Snapshot-before-deploy hook (no-op while [backup] is unconfigured).
    from oduflow import backup_ops

    backup_ops.register_pre_update_hook()

    bundled_dir = pathlib.Path(__file__).resolve().parent / "templates"

    for team_id, team in settings.teams.items():
        os.makedirs(team.workspaces_dir, exist_ok=True)
        os.makedirs(os.path.join(team.data_dir, "templates"), exist_ok=True)
        os.makedirs(team.shared_repos_dir, exist_ok=True)

        # Initialize empty service presets file
        presets_path = os.path.join(team.data_dir, "service_presets.json")
        if not os.path.isfile(presets_path):
            with open(presets_path, "w") as f:
                f.write("{}\n")

        os.makedirs(os.path.join(team.data_dir, "odoo_sanitize"), exist_ok=True)
        os.makedirs(os.path.join(team.data_dir, "agent_guides"), exist_ok=True)
        for managed in _managed_bundled_files(bundled_dir, team_id, team):
            if bundled_upgrade.seed_managed_file(managed):
                logger.info("[team.%s] Bundled file: %s", team_id, managed.destination)

        logger.info(
            "Team %s initialized (workspaces=%s, templates=%s)",
            team_id,
            team.workspaces_dir,
            os.path.join(team.data_dir, "templates"),
        )

    # Filestore overlays do not survive an Oduflow restart in Docker: the
    # fuse-overlayfs daemons die with the container's PID namespace while their
    # mounts live on in the host's. Repair them before anything serves traffic,
    # so environments do not come back up on a filestore that raises ENOTCONN.
    try:
        repaired = env_ops.reconcile_overlay_mounts(settings)
    except Exception:  # noqa: BLE001 - a repair pass must never block startup
        logger.warning("Filestore overlay reconciliation failed", exc_info=True)
    else:
        if repaired:
            logger.warning(
                "Filestore overlay reconciliation: %d repaired, %d need attention",
                sum(1 for r in repaired if r["repaired"]),
                sum(1 for r in repaired if not r["repaired"]),
            )

    global _instance_id  # noqa: PLW0603
    from oduflow.telemetry import record_startup

    _instance_id = record_startup(
        settings.etc_dir, _get_version(), settings.disable_telemetry
    )


def _run_upgrade(settings: Settings, *, force: bool = False) -> bool:
    """Three-way merge deployed bundled files with the current package."""
    bundled_dir = pathlib.Path(__file__).resolve().parent / "templates"
    managed_files: list[bundled_upgrade.ManagedFile] = []
    for team_id, team in settings.teams.items():
        managed_files.extend(_managed_bundled_files(bundled_dir, team_id, team))

    if not managed_files:
        print("Nothing to upgrade — no bundled files found.")
        return True

    plans = [
        bundled_upgrade.plan_reconcile(managed, force=force)
        for managed in managed_files
    ]
    actionable = [plan for plan in plans if plan.needs_confirmation]
    kept = [plan for plan in plans if plan.kind == "keep"]

    if not actionable:
        success = _apply_upgrade_plans(plans)
        if not success:
            return False
        if not kept:
            print("\nAll bundled files are already up to date. Nothing to do.")
            return True
        print("\nNo bundled files require reconciliation.")
        print("Files marked with # KEEP remain untouched:")
        for plan in kept:
            print(f"  {plan.managed.label} {plan.managed.destination} (kept)")
        return True

    print()
    print("=" * 70)
    print("  The following bundled files will be reconciled.")
    print("  Local changes are preserved through three-way merge.")
    print("=" * 70)
    print()
    for plan in actionable:
        print(
            f"  {plan.managed.label} {plan.managed.destination} "
            f"({_upgrade_plan_description(plan)})"
        )
    if kept:
        print()
        print("  Files marked with # KEEP will remain untouched:")
        for plan in kept:
            print(f"  {plan.managed.label} {plan.managed.destination} (kept)")
    print()
    if force:
        print("  --force: conflicts and legacy files are overwritten with the new")
        print("  bundle. The replaced file is kept under .bundled_upgrade/backups/.")
    else:
        print("  Conflicts and legacy files keep the live file unchanged and create")
        print("  a sidecar for manual resolution.")
    print()

    if not force:
        try:
            input("  Press Enter to continue or Ctrl+C to abort... ")
        except KeyboardInterrupt:
            print("\n\nAborted. No files were changed.")
            return True

    success = _apply_upgrade_plans(plans)
    if success:
        changed = sum(
            plan.kind in {"create", "update", "merge", "overwrite"} for plan in plans
        )
        print(
            f"\nDone. Reconciled {changed} file(s) across "
            f"{len(settings.teams)} team(s)."
        )
    else:
        print(
            "\nUpgrade needs attention. Live files with conflicts or legacy "
            "state were not overwritten."
        )
    return success


def _upgrade_plan_description(plan: bundled_upgrade.ReconcilePlan) -> str:
    descriptions = {
        "create": "new",
        "update": "upstream update",
        "merge": "clean merge",
        "overwrite": "overwritten with the new bundle; local file backed up",
        "legacy": f"legacy; review {plan.managed.new_path}",
        "legacy-pending": f"pending review; update {plan.managed.new_path}",
        "conflict": f"conflict; resolve {plan.managed.merge_path}",
        "error": f"merge unavailable; review {plan.managed.error_path}",
    }
    return descriptions.get(plan.kind, plan.kind)


def _apply_upgrade_plans(plans: list[bundled_upgrade.ReconcilePlan]) -> bool:
    success = True
    for plan in plans:
        try:
            bundled_upgrade.apply_reconcile(plan)
        except bundled_upgrade.BundledUpgradeError as exc:
            print(f"  ERROR: {plan.managed.destination}: {exc}")
            success = False
            continue

        destination = plan.managed.destination
        if plan.kind == "create":
            print(f"  Created: {destination}")
        elif plan.kind == "update":
            print(f"  Updated: {destination}")
        elif plan.kind == "merge":
            print(f"  Merged: {destination}")
        elif plan.kind == "overwrite":
            print(f"  Overwritten: {destination} (backup: {plan.managed.backup_path})")
        elif plan.kind in {"legacy", "legacy-pending"}:
            print(
                f"  REVIEW: {destination} kept; merge {plan.managed.new_path} "
                "manually, then delete the sidecar"
            )
            success = False
        elif plan.kind == "conflict":
            print(
                f"  CONFLICT: {destination} kept; resolve "
                f"{plan.managed.merge_path}, install it as the live file, "
                "then remove the sidecar"
            )
            success = False
        elif plan.kind == "error":
            print(
                f"  ERROR: {destination} kept; {plan.error}; "
                f"new bundle saved to {plan.managed.error_path}"
            )
            success = False
    return success


def _copy_bundled_configs() -> None:
    """Provision the shared postgresql.conf in the config directory.

    On first init it is auto-generated from the detected server resources
    (lean, SSD-oriented — see ``pg_tune``); if generation fails it falls back to
    the static bundled config. Existing files are never touched.
    """
    import pathlib

    settings = _get_settings()
    etc_dir = pathlib.Path(settings.etc_dir)
    try:
        os.makedirs(etc_dir, exist_ok=True)
    except PermissionError:
        logger.warning(
            "Cannot create %s (permission denied), skipping config copy", etc_dir
        )
        return

    dest = etc_dir / "postgresql.conf"
    if not dest.is_file():
        if not _write_tuned_pg_conf(dest, settings=settings):
            _copy_bundled_pg_conf(dest)
    else:
        _warn_stale_dev_pg_conf(dest, settings)


def _write_tuned_pg_conf(
    dest: pathlib.Path, *, settings: Settings | None = None
) -> bool:
    """Generate a resource-tuned postgresql.conf at ``dest``. True on success."""
    try:
        from oduflow import pg_tune
        from oduflow.resource_plan import build_resource_plan

        settings = settings or _get_settings()
        res = pg_tune.detect_resources()
        plan = build_resource_plan(
            res["total_ram_mb"],
            res["cpu_count"],
            production_enabled=settings.prod_enabled,
        )
        content = pg_tune.generate_postgresql_conf(
            res["total_ram_mb"],
            res["cpu_count"],
            source=res["source"],
            oduflow_version=_get_version(),
            plan=plan,
        )
        dest.write_text(content, encoding="utf-8")
        # PostgreSQL reads this non-secret bind mount as its container user.
        dest.chmod(0o644)
        logger.info(
            "Config: %s (auto-tuned: %d vCPU, %d MB RAM, source=%s)",
            dest,
            res["cpu_count"],
            int(res["total_ram_mb"]),
            res["source"],
        )
        return True
    except PermissionError:
        logger.warning("Cannot write %s (permission denied)", dest)
        return False
    except Exception:
        logger.warning(
            "Failed to auto-tune postgresql.conf, using bundled default",
            exc_info=True,
        )
        return False


def _warn_stale_dev_pg_conf(dest: pathlib.Path, settings: Settings) -> None:
    """Warn when an auto-generated dev config no longer matches host intent."""
    try:
        from oduflow import pg_tune
        from oduflow.resource_plan import (
            build_resource_plan,
            tune_marker,
            tune_status,
        )

        content = dest.read_text(encoding="utf-8", errors="replace")
        res = pg_tune.detect_resources()
        plan = build_resource_plan(
            res["total_ram_mb"],
            res["cpu_count"],
            production_enabled=settings.prod_enabled,
        )
        status = tune_status(content, tune_marker(plan, "dev"))
        if status in {"stale", "legacy"}:
            logger.warning(
                "Auto-generated PostgreSQL config %s is %s for the current "
                "host resource plan; run `oduflow retune-postgres` to preview "
                "the update",
                dest,
                status,
            )
    except Exception:
        logger.debug("Could not check PostgreSQL tuning fingerprint", exc_info=True)


def _run_retune_postgres(
    settings: Settings, *, apply: bool = False, force: bool = False
) -> bool:
    """Preview or explicitly apply the current unified host resource plan."""
    import datetime
    import difflib
    import shutil
    import tempfile

    from oduflow import pg_tune, prod_tune
    from oduflow.docker_ops.client import get_client
    from oduflow.naming import get_repo_path, get_workspace_path, prod_env_name
    from oduflow.resource_plan import (
        ProfileName,
        TuneStatus,
        build_resource_plan,
        describe_plan,
        tune_marker,
        tune_status,
    )

    res = pg_tune.detect_resources()
    plan = build_resource_plan(
        res["total_ram_mb"],
        res["cpu_count"],
        production_enabled=settings.prod_enabled,
    )
    version = _get_version()
    targets: list[tuple[ProfileName, pathlib.Path, str, str]] = [
        (
            "dev",
            pathlib.Path(settings.etc_dir) / "postgresql.conf",
            pg_tune.generate_postgresql_conf(
                res["total_ram_mb"],
                res["cpu_count"],
                source=res["source"],
                oduflow_version=version,
                plan=plan,
            ),
            settings.shared_db_container,
        )
    ]
    if settings.prod_enabled:
        targets.append(
            (
                "production",
                pathlib.Path(settings.etc_dir) / "postgresql-prod.conf",
                prod_tune.generate_prod_postgresql_conf(
                    res["total_ram_mb"],
                    res["cpu_count"],
                    source=res["source"],
                    oduflow_version=version,
                    plan=plan,
                ),
                settings.prod_db_container,
            )
        )

    odoo_targets: list[tuple[TeamSettings, str, pathlib.Path, str, str | None]] = []
    odoo_errors: list[str] = []
    if settings.prod_enabled:
        with tempfile.TemporaryDirectory(prefix="oduflow-retune-") as tmp_dir:
            for team in settings.teams.values():
                for name, record in sorted(
                    production_registry.list_productions(team).items()
                ):
                    env_name = prod_env_name(name)
                    repo_path = get_repo_path(env_name, team.workspaces_dir)
                    if not os.path.isdir(repo_path):
                        odoo_errors.append(
                            f"team {team.team_id}/{name}: repository checkout "
                            f"not found at {repo_path}"
                        )
                        continue
                    extra_addons = record.get("extra_addons", {})
                    extra_names = (
                        list(extra_addons) if isinstance(extra_addons, dict) else []
                    )
                    candidate_path = (
                        pathlib.Path(tmp_dir) / team.team_id / f"{name}.conf"
                    )
                    candidate_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        production_ops._build_prod_odoo_conf(
                            settings,
                            team,
                            name,
                            repo_path,
                            [f"/mnt/extra-addons-{repo}" for repo in extra_names],
                            plan=plan,
                            output_path=str(candidate_path),
                        )
                    except Exception as exc:  # noqa: BLE001 - report each production
                        odoo_errors.append(f"team {team.team_id}/{name}: {exc}")
                        continue
                    path = (
                        pathlib.Path(get_workspace_path(env_name, team.workspaces_dir))
                        / "odoo.conf"
                    )
                    existing = (
                        path.read_text(encoding="utf-8", errors="replace")
                        if path.is_file()
                        else None
                    )
                    odoo_targets.append(
                        (
                            team,
                            name,
                            path,
                            candidate_path.read_text(encoding="utf-8"),
                            existing,
                        )
                    )

    print("\nUnified resource plan")
    print("=" * 70)
    for line in describe_plan(plan):
        print(f"  {line}")

    inspected: list[
        tuple[ProfileName, pathlib.Path, str, str, str | None, TuneStatus]
    ] = []
    for profile, path, candidate, container in targets:
        existing = (
            path.read_text(encoding="utf-8", errors="replace")
            if path.is_file()
            else None
        )
        status = tune_status(existing, tune_marker(plan, profile))
        # A matching planner marker is authoritative. Preserve any operator
        # edits beneath it and do not churn the informational Oduflow version
        # in the header on every package upgrade.
        if status == "current" and existing is not None:
            candidate = existing
        inspected.append((profile, path, candidate, container, existing, status))
        print(f"\n[{profile}] {path} ({status})")
        if existing == candidate:
            print("  No content changes.")
            continue
        diff = difflib.unified_diff(
            (existing or "").splitlines(),
            candidate.splitlines(),
            fromfile=str(path),
            tofile=f"{path} (retuned)",
            lineterm="",
        )
        for line in diff:
            print(line)

    for team, name, path, candidate, existing in odoo_targets:
        status = "current" if existing == candidate else "stale"
        print(f"\n[production Odoo {team.team_id}/{name}] {path} ({status})")
        if existing == candidate:
            print("  No content changes.")
            continue
        diff = difflib.unified_diff(
            (existing or "").splitlines(),
            candidate.splitlines(),
            fromfile=str(path),
            tofile=f"{path} (retuned)",
            lineterm="",
        )
        for line in diff:
            print(line)

    if odoo_errors:
        print("\nCould not plan production Odoo config(s):")
        for error in odoo_errors:
            print(f"  {error}")

    changed = [item for item in inspected if item[4] != item[2]]
    changed_odoo = [item for item in odoo_targets if item[4] != item[3]]
    if not apply:
        if changed or changed_odoo:
            print("\nPreview only. Re-run with --apply to write these changes.")
        else:
            print("\nAll managed configs already match the plan.")
        if not settings.prod_enabled:
            print("Production is disabled; postgresql-prod.conf was not planned.")
        return not odoo_errors

    custom = [str(path) for _, path, _, _, _, status in changed if status == "custom"]
    if custom and not force:
        print("\nRefusing to overwrite custom PostgreSQL config(s):")
        for custom_path in custom:
            print(f"  {custom_path}")
        print("Re-run with --apply --force only if replacing them is intentional.")
        return False

    if odoo_errors:
        print("\nRefusing to apply an incomplete production Odoo resource plan.")
        return False

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pg_restart: list[str] = []
    odoo_restart: list[str] = []

    def _backup(path: pathlib.Path) -> None:
        backup = pathlib.Path(f"{path}.bak-{stamp}")
        suffix = 1
        while backup.exists():
            backup = pathlib.Path(f"{path}.bak-{stamp}-{suffix}")
            suffix += 1
        shutil.copy2(path, backup)
        print(f"Backup: {backup}")

    client = get_client() if changed_odoo else None
    for _profile, path, candidate, container, existing, _status in changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing is not None:
            _backup(path)
        path.write_text(candidate, encoding="utf-8")
        path.chmod(0o644)
        print(f"Updated: {path}")
        pg_restart.append(container)

    for team, name, path, candidate, existing in changed_odoo:
        assert client is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing is not None:
            _backup(path)
        path.write_text(candidate, encoding="utf-8")
        print(f"Updated: {path}")
        staged_container = production_ops.stage_prod_odoo_conf(
            client, settings, team, name, str(path)
        )
        if staged_container is None:
            print(
                f"No container for production {team.team_id}/{name}; "
                "updated its generated config on disk only."
            )
        else:
            print(f"Staged in container: {staged_container}")
            odoo_restart.append(staged_container)

    if not changed and not changed_odoo:
        print("\nNo files changed.")
        return True
    if pg_restart:
        print("\nRestart existing PostgreSQL containers to activate these settings:")
        for container in pg_restart:
            print(f"  docker restart {container}")
    if odoo_restart:
        print("\nRestart production Odoo containers to activate worker settings:")
        for container in odoo_restart:
            print(f"  docker restart {container}")
    return True


def _copy_bundled_pg_conf(dest: pathlib.Path) -> None:
    """Fallback: copy the static bundled postgresql.conf to ``dest``."""
    import shutil

    bundled = pathlib.Path(__file__).resolve().parent / "templates" / "postgresql.conf"
    if bundled.is_file():
        try:
            shutil.copy2(str(bundled), str(dest))
            dest.chmod(0o644)
            logger.info("Config: %s (bundled default)", dest)
        except PermissionError:
            logger.warning("Cannot write %s (permission denied)", dest)


def _inject_db_password(toml_text: str, password: str) -> str:
    """Insert an auto-generated PostgreSQL superuser password into the
    ``[database]`` section of a freshly bootstrapped oduflow.toml.

    The bundled template ships without a password so each install gets a
    unique secret. Only used when creating the config from the template —
    existing user configs are never rewritten.
    """
    line = f'password = "{password}"'
    out: list[str] = []
    injected = False
    for raw in toml_text.splitlines():
        out.append(raw)
        if not injected and raw.strip() == "[database]":
            out.append(line)
            injected = True
    if not injected:
        out.extend(["", "[database]", line])
    return "\n".join(out) + "\n"


def _inject_auth_token(toml_text: str, token: str) -> str:
    """Replace the empty ``auth_token = ""`` in a freshly bootstrapped
    oduflow.toml with a generated MCP token, so a fresh HTTP install is
    authenticated by default. Only the first empty auth_token (team 1) is set;
    existing user configs are never rewritten.
    """
    replacement = f'auth_token = "{token}"'
    out: list[str] = []
    injected = False
    for raw in toml_text.splitlines():
        if not injected and raw.split("#", 1)[0].strip() == 'auth_token = ""':
            indent = raw[: len(raw) - len(raw.lstrip())]
            comment = raw.split("#", 1)[1].strip() if "#" in raw else ""
            out.append(f"{indent}{replacement}" + (f"  # {comment}" if comment else ""))
            injected = True
        else:
            out.append(raw)
    return "\n".join(out) + "\n"


def _inject_ui_password(toml_text: str, password: str) -> str:
    """Replace the empty ``ui_password = ""`` in a freshly bootstrapped
    oduflow.toml with a generated web-UI password, so a fresh HTTP install does
    NOT serve the dashboard (interactive shells, SQL, agent, service creation)
    unauthenticated. Only the first empty ui_password (team 1) is set; existing
    user configs are never rewritten.
    """
    replacement = f'ui_password = "{password}"'
    out: list[str] = []
    injected = False
    for raw in toml_text.splitlines():
        if not injected and raw.split("#", 1)[0].strip() == 'ui_password = ""':
            indent = raw[: len(raw) - len(raw.lstrip())]
            comment = raw.split("#", 1)[1].strip() if "#" in raw else ""
            out.append(f"{indent}{replacement}" + (f"  # {comment}" if comment else ""))
            injected = True
        else:
            out.append(raw)
    return "\n".join(out) + "\n"


def _autofill_ui_passwords(toml_text: str) -> tuple[str, list[str]]:
    """Fill EVERY empty ``ui_password = ""`` in an existing oduflow.toml with a
    freshly generated password (one distinct password per team). Used to upgrade
    older configs that predate the secure-by-default bootstrap without leaving
    the dashboard open. Returns the rewritten text and the list of generated
    passwords (empty if there was nothing to fill)."""
    import secrets

    out: list[str] = []
    generated: list[str] = []
    for raw in toml_text.splitlines():
        if raw.split("#", 1)[0].strip() == 'ui_password = ""':
            password = secrets.token_urlsafe(18)
            generated.append(password)
            indent = raw[: len(raw) - len(raw.lstrip())]
            comment = raw.split("#", 1)[1].strip() if "#" in raw else ""
            out.append(
                f'{indent}ui_password = "{password}"'
                + (f"  # {comment}" if comment else "")
            )
        else:
            out.append(raw)
    return "\n".join(out) + "\n", generated


def _ensure_web_ui_password(settings: Settings) -> Settings:
    """Auto-provision a web-UI password for existing configs (HTTP mode only).

    Older installs shipped ``ui_password = ""`` (open dashboard). Rather than
    hard-fail the whole HTTP transport on upgrade — which would also take down
    token-protected MCP for headless users — generate a password, persist it to
    oduflow.toml (symmetric with the fresh-install bootstrap) and reload. Skipped
    when the operator opted into an open server (``allow_insecure_http``) or
    every team already has a password. Uses ``all`` (not ``any``) so a team added
    later with ``ui_password = ""`` still gets provisioned rather than being
    silently locked out of the web UI while other teams have passwords. On any
    write failure the caller's fail-closed check still refuses to serve an open
    dashboard."""
    global _settings
    if settings.allow_insecure_http or all(
        t.ui_password for t in settings.teams.values()
    ):
        return settings
    import pathlib

    try:
        cfg_path = find_toml()
        updated, generated = _autofill_ui_passwords(
            pathlib.Path(cfg_path).read_text(encoding="utf-8")
        )
        if not generated:
            return settings
        pathlib.Path(cfg_path).write_text(updated, encoding="utf-8")
    except Exception:
        logger.exception("Could not auto-provision a web-UI password")
        return settings
    for password in generated:
        logger.warning(
            "Auto-generated a web-UI password (user 'admin') for a team that had "
            "none, so upgrading does not serve the dashboard unauthenticated: %s",
            password,
        )
    _settings = None
    return _get_settings()


def _run_reload_template(
    settings: Settings, team: TeamSettings, template_name: str, dump_path: str = ""
) -> None:
    result = system_ops.reload_template(
        settings,
        team,
        template_name=template_name,
        dump_path=dump_path or None,
    )
    msg = f"Template DB {result['status']}.\nTemplate DB: {result['template_db']}"
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    if "tables" in result:
        msg += f"\nTables restored: {result['tables']}"
    if "message" in result and result["message"].strip():
        msg += f"\nRestore output: {result['message']}"
    print(msg)


def _run_init_template(
    settings: Settings,
    team: TeamSettings,
    odoo_image: str,
    modules: str = "base",
    template_name: str = "",
    force: bool = False,
) -> None:
    result = system_ops.init_template(
        settings,
        team,
        template_name=template_name,
        odoo_image=odoo_image,
        modules=modules,
        force=force,
    )
    msg = (
        f"Template '{template_name}' generated and loaded.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['generated_dump']}\n"
        f"Filestore: {result['generated_filestore']}"
    )
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_template_from_env(
    settings: Settings,
    team: TeamSettings,
    branch: str,
    template_name: str = "",
    reset_env_changes: bool = False,
) -> None:
    result = system_ops.publish_env_as_template(
        settings,
        team,
        env_name=branch,
        template_name=template_name,
        reset_env_changes=reset_env_changes,
    )
    lines = [
        f"Environment '{result['env_name']}' saved as template '{template_name}'.",
        f"Template DB: {result['template_db']}",
        f"Dump: {result['dump']}",
        f"Filestore: {result['filestore']}",
    ]
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    if affected:
        verb = "Reset" if reset_env_changes else "Remounted (changes preserved)"
        lines.append(f"{verb} filestore overlays for: {', '.join(affected)}")
    if failures:
        lines.append(
            "Remount issues:\n" + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    print("\n".join(lines))


def _run_refresh_template(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    reset_env_changes: bool = False,
) -> None:
    result = system_ops.refresh_template(
        settings, team, template_name, reset_env_changes=reset_env_changes
    )
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    if affected:
        verb = "Reset" if reset_env_changes else "Remounted (changes preserved)"
        lines = [f"{verb} filestore overlays for: {', '.join(affected)}"]
    else:
        lines = [
            f"No live overlay environments use template '{template_name}'; "
            "nothing to do."
        ]
    if failures:
        lines.append(
            "Remount issues:\n" + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    print("\n".join(lines))


def _run_attach_filestore(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    source: str,
    reset_env_changes: bool = False,
    strip_prefix: str = "auto",
) -> None:
    result = system_ops.attach_filestore(
        settings,
        team,
        template_name,
        source,
        reset_env_changes=reset_env_changes,
        strip_prefix=strip_prefix,
    )
    lines = [
        f"Filestore attached to template '{result['template_name']}'.",
        f"Source: {result['source']} ({result['source_kind']})",
        f"Strip prefix: {result.get('strip_prefix') or '<none>'}",
        f"Files: {result['filestore_files']}",
        f"Filestore size: {result['filestore_size_mb']} MB",
        f"Mode: {'overlay' if result.get('use_overlay') else 'copy'}",
    ]
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    if affected:
        verb = "Reset" if reset_env_changes else "Remounted (changes preserved)"
        lines.append(f"{verb} filestore overlays for: {', '.join(affected)}")
    if failures:
        lines.append(
            "Remount issues:\n" + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    print("\n".join(lines))


def _run_delete_template(
    settings: Settings, team: TeamSettings, template_name: str
) -> None:
    result = system_ops.delete_template(settings, team, template_name)
    print(
        f"Template '{result['template_name']}' deleted.\nTemplate DB '{result['template_db']}' removed."
    )


def _run_import_template(
    settings: Settings,
    team: TeamSettings,
    odoo_url: str,
    master_pwd: str,
    db_name: str = "",
    template_name: str = "",
    without_filestore: bool = False,
) -> None:
    result = system_ops.import_from_odoo(
        settings,
        team,
        odoo_url=odoo_url,
        master_pwd=master_pwd,
        db_name=db_name,
        template_name=template_name,
        without_filestore=without_filestore,
    )
    lines = [
        f"Template '{result['template_name']}' imported successfully!",
        f"Source: {result['source_url']} (db: {result['source_db']})",
        f"Odoo version: {result['odoo_version']}",
        f"Odoo image: {result['odoo_image']}",
        f"Template DB: {result['template_db']}",
        "Filestore: "
        + ("included" if result.get("includes_filestore") else "not included"),
        f"Backup size: {result['zip_size_mb']} MB",
        f"DB restore time: {result['restore_seconds']}s",
    ]
    affected = cast("list[str]", result.get("affected_envs", []))
    failures = cast("list[tuple[str, str]]", result.get("remount_failures", []))
    if affected:
        lines.append(
            "Remounted (changes preserved) filestore overlays for: "
            + ", ".join(affected)
        )
    if failures:
        lines.append(
            "Remount issues:\n" + "\n".join(f"- {env}: {msg}" for env, msg in failures)
        )
    print("\n".join(lines))


def _run_list_templates(settings: Settings, team: TeamSettings) -> None:
    templates = system_ops.list_templates(settings, team)
    if not templates:
        print("No template profiles found.")
        return
    print("Template profiles:")
    for r in templates:
        db_status = "loaded" if r["db_loaded"] else "not loaded"
        overlay_status = "overlay" if r.get("use_overlay") else "copy"
        fs_size = r.get("filestore_size_mb")
        dump_size = r.get("dump_size_mb")
        size_info = ""
        if fs_size is not None or dump_size is not None:
            fs_str = f"{fs_size:.0f} MB" if fs_size is not None else "?"
            dump_str = f"{dump_size:.0f} MB" if dump_size is not None else "?"
            size_info = f", Filestore size={fs_str}, Dump size={dump_str}"
        print(
            f"  {r['template_name']}: DB={db_status} ({r['template_db']}), SQL={'yes' if r['has_sql'] else 'no'}, Filestore={'yes' if r['has_filestore'] else 'no'}, Mode={overlay_status}{size_info}"
        )


def _run_list_services(settings: Settings, team: TeamSettings) -> None:
    from oduflow.docker_ops import service_ops

    services = service_ops.list_services(settings, team)
    if not services:
        print("No active services found.")
        return
    print("Active services:")
    for svc in services:
        status_icon = "●" if svc["status"] == "running" else "○"
        print(
            f"  {status_icon} {svc['name']} ({svc['container_name']}): {svc['status']}"
        )
        print(f"    Image: {svc['image']}")
        if svc.get("port"):
            print(f"    Port: {svc['port']}")
        if svc.get("url"):
            print(f"    URL: {svc['url']}")
        if svc.get("env_vars"):
            env_str = ", ".join(f"{k}={v}" for k, v in svc["env_vars"].items())
            print(f"    Env: {env_str}")


def _run_list_service_databases(settings: Settings, team: TeamSettings) -> None:
    rows = service_database_ops.list_databases(settings, team)
    if not rows:
        print("No service databases found.")
        return
    print("Service databases:")
    for row in rows:
        size_mb = int(row.get("size_bytes", 0)) / 1024**2
        print(
            f"  {row['name']}: {row['status']} "
            f"({row.get('database', 'unknown')}, {size_mb:.1f} MB, "
            f"{row.get('connections', 0)} connections)"
        )


def _run_cleanup(settings: Settings, team: TeamSettings, dry_run: bool = True) -> None:
    result = system_ops.cleanup_orphans(settings, team, dry_run=dry_run)
    mode = "DRY RUN" if result["dry_run"] else "CLEANUP"
    dbs = result["orphan_databases"]
    workspaces = result["orphan_workspaces"]
    ports = result["orphan_ports"]
    roles = result["orphan_roles"]

    if not dbs and not workspaces and not ports and not roles:
        print(f"[{mode}] No orphaned resources found.")
        return

    print(f"[{mode}] Orphaned resources:")
    if dbs:
        print(f"  Databases ({len(dbs)}):")
        for db in dbs:
            print(f"    - {db}")
    if workspaces:
        print(f"  Workspaces ({len(workspaces)}):")
        for ws in workspaces:
            print(f"    - {ws}")
    if ports:
        print(f"  Port registry entries ({len(ports)}):")
        for p in ports:
            print(f"    - {p}")
    if roles:
        print(f"  PostgreSQL roles ({len(roles)}):")
        for role in roles:
            print(f"    - {role}")

    total = len(dbs) + len(workspaces) + len(ports) + len(roles)
    if result["dry_run"]:
        print(f"\n  {total} resource(s) would be removed. Run with --force to apply.")
    else:
        print(f"\n  {total} resource(s) removed.")


def _run_destroy(settings: Settings) -> None:
    result = system_ops.destroy_system(settings)
    print(f"System {result['status']}.\nRemoved: {result['removed']}")


def _print_tools(verbose: bool = False) -> None:
    import inspect

    print("Registered tools:")
    for name in sorted(mcp._tool_manager._tools.keys()):
        tool_fn = cast(Any, mcp._tool_manager._tools[name]).fn
        sig = inspect.signature(tool_fn)
        params = []
        for p in sig.parameters.values():
            if p.name == "ctx":
                continue
            if p.default is inspect.Parameter.empty:
                params.append(f"<{p.name}>")
            else:
                params.append(f"[{p.name}={p.default}]")
        print(f"  {name} {' '.join(params)}")
        if verbose:
            desc = (tool_fn.__doc__ or "").strip().split("\n")[0]
            if desc:
                print(f"    {desc}")


def _run_call(argv: list[str]) -> None:
    """Execute an MCP tool from the CLI: oduflow call <tool> [args...]"""
    import inspect
    import json

    if not argv:
        _print_tools(verbose=False)
        return

    tool_name = argv[0]
    tool_argv = argv[1:]

    if tool_name not in mcp._tool_manager._tools:
        print(f"Unknown tool: {tool_name}")
        print(f"Available: {', '.join(sorted(mcp._tool_manager._tools.keys()))}")
        sys.exit(1)

    tool_fn = cast(Any, mcp._tool_manager._tools[tool_name]).fn
    sig = inspect.signature(tool_fn)

    if tool_argv and tool_argv[0].startswith("{"):
        kwargs = json.loads(tool_argv[0])
    else:
        # Filter out ctx parameter for positional arg mapping
        params = [p for p in sig.parameters.values() if p.name != "ctx"]
        kwargs = {}
        for i, value in enumerate(tool_argv):
            if i >= len(params):
                print(f"Warning: extra argument '{value}' ignored", file=sys.stderr)
                continue
            param = params[i]
            annotation = param.annotation
            if annotation is bool or (
                annotation is inspect.Parameter.empty
                and isinstance(param.default, bool)
            ):
                kwargs[param.name] = value.lower() in ("true", "1", "yes")
            elif annotation is int or (
                annotation is inspect.Parameter.empty and isinstance(param.default, int)
            ):
                kwargs[param.name] = int(value)
            elif annotation is float:
                kwargs[param.name] = float(value)
            else:
                kwargs[param.name] = value

    if not kwargs:
        required = [
            p
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty and p.name != "ctx"
        ]
        if required:
            parts = []
            for p in sig.parameters.values():
                if p.name == "ctx":
                    continue
                if p.default is inspect.Parameter.empty:
                    parts.append(f"<{p.name}>")
                else:
                    parts.append(f"[{p.name}={p.default}]")
            print(f"Usage: oduflow call {tool_name} {' '.join(parts)}")
            return

    print(f"Calling: {tool_name}({kwargs})")
    print("-" * 60)
    logging.getLogger("oduflow").setLevel(logging.WARNING)
    try:
        import asyncio

        result = tool_fn(**kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(cast(Any, result))
        print(result)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _get_version() -> str:
    """Return the installed package version."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("oduflow")
    except PackageNotFoundError:
        return "dev"


# =============================================================================
# CLI entry point
# =============================================================================


def _dispatch_client(argv: Sequence[str]) -> None:
    """Run the remote MCP client and turn its exit code into a process exit."""
    from oduflow.client import run_client

    exit_code = run_client(list(argv))
    if exit_code:
        raise SystemExit(exit_code)


def _run_cli() -> None:
    """Entry point for the Oduflow MCP server."""
    # The remote client accepts flags defined by the live server's tool schemas,
    # so the static top-level parser must not try to interpret them first.
    if len(sys.argv) > 1 and sys.argv[1] == "client":
        _dispatch_client(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="oduflow", description="Oduflow — Odoo dev environment manager"
    )
    parser.add_argument(
        "--version", action="version", version=f"oduflow {_get_version()}"
    )
    parser.add_argument(
        "-t",
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--stack",
        dest="stack_manifest",
        default="",
        help="reconcile this declarative Stack manifest before starting the server",
    )
    parser.add_argument(
        "--stack-team",
        default="1",
        help="team for --stack startup reconciliation (default: 1)",
    )
    sub = parser.add_subparsers(dest="command", title="commands", metavar="")

    # --- System commands ---
    sub.add_parser("destroy", help="Destroy all shared infrastructure")
    p_upgrade = sub.add_parser(
        "upgrade",
        help="Merge bundled configs, agent guides, and sanitize scripts",
    )
    p_upgrade.add_argument(
        "--force",
        action="store_true",
        help=(
            "reconcile without prompting and overwrite conflicting or legacy "
            "files with the new bundle (the replaced file is backed up)"
        ),
    )
    p_retune = sub.add_parser(
        "retune-postgres",
        help="Preview the unified host resource plan and managed config changes",
    )
    p_retune.add_argument(
        "--apply",
        action="store_true",
        help="Back up and write planned configs; stage production Odoo configs",
    )
    p_retune.add_argument(
        "--force",
        action="store_true",
        help="Allow --apply to replace a custom, non-Oduflow-generated config",
    )

    # --- Template commands (need --team) ---
    p_reload = sub.add_parser(
        "reload-template",
        help="Drop and re-restore a template DB from template profile",
    )
    p_reload.add_argument("template_name", help="Template profile name")
    p_reload.add_argument(
        "--dump-path",
        default="",
        help="Path to dump file (overrides template profile path)",
    )
    p_reload.add_argument(
        "--source",
        default="",
        help="Sync template from s3://... or local path before reloading",
    )
    p_reload.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress info logging (for cron)",
    )
    p_reload.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_init_tpl = sub.add_parser(
        "init-template",
        help="Generate template dump and filestore from a clean Odoo image",
    )
    p_init_tpl.add_argument(
        "--odoo-image", required=True, help="Docker image for Odoo (e.g. odoo:19.0)"
    )
    p_init_tpl.add_argument(
        "--modules",
        default="base",
        help="Comma-separated modules to install (default: base)",
    )
    p_init_tpl.add_argument(
        "--template-name", required=True, help="Template profile name"
    )
    p_init_tpl.add_argument(
        "--force", action="store_true", help="Overwrite existing dump.sql and filestore"
    )
    p_init_tpl.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_tfe = sub.add_parser(
        "template-from-env", help="Save a branch environment as the new template"
    )
    p_tfe.add_argument("branch", help="Branch name to use as template source")
    p_tfe.add_argument("--template-name", required=True, help="Template profile name")
    p_tfe.add_argument(
        "--reset-env-changes",
        action="store_true",
        help="Discard other environments' filestore changes (destructive). "
        "Default: preserve them.",
    )
    p_tfe.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_refresh = sub.add_parser(
        "refresh-template",
        help="Re-apply a template's filestore to live overlay envs (non-destructive)",
    )
    p_refresh.add_argument("template_name", help="Template profile name")
    p_refresh.add_argument(
        "--reset-env-changes",
        action="store_true",
        help="Discard environments' filestore changes (destructive). "
        "Default: preserve them.",
    )
    p_refresh.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_attach_fs = sub.add_parser(
        "attach-filestore",
        help="Attach or replace a template filestore from a directory, rsync/ssh source, or archive",
    )
    p_attach_fs.add_argument("template_name", help="Template profile name")
    p_attach_fs.add_argument(
        "source",
        help="Local dir/archive, rsync:// source, or SSH rsync source user@host:/path",
    )
    p_attach_fs.add_argument(
        "--strip-prefix",
        default="auto",
        help='Archive/source wrapper prefix to strip; "auto" detects it, "none" strips nothing',
    )
    p_attach_fs.add_argument(
        "--reset-env-changes",
        action="store_true",
        help="Discard environments' filestore changes (destructive). "
        "Default: preserve them.",
    )
    p_attach_fs.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_drop_tpl = sub.add_parser(
        "delete-template", help="Delete a template profile (template DB + files)"
    )
    p_drop_tpl.add_argument("template_name", help="Template profile name to delete")
    p_drop_tpl.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_import = sub.add_parser(
        "import-template", help="Import a template from a running Odoo instance"
    )
    p_import.add_argument(
        "odoo_url",
        help="Base URL of the Odoo instance (e.g. https://my-odoo.example.com)",
    )
    p_import.add_argument("master_pwd", help="Odoo master password")
    p_import.add_argument(
        "--db-name", default="", help="Database name (auto-detected if only one exists)"
    )
    p_import.add_argument(
        "--template-name", required=True, help="Template profile name"
    )
    p_import.add_argument(
        "--without-filestore",
        action="store_true",
        help="Request a database-only PostgreSQL custom dump",
    )
    p_import.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_lt = sub.add_parser("list-templates", help="List available template profiles")
    p_lt.add_argument("--team", default="1", help="Team ID (default: 1)")

    # --- Service commands ---
    p_ls = sub.add_parser(
        "list-services", help="List managed auxiliary service containers"
    )
    p_ls.add_argument("--team", default="1", help="Team ID (default: 1)")
    p_lsd = sub.add_parser(
        "list-service-databases",
        help="List PostgreSQL databases managed for auxiliary services",
    )
    p_lsd.add_argument("--team", default="1", help="Team ID (default: 1)")

    # --- Cleanup ---
    p_cleanup = sub.add_parser(
        "cleanup",
        help="Find and remove orphaned databases, workspaces, and port entries",
    )
    p_cleanup.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Only show what would be removed (default behavior)",
    )
    p_cleanup.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Actually remove orphaned resources",
    )
    p_cleanup.add_argument("--team", default="1", help="Team ID (default: 1)")

    # --- Tool introspection ---
    p_list = sub.add_parser("list", help="List registered MCP tools")
    p_list.add_argument(
        "--verbose", "-v", action="store_true", help="Show tool descriptions"
    )

    p_call = sub.add_parser(
        "call", help="Call an MCP tool: oduflow call <tool> [args...]"
    )
    p_call.add_argument(
        "call_args", nargs="*", default=[], help="Tool name and arguments"
    )

    p_client = sub.add_parser(
        "client",
        help="Call tools on a remote Oduflow MCP server",
        add_help=False,
    )
    p_client.add_argument(
        "client_args",
        nargs=argparse.REMAINDER,
        help="client options, remote tool name, and tool arguments",
    )

    # --- Declarative stacks ---
    p_stack = sub.add_parser(
        "stack", help="Validate, plan, apply, or inspect a declarative Stack"
    )
    stack_sub = p_stack.add_subparsers(
        dest="stack_command", title="stack commands", metavar=""
    )
    for stack_command in ("validate", "plan", "apply", "status"):
        p_stack_command = stack_sub.add_parser(stack_command)
        p_stack_command.add_argument("manifest", help="Path to oduflow.yaml")
        if stack_command != "validate":
            p_stack_command.add_argument(
                "--team", default="1", help="Team ID (default: 1)"
            )

    # --- Systemd ---
    sub.add_parser(
        "systemd-install", help="Install and enable a systemd service for Oduflow"
    )
    sub.add_parser(
        "systemd-uninstall",
        help="Stop, disable, and remove the Oduflow systemd service",
    )

    args = parser.parse_args()

    # --- Commands that don't need Settings -----------------------

    if args.command == "list":
        _print_tools(verbose=args.verbose)
        return

    if args.command == "call":
        _run_call(args.call_args)
        return

    # Reached only when a global option precedes the subcommand, as in
    # "oduflow -t http client list"; the fast path at the top of _run_cli
    # handles the plain "oduflow client ..." form.
    if args.command == "client":
        _dispatch_client(args.client_args)
        return

    if args.command == "stack" and args.stack_command == "validate":
        from oduflow.stack_loader import load_stack, validate_stack_files

        manifest = load_stack(args.manifest)
        validate_stack_files(manifest, args.manifest)
        print(f"Stack '{manifest.metadata.name}' is valid ({manifest.api_version}).")
        return

    if args.command == "systemd-install":
        from oduflow.systemd import install as systemd_install

        systemd_install()
        return

    if args.command == "systemd-uninstall":
        from oduflow.systemd import uninstall as systemd_uninstall

        systemd_uninstall()
        return

    # --- Load TOML settings ----------------------------------------

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("docker").setLevel(logging.WARNING)
    logging.getLogger("docket").setLevel(logging.WARNING)

    # Bootstrap: if no config exists, create it from the bundled default with an
    # auto-generated PostgreSQL password and a random MCP auth_token, so a fresh
    # install is authenticated by default even over HTTP (#37).
    try:
        find_toml()
    except FileNotFoundError:
        import pathlib
        import secrets

        from oduflow.settings import _resolve_etc_dir

        dest_dir = _resolve_etc_dir()
        os.makedirs(dest_dir, exist_ok=True)
        bundled = pathlib.Path(__file__).resolve().parent / "templates" / "oduflow.toml"
        dest = os.path.join(dest_dir, "oduflow.toml")
        generated_token = secrets.token_urlsafe(24)
        generated_ui_password = secrets.token_urlsafe(18)
        rendered = _inject_db_password(
            bundled.read_text(encoding="utf-8"), secrets.token_urlsafe(24)
        )
        rendered = _inject_auth_token(rendered, generated_token)
        rendered = _inject_ui_password(rendered, generated_ui_password)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(rendered)
        logger.info(
            "Config created: %s (auto-generated DB password, MCP auth_token and "
            "web-UI password)",
            dest,
        )
        logger.info(
            "Generated MCP auth_token for team 1: %s "
            "(Bearer token / OAuth client_secret; OAuth client_id is 'team_1')",
            generated_token,
        )
        logger.info(
            "Generated web-UI password for team 1 (user 'admin'): %s",
            generated_ui_password,
        )

    global _settings
    _settings = _get_settings()
    logger.info("conf=%s  data=%s", _settings.etc_dir, _settings.base_data_dir)

    # --- Resolve team for CLI commands that need it ----------------

    def _cli_team() -> TeamSettings:
        team_id = getattr(args, "team", "1")
        return _settings.get_team(team_id)

    # --- Commands that need Settings --------------------------------

    if args.command is None:
        # No subcommand → start the MCP server. Migrations run before init so
        # each step sees the data dir / Docker resources exactly as the
        # previous version left them.
        #
        # Everything here happens before the HTTP listener binds, so a Docker
        # call that never returns would leave a live process serving nothing
        # and systemd none the wiser. The watchdog turns that into an exit and
        # a restart; see startup_watchdog for why silence is the stall signal.
        from oduflow.docker_ops.client import wait_for_docker
        from oduflow.startup_watchdog import guard_startup

        with guard_startup():
            wait_for_docker()
            migrations.run_pending(_settings)
            _ensure_initialized(_settings)
            quotas.apply_all(_settings)
            if args.stack_manifest:
                from oduflow.stack_loader import load_stack
                from oduflow.stack_ops import apply_stack, format_plan

                stack_team = _settings.get_team(args.stack_team)
                stack_manifest = load_stack(args.stack_manifest)
                applied = apply_stack(
                    _settings,
                    stack_team,
                    stack_manifest,
                    args.stack_manifest,
                    lock_manager=_locks,
                )
                logger.info("Startup stack reconciliation:\n%s", format_plan(applied))
        # Record the active transport for informational purposes.
        # local_path is gated by allow_local_path in Settings.
        settings_module.TRANSPORT = args.transport
        if args.transport == "stdio":
            _start_stdio()
        else:
            _start_http()
        return

    if args.command == "destroy":
        _run_destroy(_settings)
        return

    if args.command == "upgrade":
        if not _run_upgrade(_settings, force=args.force):
            sys.exit(1)
        return

    if args.command == "retune-postgres":
        if args.force and not args.apply:
            parser.error("retune-postgres --force requires --apply")
        if not _run_retune_postgres(
            _settings,
            apply=args.apply,
            force=args.force,
        ):
            sys.exit(1)
        return

    if args.command == "reload-template":
        if args.quiet:
            logging.getLogger("oduflow").setLevel(logging.WARNING)
        if args.source:
            from oduflow.sync import sync_template_from_source

            result = sync_template_from_source(
                _settings,
                _cli_team(),
                args.template_name,
                args.source,
            )
            msg = (
                f"Template DB {result['status']}.\nTemplate DB: {result['template_db']}"
            )
            if "restore_seconds" in result:
                msg += f"\nDB restore time: {result['restore_seconds']}s"
            if not args.quiet:
                print(msg)
        else:
            _run_reload_template(
                _settings,
                _cli_team(),
                template_name=args.template_name,
                dump_path=args.dump_path,
            )
        return

    if args.command == "init-template":
        _run_init_template(
            _settings,
            _cli_team(),
            odoo_image=args.odoo_image,
            modules=args.modules,
            template_name=args.template_name,
            force=args.force,
        )
        return

    if args.command == "template-from-env":
        _run_template_from_env(
            _settings,
            _cli_team(),
            branch=args.branch,
            template_name=args.template_name,
            reset_env_changes=args.reset_env_changes,
        )
        return

    if args.command == "refresh-template":
        _run_refresh_template(
            _settings,
            _cli_team(),
            template_name=args.template_name,
            reset_env_changes=args.reset_env_changes,
        )
        return

    if args.command == "attach-filestore":
        _run_attach_filestore(
            _settings,
            _cli_team(),
            template_name=args.template_name,
            source=args.source,
            reset_env_changes=args.reset_env_changes,
            strip_prefix=args.strip_prefix,
        )
        return

    if args.command == "delete-template":
        _run_delete_template(_settings, _cli_team(), template_name=args.template_name)
        return

    if args.command == "import-template":
        _run_import_template(
            _settings,
            _cli_team(),
            odoo_url=args.odoo_url,
            master_pwd=args.master_pwd,
            db_name=args.db_name,
            template_name=args.template_name,
            without_filestore=args.without_filestore,
        )
        return

    if args.command == "list-templates":
        _run_list_templates(_settings, _cli_team())
        return

    if args.command == "list-services":
        _run_list_services(_settings, _cli_team())
        return

    if args.command == "list-service-databases":
        _run_list_service_databases(_settings, _cli_team())
        return

    if args.command == "cleanup":
        _run_cleanup(_settings, _cli_team(), dry_run=not args.force)
        return

    if args.command == "stack":
        if args.stack_command is None:
            p_stack.print_help()
            return
        from oduflow.stack_loader import load_stack
        from oduflow.stack_ops import apply_stack, build_plan, format_plan, stack_status

        manifest = load_stack(args.manifest)
        team = _cli_team()
        if args.stack_command == "plan":
            print(format_plan(build_plan(_settings, team, manifest, args.manifest)))
            return
        if args.stack_command == "apply":
            migrations.run_pending(_settings)
            _ensure_initialized(_settings)
            quotas.apply_all(_settings)
            stack_result = apply_stack(
                _settings,
                team,
                manifest,
                args.manifest,
                lock_manager=_locks,
            )
            print(format_plan(stack_result))
            return
        if args.stack_command == "status":
            print(
                json.dumps(
                    stack_status(_settings, team, manifest, args.manifest), indent=2
                )
            )
            return


def _warn_local_path_security(settings: Settings) -> None:
    """Warn operators about the host-filesystem trust granted by live-mount."""
    if not settings.allow_local_path:
        return
    logger.warning(
        "SECURITY WARNING: local_path live-mount mode is ENABLED "
        "([server].allow_local_path = true).\n"
        "Clients allowed to create environments can bind-mount any existing "
        "directory from the Oduflow host into an Odoo container with read/write "
        "access.\n"
        "Use live-mount only for trusted, single-user local development. For "
        "hosted, remote, or multi-user deployments, set "
        "[server].allow_local_path = false and restart Oduflow."
    )


def _start_stdio() -> None:
    """Start the MCP server (stdio transport)."""
    import asyncio

    from oduflow.backup_scheduler import start_backup_scheduler

    settings = _get_settings()
    _warn_local_path_security(settings)
    reaper.start_reaper(_get_settings, _locks)
    start_backup_scheduler(_get_settings, _locks)
    try:
        asyncio.run(mcp.run_stdio_async())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


def _traefik_forwarded_allow_ips(settings: Settings) -> list[str]:
    """TCP peers allowed to supply proxy headers to Uvicorn.

    Remote clients can reach the host-bound backend port directly, so trusting
    every peer (``*``) lets them spoof the client address used by login
    throttling. Trust only loopback (needed by Docker Desktop's forwarding) and
    the stable CIDRs of the Docker network that carries Traefik. A network CIDR
    remains valid if Docker recreates Traefik with a different container IP.
    """
    trusted = {"127.0.0.1", "::1"}
    from oduflow.docker_ops.client import get_client

    try:
        network = get_client().networks.get(settings.shared_network)
        network.reload()
        for config in network.attrs.get("IPAM", {}).get("Config", []):
            subnet = str(config.get("Subnet") or "").strip()
            if subnet:
                trusted.add(subnet)
    except Exception as exc:
        raise PrerequisiteNotMetError(
            f"Cannot resolve trusted proxy network '{settings.shared_network}': {exc}"
        )
    if len(trusted) == 2:
        raise PrerequisiteNotMetError(
            f"Docker network '{settings.shared_network}' has no IPAM subnet; "
            "refusing wildcard or loopback-only proxy-header trust."
        )
    return sorted(trusted)


def _start_http() -> None:
    """Start the MCP server (HTTP transport)."""
    from fastmcp.server.http import create_streamable_http_app

    settings = _get_settings()
    # Secure the dashboard on upgrades without bricking MCP: auto-provision a
    # ui_password for existing configs that still have none. The fail-closed
    # check below only trips if this could not write the config.
    settings = _ensure_web_ui_password(settings)
    _warn_local_path_security(settings)
    host = "0.0.0.0" if settings.routing_mode == "traefik" else settings.bind_host
    port = settings.port

    auth = _build_auth(settings)

    # Fail closed: never serve the MCP tool surface (run_odoo_command,
    # run_db_query, privileged service creation, …) unauthenticated by accident
    # (#37). A fresh install bootstraps a random auth_token; reaching here with
    # no auth means the operator cleared it, which must be explicit.
    if auth is None and not settings.allow_insecure_http:
        raise PrerequisiteNotMetError(
            "Refusing to start the HTTP transport with no MCP authentication: "
            "set a [team.*] auth_token in oduflow.toml. To "
            "run unauthenticated on purpose (e.g. behind your own auth proxy), "
            "set [server] allow_insecure_http = true."
        )
    if auth is None:
        logger.warning(
            "HTTP transport starting WITHOUT authentication "
            "(allow_insecure_http=true) — the full MCP tool surface is open."
        )

    # Fail closed for the WEB DASHBOARD too, symmetric to the MCP check above.
    # The dashboard exposes MORE than MCP (interactive shells/SQL/agent to every
    # environment, privileged service creation, credential management) and is
    # only authenticated when a team sets ui_password. Fresh installs bootstrap
    # one and _ensure_web_ui_password just auto-provisioned one for every existing
    # team — so reaching here with ANY team still passwordless means the config
    # write failed and the operator has not opted into an open server. Check
    # ``all`` (not ``any``): a single passwordless team can never log in (auth is
    # global; empty passwords are skipped), so refuse rather than silently lock it
    # out.
    if not settings.allow_insecure_http and (
        not settings.teams or not all(t.ui_password for t in settings.teams.values())
    ):
        raise PrerequisiteNotMetError(
            "Refusing to start the HTTP transport with an unauthenticated web "
            "dashboard: set a [team.*] ui_password in oduflow.toml (the dashboard "
            "exposes interactive shells, SQL and privileged service creation for "
            "every environment). To run it open on purpose (e.g. behind your own "
            "auth proxy), set [server] allow_insecure_http = true."
        )

    # With several teams, every team needs its own token: auth rejects a
    # tokenless team's requests before Host-based routing, so its members
    # would be locked out — and _resolve_team no longer falls back in HTTP
    # mode, so misconfiguration must fail here, at startup.
    if not settings.allow_insecure_http and len(settings.teams) > 1:
        tokenless = sorted(
            tid for tid, team in settings.teams.items() if not team.auth_token
        )
        if tokenless:
            raise PrerequisiteNotMetError(
                "HTTP transport with multiple teams requires an auth_token "
                f"for every team; missing for: {', '.join(tokenless)}."
            )
    if host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "Binding %s on all/non-loopback interface — ensure a firewall and "
            "TLS-terminating reverse proxy are in front of Oduflow.",
            host,
        )

    app = create_streamable_http_app(mcp, "/mcp", auth=auth, stateless_http=True)

    # Scoped single-environment access: gate the tool surface for /mcp/<env>
    # requests and inject the resolved env_name. No-op for the full /mcp.
    from oduflow.scoped_access import (
        ScopedAccessMiddleware,
        ScopedEnvASGI,
        build_env_param_tools,
    )

    mcp.add_middleware(ScopedAccessMiddleware(build_env_param_tools(mcp)))

    reaper.start_reaper(_get_settings, _locks)

    from oduflow.backup_scheduler import start_backup_scheduler

    start_backup_scheduler(_get_settings, _locks)

    from oduflow.web_ui import mount_web_ui

    mount_web_ui(app, _get_settings, _locks)
    global _web_bind
    _web_bind = (host, port)

    for tid, team in settings.teams.items():
        if settings.routing_mode == "traefik":
            url = f"{settings.public_scheme_for(team)}://{team.hostname}/"
        else:
            url = f"{settings.public_scheme_for(team)}://{host}:{port}/"
        mcp_status = "MCP token ON" if team.auth_token else "MCP token OFF"
        oauth_status = (
            "OAuth ON (self-hosted)" if settings.oauth_enabled else "OAuth OFF"
        )
        ui_status = "UI auth ON" if team.ui_password else "UI auth OFF"
        logger.info(
            "[team.%s] %s (%s, %s, %s)", tid, url, mcp_status, oauth_status, ui_status
        )

    import uvicorn

    from oduflow.mcp_log_filters import install_stateless_disconnect_filter

    # A client that disconnects mid-tool (long ops like create_environment can
    # outlast its HTTP timeout) makes the SDK log a benign ClosedResourceError
    # as an ERROR "Stateless session crashed" traceback. Quiet those; the
    # operation itself still completes server-side.
    install_stateless_disconnect_filter()

    # Outermost shim so /mcp/<env> routes to the canonical /mcp route.
    served: Any = ScopedEnvASGI(app)
    # FastMCP builds the 401 challenge from one placeholder base URL. Rewrite it
    # to the validated request hostname so every team discovers OAuth on its own
    # origin.
    if auth is not None:
        from oduflow.oauth_provider import HostRelativeAuthChallenge

        served = HostRelativeAuthChallenge(served, settings)

    # Behind Traefik every request arrives from the proxy's container IP, so
    # uvicorn's access log and the login rate-limiter would see one shared peer
    # instead of the real client. Trust X-Forwarded-For only from Traefik's
    # stable Docker network; wildcard trust lets a direct backend client spoof
    # its IP and bypass throttling. Port mode keeps Uvicorn's default trust.
    forwarded_allow_ips = (
        _traefik_forwarded_allow_ips(settings)
        if settings.routing_mode == "traefik"
        else None
    )

    # Bound the graceful-shutdown window: MCP clients hold long-lived
    # StreamableHTTP streams (SSE GET/keep-alive) that never close on their own,
    # so without a cap uvicorn waits indefinitely on SIGTERM and systemd escalates
    # to SIGKILL after TimeoutStopSec (~90s). Force-close lingering connections
    # after 10s so a stop/restart completes cleanly instead of being killed.
    uvicorn.run(
        served,
        host=host,
        port=port,
        ws="websockets-sansio",
        timeout_graceful_shutdown=10,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )


def _build_auth(settings: Settings):  # type: ignore[no-untyped-def]
    """Build the auth provider from settings.

    Whenever a team token exists, Oduflow serves its self-hosted OAuth
    Authorization Server and keeps the same token valid as a direct Bearer
    credential. The issuer is always derived per request from the registered
    team hostname, whether TLS terminates in Traefik or an upstream such as
    Cloudflare Tunnel in port mode.
    """
    has_team_token = any(t.auth_token for t in settings.teams.values())

    if has_team_token:
        from oduflow.oauth_provider import OduflowOAuthProvider

        return OduflowOAuthProvider(settings)

    logger.warning("HTTP auth DISABLED (no team auth_token set)")
    return None


def main() -> None:
    """CLI entry point: run the server/command and translate a missing
    prerequisite (e.g. Docker daemon unreachable) into a friendly process exit
    instead of a traceback. get_client() raises PrerequisiteNotMetError rather
    than SystemExit so the long-running server can recover; here at the CLI
    boundary we convert it to an exit code."""
    try:
        _run_cli()
    except FlowError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        sys.exit(1)
    except StackValidationError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
