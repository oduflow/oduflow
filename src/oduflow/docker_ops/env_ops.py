from __future__ import annotations

import contextlib
import datetime
import errno
import json
import logging
import os
import pathlib
import re
import shutil
import ssl
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import docker
from docker import DockerClient
from oduflow import activity, env_share, secret_store, settings
from oduflow.docker_ops.client import chown_recursive, get_client, get_odoo_uid_gid
from oduflow.docker_ops.stats import default_env_limits
from oduflow.docker_ops.system_ops import (
    _copy_file_to_container,
    _create_pg_role,
    _db_exists,
    _drop_pg_role,
    _exec_sql,
    _resolve_instance_conf,
    check_db_quota,
    check_disk_space,
    drop_signaling_sequences,
    ensure_team_network,
    ensure_team_tablespace,
    estimate_new_db_bytes,
    pg_clone_strategy_clause,
    reassign_db_ownership,
)
from oduflow.domains import env_hostname
from oduflow.env_credentials import create_credentials, load_credentials
from oduflow.env_tokens import MCP_TOKEN_LABEL, generate_token, invalidate_cache
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    FlowError,
    NotFoundError,
    PrerequisiteNotMetError,
    ProtectedError,
)
from oduflow.git_ops import RepoAuthError, git_env_for_team
from oduflow.hostname_registry import (
    CAPACITY_RESERVATION,
    allocate_hostname,
    clear_hostname_assignment,
    get_hostname,
    release_hostname,
    reserve_environment_slot,
)
from oduflow.hostname_registry import rename_env as rename_hostname_env
from oduflow.locking import env_wake_key, keyed_mutex
from oduflow.naming import (
    get_agent_checkout_dir,
    get_agent_container_name,
    get_agent_home_volume_name,
    get_agent_upload_dir,
    get_agent_workspace_volume_name,
    get_db_name,
    get_env_short_hostname,
    get_filestore_paths,
    get_repo_path,
    get_resource_name,
    get_team_network_name,
    get_template_db_name,
    get_workspace_path,
    redact_url_credentials,
    sanitize_repo_url,
    slugify_branch,
    split_team_hostname,
    validate_env_hostname,
)
from oduflow.port_registry import allocate_port, release_port
from oduflow.port_registry import rename_env as rename_port_env
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

AGENT_USER = "agent"
AGENT_HOME = "/home/agent"
ENV_HOSTNAME_LABEL = "oduflow.hostname"
ENV_HOSTNAME_SOURCE_LABEL = "oduflow.hostname_source"
HOSTNAME_SOURCE_CUSTOM = "custom"
HOSTNAME_SOURCE_SLOT = "slot"


def _hostname_registry_path(team: TeamSettings) -> str:
    if team.hostname_registry_path:
        return team.hostname_registry_path
    if team.data_dir:
        return os.path.join(team.data_dir, "hostnames.json")
    raise ValueError("Team data_dir is required for environment hostname allocation.")


def _trace(msg: str, *args: object) -> None:
    if settings.TRACE:
        logger.info("[TRACE] " + msg, *args)


_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _normalize_extra_addons(raw_addons: object) -> dict[str, str]:
    """Convert old list format or new dict format to {name: branch} dict."""
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        logger.warning(
            "Legacy list format for extra_addons (no branch info), skipping: %s",
            raw_addons,
        )
        return {}
    return {}


def _extra_checkout_sources(
    container: Any, repo_names: Iterator[str]
) -> dict[str, str]:
    """Map extra-repo names to their current host bind sources."""
    targets = {f"/mnt/extra-addons-{name}": name for name in repo_names}
    sources: dict[str, str] = {}
    for bind in container.attrs.get("HostConfig", {}).get("Binds") or []:
        parts = bind.rsplit(":", 2)
        if len(parts) < 2:
            continue
        host_path, container_path = parts[0], parts[1]
        repo_name = targets.get(container_path)
        if repo_name:
            sources[repo_name] = host_path
    return sources


def _replace_extra_checkout_mounts(
    volumes: dict[str, dict[str, str]],
    labels: dict[str, str],
    checkouts: dict[str, str],
    revisions: dict[str, str],
) -> None:
    """Replace extra-addons bind sources while keeping container paths stable."""
    targets = {f"/mnt/extra-addons-{name}" for name in checkouts}
    for source, spec in list(volumes.items()):
        if spec.get("bind") in targets:
            del volumes[source]
    for repo_name, source in checkouts.items():
        volumes[source] = {
            "bind": f"/mnt/extra-addons-{repo_name}",
            "mode": "ro",
        }
    labels["oduflow.extra_addons_revisions"] = json.dumps(revisions)


def _cleanup_legacy_extra_worktrees(
    team: TeamSettings, env_name: str, repo_names: Iterator[str]
) -> None:
    """Remove old per-environment worktrees after their mounts were migrated."""
    extra_dir = os.path.join(get_workspace_path(env_name, team.workspaces_dir), "extra")
    if not os.path.isdir(extra_dir):
        return
    from oduflow.extra_addons import remove_worktree

    for repo_name in repo_names:
        path = os.path.join(extra_dir, repo_name)
        if os.path.isdir(path):
            remove_worktree(team, repo_name, path)
    with contextlib.suppress(OSError):
        os.rmdir(extra_dir)


def _redact_repo_urls(text: str, *urls: str) -> str:
    """Remove embedded credentials from known repository URLs in output."""
    redacted = text
    for url in urls:
        if url:
            redacted = redacted.replace(url, sanitize_repo_url(url))
    return redacted


def _move_inline_repo_credentials(
    repo_url: str, cred_file: str, git_user: str
) -> tuple[str, str]:
    """Store inline credentials and make their username authoritative."""
    from oduflow.git_ops import extract_and_store_inline_credentials

    clean_url, extracted_user = extract_and_store_inline_credentials(
        repo_url, cred_file
    )
    if clean_url != repo_url:
        # An explicit credential-bearing URL overrides template metadata. This
        # also clears a stale username for token-as-username URLs, whose secret
        # must not be copied into the Docker label.
        git_user = extracted_user
    return clean_url, git_user


def _get_used_ports(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    exclude_env: str = "",
) -> set[int]:
    """Collect host ports currently bound by managed containers (excluding a specific env)."""
    used: set[int] = set()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    for c in client.containers.list(all=True, filters=filters):
        if not c.name.startswith(settings.prefix):
            continue
        env = c.labels.get(settings.branch_label, "")
        if env == exclude_env:
            continue
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        for mappings in ports.values():
            if mappings:
                for m in mappings:
                    try:
                        used.add(int(m["HostPort"]))
                    except (KeyError, ValueError, TypeError):
                        pass
    return used


def _active_environment_names(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    *,
    exclude_env: str = "",
) -> set[str]:
    active_envs: set[str] = set()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    for container in client.containers.list(all=True, filters=filters):
        env_name = container.labels.get(settings.branch_label, "")
        if env_name and env_name != exclude_env:
            active_envs.add(env_name)
    return active_envs


def _team_parent_domain(team: TeamSettings) -> str:
    """The domain environment/service hostnames are allocated under: the
    team's base_domain when set, else the parent of the team hostname
    (slots/custom-hostname legacy layout)."""
    if team.base_domain:
        return team.base_domain
    _prefix, parent_domain = split_team_hostname(team.hostname)
    return parent_domain


def _assert_env_hostname_available(
    settings: Settings, team: TeamSettings, env_name: str, route_hostname: str
) -> None:
    """Refuse an environment hostname already claimed elsewhere in the global
    Host() namespace (dashboard hostnames, production domains, static routes,
    other teams' zones, live environment and service containers)."""
    if settings.routing_mode != "traefik":
        return
    from oduflow.domains import assert_public_hostname_free

    fqdn = env_hostname(team, env_name, route_hostname)
    assert_public_hostname_free(
        settings,
        fqdn,
        own_team=team.team_id,
        # update_environment re-runs this while the environment's own
        # container still carries its Host() rule; it must not collide
        # with itself.
        exclude_env=env_name,
        purpose=f"the hostname of environment '{env_name}'",
    )


def container_route_host(container: Any) -> str:
    """The FQDN a container's Traefik router rule actually matches, or ''.

    Traefik labels are frozen at container creation, so this — not a recompute
    from current settings — is what an existing environment really answers on.
    It matters after ``base_domain`` is turned on for a team that already has
    environments: ADR 0064 deliberately performs no migration, so those keep
    their old nested hostname until their next ``update_environment``.
    """
    from oduflow.domains import _HOST_RULE_RE

    for key, value in (container.labels or {}).items():
        if key.startswith("traefik.http.routers.") and key.endswith(".rule"):
            found = _HOST_RULE_RE.findall(value)
            if found:
                return str(found[0]).lower()
    return ""


def _environment_hostname_usage(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    *,
    exclude_env: str = "",
) -> tuple[set[str], set[str]]:
    """Return active environment names and their short routing hostnames.

    Both sets come from one container listing: allocation runs on every create
    and update, so a second pass over the same containers would only double the
    Docker round trips.
    """
    active_envs: set[str] = set()
    used_hostnames: set[str] = set()
    suffix = f".{_team_parent_domain(team)}"
    for configured_team in settings.teams.values():
        if configured_team.hostname.endswith(suffix):
            used_hostnames.add(configured_team.hostname[: -len(suffix)])
    for route in settings.extra_routes:
        if route.host.endswith(suffix):
            used_hostnames.add(route.host[: -len(suffix)])
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    for container in client.containers.list(all=True, filters=filters):
        env_name = container.labels.get(settings.branch_label, "")
        if env_name == exclude_env:
            continue
        for key, value in container.labels.items():
            if not key.startswith("traefik.http.routers.") or not key.endswith(".rule"):
                continue
            for fqdn in re.findall(r"Host\(`([^`]+)`\)", value):
                if fqdn.endswith(suffix):
                    used_hostnames.add(fqdn[: -len(suffix)])
        if env_name:
            active_envs.add(env_name)
            used_hostnames.add(
                get_env_short_hostname(
                    env_name, container.labels.get(ENV_HOSTNAME_LABEL, "")
                )
            )
    return active_envs, used_hostnames


def _legacy_hostname_is_automatic(
    team: TeamSettings, env_name: str, route_hostname: str
) -> bool:
    """Recognize hostname slots created before source labels were introduced."""
    if not route_hostname:
        return False
    try:
        registered = get_hostname(_hostname_registry_path(team), env_name)
        hostname_prefix, _parent_domain = split_team_hostname(team.hostname)
    except (OSError, ValueError):
        return False
    return registered == route_hostname and bool(
        re.fullmatch(rf"{re.escape(hostname_prefix)}[1-9][0-9]*", route_hostname)
    )


def _reconcile_environment_hostname_for_update(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    labels: dict[str, str],
) -> bool:
    """Apply hostname policy before a recreate; return whether assignment clears."""
    route_hostname = labels.get(ENV_HOSTNAME_LABEL, "")
    source = labels.get(ENV_HOSTNAME_SOURCE_LABEL, "")

    if team.environment_hostname_mode == "slots":
        if route_hostname:
            if not source:
                labels[ENV_HOSTNAME_SOURCE_LABEL] = (
                    HOSTNAME_SOURCE_SLOT
                    if _legacy_hostname_is_automatic(team, env_name, route_hostname)
                    else HOSTNAME_SOURCE_CUSTOM
                )
            return False

        hostname_prefix, _parent_domain = split_team_hostname(team.hostname)
        active_envs, used_hostnames = _environment_hostname_usage(
            client, settings, team, exclude_env=env_name
        )
        labels[ENV_HOSTNAME_LABEL] = allocate_hostname(
            _hostname_registry_path(team),
            env_name,
            team.environment_slots,
            hostname_prefix=hostname_prefix,
            active_envs=active_envs,
            used_hostnames=used_hostnames,
        )
        labels[ENV_HOSTNAME_SOURCE_LABEL] = HOSTNAME_SOURCE_SLOT
        return False

    if route_hostname and (
        source == HOSTNAME_SOURCE_SLOT
        or (
            not source and _legacy_hostname_is_automatic(team, env_name, route_hostname)
        )
    ):
        labels.pop(ENV_HOSTNAME_LABEL, None)
        labels.pop(ENV_HOSTNAME_SOURCE_LABEL, None)
        return True
    if route_hostname and not source:
        labels[ENV_HOSTNAME_SOURCE_LABEL] = HOSTNAME_SOURCE_CUSTOM
    elif not route_hostname:
        labels.pop(ENV_HOSTNAME_SOURCE_LABEL, None)
        try:
            registered = get_hostname(_hostname_registry_path(team), env_name)
        except ValueError:
            return False
        return bool(registered and registered != CAPACITY_RESERVATION)
    return False


def _ensure_system_ready(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str | None = None,
) -> None:
    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            raise PrerequisiteNotMetError(
                f"{settings.shared_db_container} is not running. System not initialized. Restart oduflow."
            )
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. System not initialized. Restart oduflow."
        )

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, team.team_id)
        if not _db_exists(client, settings, tpl_db):
            raise PrerequisiteNotMetError(
                f"Template database '{tpl_db}' not found. Run init_template first."
            )

    if settings.routing_mode == "traefik":
        try:
            t = client.containers.get(settings.traefik_container)
            if t.status != "running":
                raise PrerequisiteNotMetError(
                    f"{settings.traefik_container} is not running. System not initialized. Restart oduflow."
                )
        except docker.errors.NotFound:
            raise PrerequisiteNotMetError(
                f"{settings.traefik_container} not found. System not initialized. Restart oduflow."
            )


def _dir_size_mb(path: str) -> float:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total / (1024 * 1024)


def _read_template_metadata(team: TeamSettings, template_name: str) -> dict[str, Any]:
    """Template metadata.json, or an empty dict when it is absent/unreadable."""
    metadata_path = team.get_template_metadata_path(template_name)
    if not os.path.isfile(metadata_path):
        return {}
    try:
        with open(metadata_path) as f:
            loaded: dict[str, Any] = json.load(f)
            return loaded
    except (json.JSONDecodeError, OSError):
        return {}


def _template_code_lineage(
    team: TeamSettings,
    template_name: str | None,
    repo_path: str,
    *,
    current_branch: str = "",
    fetch_missing_history: bool = False,
) -> dict[str, Any]:
    """Drift between this checkout and the commit the template DB came from.

    Advisory only: templates predating provenance (and imported ones, which have
    no commit of ours behind them) yield status ``unknown`` and say nothing.
    """
    empty: dict[str, Any] = {
        "status": "unknown",
        "message": "",
        "modules_to_install": [],
        "modules_to_upgrade": [],
    }
    if template_name is None:
        return empty
    from oduflow import git_analysis, git_ops

    metadata = _read_template_metadata(team, template_name)
    try:
        template_commit = str(metadata.get("source_commit", ""))
        source_branch = str(metadata.get("source_branch", ""))
        if template_commit and fetch_missing_history:
            git_ops.ensure_lineage_history(
                repo_path,
                template_commit,
                current_branch,
                source_branch,
                team.git_credentials_file(),
            )
        return git_analysis.template_lineage(
            repo_path,
            template_commit,
            source_branch,
        )
    except Exception:  # noqa: BLE001 - never fail creation over an advisory check
        logger.warning("Template lineage check failed", exc_info=True)
        return empty


# A dead ``fuse-overlayfs`` daemon leaves its mount table entry behind, and
# every access to the mountpoint then fails with ENOTCONN ("Transport endpoint
# is not connected"). Restarting Oduflow *in Docker* produces this on every
# overlay environment at once: the daemon lives in the container's PID
# namespace and dies with it, while the mount lives in the host's mount
# namespace and survives — it has to live there, because Odoo containers
# bind-mount the merged path and Docker resolves that on the host. Running
# Oduflow directly on the host, the daemon is detached from the Oduflow process
# and a restart leaves it alone; there a mount only goes stale to an OOM kill
# or a full disk.
#
# ``os.path.ismount()`` cannot see this state: it swallows the ENOTCONN from
# its own ``lstat`` and returns False, exactly as for a path that is not
# mounted at all (``os.path.isdir()`` returns False for the same reason). So a
# stale overlay is indistinguishable from an absent one unless we probe the
# mountpoint and look at errno — which is what this module does everywhere it
# needs to know whether an overlay is usable.
_STALE_MOUNT_ERRNOS = frozenset({errno.ENOTCONN, errno.ECONNABORTED})

MOUNT_ALIVE = "alive"
MOUNT_STALE = "stale"
MOUNT_ABSENT = "absent"


def _is_directory_strict(path: str) -> bool:
    """Like ``isdir``, but propagate permission and filesystem I/O errors."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode)


def overlay_mount_state(merged: str) -> str:
    """Liveness of a filestore mountpoint.

    ``MOUNT_ALIVE`` — mounted and serving; ``MOUNT_STALE`` — mounted but its
    daemon is gone (needs detaching and remounting); ``MOUNT_ABSENT`` — no
    overlay mount here, which also covers a copy-mode filestore directory and a
    path that does not exist.
    """
    try:
        os.stat(merged)
    except FileNotFoundError:
        return MOUNT_ABSENT
    except OSError as exc:
        if exc.errno in _STALE_MOUNT_ERRNOS:
            return MOUNT_STALE
        # Permission and I/O failures are not evidence that a mount is absent.
        # Let operational callers fail closed and let the health check surface
        # the probe error instead of reporting a false green.
        raise
    return MOUNT_ALIVE if os.path.ismount(merged) else MOUNT_ABSENT


def overlay_mount_issues(settings: Settings) -> list[dict[str, str]]:
    """Stale or unexpectedly absent workspace overlay mounts.

    A plain filesystem walk with no Docker calls, for the health check — there
    the question is only whether anything needs repair, not what to do about it.
    An existing ``upper`` directory makes an absent mount an error: it contains
    environment-specific deltas that a plain template copy would ignore.
    """
    issues: list[dict[str, str]] = []
    for team in settings.teams.values():
        try:
            entries = list(os.scandir(team.workspaces_dir))
        except FileNotFoundError:
            continue  # team never initialized
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except FileNotFoundError:
                continue  # workspace disappeared during the health scan
            paths = get_filestore_paths(entry.name, team.workspaces_dir)
            state = overlay_mount_state(paths["merged"])
            expected = _is_directory_strict(paths["upper"])
            if state == MOUNT_STALE or (state == MOUNT_ABSENT and expected):
                issues.append(
                    {
                        "team_id": team.team_id,
                        "env_name": entry.name,
                        "state": state,
                        "path": paths["merged"],
                    }
                )
    return sorted(issues, key=lambda item: (item["team_id"], item["env_name"]))


def stale_overlay_paths(settings: Settings) -> list[str]:
    """Compatibility helper returning only dead-daemon mount paths."""
    return [
        issue["path"]
        for issue in overlay_mount_issues(settings)
        if issue["state"] == MOUNT_STALE
    ]


def _mount_filestore(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    env_db: str,
    odoo_image: str,
    odoo_volumes: dict[str, Any],
    *,
    template_name: str,
    force_overlay: bool = False,
) -> None:
    template_filestore = team.get_template_filestore_path(template_name)
    if not template_filestore or not os.path.isdir(template_filestore):
        if force_overlay:
            raise PrerequisiteNotMetError(
                f"Template filestore is missing: {template_filestore}"
            )
        logger.debug(
            "Dump filestore not found at %s, skipping overlay mount", template_filestore
        )
        return

    paths = get_filestore_paths(env_name, team.workspaces_dir)
    if force_overlay and not _is_directory_strict(paths["upper"]):
        raise PrerequisiteNotMetError(
            f"Overlay upper layer is missing: {paths['upper']}"
        )

    # Detach a stale mount before anything else, whichever mode we end up in:
    # mounting over it would stack a live overlay on a dead one, and the copy
    # path (like the makedirs/chmod below) would just fail with ENOTCONN.
    if overlay_mount_state(paths["merged"]) == MOUNT_STALE:
        logger.warning(
            "Stale filestore overlay at %s (fuse-overlayfs is gone); detaching",
            paths["merged"],
            extra={"env_name": env_name},
        )
        _unmount_filestore(env_name, team)
        _wait_unmounted(paths["merged"])
        if overlay_mount_state(paths["merged"]) != MOUNT_ABSENT:
            raise PrerequisiteNotMetError(
                f"Could not detach stale filestore overlay at {paths['merged']}"
            )

    # Read use_overlay flag from template metadata (avoids slow filestore scan)
    use_overlay = None
    metadata_path = team.get_template_metadata_path(template_name)
    if os.path.isfile(metadata_path):
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            use_overlay = metadata.get("use_overlay")
        except (json.JSONDecodeError, OSError):
            pass

    if force_overlay:
        # A stale/live mount plus an existing upper layer proves that this
        # environment is overlay-backed. Recovery must preserve those deltas
        # even if the template's mutable default has since changed to copy.
        use_overlay = True
    elif use_overlay is None:
        # Fallback for old templates without the flag
        size_mb = _dir_size_mb(template_filestore)
        use_overlay = size_mb >= settings.overlay_threshold_mb

    # fuse-overlayfs is a Linux/FUSE binary; it cannot run on macOS. Off Linux,
    # always fall back to a plain filestore copy instead of failing.
    if use_overlay and not sys.platform.startswith("linux"):
        if force_overlay:
            raise PrerequisiteNotMetError(
                "Cannot preserve an overlay filestore on a non-Linux platform"
            )
        logger.info(
            "Non-Linux platform (%s): using filestore copy instead of overlay",
            sys.platform,
        )
        use_overlay = False

    if not use_overlay:
        logger.info("Template use_overlay=False, using copy")
        merged = paths["merged"]
        if os.path.exists(merged):
            shutil.rmtree(merged)
        shutil.copytree(template_filestore, merged)
        odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
        uid_str, gid_str = odoo_uid_gid.split(":")
        chown_recursive(merged, int(uid_str), int(gid_str), client, odoo_image)
        odoo_volumes[merged] = {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
            "mode": "rw",
        }
        return

    logger.info("Template use_overlay=True, using overlay")
    for d in (paths["upper"], paths["work"], paths["merged"]):
        os.makedirs(d, mode=0o777, exist_ok=True)
        os.chmod(d, 0o777)

    odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
    uid_str, gid_str = odoo_uid_gid.split(":")
    for d in (paths["upper"], paths["work"], paths["merged"]):
        chown_recursive(d, int(uid_str), int(gid_str), client, odoo_image)

    if not shutil.which("fuse-overlayfs"):
        raise PrerequisiteNotMetError(
            "fuse-overlayfs is not installed. "
            "Install it with: sudo apt install fuse-overlayfs"
        )

    result = subprocess.run(
        [
            "fuse-overlayfs",
            "-o",
            f"lowerdir={template_filestore},upperdir={paths['upper']},workdir={paths['work']},allow_other",
            paths["merged"],
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        error_msg = (
            result.stderr.decode("utf-8", errors="replace").strip()
            if result.stderr
            else ""
        )
        hint = ""
        if "allow_other" in error_msg or "permission" in error_msg.lower():
            hint = (
                " Hint: Oduflow normally runs as root, where 'allow_other' needs "
                "no extra config. If you run it as a non-root user, uncomment "
                "'user_allow_other' in /etc/fuse.conf."
            )
        raise PrerequisiteNotMetError(
            f"Failed to mount filestore overlay: {error_msg}.{hint}"
        )

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if os.path.ismount(paths["merged"]):
            try:
                os.listdir(paths["merged"])
                break
            except OSError:
                pass
        time.sleep(0.05)

    if not os.path.ismount(paths["merged"]):
        raise PrerequisiteNotMetError(
            f"Filestore overlay mount at {paths['merged']} did not become ready"
        )

    odoo_volumes[paths["merged"]] = {
        "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
        "mode": "rw",
    }
    logger.info("Filestore overlay mounted", extra={"env_name": env_name})


def _unmount_filestore(env_name: str, team: TeamSettings) -> None:
    paths = get_filestore_paths(env_name, team.workspaces_dir)
    merged = paths["merged"]
    # Deliberately not ``isdir``/``ismount``: both report False for a stale
    # mount, because the stat they rely on fails with ENOTCONN — and that is
    # exactly the mount that most needs detaching.
    if overlay_mount_state(merged) == MOUNT_ABSENT:
        return

    # Prefer a direct, clean ``umount``: Oduflow runs as root, and umount(2) is
    # not mediated by the AppArmor ``fusermount3`` profile shipped on Ubuntu
    # 24.04+ (which otherwise DENIES the setuid ``fusermount`` helper, forcing
    # the lazy fallback). ``fusermount3``/``fusermount`` stay in the chain for
    # unprivileged/rootless deployments where umount(2) needs the helper. Lazy
    # ``umount -l`` is the last resort only — it detaches even a busy mount,
    # which can leave a still-bound container with a broken filestore.
    for cmd in (
        ["umount", merged],
        ["fusermount3", "-u", merged],
        ["fusermount", "-u", merged],
        ["umount", "-l", merged],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info(
                "Filestore overlay unmounted via '%s'",
                " ".join(cmd[:-1]),
                extra={"env_name": env_name},
            )
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    logger.warning("Could not unmount filestore overlay at %s", merged)


def _wait_unmounted(merged: str, timeout: float = 3.0) -> None:
    """Poll until ``merged`` is no longer a mount point (best-effort)."""
    deadline = time.time() + timeout
    while time.time() < deadline and overlay_mount_state(merged) != MOUNT_ABSENT:
        time.sleep(0.1)


class RemountResult:
    """Outcome of :func:`remount_template_overlays`."""

    def __init__(self, affected: list[str]) -> None:
        self.affected = affected
        self.failures: list[tuple[str, str]] = []


@contextlib.contextmanager
def remount_template_overlays(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    *,
    reset_upper: bool = False,
    exclude_envs: tuple[str, ...] = (),
) -> Iterator[RemountResult]:
    """Safely mutate a template's lower filestore layer under live overlay envs.

    Non-destructive template update (see issue #2): fuse-overlayfs keeps each
    environment's changes in a separate ``upper`` layer, so we can unmount an
    overlay while preserving its ``upper``/``work`` dirs, swap the read-only
    lower layer (the template filestore), and remount against the new lower with
    the same ``upper`` — keeping the environment's data.

    Usage::

        with remount_template_overlays(client, settings, team, name) as r:
            # ... mutate team.get_template_filestore_path(name) ...
        # r.affected / r.failures now describe what happened

    On enter: for every overlay-mounted environment that uses ``template_name``
    (excluding ``exclude_envs``), stop its Odoo container and unmount the
    overlay, keeping ``upper``/``work``. The caller mutates the template
    filestore inside the ``with`` block. On exit (always — even if the block
    raised, so envs are never left without a lower layer): remount each
    environment against the new lower reusing its preserved ``upper`` (unless
    ``reset_upper=True``) and restart the container if it had been running.

    Copy-mode environments (``use_overlay=False``) are not mounted, so they are
    skipped — updating the lower layer does not affect their independent copy.
    """
    exclude = set(exclude_envs)
    affected: list[dict[str, Any]] = []

    for env in list_environments(settings, team):
        env_name = env["env_name"]
        if env_name in exclude:
            continue
        if env.get("template_name") != template_name:
            continue
        merged = get_filestore_paths(env_name, team.workspaces_dir)["merged"]
        # Stale mounts are included: they need the very same stop / unmount /
        # remount treatment, and skipping them left them broken and unnoticed.
        if overlay_mount_state(merged) == MOUNT_ABSENT:
            continue

        container_name = get_resource_name(
            env_name, "odoo", settings.prefix, team.team_id
        )
        image = env.get("odoo_image") or "odoo:19.0"
        was_running = False
        try:
            container = client.containers.get(container_name)
            was_running = container.status == "running"
            if container.image.tags:
                image = container.image.tags[0]
        except docker.errors.NotFound:
            pass

        affected.append(
            {
                "env_name": env_name,
                "container_name": container_name,
                "image": image,
                "env_db": get_db_name(env_name, team.team_id),
                "was_running": was_running,
            }
        )

    result = RemountResult([a["env_name"] for a in affected])

    # Stop containers and unmount overlays (keeping upper/work).
    for a in affected:
        try:
            try:
                client.containers.get(a["container_name"]).stop(timeout=10)
            except docker.errors.NotFound:
                pass
            _unmount_filestore(a["env_name"], team)
            _wait_unmounted(
                get_filestore_paths(a["env_name"], team.workspaces_dir)["merged"]
            )
            logger.info("Unmounted overlay for env %s", a["env_name"])
        except Exception as exc:  # noqa: BLE001 - best-effort, reported via result
            logger.warning("Could not unmount overlay for %s: %s", a["env_name"], exc)
            result.failures.append((a["env_name"], f"unmount: {exc}"))

    try:
        yield result
    finally:
        for a in affected:
            env_name = a["env_name"]
            paths = get_filestore_paths(env_name, team.workspaces_dir)
            try:
                if overlay_mount_state(paths["merged"]) == MOUNT_ALIVE:
                    # Unmount failed earlier — don't stack a second mount. A
                    # stale mount is not a reason to skip: _mount_filestore
                    # detaches it first.
                    result.failures.append((env_name, "still mounted, skipped remount"))
                else:
                    if reset_upper:
                        for key in ("upper", "work"):
                            d = paths[key]
                            if os.path.isdir(d):
                                shutil.rmtree(d)
                                os.makedirs(d, mode=0o777, exist_ok=True)
                    _mount_filestore(
                        client,
                        settings,
                        team,
                        env_name,
                        a["env_db"],
                        a["image"],
                        {},
                        template_name=template_name,
                        force_overlay=True,
                    )
                    logger.info("Remounted overlay for env %s", env_name)
                if a["was_running"]:
                    try:
                        client.containers.get(a["container_name"]).start()
                    except docker.errors.NotFound:
                        pass
                    except docker.errors.APIError:
                        pass  # already running
            except Exception as exc:  # noqa: BLE001 - best-effort, reported via result
                logger.warning("Could not remount overlay for %s: %s", env_name, exc)
                result.failures.append((env_name, f"remount: {exc}"))


def reconcile_overlay_mounts(settings: Settings) -> list[dict[str, Any]]:
    """Repair stale or unexpectedly absent filestore overlays.

    Run on every server start. When Oduflow itself runs in Docker this is the
    normal case rather than an exception (see :func:`overlay_mount_state`), and
    until this existed every overlay environment came back from a restart with
    a filestore that raised ENOTCONN on first access — with nothing in Oduflow
    noticing, reporting or repairing it.

    Each affected environment is stopped, detached when needed, remounted
    against its template's lower layer keeping its own ``upper`` deltas, and
    started again if it had been running. The container restart is required
    rather than incidental: a running container's bind mount still points at
    the dead mount, so remounting on the host alone would not reach it.

    Best-effort per environment — a failure is logged and reported, never
    raised, so one broken environment cannot keep the server from starting.
    Returns one record per environment that needed attention.
    """
    results: list[dict[str, Any]] = []
    client = get_client()

    for team in settings.teams.values():
        try:
            envs = list_environments(settings, team)
        except Exception as exc:  # noqa: BLE001 - startup must survive this
            logger.warning(
                "[team.%s] Could not list environments to reconcile overlays: %s",
                team.team_id,
                exc,
            )
            continue

        for env in envs:
            env_name = env["env_name"]
            paths = get_filestore_paths(env_name, team.workspaces_dir)
            record: dict[str, Any] = {
                "team_id": team.team_id,
                "env_name": env_name,
                "repaired": False,
                "detail": "",
            }
            try:
                mount_state = overlay_mount_state(paths["merged"])
                has_overlay_upper = _is_directory_strict(paths["upper"])
            except OSError as exc:
                record["detail"] = f"mount probe failed: {exc}"
                logger.error(
                    "Could not inspect filestore overlay for %s: %s", env_name, exc
                )
                results.append(record)
                continue
            if mount_state != MOUNT_STALE and not (
                mount_state == MOUNT_ABSENT and has_overlay_upper
            ):
                continue
            if not has_overlay_upper:
                record["detail"] = "overlay upper layer is missing"
                logger.error(
                    "Broken filestore overlay for %s has no upper layer at %s; "
                    "leaving the mount alone for manual recovery",
                    env_name,
                    paths["upper"],
                )
                results.append(record)
                continue

            template_name = env.get("template_name") or ""
            if not template_name or template_name == "none":
                # Without a template there is no lower layer to remount against,
                # and only an operator can decide what to restore this from.
                record["detail"] = "no template to remount against"
                logger.error(
                    "Broken filestore overlay for %s has no template: %s needs "
                    "manual recovery",
                    env_name,
                    paths["merged"],
                )
                results.append(record)
                continue

            # _mount_filestore returns quietly when the lower layer is gone, so
            # check it here — before detaching anything. Otherwise this pass
            # would unmount the environment and report a repair it never made.
            template_filestore = team.get_template_filestore_path(template_name)
            if not template_filestore or not os.path.isdir(template_filestore):
                record["detail"] = f"template filestore missing: {template_filestore}"
                logger.error(
                    "Broken filestore overlay for %s: template '%s' has no "
                    "filestore at %s; leaving the mount alone for manual recovery",
                    env_name,
                    template_name,
                    template_filestore,
                )
                results.append(record)
                continue

            logger.warning(
                "[team.%s] Broken filestore overlay for %s (%s); repairing",
                team.team_id,
                env_name,
                mount_state,
            )
            container_name = get_resource_name(
                env_name, "odoo", settings.prefix, team.team_id
            )
            image = env.get("odoo_image") or "odoo:19.0"
            was_running = False
            container = None
            try:
                container = client.containers.get(container_name)
                was_running = container.status == "running"
                if container.image.tags:
                    image = container.image.tags[0]
            except docker.errors.NotFound:
                pass
            except Exception as exc:  # noqa: BLE001 - fail closed per environment
                record["detail"] = f"container lookup failed: {exc}"
                logger.error("Could not inspect %s: %s", container_name, exc)
                results.append(record)
                continue

            if container is not None:
                status = container.status
                if status == "running":
                    try:
                        container.stop(timeout=10)
                        container.reload()
                    except Exception as exc:  # noqa: BLE001 - fail closed
                        record["detail"] = f"stop failed: {exc}"
                        logger.error("Could not stop %s: %s", container_name, exc)
                        results.append(record)
                        continue
                    if container.status == "running":
                        record["detail"] = "stop failed: container is still running"
                        logger.error("Container %s is still running", container_name)
                        results.append(record)
                        continue
                elif status not in {"created", "exited", "dead"}:
                    record["detail"] = f"unsafe container state: {status}"
                    logger.error(
                        "Will not detach overlay while %s is %s",
                        container_name,
                        status,
                    )
                    results.append(record)
                    continue

            try:
                _unmount_filestore(env_name, team)
                _wait_unmounted(paths["merged"])
                if overlay_mount_state(paths["merged"]) != MOUNT_ABSENT:
                    raise PrerequisiteNotMetError("mount did not detach")
                _mount_filestore(
                    client,
                    settings,
                    team,
                    env_name,
                    get_db_name(env_name, team.team_id),
                    image,
                    {},
                    template_name=template_name,
                    force_overlay=True,
                )
                state = overlay_mount_state(paths["merged"])
                if state != MOUNT_ALIVE:
                    raise PrerequisiteNotMetError(
                        f"remount did not become live (state: {state})"
                    )
            except Exception as exc:  # noqa: BLE001 - never block startup
                record["detail"] = str(exc)
                logger.error(
                    "Could not repair filestore overlay for %s: %s", env_name, exc
                )
                # Do not restart against an absent or unverified filestore.
                results.append(record)
                continue

            if was_running and container is not None:
                try:
                    container.start()
                    container.reload()
                    if container.status != "running":
                        raise RuntimeError(
                            f"container status after start is {container.status}"
                        )
                except Exception as exc:  # noqa: BLE001 - best-effort
                    record["detail"] = f"restart failed: {exc}"
                    logger.error("Could not restart %s: %s", container_name, exc)
                    results.append(record)
                    continue

            record["repaired"] = True
            logger.warning(
                "Remounted filestore overlay for %s (upper deltas kept)",
                env_name,
            )
            results.append(record)

    return results


def _install_apt_packages(container: Any, repo_path: str) -> str:
    """Install apt packages. Returns a human-readable log of what happened."""
    apt_file = os.path.join(repo_path, ".oduflow", "apt_packages.txt")
    if not os.path.isfile(apt_file):
        logger.debug("No .oduflow/apt_packages.txt in repo, skipping apt install")
        return ""

    with open(apt_file) as f:
        packages = [
            line.strip() for line in f if line.strip() and not line.startswith("#")
        ]
    if not packages:
        return ""

    logger.info("Updating apt and installing packages: %s", " ".join(packages))
    exit_code, output = container.exec_run("apt-get update", user="root")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.warning("apt-get update failed (exit %d): %s", exit_code, output_str)
        return f"[APT] apt-get update FAILED (exit {exit_code}): {output_str}"

    cmd = "apt-get install -y " + " ".join(packages)
    exit_code, output = container.exec_run(cmd, user="root")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.warning("apt install failed (exit %d): %s", exit_code, output_str)
        return f"[APT] install FAILED (exit {exit_code}): {output_str}"
    else:
        logger.info("apt packages installed")
        return f"[APT] Installed: {' '.join(packages)}"


def _ensure_user_site_packages(container: Any) -> None:
    """Create the user site-packages directory for the odoo user and fix ownership.

    This allows ``pip install --user`` to work inside containers where
    ``/var/lib/odoo/.local`` may not exist or may be owned by root.

    Only the pip target dirs are chowned, never recursively into
    ``.local/share``: the Odoo filestore is bind-mounted at
    ``.local/share/Odoo/filestore/<db>`` from an overlay's read-only lower
    layer, so ``chown -R /var/lib/odoo/.local`` would change ownership on every
    filestore file and force fuse-overlayfs to copy the entire template
    filestore up into this environment's upper layer — defeating the overlay
    (each env would duplicate the full filestore instead of sharing the
    template's lower layer).
    """
    container.exec_run(
        "mkdir -p /var/lib/odoo/.local/lib /var/lib/odoo/.local/bin",
        user="root",
    )
    # Non-recursive on .local so .local/share (the bind-mounted filestore) is
    # never touched; recursive only on the pip dirs, which never hold it.
    container.exec_run(
        "chown odoo:odoo /var/lib/odoo/.local",
        user="root",
    )
    container.exec_run(
        "chown -R odoo:odoo /var/lib/odoo/.local/lib /var/lib/odoo/.local/bin",
        user="root",
    )
    logger.debug("Ensured pip user dirs under /var/lib/odoo/.local are owned by odoo")


def _install_pip_requirements(
    container: Any,
    repo_path: str,
    *,
    restart: bool = True,
    container_base: str = "/mnt/extra-addons",
) -> tuple[bool, str]:
    """Install pip requirements from repo.

    Returns (installed, log) — *installed* is True when packages were
    installed, *log* is a human-readable summary of what happened.

    When *restart* is False the caller is responsible for restarting the
    container after all setup steps are done.

    ``container_base`` is where *repo_path* is mounted inside the container:
    ``/mnt/extra-addons`` for the main repo, ``/mnt/extra-addons-{name}`` for
    an extra-addons checkout.
    """
    # Prefer .oduflow/requirements.txt; fall back to the repo root for
    # compatibility with conventions used elsewhere (e.g. odoo.sh).
    oduflow_req = os.path.join(repo_path, ".oduflow", "requirements.txt")
    root_req = os.path.join(repo_path, "requirements.txt")
    if os.path.isfile(oduflow_req):
        container_req = f"{container_base}/.oduflow/requirements.txt"
    elif os.path.isfile(root_req):
        container_req = f"{container_base}/requirements.txt"
    else:
        logger.debug("No requirements.txt in repo, skipping pip install")
        return False, ""

    # Ensure the odoo user can write to its local site-packages
    _ensure_user_site_packages(container)

    cmd = f"pip3 install --user --break-system-packages -r {container_req}"
    logger.info("Installing pip requirements from %s", container_req)
    exit_code, output = container.exec_run(cmd, user="odoo")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0 and "no such option" in output_str.lower():
        logger.info("--break-system-packages not supported, retrying without it")
        cmd = f"pip3 install --user -r {container_req}"
        exit_code, output = container.exec_run(cmd, user="odoo")
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
    if exit_code != 0:
        logger.warning("pip install failed (exit %d): %s", exit_code, output_str)
        return False, f"[PIP] install FAILED (exit {exit_code}):\n{output_str}"
    else:
        logger.info("pip requirements installed")
        if restart:
            container.restart()
            logger.info("Container restarted after pip install")
        return True, f"[PIP] Requirements installed successfully:\n{output_str}"


def _install_repo_dependencies(
    container: Any, dep_paths: list[tuple[str, str]]
) -> tuple[int, bool, list[str]]:
    """Install apt + pip dependencies for each ``(host_path, container_base)``.

    The main repo comes first, then extra-addons checkouts, apt before pip
    within each repo (same order as before extra-repo deps existed). Never
    restarts — callers restart once after all setup steps.

    Returns ``(exit_code, pip_installed_any, logs)`` where *exit_code* is 1 if
    any apt or pip step failed.
    """
    exit_code = 0
    pip_installed_any = False
    logs: list[str] = []
    for host_path, container_base in dep_paths:
        apt_log = _install_apt_packages(container, host_path)
        if apt_log:
            logs.append(apt_log)
            if "FAILED" in apt_log:
                exit_code = 1
        installed, pip_log = _install_pip_requirements(
            container, host_path, restart=False, container_base=container_base
        )
        pip_installed_any = pip_installed_any or installed
        if pip_log:
            logs.append(pip_log)
            if not installed:  # log present but not installed => FAILED
                exit_code = 1
    return exit_code, pip_installed_any, logs


def _odoo_registry_probe(env_db: str) -> list[str]:
    """Build an in-container probe that forces the target registry to load."""
    script = "\n".join(
        [
            "import http.cookiejar",
            "import sys",
            "import urllib.parse",
            "import urllib.request",
            f"db = {env_db!r}",
            "jar = http.cookiejar.CookieJar()",
            "opener = urllib.request.build_opener(",
            "    urllib.request.HTTPCookieProcessor(jar)",
            ")",
            "url = 'http://127.0.0.1:8069/web/login?' + urllib.parse.urlencode({'db': db})",
            "try:",
            "    response = opener.open(url, timeout=5)",
            "    if response.status != 200:",
            "        raise RuntimeError(f'HTTP {response.status}')",
            "except Exception as exc:",
            "    print(f'{type(exc).__name__}: {exc}', file=sys.stderr)",
            "    sys.exit(1)",
        ]
    )
    return ["python3", "-c", script]


@dataclass(frozen=True)
class _OdooReadinessResult:
    ready: bool
    elapsed_seconds: float
    attempts: int
    timeout_seconds: int
    last_error: str = ""


def _wait_for_container_odoo_ready(
    container: Any, env_db: str, timeout: int = 120
) -> _OdooReadinessResult:
    """Wait until the serving Odoo process has finished registry startup.

    The probe runs inside the container so readiness does not depend on public
    DNS, TLS, Traefik, or a published host port. It requests ``/web/login`` with
    a cookie-aware opener: Odoo's first response selects ``env_db`` and the
    redirected request must pass through that database's registry. A secondary
    Odoo command must not start before this succeeds: on cloned Odoo 15
    databases both processes can otherwise race while re-creating the registry
    signaling sequences. ``/web/health`` is not sufficient here because it is
    database-independent and can respond before registry preloading completes.
    """
    probe = _odoo_registry_probe(env_db)
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    last_error = "readiness probe has not run"
    next_progress_log = 30.0
    while True:
        attempts += 1
        try:
            exit_code, output = container.exec_run(probe)
            output_str = (
                output.decode("utf-8", errors="replace")
                if isinstance(output, bytes)
                else str(output)
            ).strip()
            if exit_code == 0:
                return _OdooReadinessResult(
                    ready=True,
                    elapsed_seconds=round(time.monotonic() - started, 1),
                    attempts=attempts,
                    timeout_seconds=timeout,
                )
            last_error = output_str or f"probe exited with code {exit_code}"
        except docker.errors.APIError as exc:
            # The container may briefly reject execs while restarting after a
            # dependency install. Treat that exactly like a failed probe.
            last_error = f"Docker exec failed: {exc}"
        now = time.monotonic()
        elapsed = now - started
        if now >= deadline:
            return _OdooReadinessResult(
                ready=False,
                elapsed_seconds=round(elapsed, 1),
                attempts=attempts,
                timeout_seconds=timeout,
                last_error=last_error[-2000:],
            )
        if elapsed >= next_progress_log:
            logger.info(
                "Still waiting for serving Odoo registry for '%s' after %.0f seconds: %s",
                env_db,
                elapsed,
                last_error[-500:],
            )
            next_progress_log += 30.0
        time.sleep(2)


def _recent_container_logs(container: Any, tail: int = 200) -> str:
    """Return a bounded log tail without masking the readiness failure."""
    try:
        output = container.logs(tail=tail, stdout=True, stderr=True)
    except Exception as exc:
        return f"Could not read container logs: {exc}"
    output_str = (
        output.decode("utf-8", errors="replace")
        if isinstance(output, bytes)
        else str(output)
    ).strip()
    return output_str[-20_000:] or "(container produced no logs)"


def _readiness_error(
    container: Any,
    env_db: str,
    result: _OdooReadinessResult,
    *,
    action: str,
) -> PrerequisiteNotMetError:
    logs = _recent_container_logs(container)
    return PrerequisiteNotMetError(
        f"{action}: serving Odoo did not load registry for database '{env_db}' "
        f"within {result.timeout_seconds} seconds ({result.attempts} probes). "
        f"Last readiness error: {result.last_error}\n\n"
        f"Recent Odoo container logs:\n{logs}"
    )


def _auto_install_modules(
    container: Any,
    settings: Settings,
    team: TeamSettings,
    env_db: str,
    modules: list[str],
    env_name: str,
) -> str:
    """Install modules after the serving registry is ready and return its log."""
    from oduflow.docker_ops.odoo_ops import (
        _require_installed_modules,
        _validate_module_names,
    )

    _validate_module_names(modules)
    modules_str = ",".join(modules)
    logger.info(
        "Waiting for serving Odoo before auto-installing modules",
        extra={"env_name": env_name},
    )
    readiness = _wait_for_container_odoo_ready(container, env_db)
    if not readiness.ready:
        error = _readiness_error(
            container,
            env_db,
            readiness,
            action=f"Auto-install of modules {modules_str} was not attempted",
        )
        logger.error(str(error), extra={"env_name": env_name})
        raise error

    install_cmd = (
        f"/entrypoint.sh odoo -d {env_db} -i {modules_str} --stop-after-init --no-http"
    )
    exit_code, output = container.exec_run(install_cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.error(
            "Auto-install modules failed (exit %d): %s",
            exit_code,
            output_str,
            extra={"env_name": env_name},
        )
        raise ExternalCommandError(install_cmd, exit_code, output_str[-20_000:])
    _require_installed_modules(settings, team, env_name, tuple(modules))
    return f"[AUTO-INSTALL] odoo -i {modules_str} completed successfully"


def _configure_serving_environment(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    container: Any,
    repo_path: str,
    env_db: str,
    env_name: str,
    template_name: str | None,
    sanitize: bool,
    auto_install_modules: list[str] | None,
    odoo_conf_to_copy: str | None,
    extra_mount_paths: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Run required post-start setup before the environment is declared ready."""
    setup_logs: list[str] = []
    if odoo_conf_to_copy:
        _copy_file_to_container(container, odoo_conf_to_copy, "/etc/odoo")

    # No restart here: the unconditional one below is the single gated restart
    # that picks up both the conf and the freshly installed packages.
    dep_paths = [(repo_path, "/mnt/extra-addons")] + list(extra_mount_paths or [])
    _, _, dep_logs = _install_repo_dependencies(container, dep_paths)
    setup_logs.extend(dep_logs)

    # Odoo is PID1 and has already booted — with the image's stock conf and
    # without the repo's pip packages. One restart here picks up both, in the
    # same order as production's _run_odoo_container. Unconditional on purpose:
    # restarting only when pip installed something left a repo that ships an
    # .oduflow/odoo.conf but no requirements.txt serving on the stock
    # addons_path and worker settings until some later restart.
    container.restart()
    logger.info(
        "Waiting for serving Odoo after the setup restart",
        extra={"env_name": env_name},
    )
    readiness = _wait_for_container_odoo_ready(container, env_db)
    if not readiness.ready:
        raise _readiness_error(
            container,
            env_db,
            readiness,
            action="Environment setup did not complete",
        )

    # Install first so neutralization includes SQL shipped by the new modules.
    if auto_install_modules:
        modules_str = ",".join(auto_install_modules)
        logger.info(
            "Auto-installing modules: %s",
            modules_str,
            extra={"env_name": env_name},
        )
        setup_logs.append(
            _auto_install_modules(
                container,
                settings,
                team,
                env_db,
                auto_install_modules,
                env_name,
            )
        )

    if sanitize and template_name is not None:
        from oduflow.sanitizer import neutralize_environment, sanitize_environment

        setup_logs.extend(neutralize_environment(client, settings, team, env_name))
        setup_logs.extend(sanitize_environment(client, settings, team, env_name))

    if auto_install_modules:
        # Reload only after sanitization has disabled the newly-installed
        # modules' crons and external credentials.
        container.restart()
        logger.info(
            "Waiting for serving Odoo after auto-install setup",
            extra={"env_name": env_name},
        )
        readiness = _wait_for_container_odoo_ready(container, env_db)
        if not readiness.ready:
            raise _readiness_error(
                container,
                env_db,
                readiness,
                action="Environment setup did not complete",
            )

    return setup_logs


def _cleanup_old_environment(
    client: "DockerClient",
    settings: Settings,
    team: TeamSettings,
    env_name: str,
) -> None:
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        old = client.containers.get(odoo_container_name)
        old.stop()
        old.remove(v=True)
        logger.info("Removed old container %s", odoo_container_name)
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError:
        try:
            old.remove(v=True, force=True)
        except Exception:
            pass

    env_db = get_db_name(env_name, team.team_id)

    try:
        creds = load_credentials(
            env_name, team.workspaces_dir, settings.db_user, settings.db_password
        )
        _drop_pg_role(client, settings, creds["pg_user"])
    except Exception:
        pass

    if _db_exists(client, settings, env_db):
        try:
            _exec_sql(
                client, settings, f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);'
            )
            logger.info("Dropped old database %s", env_db)
        except Exception:
            pass

    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    if os.path.exists(workspace_path):
        _unmount_filestore(env_name, team)
        shutil.rmtree(workspace_path)


def _rollback_partial_environment(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
) -> None:
    """Best-effort rollback that preserves the original provisioning error."""
    if settings.routing_mode == "port":
        try:
            release_port(team.port_registry_path, env_name)
        except Exception:
            logger.exception("Failed to release port while rolling back '%s'", env_name)
    try:
        _cleanup_old_environment(client, settings, team, env_name)
    except Exception:
        logger.exception("Failed to fully roll back environment '%s'", env_name)


def _init_empty_database(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    odoo_image: str,
    env_db: str,
    odoo_env: dict[str, Any],
    odoo_volumes: dict[str, Any],
    env_name: str,
) -> str:
    """Initialize a fresh empty database with ``-i base`` in an isolated,
    short-lived container, then remove it. Returns a setup-log line.

    This runs *before* the long-running serving container exists, so the init
    is the only Odoo process touching the database. It avoids the registry
    signaling race that occurs when the serving PID1 (``odoo -d <db> --dev=xml``)
    and an in-container ``-i base`` exec set up the registry concurrently and
    collide on ``CREATE TABLE orm_signaling_registry`` (``UniqueViolation`` in
    the pg catalog). ``-i base`` is explicit, so it works regardless of whether
    the image build auto-initializes an empty DB on plain ``-d``.
    """
    init_name = get_resource_name(env_name, "odoo-init", settings.prefix, team.team_id)
    try:
        client.containers.get(init_name).remove(force=True)
    except docker.errors.NotFound:
        pass

    init_container = client.containers.run(
        image=odoo_image,
        name=init_name,
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        extra_hosts={"host.docker.internal": "host-gateway"},
        **default_env_limits(),
        environment=odoo_env,
        volumes=odoo_volumes,
        command=f"odoo -d {env_db} -i base --stop-after-init --no-http",
    )
    exit_code: int = -1
    logs = ""
    try:
        result = init_container.wait(timeout=600)
        exit_code = (
            result.get("StatusCode", -1) if isinstance(result, dict) else int(result)
        )
        try:
            logs = init_container.logs().decode("utf-8", errors="replace")
        except Exception:
            logs = ""
    finally:
        try:
            init_container.remove(force=True)
        except docker.errors.APIError:
            pass

    if exit_code != 0:
        logger.error(
            "Base init failed (exit %s)", exit_code, extra={"env_name": env_name}
        )
        return f"[INIT] odoo -i base FAILED (exit {exit_code}):\n{logs[-4000:]}"
    logger.info("Base init completed", extra={"env_name": env_name})
    return "[INIT] odoo -i base completed successfully"


def _clone_repo(
    repo_url: str,
    branch: str,
    repo_path: str,
    team: TeamSettings,
    *,
    git_user: str = "",
    depth: int = 1,
    timeout: int = 60,
) -> None:
    """Clone *branch* of *repo_url* into *repo_path* with team credentials.

    ``depth=1`` (default) is the dev-environment shallow clone; production
    passes ``depth=0`` for a full clone — commit history is the point there
    (rollback targets, deploy history display).
    """
    git_env = git_env_for_team(team.git_credentials_file(), team.ssh_dir())

    from oduflow.git_ops import inject_credential_user

    clone_url = inject_credential_user(repo_url, git_user)

    auth_keywords = (
        "Authentication failed",
        "could not read Username",
        "Permission denied",
        "Repository not found",
        "terminal prompts disabled",
        "Invalid username or password",
    )

    cmd = ["git", "clone", "--branch", branch]
    if depth > 0:
        cmd += ["--depth", str(depth)]
    cmd += [clone_url, repo_path]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=timeout,
            env=git_env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = redact_url_credentials(
            e.stderr.decode("utf-8") if e.stderr else str(e)
        )
        if any(kw.lower() in error_msg.lower() for kw in auth_keywords):
            from oduflow.git_ops import is_ssh_url

            # SSH and HTTPS remotes have different remedies: pointing an SSH
            # failure at setup_repo_auth (the PAT flow) is a dead end.
            if is_ssh_url(repo_url):
                raise RepoAuthError(
                    f"Git authentication failed for {sanitize_repo_url(repo_url)}. "
                    "The remote uses SSH: register the team deploy key "
                    "(get_ssh_public_key) with the git host, or use an "
                    "HTTPS URL with setup_repo_auth."
                )
            raise RepoAuthError(
                f"Git authentication failed for {sanitize_repo_url(repo_url)}. "
                f"Call 'setup_repo_auth' first to cache credentials."
            )
        raise ExternalCommandError("git clone", e.returncode, error_msg)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git clone",
            -1,
            f"Repository clone timed out ({timeout}s). Repository may be too "
            "large or network is slow.",
        )


def build_env_traefik_labels(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    route_hostname: str = "",
) -> dict[str, str]:
    """Traefik docker-provider routing labels for an environment's Odoo container.

    Empty in port mode. This is the single source of an environment's routing
    labels, shared by :func:`create_environment` and :func:`update_environment`
    so a container recreated by an update always reflects the *current*
    ``routing_mode`` / ``routing_tls`` / team ``hostname`` — labels are frozen at
    container creation, so this is what lets a mode/TLS/hostname switch be
    repaired by a plain ``update_environment`` instead of a destroy+create.
    """
    if settings.routing_mode != "traefik":
        return {}
    slug = slugify_branch(env_name)
    router = f"oduflow-{team.team_id}-{slug}"
    host = env_hostname(team, env_name, route_hostname)
    labels: dict[str, str] = {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
        f"traefik.http.services.{router}.loadbalancer.server.port": "8069",
        "traefik.docker.network": get_team_network_name(team.team_id, settings.prefix),
    }
    if settings.routing_tls:
        labels[f"traefik.http.routers.{router}.entrypoints"] = "websecure"
        labels[f"traefik.http.routers.{router}.tls"] = "true"
        if settings.uses_acme:
            labels[f"traefik.http.routers.{router}.tls.certresolver"] = "letsencrypt"
    else:
        # Upstream (e.g. Cloudflare tunnel) terminates TLS; Traefik routes plain
        # HTTP on the web entrypoint. Public URLs use the team's resolved
        # public scheme (the upstream's scheme, https unless overridden), not
        # this entrypoint's.
        labels[f"traefik.http.routers.{router}.entrypoints"] = "web"
    return labels


def adopt_existing_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    branch: str = "",
) -> dict[str, Any] | None:
    """Describe an environment that already exists, starting it when stopped.

    The agent-facing create path calls this first, so asking for an environment
    that is already provisioned is a fast successful answer ("here is its URL")
    instead of an error the agent has to recover from — no `list_environments`
    round-trip needed before every create.

    Returns ``None`` when nothing is provisioned under *env_name*, so the caller
    goes on to create it for real. A **different git branch is never adopted**:
    the database, the URL and the running code are shared state, and moving them
    onto another branch is a decision for the person driving the task
    (``switch_branch``), not a side effect of a create call.
    """
    try:
        client = get_client()
    except PrerequisiteNotMetError:
        # This lookup is an optimisation, not the place to report a dead Docker
        # daemon — the creation path right after it says that properly.
        return None
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        return None
    _assert_team_owns(container, settings, team, env_name)

    labels = container.labels
    current_branch = labels.get("oduflow.git_branch", env_name)
    if branch and branch != current_branch:
        raise ConflictError(
            f"Environment '{env_name}' already exists and tracks branch "
            f"'{current_branch}', not '{branch}'. Move it with switch_branch "
            f"(env_name='{env_name}', branch='{branch}'), which keeps its "
            "database and URL, or delete it first if you want a fresh one."
        )

    started = container.status != "running"
    if started:
        start_environment(settings, env_name, team)
        container.reload()

    try:
        url = get_env_base_url(settings, team, env_name, container)[0]
    except NotFoundError:
        # Port mode with no published port (container stopped mid-start).
        url = ""

    return {
        "env_name": env_name,
        "url": url,
        "git_branch": current_branch,
        "odoo_image": labels.get(settings.image_label, ""),
        "template_name": labels.get("oduflow.template", "none"),
        "hostname": get_env_short_hostname(
            env_name, labels.get(ENV_HOSTNAME_LABEL, "")
        ),
        "odoo_container": odoo_container_name,
        "database": get_db_name(env_name, team.team_id),
        "workspace": get_workspace_path(env_name, team.workspaces_dir),
        "local_path": labels.get("oduflow.local_path", ""),
        "status": container.status,
        "started": started,
    }


def create_environment(
    settings: Settings,
    team: TeamSettings,
    branch: str,
    repo_url: str,
    odoo_image: str,
    env_name: str = "",
    template_name: str | None = None,
    extra_addons: dict[str, str] | None = None,
    git_user: str = "",
    sanitize: bool = True,
    auto_install_modules: list[str] | None = None,
    env_vars: dict[str, str] | None = None,
    local_path: str = "",
    stack_labels: dict[str, str] | None = None,
    hostname: str = "",
    hostname_source: str = "",
) -> dict[str, Any]:
    """Allocate routing state, then provision the environment itself."""
    resolved_env_name = env_name or branch
    requested_hostname = validate_env_hostname(hostname) if hostname else ""
    if requested_hostname and settings.routing_mode != "traefik":
        raise ValueError("hostname is supported only when routing.mode = 'traefik'.")

    assigned_hostname = ""
    assigned_hostname_source = ""
    reservation_active = False
    needs_docker_state = team.environment_slots > 0 or bool(requested_hostname)
    if needs_docker_state:
        try:
            client = get_client()
        except Exception as exc:
            raise PrerequisiteNotMetError(
                f"Failed to connect to Docker daemon: {exc}. Ensure Docker is running."
            ) from exc
        container_name = get_resource_name(
            resolved_env_name, "odoo", settings.prefix, team.team_id
        )
        try:
            client.containers.get(container_name)
        except docker.errors.NotFound:
            # Read the team's Docker state once; both the capacity reservation
            # and the hostname allocation below are driven from this snapshot.
            wants_hostname = bool(requested_hostname) or (
                team.environment_hostname_mode == "slots"
            )
            hostname_prefix = ""
            used_hostnames: set[str] = set()
            if wants_hostname:
                hostname_prefix, _parent_domain = split_team_hostname(team.hostname)
                active_envs, used_hostnames = _environment_hostname_usage(
                    client, settings, team, exclude_env=resolved_env_name
                )
            else:
                active_envs = _active_environment_names(
                    client, settings, team, exclude_env=resolved_env_name
                )
            if team.environment_slots > 0:
                reserve_environment_slot(
                    _hostname_registry_path(team),
                    resolved_env_name,
                    team.environment_slots,
                    active_envs=active_envs,
                )
                reservation_active = True
            if wants_hostname:
                try:
                    assigned_hostname = allocate_hostname(
                        _hostname_registry_path(team),
                        resolved_env_name,
                        team.environment_slots,
                        requested_hostname=requested_hostname,
                        hostname_prefix=hostname_prefix,
                        active_envs=active_envs,
                        used_hostnames=used_hostnames,
                    )
                    reservation_active = True
                    assigned_hostname_source = (
                        hostname_source
                        if requested_hostname
                        and hostname_source
                        in (HOSTNAME_SOURCE_CUSTOM, HOSTNAME_SOURCE_SLOT)
                        else (
                            HOSTNAME_SOURCE_CUSTOM
                            if requested_hostname
                            else HOSTNAME_SOURCE_SLOT
                        )
                    )
                except Exception:
                    if reservation_active:
                        release_hostname(
                            _hostname_registry_path(team), resolved_env_name
                        )
                    raise

    try:
        return _create_environment_impl(
            settings,
            team,
            branch,
            repo_url,
            odoo_image,
            env_name=env_name,
            template_name=template_name,
            extra_addons=extra_addons,
            git_user=git_user,
            sanitize=sanitize,
            auto_install_modules=auto_install_modules,
            env_vars=env_vars,
            local_path=local_path,
            stack_labels=stack_labels,
            hostname=assigned_hostname,
            hostname_source=assigned_hostname_source,
        )
    except Exception:
        if reservation_active:
            try:
                get_client().containers.get(
                    get_resource_name(
                        resolved_env_name, "odoo", settings.prefix, team.team_id
                    )
                )
            except docker.errors.NotFound:
                release_hostname(_hostname_registry_path(team), resolved_env_name)
            except Exception:
                # Keep the reservation when Docker state cannot be checked; a
                # later retry of the same environment will reuse it safely.
                pass
        raise


def _create_environment_impl(
    settings: Settings,
    team: TeamSettings,
    branch: str,
    repo_url: str,
    odoo_image: str,
    env_name: str = "",
    template_name: str | None = None,
    extra_addons: dict[str, str] | None = None,
    git_user: str = "",
    sanitize: bool = True,
    auto_install_modules: list[str] | None = None,
    env_vars: dict[str, str] | None = None,
    local_path: str = "",
    stack_labels: dict[str, str] | None = None,
    hostname: str = "",
    hostname_source: str = "",
) -> dict[str, Any]:
    env_name = env_name or branch
    from oduflow.naming import PROD_ENV_PREFIX

    if env_name.startswith(PROD_ENV_PREFIX):
        raise ConflictError(
            f"Environment names starting with '{PROD_ENV_PREFIX}' are reserved "
            "for production environments. Use create_production instead."
        )
    if auto_install_modules:
        # Fail before anything is provisioned: a malformed module name would
        # otherwise only surface at the auto-install step, after the clone,
        # template restore, container start and dependency installs — all of
        # which the setup rollback then discards.
        from oduflow.docker_ops.odoo_ops import _validate_module_names

        _validate_module_names(auto_install_modules)
    start_time = time.time()
    try:
        client = get_client()
    except Exception as e:
        raise PrerequisiteNotMetError(
            f"Failed to connect to Docker daemon: {e}. Ensure Docker is running."
        )

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, team.team_id)
        if not _db_exists(client, settings, tpl_db):
            logger.warning(
                "Template DB '%s' not found, falling back to init from scratch",
                tpl_db,
            )
            template_name = None

    _ensure_system_ready(client, settings, team, template_name)

    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        existing = client.containers.get(odoo_container_name)
        if existing.status == "running":
            existing.reload()
            try:
                url = get_env_base_url(settings, team, env_name, existing)[0]
            except NotFoundError:
                url = ""
            raise ConflictError(
                f"Environment '{env_name}' already exists and is running"
                + (f" at {url}." if url else ".")
            )
        raise ConflictError(
            f"Environment '{env_name}' already exists (status: {existing.status})."
        )
    except docker.errors.NotFound:
        pass

    # Different branch names can normalise to the same database name (the
    # container name keeps case/punctuation but get_db_name slugifies). Refuse
    # if the target DB is already owned by another live environment — otherwise
    # the cleanup below would DROP a running environment's database (#41).
    target_db = get_db_name(env_name, team.team_id)
    for other in client.containers.list(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
            ]
        },
    ):
        if other.name == odoo_container_name:
            continue
        other_branch = other.labels.get(settings.branch_label)
        if other_branch and get_db_name(other_branch, team.team_id) == target_db:
            raise ConflictError(
                f"Environment '{env_name}' maps to database '{target_db}', which "
                f"is already used by environment '{other_branch}'. Choose a name "
                "that does not normalise to the same database."
            )

    # Claim the public name before anything is provisioned or destroyed: the
    # cleanup below drops a leftover database, role and workspace, and the
    # credential move writes to the team store, so a name rejected further
    # down would still have had side effects.
    _assert_env_hostname_available(settings, team, env_name, hostname)

    est_db_bytes = estimate_new_db_bytes(client, settings, team, template_name)
    check_db_quota(client, settings, team, estimated_new_db_bytes=est_db_bytes)
    check_disk_space(
        client,
        settings,
        team,
        template_name,
        estimated_db_bytes=est_db_bytes,
        local_mount=bool(local_path),
        env_name=env_name,
    )
    ensure_team_network(client, settings, team)

    _cleanup_old_environment(client, settings, team, env_name)
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    # Live-mount mode: bind-mount the agent's own checkout
    # directly instead of cloning. The repo lives OUTSIDE the managed
    # workspace, so it is never touched by cleanup/delete.
    local_mount = bool(local_path)
    repo_path = (
        os.path.abspath(local_path)
        if local_mount
        else get_repo_path(env_name, team.workspaces_dir)
    )
    if local_mount and not settings.allow_local_path:
        raise PrerequisiteNotMetError(
            "local_path (live-mount) is disabled. Set allow_local_path = true "
            "in oduflow.toml [server] to enable it."
        )
    env_db = get_db_name(env_name, team.team_id)

    # Keep inline git credentials (https://user:PAT@host/...) out of the Docker
    # label (world-readable via `docker inspect`, copied into template metadata):
    # move them into the team credential store, then persist only a sanitized URL.
    # Clone/recreate/agent paths already authenticate via that store. No-op for
    # clean URLs and for live-mount (which never clones).
    if not local_mount and repo_url:
        repo_url, git_user = _move_inline_repo_credentials(
            repo_url, team.git_credentials_file(), git_user
        )

    # Fail fast on dangling secret references, before any database, checkout
    # or container work. The oduflow.env_vars label below keeps the references;
    # only the container environment receives the resolved values.
    resolved_env_vars = secret_store.resolve_env_secrets(team, env_vars)

    labels = {
        settings.managed_label: "true",
        settings.team_label: team.team_id,
        settings.branch_label: env_name,
        settings.repo_label: repo_url,
        settings.image_label: odoo_image,
        "oduflow.template": template_name if template_name is not None else "none",
        "oduflow.git_branch": branch,
        "oduflow.created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Personal token authorizing the scoped MCP endpoint /mcp/<env>.
        MCP_TOKEN_LABEL: generate_token(),
    }

    if extra_addons:
        labels["oduflow.extra_addons"] = json.dumps(extra_addons)
    if git_user:
        labels["oduflow.git_user"] = git_user
    if local_mount:
        labels["oduflow.local_path"] = repo_path
    if auto_install_modules:
        labels["oduflow.auto_install_modules"] = ",".join(auto_install_modules)
    if env_vars:
        labels["oduflow.env_vars"] = json.dumps(env_vars)
    if stack_labels:
        labels.update(stack_labels)
    if hostname:
        labels[ENV_HOSTNAME_LABEL] = hostname
        labels[ENV_HOSTNAME_SOURCE_LABEL] = hostname_source or HOSTNAME_SOURCE_CUSTOM

    labels.update(build_env_traefik_labels(settings, team, env_name, hostname))

    logger.info(
        "Creating environment",
        extra={
            "env_name": env_name,
            "branch": branch,
            "repo": sanitize_repo_url(repo_url),
            "image": odoo_image,
            "prefix": settings.prefix,
            "routing_mode": settings.routing_mode,
            "hostname": hostname or get_env_short_hostname(env_name),
            "workspaces_dir": team.workspaces_dir,
        },
    )

    os.makedirs(workspace_path, exist_ok=True)

    if local_mount:
        # No clone: the agent's checkout is bind-mounted live. The directory
        # must already exist on the host (validated in the tool layer too).
        if not os.path.isdir(repo_path):
            raise PrerequisiteNotMetError(
                f"local_path does not exist or is not a directory: {repo_path}"
            )
        logger.info("Live-mount mode: using local checkout at %s", repo_path)
        # Baseline for local change detection: first pull_and_apply diffs
        # against this snapshot. Git is deliberately ignored in live-mount mode.
        _write_local_snapshot(repo_path, env_name, team)
    else:
        _clone_repo(
            repo_url,
            branch,
            repo_path,
            team,
            git_user=git_user,
        )

    # The template database is a snapshot of some commit; this checkout may sit
    # before or after it. Report the drift now, while the agent is still deciding
    # what its first pull_and_apply should do.
    lineage = _template_code_lineage(
        team,
        template_name,
        repo_path,
        current_branch=branch,
        fetch_missing_history=not local_mount,
    )

    # --- Shared immutable extra-addons checkouts ---
    extra_mount_paths: list[tuple[str, str]] = []
    extra_conf_paths: list[str] = []
    extra_revisions: dict[str, str] = {}
    if extra_addons:
        from oduflow.extra_addons import (
            ensure_shared_checkout,
            resolve_extra_addons_path,
        )

        for repo_name, addon_branch in extra_addons.items():
            checkout = ensure_shared_checkout(team, repo_name, addon_branch)
            container_path = f"/mnt/extra-addons-{repo_name}"
            extra_mount_paths.append((checkout["path"], container_path))
            extra_conf_paths.append(
                resolve_extra_addons_path(str(checkout["path"]), repo_name)
            )
            extra_revisions[repo_name] = checkout["revision"]
        labels["oduflow.extra_addons_revisions"] = json.dumps(extra_revisions)

    ts_name = ensure_team_tablespace(client, settings, team)
    if template_name is not None:
        tpl_db = get_template_db_name(template_name, team.team_id)
        # PG 15+ defaults to STRATEGY WAL_LOG, which writes the entire clone
        # through WAL — a large template transiently doubles its disk cost and
        # can fill PGDATA mid-clone. FILE_COPY keeps transient WAL small.
        strategy = pg_clone_strategy_clause(client, settings)
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{env_db}" TEMPLATE "{tpl_db}"{strategy} '
            f'TABLESPACE "{ts_name}";',
        )
    else:
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{env_db}" TABLESPACE "{ts_name}";',
        )

    env_creds = create_credentials(env_name, team.team_id, team.workspaces_dir)
    _create_pg_role(
        client, settings, env_creds["pg_user"], env_creds["pg_password"], env_db
    )

    if template_name is not None:
        # A template DB's objects are owned by whatever role created them —
        # normally the superuser (pg_restore --no-owner), but the plain-SQL /
        # import_template_from_odoo path (psql without --no-owner) can leave
        # objects owned by the source env's per-env role (e.g. u_2_fs19). DDL
        # during module upgrades requires ownership; Odoo connects as the env
        # role and never SET ROLEs, so per-object ownership — not role
        # membership — is what actually enables its DDL.
        reassign_db_ownership(client, settings, env_db, env_creds["pg_user"])
        drop_signaling_sequences(client, settings, env_db)
        logger.info(
            "Post-clone fixup done for '%s': ownership transferred, signaling sequences dropped",
            env_db,
        )

    odoo_env = {
        "HOST": settings.shared_db_container,
        "USER": env_creds["pg_user"],
        "PASSWORD": env_creds["pg_password"],
        **(resolved_env_vars or {}),
    }
    odoo_volumes = {repo_path: {"bind": "/mnt/extra-addons", "mode": "rw"}}

    for host_path, container_path in extra_mount_paths:
        odoo_volumes[host_path] = {"bind": container_path, "mode": "ro"}

    repo_odoo_conf = os.path.join(repo_path, ".oduflow", "odoo.conf")
    if os.path.isfile(repo_odoo_conf):
        base_conf_path = repo_odoo_conf
        logger.info("Using odoo.conf from repository")
    elif _resolve_instance_conf("odoo.conf", team.data_dir).exists():
        base_conf_path = str(_resolve_instance_conf("odoo.conf", team.data_dir))
    else:
        base_conf_path = None

    odoo_conf_to_copy: str | None = None
    if base_conf_path:
        from oduflow.extra_addons import generate_odoo_conf, resolve_main_addons_path

        generated_conf = os.path.join(workspace_path, "odoo.conf")
        extra_container_paths = extra_conf_paths
        main_addons_path = resolve_main_addons_path(repo_path)
        generate_odoo_conf(
            base_conf_path, generated_conf, extra_container_paths, main_addons_path
        )
        odoo_conf_to_copy = generated_conf

    if template_name is not None:
        _mount_filestore(
            client,
            settings,
            team,
            env_name,
            env_db,
            odoo_image,
            odoo_volumes,
            template_name=template_name,
        )
    else:
        # No template — create a plain filestore directory on the host so data
        # survives container restarts (no overlay needed).
        filestore_path = os.path.join(workspace_path, "filestore")
        os.makedirs(filestore_path, mode=0o777, exist_ok=True)
        os.chmod(filestore_path, 0o777)
        _uid, _gid = get_odoo_uid_gid(client, odoo_image).split(":")
        chown_recursive(filestore_path, int(_uid), int(_gid), client, odoo_image)
        odoo_volumes[filestore_path] = {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
            "mode": "rw",
        }

    host_port: int | None = None
    if settings.routing_mode == "port":
        used_ports = _get_used_ports(client, settings, team, exclude_env=env_name)
        host_port = allocate_port(
            team.port_registry_path,
            env_name,
            team.port_range_start,
            team.port_range_end,
            used_ports=used_ports,
        )

    sessions_path = os.path.join(workspace_path, "sessions")
    os.makedirs(sessions_path, mode=0o777, exist_ok=True)
    os.chmod(sessions_path, 0o777)
    uid_str, gid_str = get_odoo_uid_gid(client, odoo_image).split(":")
    chown_recursive(sessions_path, int(uid_str), int(gid_str), client, odoo_image)
    odoo_volumes[sessions_path] = {
        "bind": "/var/lib/odoo/.local/share/Odoo/sessions",
        "mode": "rw",
    }

    run_kwargs: dict[str, Any] = dict(
        image=odoo_image,
        name=odoo_container_name,
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        extra_hosts={"host.docker.internal": "host-gateway"},
        **default_env_limits(),
        environment=odoo_env,
        labels=labels,
        volumes=odoo_volumes,
        restart_policy={"Name": "unless-stopped"},
        command=f"odoo -d {env_db} --dev=xml",
    )
    if settings.routing_mode == "port":
        run_kwargs["ports"] = {"8069/tcp": host_port}

    try:
        logger.info("Pulling image %s", odoo_image)
        client.images.pull(odoo_image)
    except Exception as exc:
        logger.warning("Could not pull image %s, using local copy: %s", odoo_image, exc)

    setup_logs: list[str] = []

    # Greenfield (no template): initialize the empty DB with `-i base` in an
    # isolated, short-lived container BEFORE the serving container exists, so
    # the init is the only Odoo process touching the DB. This avoids the
    # registry-signaling race between the serving PID1 and an in-container
    # `-i base` exec (UniqueViolation on orm_signaling_registry).
    if template_name is None:
        logger.info(
            "No template — initialising Odoo with -i base (isolated container)",
            extra={"env_name": env_name},
        )
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

    try:
        container = client.containers.run(**run_kwargs)
    except Exception:
        # The serving container failed to start (e.g. an invalid container name
        # derived from the branch, or a host-port bind conflict). Roll back the
        # resources created so far — database, role, filestore mount, workspace
        # and the allocated port — so they are not left orphaned (#49).
        logger.error(
            "containers.run failed for '%s'; rolling back partial environment",
            env_name,
        )
        _rollback_partial_environment(client, settings, team, env_name)
        raise

    try:
        setup_logs.extend(
            _configure_serving_environment(
                client,
                settings,
                team,
                container,
                repo_path,
                env_db,
                env_name,
                template_name,
                sanitize,
                auto_install_modules,
                odoo_conf_to_copy,
                extra_mount_paths,
            )
        )
    except Exception:
        logger.error(
            "Environment setup failed for '%s'; rolling back partial environment",
            env_name,
        )
        _rollback_partial_environment(client, settings, team, env_name)
        raise

    if settings.routing_mode == "traefik":
        url = f"{settings.public_scheme_for(team)}://{env_hostname(team, env_name, hostname)}"
    else:
        url = f"{settings.public_scheme_for(team)}://{team.hostname}:{host_port}"
    logger.info(
        "Environment created",
        extra={"env_name": env_name, "url": url, "container": odoo_container_name},
    )

    # Add this environment's checkout to the team's agent container
    # (best-effort; ensures the container exists; only when enabled). Live-mount
    # environments have no repo to clone — clone-env.sh skips on an empty URL.
    _agent_add_env(
        client,
        settings,
        team,
        env_name,
        "" if local_mount else repo_url,
        branch,
        git_user,
    )

    result: dict[str, Any] = {
        "url": url,
        "hostname": hostname or get_env_short_hostname(env_name),
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace_path,
        "setup_logs": setup_logs,
    }
    result["extra_addons"] = extra_addons or {}
    result["extra_addons_revisions"] = extra_revisions
    result["local_path"] = repo_path if local_mount else ""
    result["template_lineage"] = lineage
    result["elapsed_seconds"] = round(time.time() - start_time, 1)
    activity.touch(team, env_name)
    # Let the new env's MCP token resolve without waiting for the scan interval.
    invalidate_cache()
    return result


_AGENT_PROVIDER_CREDENTIALS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENCODE_API_KEY",
)


def _agent_env_vars(settings: Settings, team: TeamSettings) -> dict[str, str]:
    """Variables injected into the team's agent container.

    Two sources, in order of increasing precedence:
      1. Known provider keys present in the SERVER environment (convenient for
         headless/prod: CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY /
         OPENAI_API_KEY / OPENCODE_API_KEY) — but only in single-team
         deployments; with several teams a server-level key would leak the
         operator's credential into every tenant's container.
      2. The team's ``[team.X.agent_env]`` TOML table (arbitrary KEY=VALUE,
         including those same credentials).

    For Claude the OAuth token and the API key are mutually exclusive — the key
    would silently override the subscription — so when a non-empty token is
    present the API key is dropped.
    """
    env: dict[str, str] = {}
    if len(settings.teams) == 1:
        for key in _AGENT_PROVIDER_CREDENTIALS:
            value = os.environ.get(key, "").strip()
            if value:
                env[key] = value
    env.update(team.agent_env)

    # Credentials are routinely pasted from another terminal. Normalize only
    # the known secret values (arbitrary user variables may intentionally carry
    # surrounding whitespace), and make an explicitly blank team override mask
    # a same-named server credential instead of injecting an empty value.
    for key in _AGENT_PROVIDER_CREDENTIALS:
        if key not in env:
            continue
        value = env[key].strip()
        if value:
            env[key] = value
        else:
            env.pop(key)

    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def _claude_auth_mode(settings: Settings, team: TeamSettings) -> str:
    """Return the one effective Claude credential mode, without secrets.

    Environment credentials are alternatives, not an automatic fallback
    chain: a setup token wins over an API key, and a persisted interactive
    login is used only when neither environment credential is present.
    """
    env = _agent_env_vars(settings, team)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "setup_token"
    if env.get("ANTHROPIC_API_KEY"):
        return "api_key"
    return "interactive"


def get_agent_mcp_url(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """This environment's SCOPED MCP endpoint as seen from the agent container.

    The scoped ``/mcp/<env>`` endpoint + per-environment token (ADR 0028) is
    the only Oduflow access the agent gets — the team ``auth_token`` never
    enters the agent container. In Traefik mode agents use the team's public
    TLS endpoint, matching the dashboard's MCP Access URL. In port mode they use
    the Docker host gateway and do not hairpin through an external tunnel."""
    from urllib.parse import quote

    if settings.routing_mode == "traefik":
        base = f"{settings.public_scheme_for(team)}://{team.hostname}"
    else:
        base = f"http://host.docker.internal:{settings.port}"
    return f"{base}/mcp/{quote(env_name, safe='/')}"


def refresh_agent_mcp_config(
    container: docker.models.containers.Container,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
) -> None:
    """Refresh an existing Claude checkout's generated Oduflow MCP entry.

    Agent workspaces persist across Oduflow/coder upgrades, so a checkout may
    still contain the old host-gateway URL even after URL selection is fixed.
    Rewrite only the generated ``oduflow`` entry, preserving Agent Browser and
    any user-added servers. The token remains an environment placeholder and
    is never written to disk.
    """
    config_path = os.path.join(get_agent_checkout_dir(env_name), ".mcp.json")
    script = """\
set -eu
config_path=$1
mcp_url=$2
[ -f "$config_path" ] || exit 0
tmp_path="${config_path}.tmp.$$"
trap 'rm -f "$tmp_path"' EXIT
jq --arg url "$mcp_url" \
  '.mcpServers.oduflow = {type: "http", url: $url, headers: {Authorization: "Bearer ${ODUFLOW_MCP_TOKEN}"}}' \
  "$config_path" > "$tmp_path"
mv "$tmp_path" "$config_path"
"""
    exit_code, output = container.exec_run(
        [
            "sh",
            "-c",
            script,
            "sh",
            config_path,
            get_agent_mcp_url(settings, team, env_name),
        ],
        user=AGENT_USER,
    )
    if exit_code not in (0, None):
        detail = (
            output.decode("utf-8", "replace")
            if isinstance(output, (bytes, bytearray))
            else output
        )
        logger.warning(
            "Agent MCP config refresh for '%s' exited %s: %s",
            env_name,
            exit_code,
            detail,
        )


def _agent_container_config(
    settings: Settings, team: TeamSettings, env: dict[str, str]
) -> dict[str, Any]:
    """Docker run configuration for the long-lived per-team agent container.

    This dictionary is both hashed and passed to ``containers.run``. Keeping a
    single declarative representation makes every material container-spec
    change trigger recreation without a separately maintained runtime epoch.
    """
    return {
        "image": settings.agent_image,
        "detach": True,
        "user": AGENT_USER,
        "network": get_team_network_name(team.team_id, settings.prefix),
        "environment": env,
        "volumes": {
            get_agent_home_volume_name(team.team_id, settings.prefix): {
                "bind": AGENT_HOME,
                "mode": "rw",
            },
            get_agent_workspace_volume_name(team.team_id, settings.prefix): {
                "bind": "/workspace",
                "mode": "rw",
            },
        },
        "extra_hosts": {"host.docker.internal": "host-gateway"},
        "shm_size": "1g",
        "restart_policy": {"Name": "unless-stopped"},
    }


def _agent_config_hash(
    container_config: dict[str, Any],
    has_git_credentials: bool,
    ssh_key_fingerprint: str = "",
) -> str:
    """Fingerprint of the config the agent container was created with.

    Stored as a container label; a mismatch on ensure means the image, injected
    config, or Docker run specification changed, so the container is recreated.
    HOME and /workspace are volumes — nothing is lost. Git credentials and the
    team SSH key are copied into HOME by the volume init step at container
    creation, so their presence is also part of the fingerprint: a container
    created before setup_repo_auth (or before the key existed) would otherwise
    keep matching forever and never pick up the file. The SSH part is the key's
    fingerprint, not a boolean, so a regenerated key also recreates the
    container."""
    import hashlib

    payload = json.dumps(
        {
            "container": container_config,
            "has_git_credentials": has_git_credentials,
            "ssh_key_fingerprint": ssh_key_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _prepare_agent_volumes(
    client: DockerClient,
    image: str,
    home_volume: str,
    workspace_volume: str,
    cred_file: str | None,
    ssh_key_file: str | None = None,
) -> None:
    """Make persistent agent data usable by the unprivileged image user.

    Older coder images mounted HOME at ``/root`` and consequently left both
    named volumes root-owned. A short-lived, networkless init container runs
    as root only to migrate that existing data and copy the host credential
    store and team SSH deploy key; the long-lived container and every agent
    exec run as ``agent``. Marker files avoid recursively walking large
    checkouts after migration.
    """
    volumes: dict[str, dict[str, str]] = {
        home_volume: {"bind": AGENT_HOME, "mode": "rw"},
        workspace_volume: {"bind": "/workspace", "mode": "rw"},
    }
    if cred_file:
        volumes[cred_file] = {
            "bind": "/run/oduflow/git-credentials",
            "mode": "ro",
        }
    if ssh_key_file:
        volumes[ssh_key_file] = {
            "bind": "/run/oduflow/ssh-key",
            "mode": "ro",
        }

    script = """
if ! id agent >/dev/null 2>&1; then
    echo "incompatible coder image: missing required user 'agent'" >&2
    echo "build the current docker/agent image and configure [agent].image to that tag" >&2
    exit 64
fi
if [ ! -f /home/agent/.oduflow-owner-v2 ]; then
    chown -R agent:agent /home/agent
    touch /home/agent/.oduflow-owner-v2
    chown agent:agent /home/agent/.oduflow-owner-v2
fi
if [ ! -f /workspace/.oduflow-owner-v2 ]; then
    chown -R agent:agent /workspace
    touch /workspace/.oduflow-owner-v2
    chown agent:agent /workspace/.oduflow-owner-v2
fi
if [ -f /run/oduflow/git-credentials ]; then
    install -m 600 -o agent -g agent \
        /run/oduflow/git-credentials /home/agent/.git-credentials
fi
if [ -f /run/oduflow/ssh-key ]; then
    install -d -m 700 -o agent -g agent /home/agent/.ssh
    install -m 600 -o agent -g agent \
        /run/oduflow/ssh-key /home/agent/.ssh/id_ed25519
    # Oduflow's SSH policy lives in its own file, rewritten on every
    # container creation so policy changes reach existing (persistent) homes;
    # the user-editable config only carries the Include. Mirrors the
    # server-side team_ssh_command options.
    printf '%s\\n' 'Host *' '  IdentitiesOnly yes' '  BatchMode yes' \
        '  StrictHostKeyChecking accept-new' > /home/agent/.ssh/oduflow_config
    chown agent:agent /home/agent/.ssh/oduflow_config
    chmod 600 /home/agent/.ssh/oduflow_config
    if [ ! -f /home/agent/.ssh/config ]; then
        printf 'Include oduflow_config\\n' > /home/agent/.ssh/config
    elif ! grep -q 'Include oduflow_config' /home/agent/.ssh/config; then
        { printf 'Include oduflow_config\\n'; \
          cat /home/agent/.ssh/config; } > /home/agent/.ssh/config.tmp
        mv /home/agent/.ssh/config.tmp /home/agent/.ssh/config
    fi
    chown agent:agent /home/agent/.ssh/config
    chmod 600 /home/agent/.ssh/config
fi
"""
    client.containers.run(
        image=image,
        command=["-eu", "-c", script],
        entrypoint=["/bin/sh"],
        user="root",
        remove=True,
        network_disabled=True,
        volumes=volumes,
    )


def _ensure_agent_container(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> None:
    """Ensure the team's coding-agent container (best-effort, opt-in).

    One container serves every environment of the team. Its HOME (auth +
    sessions) and /workspace (one checkout per environment, at
    /workspace/<slug>) live on persistent named volumes, so a login done once
    survives recreation and is shared by the team's environments. Per-env
    checkouts are created by ``_agent_add_env``; the agent reaches each
    environment only through the Oduflow MCP server (git push ->
    pull_and_apply). Config (oduflow.toml) is the source of truth: an existing
    container created with a different agent config (credentials, image, port)
    is recreated automatically. Never fatal to the caller.
    See specs/0029-agent-console-and-chat.md.
    """
    if not team.agent_enabled:
        return

    try:
        agent_labels = {
            settings.managed_label: "true",
            settings.system_label: "true",
            settings.team_label: team.team_id,
        }
        home_volume = get_agent_home_volume_name(team.team_id, settings.prefix)
        workspace_volume = get_agent_workspace_volume_name(
            team.team_id, settings.prefix
        )
        for volume in (home_volume, workspace_volume):
            try:
                client.volumes.get(volume)
            except docker.errors.NotFound:
                client.volumes.create(volume, labels=agent_labels)

        # Provider/user vars only. Deliberately NO Oduflow MCP credentials
        # here: the container-wide env is readable from any agent console
        # (`env`, /proc/1/environ), so the team auth_token must never enter
        # the container. Each console/chat session injects its environment's
        # SCOPED token + /mcp/<env> URL into its own `docker exec` env instead
        # (see web_ui.ws_agent_console / ws_agent_acp and ADR 0028).
        agent_env = dict(_agent_env_vars(settings, team))
        cred_file = team.git_credentials_file()
        has_git_credentials = os.path.isfile(cred_file)
        from oduflow import git_ops

        ssh_key_file = git_ops.ssh_key_path(team.ssh_dir())
        ssh_fingerprint = git_ops.ssh_key_fingerprint(team.ssh_dir())
        container_config = _agent_container_config(settings, team, agent_env)
        config_hash = _agent_config_hash(
            container_config, has_git_credentials, ssh_fingerprint
        )

        container_name = get_agent_container_name(team.team_id, settings.prefix)
        existing_container = None
        try:
            existing_container = client.containers.get(container_name)
            if (
                existing_container.labels.get("oduflow.agent_config_hash", "")
                == config_hash
            ):
                if existing_container.status != "running":
                    existing_container.start()
                return
        except docker.errors.NotFound:
            pass

        # Resolve the exact immutable image before touching a working
        # container. A failed pull therefore leaves the previous agent
        # available and its old config hash ensures a later restart retries.
        try:
            client.images.pull(settings.agent_image)
        except Exception:
            outcome = (
                "keeping existing container"
                if existing_container is not None
                else "agent container was not created"
            )
            logger.warning(
                "Could not pull agent image %s; %s",
                settings.agent_image,
                outcome,
                extra={"container": container_name},
                exc_info=True,
            )
            return

        if existing_container is not None:
            # Config changed — recreate with the already-pulled image. HOME and
            # /workspace are volumes, so auth, sessions and checkouts survive.
            logger.info(
                "Agent config changed; recreating container",
                extra={"container": container_name},
            )
            existing_container.remove(force=True)

        ensure_team_network(client, settings, team)

        labels = dict(agent_labels)
        labels.update(
            {
                "oduflow.agent": "true",
                "oduflow.agent_config_hash": config_hash,
                "oduflow.created_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
            }
        )

        _prepare_agent_volumes(
            client,
            settings.agent_image,
            home_volume,
            workspace_volume,
            cred_file if has_git_credentials else None,
            # Gate the mount on the key file itself; the fingerprint only
            # feeds the config hash and may be "" while the key is usable.
            ssh_key_file if os.path.isfile(ssh_key_file) else None,
        )
        client.containers.run(
            name=container_name,
            labels=labels,
            **container_config,
        )
        logger.info("Agent container ensured", extra={"container": container_name})
        # Report which Claude credential the (re)created container will use, so
        # authentication failures can be diagnosed from the logs. The secret
        # value is never logged, only the selected mode.
        auth_label = {
            "setup_token": "subscription (CLAUDE_CODE_OAUTH_TOKEN)",
            "api_key": "Console API (ANTHROPIC_API_KEY)",
            "interactive": "interactive /login",
        }.get(_claude_auth_mode(settings, team), "unknown")
        logger.info("Claude auth: %s", auth_label, extra={"container": container_name})
    except Exception:
        logger.warning("Agent container ensure failed", exc_info=True)


def _agent_add_env(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    repo_url: str,
    branch: str,
    git_user: str,
) -> None:
    """Create/refresh one environment's checkout in the team's agent container.

    Ensures the container exists, then execs clone-env.sh to clone/fetch the
    branch into /workspace/<slug> and write that checkout's Claude .mcp.json.
    Best-effort: never fatal to environment creation.
    """
    if not team.agent_enabled:
        return
    try:
        _ensure_agent_container(client, settings, team)
        container = client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        )
        # The scoped per-env URL only; no token argument — the checkout's
        # .mcp.json holds a ${ODUFLOW_MCP_TOKEN} placeholder that each session
        # resolves from its own exec env.
        cmd = [
            "/usr/local/bin/clone-env.sh",
            repo_url,
            branch,
            slugify_branch(env_name),
            get_agent_mcp_url(settings, team, env_name),
            git_user,
        ]
        exit_code, output = container.exec_run(cmd, user=AGENT_USER)
        if exit_code not in (0, None):
            detail = (
                output.decode("utf-8", "replace")
                if isinstance(output, (bytes, bytearray))
                else output
            )
            logger.warning(
                "Agent checkout setup for '%s' exited %s: %s",
                env_name,
                exit_code,
                detail,
            )
    except Exception:
        logger.warning("Agent checkout setup failed for '%s'", env_name, exc_info=True)


def ensure_agent_env_checkout(
    settings: Settings, team: TeamSettings, env_name: str
) -> None:
    """Ensure an environment's checkout exists in the team's agent container.

    Clones it on demand (idempotent) by reading repo/branch/git_user from the
    environment's Odoo container labels. Used when opening a console/chat so
    that environments created before the agent feature — or a fresh workspace
    volume — get their ``/workspace/<slug>`` checkout without a full recreate.
    Best-effort.
    """
    if not team.agent_enabled:
        return
    try:
        client = get_client()
        odoo = client.containers.get(
            get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
        )
    except docker.errors.NotFound:
        return
    except Exception:
        logger.warning("Agent checkout ensure failed for '%s'", env_name, exc_info=True)
        return
    labels = odoo.labels or {}
    # Live-mount environments have no repo to clone from; skip (clone-env.sh
    # would skip on the empty URL anyway).
    repo_url = (
        "" if labels.get("oduflow.local_path") else labels.get(settings.repo_label, "")
    )
    branch = labels.get("oduflow.git_branch", env_name)
    git_user = labels.get("oduflow.git_user", "")
    _agent_add_env(client, settings, team, env_name, repo_url, branch, git_user)


def _agent_rename_env(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    old_name: str,
    new_name: str,
) -> None:
    """Move an environment's agent checkout and attachments to its new name.

    Deliberately a move, not remove + re-clone: the checkout can hold the coding
    agent's uncommitted work, which ``_agent_remove_env`` would delete. The
    caller follows with :func:`_agent_add_env`, which rewrites that checkout's
    ``.mcp.json`` with the renamed environment's scoped MCP URL. Best-effort,
    like every other agent-container side effect.
    """
    if not team.agent_enabled:
        return
    # Same guard as _agent_remove_env: an empty slug would make the paths below
    # the shared /workspace volume itself.
    if not slugify_branch(old_name) or not slugify_branch(new_name):
        return
    try:
        container = client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        )
    except docker.errors.NotFound:
        return
    except Exception:
        logger.warning("Agent checkout move failed for '%s'", old_name, exc_info=True)
        return
    for old_dir, new_dir in (
        (get_agent_checkout_dir(old_name), get_agent_checkout_dir(new_name)),
        (get_agent_upload_dir(old_name), get_agent_upload_dir(new_name)),
    ):
        if old_dir == new_dir:
            continue
        with contextlib.suppress(Exception):
            container.exec_run(
                [
                    "sh",
                    "-c",
                    f'[ -d "{old_dir}" ] || exit 0; rm -rf -- "{new_dir}"; '
                    f'mv -- "{old_dir}" "{new_dir}"',
                ],
                user=AGENT_USER,
            )


def _agent_remove_env(
    client: DockerClient, settings: Settings, team: TeamSettings, env_name: str
) -> None:
    """Remove one environment's checkout and Agent Chat attachments.

    Best-effort; the container itself keeps running (it serves other envs).
    """
    # A name that slugifies to nothing would make the checkout dir /workspace/
    # itself — never rm -rf the shared volume holding every env's checkout.
    # (clone-env.sh has the matching guard on the create side.)
    if not slugify_branch(env_name):
        logger.warning("Agent checkout removal skipped: empty slug for '%s'", env_name)
        return
    try:
        container = client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        )
    except docker.errors.NotFound:
        return
    except Exception:
        logger.warning(
            "Agent checkout removal failed for '%s'", env_name, exc_info=True
        )
        return
    with contextlib.suppress(Exception):
        container.exec_run(
            [
                "rm",
                "-rf",
                "--",
                get_agent_checkout_dir(env_name),
                get_agent_upload_dir(env_name),
            ],
            user=AGENT_USER,
        )


def _remove_agent_container(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> None:
    """Remove the team's agent container (best-effort).

    Volumes are left intact so auth/sessions and checkouts survive.
    """
    try:
        client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        ).remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception:
        logger.warning("Agent container removal failed", exc_info=True)


def is_protected(settings: Settings, team: TeamSettings, env_name: str) -> bool:
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    return os.path.exists(os.path.join(workspace_path, ".protected"))


def _get_note_text(env_name: str, workspaces_dir: str) -> str:
    note_path = os.path.join(get_workspace_path(env_name, workspaces_dir), ".note")
    if os.path.exists(note_path):
        try:
            with open(note_path) as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def protect_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    """Mark environment as protected by creating .protected marker file."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    open(marker, "w").close()
    logger.info("Environment protected", extra={"env_name": env_name})
    return {"env_name": env_name, "protected": True}


def unprotect_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    """Remove protection from environment by deleting .protected marker file."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    if os.path.exists(marker):
        os.remove(marker)
    logger.info("Environment unprotected", extra={"env_name": env_name})
    return {"env_name": env_name, "protected": False}


def require_environment(settings: Settings, team: TeamSettings, env_name: str) -> None:
    """Raise NotFoundError unless the team owns an environment by that name."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")


def get_env_token(settings: Settings, team: TeamSettings, env_name: str) -> str | None:
    """Return the per-environment MCP access token from the container label.

    Returns ``None`` for environments created before the feature existed (their
    container carries no ``oduflow.mcp_token`` label and Docker labels cannot be
    added without recreating the container).
    """
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    return container.labels.get(MCP_TOKEN_LABEL) or None


def get_note(settings: Settings, team: TeamSettings, env_name: str) -> str:
    return _get_note_text(env_name, team.workspaces_dir)


def set_note(
    settings: Settings, team: TeamSettings, env_name: str, note: str
) -> dict[str, Any]:
    """Set or clear a note for an environment."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    note_path = os.path.join(workspace_path, ".note")
    note = note.strip()
    if note:
        with open(note_path, "w") as f:
            f.write(note)
    elif os.path.exists(note_path):
        os.remove(note_path)
    logger.info("Environment note updated", extra={"env_name": env_name, "note": note})
    return {"env_name": env_name, "note": note}


def delete_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    preserve_share: bool = False,
) -> list[str]:
    if is_protected(settings, team, env_name):
        raise ProtectedError(
            f"Environment '{env_name}' is protected. Unprotect it before deleting."
        )
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)

    # Defence in depth before the rmtree below: env_name is validated at creation,
    # but reject any name whose derived path escapes (or equals) the workspaces
    # dir — a "." / ".." env_name must never let a delete reach outside it.
    _ws_root = os.path.realpath(team.workspaces_dir)
    _target = os.path.realpath(workspace_path)
    if _target == _ws_root or os.path.commonpath([_ws_root, _target]) != _ws_root:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")

    container_exists = True
    try:
        existing = client.containers.get(odoo_container_name)
        # Defence in depth on top of team-scoped names: never stop/remove a
        # container that belongs to another team (issue #39). Treat it as
        # absent so this team's own DB/workspace/port are still cleaned up.
        label = existing.labels.get(settings.team_label)
        if label is not None and label != team.team_id:
            container_exists = False
    except docker.errors.NotFound:
        container_exists = False

    if not container_exists and not os.path.exists(workspace_path):
        raise NotFoundError(f"Environment '{env_name}' does not exist.")

    warnings: list[str] = []

    logger.info("Deleting environment", extra={"env_name": env_name})

    _agent_remove_env(client, settings, team, env_name)

    # Forget any stored ACP chat sessions for this env (host-side state, so
    # cleared regardless of the agent container's presence) — a later env of the
    # same name then starts a fresh conversation instead of resuming this one.
    from oduflow import agent_sessions

    with contextlib.suppress(Exception):
        agent_sessions.clear_session(team, env_name)

    if settings.routing_mode == "port":
        release_port(team.port_registry_path, env_name)
    if team.hostname_registry_path or team.data_dir:
        release_hostname(_hostname_registry_path(team), env_name)

    if container_exists:
        try:
            container = client.containers.get(odoo_container_name)
            container.stop()
            container.remove(v=True)
        except docker.errors.NotFound:
            pass

    try:
        _exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);',
        )
    except Exception as exc:
        msg = f'Failed to drop database "{env_db}": {exc}'
        logger.warning(msg, extra={"env_name": env_name})
        warnings.append(msg)

    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    try:
        _drop_pg_role(client, settings, creds["pg_user"])
    except Exception as exc:
        msg = f'Failed to drop PG role "{creds["pg_user"]}": {exc}'
        logger.warning(msg, extra={"env_name": env_name})
        warnings.append(msg)

    if os.path.exists(workspace_path):
        _unmount_filestore(env_name, team)
        extra_dir = os.path.join(workspace_path, "extra")
        if os.path.isdir(extra_dir):
            from oduflow.extra_addons import remove_worktree

            for repo_name in os.listdir(extra_dir):
                wt_path = os.path.join(extra_dir, repo_name)
                if os.path.isdir(wt_path):
                    remove_worktree(team, repo_name, wt_path)
        shutil.rmtree(workspace_path)

    activity.remove(team, env_name)
    if not preserve_share:
        env_share.remove(team, env_name)
    invalidate_cache()
    logger.info("Environment deleted", extra={"env_name": env_name})
    return warnings


def list_environments(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    client = get_client()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    containers = client.containers.list(
        all=True,
        filters=filters,
        ignore_removed=True,
    )

    envs: dict[str, dict[str, Any]] = {}
    for container in containers:
        if not container.name.startswith(settings.prefix):
            continue
        env_name = container.labels.get(settings.branch_label)
        if not env_name:
            continue

        if env_name not in envs:
            git_branch = container.labels.get("oduflow.git_branch", env_name)
            envs[env_name] = {
                "env_name": env_name,
                "branch": env_name,  # backward compat alias
                "git_branch": git_branch,
                "containers": [],
                "status": "running",
                "url": None,
                "odoo_image": container.labels.get(settings.image_label, ""),
                "repo_url": sanitize_repo_url(
                    container.labels.get(settings.repo_label, "")
                ),
                "local_path": container.labels.get("oduflow.local_path", ""),
                "template_name": container.labels.get("oduflow.template", ""),
                "extra_addons": _normalize_extra_addons(
                    json.loads(container.labels.get("oduflow.extra_addons", "{}")),
                ),
                "extra_addons_revisions": json.loads(
                    container.labels.get("oduflow.extra_addons_revisions", "{}")
                ),
                "db_name": get_db_name(env_name, team.team_id),
                "protected": is_protected(settings, team, env_name),
                "auto_install_modules": container.labels.get(
                    "oduflow.auto_install_modules", ""
                ),
                "created_at": container.labels.get("oduflow.created_at", "")
                or container.attrs.get("Created", ""),
                "note": _get_note_text(env_name, team.workspaces_dir),
                "stack": container.labels.get("oduflow.stack", ""),
                "stack_resource": container.labels.get("oduflow.stack-resource", ""),
                "stack_spec_hash": container.labels.get("oduflow.stack-spec-hash", ""),
                "stack_sanitize": container.labels.get("oduflow.stack-sanitize", ""),
                "hostname": get_env_short_hostname(
                    env_name, container.labels.get(ENV_HOSTNAME_LABEL, "")
                ),
            }

        try:
            image_name = container.image.tags[0] if container.image.tags else "unknown"
        except Exception:
            image_name = container.attrs.get("Config", {}).get("Image", "unknown")

        container_info = {
            "name": container.name,
            "status": container.status,
            "image": image_name,
        }

        if "-odoo" in container.name:
            try:
                envs[env_name]["url"] = (
                    get_env_base_url(settings, team, env_name, container)[0]
                    + "/web?debug=1"
                )
            except NotFoundError:
                # Port mode with no published port: leave the URL unset.
                pass

        envs[env_name]["containers"].append(container_info)

    records = activity.get_all(team)
    for env_name, env in envs.items():
        total = len(env["containers"])
        running = sum(1 for c in env["containers"] if c["status"] == "running")
        if total and running == 0:
            env["status"] = "stopped"
        elif running < total:
            env["status"] = "partial"
        else:
            env["status"] = "running"
        rec = records.get(env_name, {})
        env["last_activity"] = rec.get("last_activity", "")
        env["stopped_at"] = rec.get("stopped_at", "")
        env["stopped_by"] = rec.get("stopped_by", "")
        env["auto_stopped"] = rec.get("stopped_by") == "auto"

    return list(envs.values())


def wait_for_odoo_ready(
    settings: Settings, team: TeamSettings, env_name: str, timeout: int = 120
) -> bool:
    """Poll Odoo /web/health until it responds 200 or timeout."""
    import time
    import urllib.error
    import urllib.request

    # Path-free base URL: `get_environment_info()["url"]` ends in "/web?debug=1",
    # so appending "/web/health" to it probed "/web" instead — readiness then
    # meant "the login page renders", not "Odoo is healthy".
    try:
        base_url, _cookie_domain = get_env_base_url(settings, team, env_name)
    except NotFoundError:
        return False

    url = f"{base_url}/web/health"
    context = public_url_ssl_context(settings)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5, context=context) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def ensure_running(settings: Settings, env_name: str, team: TeamSettings) -> bool:
    """Start the environment's Odoo container if it is stopped.

    Returns True when a start was needed (the caller may want to tell the
    agent the environment was woken up), False when it was already running.
    """
    # Callers reach this without the environment lock (the lock-free odoo_* and
    # http tools), so two of them can find the same stopped container at once.
    # The wake key makes read-status-then-start atomic per environment: the
    # second caller waits, then sees "running" and reports no wake-up.
    with keyed_mutex(env_wake_key(team.team_id, env_name)):
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
        _assert_team_owns(container, settings, team, env_name)
        if container.status == "running":
            return False
        start_environment(settings, env_name, team)
        return True


def _assert_team_owns(
    container: Any, settings: Settings, team: TeamSettings, env_name: str
) -> None:
    """Reject operating on a container that belongs to another team.

    Defence in depth on top of team-scoped container names (issue #39): even
    if a name unexpectedly resolves across teams, the team label must match.
    The NotFound message is reused so the existence of another team's env is
    not disclosed.
    """
    label = container.labels.get(settings.team_label)
    if label is not None and label != team.team_id:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )


def restart_environment(
    settings: Settings, env_name: str, team: TeamSettings
) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        odoo_container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(odoo_container, settings, team, env_name)
    odoo_container.restart()

    logger.info("Environment restarted", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name}


def stop_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    if is_protected(settings, team, env_name):
        raise ProtectedError(
            f"Environment '{env_name}' is protected. Unprotect it before stopping."
        )
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        odoo_container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(odoo_container, settings, team, env_name)
    odoo_container.stop()

    activity.mark_stopped(team, env_name, by="manual")
    logger.info("Environment stopped", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name, "stopped": [odoo_container_name]}


def _container_image(container: Any, settings: Settings) -> str:
    """Best-effort image reference for a container (label first, then tag)."""
    image = container.labels.get(settings.image_label, "")
    if image:
        return str(image)
    try:
        if container.image.tags:
            return str(container.image.tags[0])
    except Exception:
        pass
    return str(container.attrs.get("Config", {}).get("Image", ""))


def ensure_overlay_mounted(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    labels: dict[str, str],
    odoo_image: str,
) -> bool:
    """Re-mount an overlay-mode env's filestore if the mount is missing or stale.

    fuse-overlayfs mounts do not survive a host reboot, so a container started
    (or auto-restarted) afterwards would serve an *empty* filestore directory —
    attachments 404 and Odoo scribbles assets into the raw merged dir that a
    later remount then shadows. Returns True if a remount was performed. Only
    overlay-mode envs (an existing ``upper`` dir) are touched; copy-mode and
    already-alive mounts are left alone.
    """
    paths = get_filestore_paths(env_name, team.workspaces_dir)
    if not os.path.isdir(paths["upper"]):
        return False  # copy-mode env: nothing to overlay-mount
    if overlay_mount_state(paths["merged"]) == MOUNT_ALIVE:
        return False
    template_name = labels.get("oduflow.template", "none")
    if not template_name or template_name == "none":
        return False
    _mount_filestore(
        client,
        settings,
        team,
        env_name,
        get_db_name(env_name, team.team_id),
        odoo_image,
        {},
        template_name=template_name,
        force_overlay=True,
    )
    logger.info("Re-mounted filestore overlay", extra={"env_name": env_name})
    return True


def start_environment(
    settings: Settings, env_name: str, team: TeamSettings
) -> dict[str, Any]:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            db_container.start()
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. System not initialized. Restart oduflow."
        )

    started = [settings.shared_db_container]

    try:
        odoo_container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(odoo_container, settings, team, env_name)

    # A stopped env's overlay may be gone (host reboot) — remount before start,
    # or the container comes up on an empty filestore. A failed remount must
    # abort the start: running on the raw merged directory loses attachments,
    # and writes made there are hidden by a later successful remount
    # (reconcile_overlay_mounts likewise leaves such containers stopped).
    try:
        ensure_overlay_mounted(
            client,
            settings,
            team,
            env_name,
            odoo_container.labels,
            _container_image(odoo_container, settings),
        )
    except Exception as exc:
        raise PrerequisiteNotMetError(
            f"Cannot start '{env_name}': its filestore overlay could not be "
            f"re-mounted ({exc}). Starting without it would run Odoo on an "
            "empty filestore and hide any files written there."
        ) from exc

    odoo_container.start()
    started.append(odoo_container_name)

    logger.info("Environment started", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name, "started": started}


def public_url_ssl_context(settings: Settings) -> ssl.SSLContext | None:
    """Verification policy for Oduflow's own HTTPS calls to a URL it hands out.

    ``None`` means "use the default verifying context". With ``tls = {}``
    Traefik terminates HTTPS using its default certificate, which is
    self-signed unless the operator supplied one — there is no trust anchor a
    verifying client could succeed against, so every internal probe would fail
    the handshake and report the environment as unreachable. These calls only
    ever target Oduflow's own Traefik, and choosing that mode is already an
    acceptance of its certificate, so verification is disabled there. Every
    other mode keeps full verification.
    """
    if not settings.uses_default_tls_cert:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def get_env_base_url(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    container: Any = None,
) -> tuple[str, str]:
    """Return ``(base_url, cookie_domain)`` for an environment's Odoo web UI.

    ``base_url`` is scheme + host (+ published port) with no path; ``cookie_domain``
    is the bare host an Odoo ``session_id`` cookie must be scoped to. This is
    the single source of the URL rule (traefik subdomain vs. published host
    port): ``adopt_existing_environment``, ``create_environment``,
    ``list_environments``, ``get_environment_info`` and ``connect_as_user`` all
    build their URLs from it. Pass ``container`` when it is already in hand to
    skip the Docker lookup. ``update_environment`` is the one deliberate
    exception: right after recreating the container it builds the URL from its
    own authoritative locals instead of re-reading fresh container attrs.
    """
    if settings.routing_mode == "traefik":
        if container is None:
            client = get_client()
            try:
                container = client.containers.get(
                    get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
                )
            except docker.errors.NotFound:
                raise NotFoundError(
                    f"Environment '{env_name}' does not exist. Use create_environment first."
                )
        # The container's own router rule wins over a recompute from current
        # settings: an environment created before the team gained a
        # base_domain still routes on its old nested name, and reporting the
        # zone name would hand out a URL Traefik does not serve.
        host = container_route_host(container) or env_hostname(
            team, env_name, container.labels.get(ENV_HOSTNAME_LABEL, "")
        )
        return f"{settings.public_scheme_for(team)}://{host}", host

    # Port routing: read the container's published 8069 port. Cookies are not
    # port-scoped, so the domain is just the host.
    if container is None:
        client = get_client()
        try:
            container = client.containers.get(
                get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
            )
        except docker.errors.NotFound:
            raise NotFoundError(
                f"Environment '{env_name}' does not exist. Use create_environment first."
            )
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    mappings = ports.get("8069/tcp") or []
    host_port = mappings[0].get("HostPort") if mappings else None
    if not host_port:
        raise NotFoundError(
            f"Environment '{env_name}' has no published HTTP port; "
            "is the environment running?"
        )
    return (
        f"{settings.public_scheme_for(team)}://{team.hostname}:{host_port}",
        team.hostname,
    )


def get_environment_info(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    from oduflow.docker_ops.stats import _get_one_container_stats

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    result: dict[str, Any] = {
        "env_name": env_name,
        "branch": env_name,  # backward compat alias
        "db_name": get_db_name(env_name, team.team_id),
        "workspace": get_workspace_path(env_name, team.workspaces_dir),
        "odoo": {"name": odoo_container_name, "running": False, "status": "not found"},
        "db": {
            "name": settings.shared_db_container,
            "running": False,
            "status": "not found",
        },
    }

    try:
        odoo_container = client.containers.get(odoo_container_name)
        result["odoo"]["status"] = odoo_container.status
        result["odoo"]["running"] = odoo_container.status == "running"

        labels = odoo_container.labels
        result["template_name"] = labels.get("oduflow.template", "none")
        result["repo_url"] = sanitize_repo_url(labels.get(settings.repo_label, ""))
        result["local_path"] = labels.get("oduflow.local_path", "")
        result["odoo_image"] = labels.get(settings.image_label, "")
        result["git_branch"] = labels.get("oduflow.git_branch", env_name)
        result["git_user"] = labels.get("oduflow.git_user", "")
        result["extra_addons"] = _normalize_extra_addons(
            json.loads(labels.get("oduflow.extra_addons", "{}")),
        )
        result["extra_addons_revisions"] = json.loads(
            labels.get("oduflow.extra_addons_revisions", "{}")
        )
        result["auto_install_modules"] = labels.get("oduflow.auto_install_modules", "")
        result["env_vars"] = json.loads(labels.get("oduflow.env_vars", "{}"))
        result["created_at"] = labels.get(
            "oduflow.created_at", ""
        ) or odoo_container.attrs.get("Created", "")
        result["note"] = _get_note_text(env_name, team.workspaces_dir)
        result["stack"] = labels.get("oduflow.stack", "")
        result["stack_resource"] = labels.get("oduflow.stack-resource", "")
        result["stack_spec_hash"] = labels.get("oduflow.stack-spec-hash", "")
        result["stack_sanitize"] = labels.get("oduflow.stack-sanitize", "")
        result["hostname"] = get_env_short_hostname(
            env_name, labels.get(ENV_HOSTNAME_LABEL, "")
        )

        try:
            result["url"] = (
                get_env_base_url(settings, team, env_name, odoo_container)[0]
                + "/web?debug=1"
            )
        except NotFoundError:
            # Port mode with no published port: leave the URL unset.
            pass

        stats = _get_one_container_stats(odoo_container)
        if stats:
            result["odoo"]["cpu_percent"] = stats["cpu_percent"]
            result["odoo"]["mem_usage_mb"] = stats["mem_usage_mb"]
            result["odoo"]["mem_percent"] = stats["mem_percent"]
    except docker.errors.NotFound:
        pass

    rec = activity.get_all(team).get(env_name, {})
    result["protected"] = is_protected(settings, team, env_name)
    result["last_activity"] = rec.get("last_activity", "")
    result["stopped_at"] = rec.get("stopped_at", "")
    result["stopped_by"] = rec.get("stopped_by", "")
    result["auto_stopped"] = rec.get("stopped_by") == "auto"

    try:
        db_container = client.containers.get(settings.shared_db_container)
        result["db"]["status"] = db_container.status
        result["db"]["running"] = db_container.status == "running"
        stats = _get_one_container_stats(db_container)
        if stats:
            result["db"]["cpu_percent"] = stats["cpu_percent"]
            result["db"]["mem_usage_mb"] = stats["mem_usage_mb"]
            result["db"]["mem_percent"] = stats["mem_percent"]
    except docker.errors.NotFound:
        pass

    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    result["db_user"] = creds["pg_user"]

    result["all_running"] = result["odoo"]["running"] and result["db"]["running"]
    return result


_LOCAL_SNAPSHOT_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    ".idea",
    ".vscode",
    "node_modules",
}
_LOCAL_SNAPSHOT_SKIP_SUFFIXES = (".pyc", ".pyo")


def _scan_local_snapshot(root: str) -> dict[str, dict[str, int]]:
    """Map relative paths to cheap file fingerprints for live-mount mode."""
    out: dict[str, dict[str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _LOCAL_SNAPSHOT_SKIP_DIRS]
        for f in filenames:
            if f.endswith(_LOCAL_SNAPSHOT_SKIP_SUFFIXES):
                continue
            fp = os.path.join(dirpath, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out[os.path.relpath(fp, root)] = {
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
            }
    return out


def _local_snapshot_path(env_name: str, team: TeamSettings) -> str:
    return os.path.join(
        get_workspace_path(env_name, team.workspaces_dir), ".oduflow_local_state.json"
    )


def _load_local_snapshot(
    env_name: str, team: TeamSettings
) -> dict[str, dict[str, int]]:
    snap_path = _local_snapshot_path(env_name, team)
    if not os.path.isfile(snap_path):
        return {}
    try:
        with open(snap_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(raw, dict):
        return {}

    # Current format stores {"version": 1, "files": {...}}. Older dev builds
    # stored the file map directly; accept both so existing environments recover.
    files = raw.get("files") if "files" in raw else raw
    if not isinstance(files, dict):
        return {}
    return files


def _write_local_snapshot(repo_path: str, env_name: str, team: TeamSettings) -> None:
    snap_path = _local_snapshot_path(env_name, team)
    try:
        os.makedirs(os.path.dirname(snap_path), exist_ok=True)
        with open(snap_path, "w") as f:
            json.dump(
                {
                    "version": 1,
                    "repo_path": repo_path,
                    "files": _scan_local_snapshot(repo_path),
                },
                f,
                sort_keys=True,
            )
    except OSError:
        logger.warning(
            "Could not write live-mount snapshot for %s", env_name, exc_info=True
        )


def _detect_local_changes(
    repo_path: str, env_name: str, team: TeamSettings
) -> tuple[str | None, list[str]]:
    """Return ``(base_ref, changed_files)`` for a live-mounted checkout.

    Live-mount mode is independent of Git: commits are the user's choice, and
    Oduflow tracks only what has been successfully applied to Odoo. The diff is
    computed from a per-env file snapshot and the snapshot is advanced only
    after a successful apply.
    """
    current = _scan_local_snapshot(repo_path)
    old = _load_local_snapshot(env_name, team)
    changed = sorted(
        {p for p, fingerprint in current.items() if old.get(p) != fingerprint}
        | {p for p in old if p not in current}
    )
    return None, changed


def _reapply_odoo_conf(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    container: Any,
) -> bool:
    """Reconstruct ``/etc/odoo/odoo.conf`` inside *container*.

    Resolves the base config (repo ``.oduflow/odoo.conf`` takes priority over the
    instance-level conf), regenerates the merged conf with *container*'s current
    extra-addons paths, and copies it into the container. Returns ``True`` if a
    conf was applied, ``False`` if no base config exists.

    The container reads ``/etc/odoo/odoo.conf`` as a *copy* made at create/update
    time — a plain restart reuses the stale copy, so this must run before a
    restart whenever the source ``odoo.conf`` changed.
    """
    labels = container.labels if isinstance(container.labels, dict) else {}
    if labels.get("oduflow.prod") == "true":
        # Production uses its own conf chain (odoo.prod.conf + tuned worker
        # overrides). Local import: production_ops composes env_ops helpers.
        from oduflow.docker_ops.production_ops import reapply_prod_odoo_conf

        return reapply_prod_odoo_conf(
            settings, team, labels.get("oduflow.prod_name", ""), container
        )
    repo_path = labels.get("oduflow.local_path") or get_repo_path(
        env_name, team.workspaces_dir
    )
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    repo_odoo_conf = os.path.join(repo_path, ".oduflow", "odoo.conf")
    if os.path.isfile(repo_odoo_conf):
        base_conf_path = repo_odoo_conf
    elif _resolve_instance_conf("odoo.conf", team.data_dir).exists():
        base_conf_path = str(_resolve_instance_conf("odoo.conf", team.data_dir))
    else:
        return False

    from oduflow.extra_addons import (
        generate_odoo_conf,
        resolve_extra_addons_path,
        resolve_main_addons_path,
    )

    extra_addons_json = (container.labels or {}).get("oduflow.extra_addons", "")
    extra_container_paths: list[str] = []
    if extra_addons_json:
        extra_dict = _normalize_extra_addons(json.loads(extra_addons_json))
        sources = _extra_checkout_sources(container, iter(extra_dict))
        extra_container_paths = [
            resolve_extra_addons_path(sources.get(rn, ""), rn) for rn in extra_dict
        ]
    generated_conf = os.path.join(workspace_path, "odoo.conf")
    main_addons_path = resolve_main_addons_path(repo_path)
    generate_odoo_conf(
        base_conf_path, generated_conf, extra_container_paths, main_addons_path
    )
    _copy_file_to_container(container, generated_conf, "/etc/odoo")
    return True


def _apply_actions(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    odoo_container_name: str,
    *,
    to_install: list[str],
    to_upgrade: list[str],
    do_restart: bool,
    changed_files: list[str],
    do_refresh: bool = False,
    config_changed: bool = False,
    dep_units: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Run install/upgrade/restart and build the result dict (shared by the
    explicit and auto paths of :func:`pull_environment`).

    ``dep_units`` lists the ``(host_path, container_base)`` repos whose
    dependency descriptors changed and need an apt/pip reinstall."""
    from oduflow.docker_ops.odoo_ops import (
        install_odoo_modules,
        upgrade_odoo_modules,
    )

    deps_changed = bool(dep_units)
    # Reinstall apt/pip deps up front so a subsequent module install/upgrade can
    # import newly-added libs (same apt-then-pip order as create/update). Never
    # restart here — a single restart happens in the branches below.
    dep_logs: list[str] = []
    dep_exit = 0
    if dep_units:
        dep_container = client.containers.get(odoo_container_name)
        dep_exit, _, dep_logs = _install_repo_dependencies(dep_container, dep_units)

    if to_install or to_upgrade:
        messages: list[str] = []
        odoo_output_parts: list[str] = []
        # Keep the FIRST non-zero code: a failed install followed by a
        # successful upgrade must still report failure. Callers treat this as
        # the whole apply's status (the compact pull_and_apply summary hides the
        # log behind an output_id, and the live-mount snapshot is only written
        # on 0), so a later success must not overwrite an earlier failure.
        apply_exit_code = 0
        if to_install:
            res = install_odoo_modules(settings, team, env_name, *to_install)
            apply_exit_code = apply_exit_code or res["exit_code"]
            messages.append(f"Installed modules: {','.join(to_install)}")
            if res.get("output"):
                odoo_output_parts.append(res["output"])
        if to_upgrade:
            res = upgrade_odoo_modules(settings, team, env_name, *to_upgrade)
            apply_exit_code = apply_exit_code or res["exit_code"]
            messages.append(f"Upgraded modules: {','.join(to_upgrade)}")
            if res.get("output"):
                odoo_output_parts.append(res["output"])
        if dep_logs:
            messages.insert(0, "Reinstalled dependencies.")
        container = client.containers.get(odoo_container_name)
        if config_changed and _reapply_odoo_conf(settings, team, env_name, container):
            messages.append("Reapplied odoo.conf.")
        container.restart()
        messages.append("Container restarted.")
        logger.info(
            "Container restarted after module update", extra={"env_name": env_name}
        )
        return {
            "action": "install" if to_install else "upgrade",
            "modules_installed": to_install,
            "modules_upgraded": to_upgrade,
            "exit_code": apply_exit_code or dep_exit,
            "changed_files": changed_files,
            "message": " ".join(messages),
            "output": "\n".join(dep_logs + odoo_output_parts),
        }

    if do_restart or deps_changed:
        container = client.containers.get(odoo_container_name)
        reapplied = config_changed and _reapply_odoo_conf(
            settings, team, env_name, container
        )
        container.restart()
        logger.info("Container restarted", extra={"env_name": env_name})
        parts: list[str] = []
        if reapplied:
            parts.append("Reapplied odoo.conf.")
        if dep_logs:
            parts.append("Reinstalled dependencies.")
        elif deps_changed:
            # A dependency file changed but nothing was installed (e.g. it was
            # deleted, or only removed lines). Removed packages are not
            # uninstalled until the container is rebuilt (update_environment).
            parts.append("Dependencies changed.")
        parts.append("Container restarted.")
        return {
            "action": "restart",
            "changed_files": changed_files,
            "message": " ".join(parts),
            "output": "\n".join(dep_logs),
            "exit_code": dep_exit,
        }

    if do_refresh:
        return {
            "action": "refresh",
            "changed_files": changed_files,
            "message": (
                "Only XML/JS changes detected. Refresh your browser "
                "(--dev=xml is active)."
            ),
        }
    return {
        "action": "none",
        "changed_files": changed_files,
        "message": (
            "Only Markdown files changed. No Odoo action required."
            if changed_files
            else "No changes detected."
        ),
    }


def pull_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    install: list[str] | None = None,
    upgrade: list[str] | None = None,
    restart: bool = False,
    strict: bool = False,
    presynced: tuple[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Sync code into an environment and apply the right Odoo action.

    Code-delivery mode is chosen from the env's labels:

    * **git** (default) — ``git pull`` the branch into the managed clone. Dev
      extra-addons resolve to immutable shared SHA checkouts; production keeps
      its private mutable worktrees for independent rollback.
    * **live-mount** (``oduflow.local_path`` label, gated by allow_local_path) — the
      agent's checkout is already bind-mounted, so there is nothing to pull;
      changes are detected from Oduflow's last successful local snapshot.

    ``presynced`` is for callers that already moved the checkout themselves and
    hold the resulting ``(old_head, changed_files)`` — currently
    :func:`switch_environment_branch`. Pulling again would find the clone
    already at origin's tip and report "up to date", losing the very diff that
    has to be applied, so the sync step is skipped and those values are used.

    Action selection:

    * **explicit** — when any of ``install`` / ``upgrade`` / ``restart`` is
      given, do exactly that. A guardrail compares the request against what the
      changed files suggest and returns non-blocking ``warnings`` (or, with
      ``strict=True``, refuses with ``action="blocked"``).
    * **auto** — otherwise fall back to ``classify_changes`` in git mode or
      path-only ``shallow_classify`` in live-mount mode.
    """
    from oduflow.git_analysis import (
        _is_active_dep_file,
        guardrail_warnings,
        merge_recommendations,
        recommend,
    )
    from oduflow.git_ops import pull_repo

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        container_obj = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    local_path = container_obj.labels.get("oduflow.local_path", "")
    is_local = bool(local_path)
    repo_path = local_path or get_repo_path(env_name, team.workspaces_dir)

    if not os.path.isdir(repo_path):
        raise NotFoundError(
            f"Repository for environment '{env_name}' not found at {repo_path}"
        )

    # --- 1. Sync code + detect changed files and a diff base ---
    # Each (repo_path, base_ref, files) unit is classified against its OWN repo
    # and old HEAD; extra-addon checkouts must not be classified against the main
    # repo's tree or their changes are misread (issue #51).
    classify_units: list[tuple[str, str | None, list[str]]] = []
    pending_extra_checkouts: dict[str, str] = {}
    pending_extra_revisions: dict[str, str] = {}
    extra_mount_switch_needed = False
    if is_local:
        _trace("pull_environment(%s): live-mount, detecting local changes", env_name)
        base_ref, all_changed = _detect_local_changes(repo_path, env_name, team)
        classify_units.append((repo_path, base_ref, all_changed))
        main_changed_files = all_changed
    else:
        if presynced is not None:
            _trace("pull_environment(%s): using pre-synced checkout", env_name)
            old_head, changed_files = presynced
        else:
            _trace("pull_environment(%s): git pull started", env_name)
            git_branch = container_obj.labels.get("oduflow.git_branch", env_name)
            old_head, changed_files = pull_repo(
                repo_path, git_branch, cred_file=team.git_credentials_file()
            )
        base_ref = old_head
        classify_units.append((repo_path, old_head, changed_files))

        all_changed = changed_files
        # Main-repo files for the per-repo dependency check below (extra-addons
        # checkouts contribute their own units via extra_dep_candidates).
        main_changed_files = changed_files

    try:
        extra_addons = _normalize_extra_addons(
            json.loads(container_obj.labels.get("oduflow.extra_addons", "{}"))
        )
    except (json.JSONDecodeError, TypeError):
        extra_addons = {}

    extra_changed_files: list[str] = []
    # (host_path, container_base, changed_files) per extra repo, for the
    # per-repo dependency-descriptor check below.
    extra_dep_candidates: list[tuple[str, str, list[str]]] = []
    if extra_addons:
        is_production = container_obj.labels.get("oduflow.prod") == "true"
        if is_production:
            # Production retains one mutable worktree per deployment. Its
            # deploy log records these HEADs and rollback resets them in place.
            from oduflow.extra_addons import pull_extra_worktree

            extra_dir = os.path.join(
                get_workspace_path(env_name, team.workspaces_dir), "extra"
            )
            for repo_name, branch in extra_addons.items():
                wt_path = os.path.join(extra_dir, repo_name)
                if not os.path.isdir(wt_path):
                    continue
                extra_old, extra_files = pull_extra_worktree(
                    team, repo_name, branch, wt_path
                )
                extra_changed_files.extend(extra_files)
                if extra_files:
                    classify_units.append((wt_path, extra_old, extra_files))
                    extra_dep_candidates.append(
                        (wt_path, f"/mnt/extra-addons-{repo_name}", extra_files)
                    )
        else:
            from oduflow.extra_addons import checkout_revision, ensure_shared_checkout

            try:
                stored_revisions = json.loads(
                    container_obj.labels.get("oduflow.extra_addons_revisions", "{}")
                )
            except (json.JSONDecodeError, TypeError):
                stored_revisions = {}
            if not isinstance(stored_revisions, dict):
                stored_revisions = {}
            current_sources = _extra_checkout_sources(container_obj, iter(extra_addons))
            for repo_name, branch in extra_addons.items():
                source = current_sources.get(repo_name, "")
                current_revision = str(stored_revisions.get(repo_name, ""))
                if not current_revision and source:
                    current_revision = checkout_revision(source)
                checkout = ensure_shared_checkout(
                    team,
                    repo_name,
                    branch,
                    current_revision=current_revision,
                )
                checkout_path = str(checkout["path"])
                revision = str(checkout["revision"])
                pending_extra_checkouts[repo_name] = checkout_path
                pending_extra_revisions[repo_name] = revision
                extra_mount_switch_needed = extra_mount_switch_needed or (
                    source != checkout_path or current_revision != revision
                )
                extra_files = list(checkout["changed_files"])
                extra_changed_files.extend(extra_files)
                if extra_files:
                    classify_units.append(
                        (checkout_path, current_revision or None, extra_files)
                    )
                    extra_dep_candidates.append(
                        (checkout_path, f"/mnt/extra-addons-{repo_name}", extra_files)
                    )

    all_changed = all_changed + extra_changed_files

    _trace(
        "pull_environment(%s): %d changed files: %s",
        env_name,
        len(all_changed),
        all_changed,
    )

    # --- 2. Determine actions: explicit (agent-driven) vs auto (classify) ---
    explicit = bool(install) or bool(upgrade) or restart
    recommended = (
        merge_recommendations(
            recommend(files, rp, ref) for rp, ref, files in classify_units if files
        )
        if all_changed
        else {"action": "none", "modules_to_install": [], "modules_to_upgrade": []}
    )
    # A changed .oduflow/odoo.conf must be reconstructed and re-copied into the
    # container before any restart (a plain restart reuses the stale copy).
    details = recommended.get("details")
    config_changed = isinstance(details, dict) and bool(details.get("restart_required"))
    # A changed dependency descriptor (requirements.txt / apt_packages.txt) in
    # the main repo or an extra-addons repo must reinstall apt/pip deps into
    # the running container and restart. Checked per repo — the "active" rule
    # (.oduflow/ shadows the repo root) applies within each repo separately;
    # applies to both explicit and auto modes (a prerequisite, not an agent action).
    dep_units: list[tuple[str, str]] = []
    if any(_is_active_dep_file(f, repo_path) for f in main_changed_files):
        dep_units.append((repo_path, "/mnt/extra-addons"))
    for dep_host, dep_base, dep_files in extra_dep_candidates:
        if any(_is_active_dep_file(f, dep_host) for f in dep_files):
            dep_units.append((dep_host, dep_base))

    warnings: list[str] = []
    do_refresh = False
    if explicit:
        to_install = list(install or [])
        to_upgrade = list(upgrade or [])
        do_restart = bool(restart)
        if all_changed:
            warnings = guardrail_warnings(
                recommended, to_install, to_upgrade, do_restart
            )
        if strict and warnings:
            return {
                "action": "blocked",
                "warnings": warnings,
                "changed_files": all_changed,
                "message": (
                    "Guardrail (strict) blocked apply: the requested action looks "
                    "incomplete for the detected changes. Re-call with the suggested "
                    "install/upgrade, or pass strict=False to apply anyway."
                ),
            }
    else:
        if not all_changed:
            if extra_mount_switch_needed:
                # install_dependencies is on because this recreates the
                # container, and pip --user packages live in the container
                # filesystem (/var/lib/odoo/.local/lib), not in a volume — a
                # recreate destroys them. update_environment reinstalls apt +
                # pip for the main repo and every extra checkout from the new
                # mount sources, which is exactly the recovery needed here.
                update_environment(
                    settings,
                    team,
                    env_name,
                    extra_checkout_overrides=pending_extra_checkouts,
                    extra_revision_overrides=pending_extra_revisions,
                    pull_image=False,
                    install_dependencies=True,
                )
                _cleanup_legacy_extra_worktrees(
                    team, env_name, iter(pending_extra_checkouts)
                )
                return {
                    "action": "none",
                    "extra_addons_cache_migrated": True,
                    "message": (
                        "Already up to date. Extra-addons mounts now use the "
                        "shared immutable checkout cache."
                    ),
                }
            return {
                "action": "none",
                "message": (
                    "No local changes detected." if is_local else "Already up to date."
                ),
            }
        to_install = list(recommended.get("modules_to_install", []))
        to_upgrade = list(recommended.get("modules_to_upgrade", []))
        do_restart = recommended.get("action") == "restart"
        do_refresh = recommended.get("action") == "refresh"

    # --- 3. Switch immutable mounts, then execute ---
    # Do this only after strict guardrails have accepted the requested action.
    # The checkout itself was prepared off to the side and is safe to cache even
    # when strict mode blocks without touching the running container.
    if extra_mount_switch_needed:
        # See the note on the early-return recreate above: the container is
        # replaced, so its pip --user packages go with it and have to be
        # reinstalled unconditionally, not just for the repos whose dependency
        # descriptor happens to be in this diff.
        update_environment(
            settings,
            team,
            env_name,
            extra_checkout_overrides=pending_extra_checkouts,
            extra_revision_overrides=pending_extra_revisions,
            pull_image=False,
            install_dependencies=True,
        )
        _cleanup_legacy_extra_worktrees(team, env_name, iter(pending_extra_checkouts))

    result = _apply_actions(
        client,
        settings,
        team,
        env_name,
        odoo_container_name,
        to_install=to_install,
        to_upgrade=to_upgrade,
        do_restart=do_restart,
        do_refresh=do_refresh,
        changed_files=all_changed,
        config_changed=config_changed,
        dep_units=dep_units,
    )
    if warnings:
        result["warnings"] = warnings
    if is_local and int(result.get("exit_code", 0)) == 0:
        _write_local_snapshot(repo_path, env_name, team)
    return result


def switch_environment_branch(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    branch: str,
    *,
    install: list[str] | None = None,
    upgrade: list[str] | None = None,
    restart: bool = False,
    strict: bool = False,
    extra_addons: dict[str, str] | None = None,
    new_name: str | None = None,
) -> dict[str, Any]:
    """Point an existing environment at another git branch and apply the diff.

    The environment is the reusable unit. Its database, filestore, hostname,
    ports, credentials and scoped MCP token all stay exactly as they are — only
    the code it serves changes. That is what turns a finished branch's
    environment into the starting point for the next task instead of something
    to delete and provision again ([[0048]] pooled the hostname; this pools the
    whole environment).

    Order matters:

    1. Fetch the target branch. A branch that was never pushed fails here, with
       nothing mutated.
    2. Flip the ``oduflow.git_branch`` label — deliberately *before* the
       checkout. Labels are frozen at container creation, so this recreates the
       container; if it fails, the environment is still on the old branch in
       both label and tree. The reverse order would leave a switched tree that
       the next pull silently resets back to the old branch.
    3. Check out the branch and hand the resulting diff to
       :func:`pull_environment`, so the switch shares one implementation of
       classification, the guardrail, extra-addons mounts and apply.

    ``new_name`` optionally renames the environment along the way, so a slot
    whose name still echoes a finished branch can be relabelled instead of
    re-provisioned. It rides on the same container recreate as the label flip
    (step 2), which keeps the whole switch at one recreate; everything after
    that point works under the new name.
    """
    from oduflow.git_ops import checkout_branch, fetch_branch, is_git_repository
    from oduflow.naming import PROD_ENV_PREFIX

    # Productions deploy from productions.json and keep a deploy log for
    # rollback; moving their branch behind that record would strand both. The
    # MCP tool is already fenced off by the dev-tool namespace check, so this is
    # the guard for the REST route and any direct caller.
    if env_name.startswith(PROD_ENV_PREFIX):
        raise ConflictError(
            f"'{env_name}' is a production environment. Change the branch it "
            "deploys with update_production, which records the deploy and keeps "
            "rollback available."
        )

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        container_obj = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    labels = dict(container_obj.labels or {})
    if labels.get("oduflow.prod") == "true":
        raise ConflictError(
            f"Environment '{env_name}' is a production container. Change the "
            "branch it deploys with update_production."
        )
    local_path = labels.get("oduflow.local_path", "")
    if local_path:
        raise PrerequisiteNotMetError(
            f"Environment '{env_name}' is live-mounted from {local_path}, so its "
            "git state belongs to that checkout, not to Oduflow. Switch the "
            "branch there and call pull_and_apply."
        )

    rename_to = (new_name or "").strip()
    if rename_to == env_name:
        rename_to = ""
    if rename_to:
        # Everything below this point mutates; a bad or taken target name must
        # fail here.
        check_rename_target(
            settings=settings,
            team=team,
            client=client,
            env_name=env_name,
            new_name=rename_to,
            container_id=container_obj.id,
            source_labels=labels,
        )

    current_branch = labels.get("oduflow.git_branch", env_name)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    if not is_git_repository(repo_path):
        raise NotFoundError(
            f"Repository for environment '{env_name}' not found at {repo_path}"
        )

    extra_override = (
        _normalize_extra_addons(extra_addons) if extra_addons is not None else None
    )

    if branch == current_branch and extra_override is None:
        # Not an error: the caller wanted this environment on this branch, and it
        # is. Pull instead, so "switch" is safe to call at the start of any task.
        previous_env_name = env_name
        result = pull_environment(
            settings,
            team,
            env_name,
            install=install,
            upgrade=upgrade,
            restart=restart,
            strict=strict,
        )
        # The pull runs first so that a strict refusal keeps its meaning:
        # "blocked" has to mean nothing changed, the name included.
        if rename_to and result.get("action") != "blocked":
            update_environment(
                settings,
                team,
                env_name,
                rename_to=rename_to,
                pull_image=False,
                install_dependencies=False,
                # The agent checkout is moved and refreshed right below, with
                # the branch in hand; clone-env.sh must not run twice.
                sync_agent_checkout=False,
            )
            _agent_rename_env(client, settings, team, env_name, rename_to)
            env_name = rename_to
            _agent_add_env(
                client,
                settings,
                team,
                env_name,
                labels.get(settings.repo_label, ""),
                branch,
                labels.get("oduflow.git_user", ""),
            )
            result["renamed_from"] = previous_env_name
        result["branch"] = branch
        result["previous_branch"] = current_branch
        result["branch_switched"] = False
        result["env_name"] = env_name
        blocked = result.get("action") == "blocked"
        renamed = (
            f"Renamed '{previous_env_name}' to '{env_name}'. "
            if rename_to and not blocked
            else ""
        )
        not_renamed = (
            f" The rename to '{rename_to}' was not applied."
            if rename_to and blocked
            else ""
        )
        result["message"] = (
            f"{renamed}Environment '{env_name}' is already on branch '{branch}'; "
            f"pulled it instead. {result.get('message', '')}".strip()
            + not_renamed
        )
        return result

    fetch_branch(repo_path, branch, cred_file=team.git_credentials_file())

    label_overrides = {"oduflow.git_branch": branch}
    if extra_override is not None:
        label_overrides["oduflow.extra_addons"] = json.dumps(extra_override)
    logger.info(
        "Switching environment branch",
        extra={
            "env_name": env_name,
            "from_branch": current_branch,
            "to_branch": branch,
        },
    )
    # Dependencies are deliberately not reinstalled here: the checkout is still
    # on the old branch at this point. A changed requirements.txt / apt list is
    # part of the diff below, which pull_environment installs before restarting.
    # A requested rename rides on this same recreate.
    previous_env_name = env_name
    update_environment(
        settings,
        team,
        env_name,
        label_overrides=label_overrides,
        pull_image=False,
        install_dependencies=False,
        rename_to=rename_to or None,
        # update_environment moves the agent checkout; the .mcp.json rewrite it
        # would follow with is the same clone-env.sh run as _agent_add_env
        # below, which also carries the target branch.
        sync_agent_checkout=False,
    )
    if rename_to:
        _agent_rename_env(client, settings, team, env_name, rename_to)
        env_name = rename_to
        repo_path = get_repo_path(env_name, team.workspaces_dir)

    try:
        old_head, new_head, changed_files = checkout_branch(repo_path, branch)
    except FlowError as exc:
        raise ExternalCommandError(
            "git checkout",
            1,
            f"Environment '{env_name}' is now labelled with branch '{branch}', but "
            f"its checkout is still on '{current_branch}': {exc} Re-run "
            "switch_branch (or pull_and_apply) once the clone is healthy.",
        ) from exc

    # The coding agent's own checkout for this environment follows the switch;
    # uncommitted work there is preserved by clone-env.sh, which warns instead.
    _agent_add_env(
        client,
        settings,
        team,
        env_name,
        labels.get(settings.repo_label, ""),
        branch,
        labels.get("oduflow.git_user", ""),
    )

    result = pull_environment(
        settings,
        team,
        env_name,
        install=install,
        upgrade=upgrade,
        restart=restart,
        strict=strict,
        presynced=(old_head, changed_files),
    )

    if rename_to:
        switched = (
            f"Switched '{previous_env_name}' from '{current_branch}' to '{branch}' "
            f"and renamed it to '{env_name}'."
        )
    else:
        switched = f"Switched '{env_name}' from '{current_branch}' to '{branch}'."
    # "Already up to date" is the pull vocabulary and reads as a contradiction
    # right after a switch; what it means here is that the two branches leave
    # the running Odoo with nothing to catch up on.
    tail = (
        "No code difference to apply."
        if result.get("action") == "none"
        else str(result.get("message", ""))
    )
    result["branch"] = branch
    result["previous_branch"] = current_branch
    result["branch_switched"] = True
    result["env_name"] = env_name
    if rename_to:
        result["renamed_from"] = previous_env_name
    result["old_head"] = old_head
    result["new_head"] = new_head
    result["message"] = f"{switched} {tail}".strip()
    return result


def check_rename_target(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    new_name: str,
    *,
    container_id: str = "",
    source_labels: dict[str, str] | None = None,
) -> None:
    """Reject a rename target before anything is mutated.

    A name keys three independent resources — a container, a workspace directory
    and a database — and slugification is lossy (``feature/x`` and ``feature-x``
    collide on all three). Every check therefore compares against the resource
    the *target* name derives, and skips the case where that resource is the
    environment's own (a rename whose slug does not change).

    ``source_labels`` are the renamed container's own labels, which say whether
    the name is Oduflow's to change at all: a production's name is recorded in
    productions.json and a stack member's in the stack definition.
    """
    from oduflow.naming import PROD_ENV_PREFIX, validate_env_name

    labels = source_labels or {}
    if env_name.startswith(PROD_ENV_PREFIX) or labels.get("oduflow.prod") == "true":
        raise ConflictError(
            f"'{env_name}' is a production environment. Its name is recorded in "
            "productions.json and cannot be changed here; use the *_production "
            "tools instead."
        )
    stack = labels.get("oduflow.stack")
    if stack:
        raise ConflictError(
            f"Environment '{env_name}' belongs to stack '{stack}', which "
            "reconciles environments by name. Rename it in the stack definition "
            "instead."
        )

    validate_env_name(new_name)
    if new_name.startswith(PROD_ENV_PREFIX):
        raise ConflictError(
            f"'{new_name}' is reserved for production environments; choose "
            "another name."
        )

    target_container = get_resource_name(
        new_name, "odoo", settings.prefix, team.team_id
    )
    try:
        existing = client.containers.get(target_container)
    except docker.errors.NotFound:
        pass
    else:
        if not container_id or existing.id != container_id:
            raise ConflictError(
                f"An environment named '{new_name}' already exists. Delete it "
                "first or pick another name."
            )

    old_workspace = get_workspace_path(env_name, team.workspaces_dir)
    new_workspace = get_workspace_path(new_name, team.workspaces_dir)
    if os.path.normpath(old_workspace) != os.path.normpath(
        new_workspace
    ) and os.path.exists(new_workspace):
        raise ConflictError(
            f"A workspace directory for '{new_name}' already exists at "
            f"{new_workspace}. Remove it first or pick another name."
        )

    old_db = get_db_name(env_name, team.team_id)
    new_db = get_db_name(new_name, team.team_id)
    if new_db != old_db and _db_exists(client, settings, new_db):
        raise ConflictError(
            f"Database '{new_db}' (derived from '{new_name}') already exists. "
            "Pick another name."
        )


def _repair_extra_worktrees(workspace_path: str) -> None:
    """Re-point legacy per-workspace extra-addon worktrees after a move.

    A worktree and its bare repository record each other's absolute path, so
    moving the workspace directory breaks the pair. ``git worktree repair`` run
    inside the moved worktree rewrites both sides. Environments on the shared
    SHA-keyed checkouts keep their addons outside the workspace and are not
    affected. Best-effort: a broken legacy worktree is re-created by the next
    pull.
    """
    extra_dir = os.path.join(workspace_path, "extra")
    if not os.path.isdir(extra_dir):
        return
    for repo_name in sorted(os.listdir(extra_dir)):
        worktree_path = os.path.join(extra_dir, repo_name)
        if not os.path.exists(os.path.join(worktree_path, ".git")):
            continue
        try:
            subprocess.run(
                ["git", "-C", worktree_path, "worktree", "repair"],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Could not repair extra worktree %s: %s", worktree_path, exc)


def _rebase_volumes(
    volumes: dict[str, dict[str, str]],
    old_workspace: str,
    new_workspace: str,
    old_db: str,
    new_db: str,
) -> dict[str, dict[str, str]]:
    """Re-point an existing container's binds at the renamed environment.

    Two things move with the name: the host side of every bind under the
    workspace directory, and the filestore's container-side path, which Odoo
    derives from the database name.
    """
    rebased: dict[str, dict[str, str]] = {}
    for source, spec in volumes.items():
        if source == old_workspace or source.startswith(old_workspace + os.sep):
            source = new_workspace + source[len(old_workspace) :]
        bind = spec.get("bind", "")
        if old_db != new_db and bind.endswith(f"/filestore/{old_db}"):
            spec = {**spec, "bind": bind[: -len(old_db)] + new_db}
        rebased[source] = spec
    return rebased


def _relocate_environment_state(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    old_name: str,
    new_name: str,
) -> None:
    """Move everything an environment's *name* keys, with its container down.

    Called from :func:`update_environment` in the window between removing the
    old container and creating the new one — the only moment at which the
    workspace directory can be moved and the database renamed. No transaction
    spans a directory rename, an ``ALTER DATABASE`` and three registries, so the
    order is chosen to keep the environment's *data* whole under exactly one of
    the two names. The container itself is already gone either way — a failure
    here leaves the environment containerless, which :func:`update_environment`
    reports explicitly instead of pretending to roll back:

    1. Unmount the filestore overlay and move the workspace directory. The move
       is atomic within one filesystem.
    2. Rename the database. This is the one step that can fail on its own (a
       connection that survived container removal), so a failure moves the
       directory back and re-raises, leaving the environment whole under its old
       name.
    3. Move the registry keys — port, hostname, activity, agent sessions — which
       carry the environment's port, URL, idle clock and chat history over.
    """
    old_workspace = get_workspace_path(old_name, team.workspaces_dir)
    new_workspace = get_workspace_path(new_name, team.workspaces_dir)
    old_db = get_db_name(old_name, team.team_id)
    new_db = get_db_name(new_name, team.team_id)

    moved = False
    if os.path.normpath(old_workspace) != os.path.normpath(
        new_workspace
    ) and os.path.isdir(old_workspace):
        _unmount_filestore(old_name, team)
        _wait_unmounted(get_filestore_paths(old_name, team.workspaces_dir)["merged"])
        os.makedirs(os.path.dirname(new_workspace) or ".", exist_ok=True)
        os.rename(old_workspace, new_workspace)
        moved = True
        _repair_extra_worktrees(new_workspace)

    if new_db != old_db and _db_exists(client, settings, old_db):
        try:
            # The container is gone, but a psql session or a pooled connection
            # can still hold the database and make RENAME fail.
            _exec_sql(
                client,
                settings,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{old_db}' AND pid <> pg_backend_pid();",
            )
            _exec_sql(
                client, settings, f'ALTER DATABASE "{old_db}" RENAME TO "{new_db}";'
            )
        except Exception:
            if moved:
                with contextlib.suppress(OSError):
                    os.rename(new_workspace, old_workspace)
            raise

    rename_port_env(team.port_registry_path, old_name, new_name)
    if team.hostname_registry_path or team.data_dir:
        rename_hostname_env(_hostname_registry_path(team), old_name, new_name)
    activity.rename(team, old_name, new_name)
    env_share.rename(team, old_name, new_name)

    from oduflow import agent_sessions

    with contextlib.suppress(Exception):
        agent_sessions.rename(team, old_name, new_name)

    # The scoped MCP endpoint is /mcp/<env>, and its token->environment mapping
    # is cached; the renamed environment answers on a different path.
    invalidate_cache()
    logger.info(
        "Environment state relocated",
        extra={"env_name": new_name, "previous_env_name": old_name},
    )


def update_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    env_override: dict[str, str] | None = None,
    image_override: str | None = None,
    hostname_override: str | None = None,
    extra_checkout_overrides: dict[str, str] | None = None,
    extra_revision_overrides: dict[str, str] | None = None,
    pull_image: bool = True,
    install_dependencies: bool = True,
    label_overrides: dict[str, str] | None = None,
    rename_to: str | None = None,
    sync_agent_checkout: bool = True,
) -> dict[str, Any]:
    """Re-create an environment's container, preserving DB, repo and filestore.

    With no overrides this simply rebuilds the container from its current image
    and configuration (useful when the container is broken). Optionally pulls a
    different ``image_override`` and/or fully replaces the user-supplied
    environment variables with ``env_override`` (an explicit dict; an empty dict
    clears them). Internal extra-checkout overrides atomically switch immutable
    SHA mounts during pull_and_apply; that path disables image pulls and the
    redundant dependency reinstall. The PostgreSQL connection variables
    (HOST/USER/PASSWORD) are always re-derived from the environment credentials,
    and image-baked env comes from the image itself.

    ``hostname_override`` changes the short public hostname in Traefik mode.
    Empty or omitted values keep the current hostname policy. Conflicts are
    rejected before stopping the current container.

    ``rename_to`` additionally moves the environment onto another name. The
    container has to be re-created for that anyway — it carries the name in its
    own name, its labels and its bind mounts — so the rename rides on the same
    recreate instead of adding a second one: the name-keyed state is relocated
    (see :func:`_relocate_environment_state`) while the container is down, and
    everything the rest of this function derives from ``env_name`` is then
    derived from the new one. The coding agent's checkout for the environment
    follows the name as well, unless ``sync_agent_checkout`` is False:
    switch_branch refreshes that checkout itself once it is on the target
    branch, and clone-env.sh is too expensive to run twice.
    """
    requested_hostname = (hostname_override or "").strip()
    if requested_hostname:
        requested_hostname = validate_env_hostname(requested_hostname)
        if settings.routing_mode != "traefik":
            raise ValueError(
                "hostname is supported only when routing.mode = 'traefik'."
            )

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    # ------------------------------------------------------------------
    # 1. Look up existing container and extract its configuration
    # ------------------------------------------------------------------
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    if rename_to == env_name:
        rename_to = None
    if rename_to:
        # Before the first mutation: a taken name must not cost the environment
        # its container.
        check_rename_target(
            client,
            settings,
            team,
            env_name,
            rename_to,
            container_id=container.id,
            source_labels=dict(container.labels),
        )

    # Labels
    labels = dict(container.labels)
    if label_overrides:
        labels.update(label_overrides)

    # Image (current → target). A digest-pinned recreation leaves Config.Image
    # as sha256:..., so prefer the persisted tag before falling back to it.
    # Capture the current digest so we can report whether the running image
    # actually changed after the pull.
    try:
        current_image = container.image.tags[0]
    except (IndexError, Exception):
        current_image = (
            labels.get(settings.image_label) or container.attrs["Config"]["Image"]
        )
    odoo_image = image_override or current_image
    try:
        old_digest = container.image.id
    except Exception:
        old_digest = container.attrs.get("Image", "")
    run_image = (
        old_digest
        if not pull_image and not image_override and old_digest
        else odoo_image
    )

    # User-supplied environment variables: an explicit override wins (full
    # replace), otherwise restore from the persisted label. The DB connection
    # variables are added back from the credentials below.
    if env_override is not None:
        user_env = dict(env_override)
    else:
        user_env = json.loads(labels.get("oduflow.env_vars", "{}"))

    # Resolve secret references while the old container is still intact — a
    # dangling reference must fail the update here, not after the removal. The
    # label keeps the references; only the container env gets real values.
    resolved_user_env = secret_store.resolve_env_secrets(team, user_env) or {}

    # Volumes / bind mounts – parse "host:container:mode" strings
    raw_binds = container.attrs.get("HostConfig", {}).get("Binds") or []
    volumes: dict[str, dict[str, str]] = {}
    for bind in raw_binds:
        parts = bind.split(":")
        if len(parts) >= 3:
            volumes[parts[0]] = {"bind": parts[1], "mode": parts[2]}
        elif len(parts) == 2:
            volumes[parts[0]] = {"bind": parts[1], "mode": "rw"}

    if extra_checkout_overrides is not None:
        _replace_extra_checkout_mounts(
            volumes,
            labels,
            extra_checkout_overrides,
            extra_revision_overrides or {},
        )

    # Command
    raw_cmd = container.attrs["Config"].get("Cmd") or []
    command = " ".join(raw_cmd) if raw_cmd else None

    # Port bindings (only relevant in port mode). Only read here; a fresh
    # allocation (when switching into port mode) is deferred until just before
    # the container is recreated, so an earlier abort (e.g. image pull failure)
    # does not leak a reserved port.
    host_port: int | None = None
    port_newly_allocated = False
    if settings.routing_mode == "port":
        port_bindings = container.attrs.get("HostConfig", {}).get("PortBindings") or {}
        tcp_bindings = port_bindings.get("8069/tcp")
        if tcp_bindings:
            try:
                host_port = int(tcp_bindings[0]["HostPort"])
            except (KeyError, IndexError, ValueError, TypeError):
                pass

    # Validate the target image is available BEFORE removing the old container,
    # so a bad or unreachable image does not leave the env with no container
    # at all (#49). If the pull fails, fall back to a local copy; if there is
    # none, abort with the existing environment left untouched.
    image_updated = False
    if pull_image:
        try:
            logger.info("Pulling image %s", odoo_image)
            new_image_obj = client.images.pull(odoo_image)
            image_updated = bool(old_digest) and new_image_obj.id != old_digest
        except Exception as exc:
            logger.warning("Could not pull image %s: %s", odoo_image, exc)
            try:
                client.images.get(odoo_image)
            except docker.errors.ImageNotFound:
                raise PrerequisiteNotMetError(
                    f"Image '{odoo_image}' could not be pulled and is not available "
                    "locally; leaving the existing environment untouched."
                ) from exc

    if requested_hostname:
        hostname_prefix, _parent_domain = split_team_hostname(team.hostname)
        active_envs, used_hostnames = _environment_hostname_usage(
            client, settings, team, exclude_env=env_name
        )
        # Reserve before stopping: conflicts leave the current container intact.
        # A simultaneous rename moves this reservation with the environment.
        labels[ENV_HOSTNAME_LABEL] = allocate_hostname(
            _hostname_registry_path(team),
            env_name,
            0,  # An update does not consume a new environment slot.
            requested_hostname=requested_hostname,
            hostname_prefix=hostname_prefix,
            active_envs=active_envs,
            used_hostnames=used_hostnames,
        )
        labels[ENV_HOSTNAME_SOURCE_LABEL] = HOSTNAME_SOURCE_CUSTOM
        clear_hostname_after_update = False
    else:
        clear_hostname_after_update = _reconcile_environment_hostname_for_update(
            client, settings, team, env_name, labels
        )

    logger.info(
        "Updating environment – stopping old container",
        extra={"env_name": env_name, "container": odoo_container_name},
    )

    # ------------------------------------------------------------------
    # 2. Stop and remove ONLY the container
    # ------------------------------------------------------------------
    try:
        container.stop()
    except Exception:
        pass
    try:
        container.remove(v=True)
    except docker.errors.APIError:
        try:
            container.remove(v=True, force=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 2b. Rename: relocate the name-keyed state, then work under the new name
    # ------------------------------------------------------------------
    renamed_from = ""
    if rename_to:
        old_workspace = get_workspace_path(env_name, team.workspaces_dir)
        old_db_name = get_db_name(env_name, team.team_id)
        try:
            _relocate_environment_state(client, settings, team, env_name, rename_to)
        except Exception as exc:
            # The old container is already removed and cannot be brought back
            # from here; a bare ALTER DATABASE / rename error would hide that.
            # State exactly what the failed operation left behind.
            raise FlowError(
                f"Renaming '{env_name}' to '{rename_to}' failed after the old "
                f"container was already removed: {exc} The database and "
                f"workspace are intact under '{env_name}', but the environment "
                "currently has no container."
            ) from exc
        renamed_from = env_name
        # A move, not a re-clone: the checkout can hold the agent's uncommitted
        # work. Its .mcp.json still points at the old scoped URL and is
        # rewritten below, once the environment answers on the new name.
        if sync_agent_checkout:
            _agent_rename_env(client, settings, team, env_name, rename_to)
        env_name = rename_to
        odoo_container_name = get_resource_name(
            env_name, "odoo", settings.prefix, team.team_id
        )
        # The environment's identity in every listing (see list_environments).
        labels[settings.branch_label] = env_name
        new_db_name = get_db_name(env_name, team.team_id)
        volumes = _rebase_volumes(
            volumes,
            old_workspace,
            get_workspace_path(env_name, team.workspaces_dir),
            old_db_name,
            new_db_name,
        )
        if command and old_db_name != new_db_name:
            # The command is "odoo -d <db> ...", captured from the old container.
            command = command.replace(old_db_name, new_db_name)

    # ------------------------------------------------------------------
    # 3. Re-mount filestore overlay if needed (only for overlay-mode envs)
    # ------------------------------------------------------------------
    fs_paths = get_filestore_paths(env_name, team.workspaces_dir)
    merged = fs_paths["merged"]
    has_overlay_dirs = os.path.isdir(fs_paths["upper"])
    # ``os.path.isdir(merged)`` used to be part of this condition and silently
    # excluded the stale case, where it reports False (ENOTCONN) — the one
    # state that most needs a remount.
    if has_overlay_dirs and overlay_mount_state(merged) != MOUNT_ALIVE:
        env_db = get_db_name(env_name, team.team_id)
        template_name = labels.get("oduflow.template", "none")
        if template_name and template_name != "none":
            try:
                _tmp_vols: dict[str, Any] = {}
                _mount_filestore(
                    client,
                    settings,
                    team,
                    env_name,
                    env_db,
                    run_image,
                    _tmp_vols,
                    template_name=template_name,
                    force_overlay=True,
                )
            except Exception as exc:
                logger.warning("Could not re-mount filestore overlay: %s", exc)

    # Re-create PG role in case PG container was recreated
    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    if creds["pg_user"] != settings.db_user:
        env_db = get_db_name(env_name, team.team_id)
        try:
            _create_pg_role(
                client, settings, creds["pg_user"], creds["pg_password"], env_db
            )
        except Exception as exc:
            logger.warning("Could not re-create PG role: %s", exc)

    # Build the container environment from fresh credentials + user env, and
    # keep the persisted labels (image + env vars) in sync with the new config.
    env_dict = {
        "HOST": settings.shared_db_container,
        "USER": creds["pg_user"],
        "PASSWORD": creds["pg_password"],
        **resolved_user_env,
    }
    labels[settings.image_label] = odoo_image
    if user_env:
        labels["oduflow.env_vars"] = json.dumps(user_env)
    else:
        labels.pop("oduflow.env_vars", None)

    # Recompute the Traefik routing labels from the CURRENT settings instead of
    # copying the old container's: labels are frozen at creation, so this is what
    # lets a routing-mode / TLS / hostname switch be repaired by a plain
    # update_environment. Strip every stale ``traefik.*`` key first so a
    # traefik→port switch drops them and a renamed router does not linger.
    labels = {k: v for k, v in labels.items() if not k.startswith("traefik.")}
    route_hostname = labels.get(ENV_HOSTNAME_LABEL, "")
    _assert_env_hostname_available(settings, team, env_name, route_hostname)
    labels.update(build_env_traefik_labels(settings, team, env_name, route_hostname))
    if settings.routing_mode == "traefik":
        # No published port in traefik mode; drop any stale reservation left from
        # a previous port-mode life so the slot is reusable.
        release_port(team.port_registry_path, env_name)

    # Verify extra addons checkouts are intact. New environments use persistent
    # SHA-keyed shared paths; legacy environments keep per-workspace worktrees
    # until their next pull_and_apply migrates the mounts.
    extra_addons_json = labels.get("oduflow.extra_addons", "")
    if extra_addons_json:
        parsed = json.loads(extra_addons_json)
        extra_dict = _normalize_extra_addons(parsed)
        revisions = json.loads(labels.get("oduflow.extra_addons_revisions", "{}"))
        extra_dir = os.path.join(
            get_workspace_path(env_name, team.workspaces_dir), "extra"
        )
        for rn in extra_dict:
            revision = revisions.get(rn, "") if isinstance(revisions, dict) else ""
            wt = (
                os.path.join(team.shared_extra_checkouts_dir, rn, revision)
                if revision
                else os.path.join(extra_dir, rn)
            )
            if not os.path.isdir(wt):
                logger.warning("Extra addons checkout missing: %s", wt)

    # Switching into port mode (was traefik) or a legacy container that never
    # published a port: allocate one now, right before recreation, so any
    # earlier abort could not have leaked the reservation.
    if settings.routing_mode == "port" and host_port is None:
        used_ports = _get_used_ports(client, settings, team, exclude_env=env_name)
        host_port = allocate_port(
            team.port_registry_path,
            env_name,
            team.port_range_start,
            team.port_range_end,
            used_ports=used_ports,
        )
        port_newly_allocated = True

    # ------------------------------------------------------------------
    # 4. Re-create the container with the same settings
    # ------------------------------------------------------------------
    run_kwargs: dict[str, Any] = dict(
        image=run_image,
        name=odoo_container_name,
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        extra_hosts={"host.docker.internal": "host-gateway"},
        **default_env_limits(),
        environment=env_dict,
        labels=labels,
        volumes=volumes,
        restart_policy={"Name": "unless-stopped"},
    )
    if command:
        run_kwargs["command"] = command
    if settings.routing_mode == "port" and host_port is not None:
        run_kwargs["ports"] = {"8069/tcp": host_port}

    try:
        new_container = client.containers.run(**run_kwargs)
    except Exception:
        # The old container is already gone; at least don't leak a port slot we
        # freshly reserved for this recreate.
        if port_newly_allocated:
            release_port(team.port_registry_path, env_name)
        raise

    if clear_hostname_after_update:
        try:
            clear_hostname_assignment(
                _hostname_registry_path(team),
                env_name,
                retain_slot=team.environment_slots > 0,
            )
        except OSError as exc:
            # The serving container already has the correct route. Keep the
            # update successful and retry registry normalization next time.
            logger.warning(
                "Could not clear stale hostname assignment for '%s': %s",
                env_name,
                exc,
            )

    # Regenerate and copy odoo.conf into the new container (repo .oduflow/ takes
    # priority over the instance conf; extra-addons paths merged from labels).
    repo_path = labels.get("oduflow.local_path") or get_repo_path(
        env_name, team.workspaces_dir
    )
    conf_applied = _reapply_odoo_conf(settings, team, env_name, new_container)

    # ------------------------------------------------------------------
    # 5. Re-install apt packages and pip requirements
    # ------------------------------------------------------------------
    setup_logs: list[str] = []
    deps_installed = False
    if install_dependencies:
        dep_paths = [(repo_path, "/mnt/extra-addons")]
        try:
            extra_dict = _normalize_extra_addons(
                json.loads(labels.get("oduflow.extra_addons", "{}"))
            )
        except (json.JSONDecodeError, TypeError):
            extra_dict = {}
        if extra_dict:
            sources = _extra_checkout_sources(new_container, iter(extra_dict))
            dep_paths.extend(
                (sources[rn], f"/mnt/extra-addons-{rn}")
                for rn in extra_dict
                if sources.get(rn)
            )
        _, pip_installed, dep_logs = _install_repo_dependencies(
            new_container, dep_paths
        )
        setup_logs.extend(dep_logs)
        # A log from any repo means apt or pip did something worth restarting
        # for. This over-approximates only when a pip step failed outright, and
        # an extra restart is the harmless side of that trade.
        deps_installed = pip_installed or bool(dep_logs)

    # The recreated container booted Odoo before the conf was reapplied and the
    # dependencies reinstalled; one restart picks up both (same order as create).
    # Skipped when neither landed — nothing inside the fresh container changed
    # since it started, so a restart would only cost a registry reload.
    if conf_applied or deps_installed:
        new_container.restart()

    # ------------------------------------------------------------------
    # 5b. Point the agent checkout at the renamed environment
    # ------------------------------------------------------------------
    if renamed_from and sync_agent_checkout:
        # The checkout itself was moved above; this rewrites its .mcp.json with
        # the scoped MCP URL, which carries the environment name in its path.
        # Live-mounted environments have no repo to clone from (clone-env.sh
        # skips on an empty URL).
        _agent_add_env(
            client,
            settings,
            team,
            env_name,
            ""
            if labels.get("oduflow.local_path")
            else labels.get(settings.repo_label, ""),
            labels.get("oduflow.git_branch", renamed_from),
            labels.get("oduflow.git_user", ""),
        )

    # ------------------------------------------------------------------
    # 6. Build URL and return result
    # ------------------------------------------------------------------
    if settings.routing_mode == "traefik":
        url = f"{settings.public_scheme_for(team)}://{env_hostname(team, env_name, route_hostname)}"
    else:
        url = f"{settings.public_scheme_for(team)}://{team.hostname}:{host_port}"

    env_db = get_db_name(env_name, team.team_id)
    workspace = get_workspace_path(env_name, team.workspaces_dir)
    logger.info(
        "Environment updated",
        extra={"env_name": env_name, "url": url, "container": odoo_container_name},
    )

    return {
        "url": url,
        "env_name": env_name,
        "renamed_from": renamed_from,
        "hostname": get_env_short_hostname(env_name, route_hostname),
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace,
        "image": odoo_image,
        "image_updated": image_updated,
        "env_vars": user_env,
        "setup_logs": setup_logs,
    }
