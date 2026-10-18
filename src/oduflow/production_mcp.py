"""Production OduMCP provisioning and policy-governed HTTP access."""

from __future__ import annotations

import ast
import io
import json
import logging
import shlex
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from oduflow import branding, production_registry
from oduflow.errors import FlowError, PrerequisiteNotMetError
from oduflow.naming import get_repo_path, get_workspace_path, prod_env_name
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

MOUNT = "/mnt/oduflow-addons"
MAX_RESPONSE_BYTES = 4_000_000
READ_OPERATIONS = frozenset(
    {
        "models.list",
        "models.describe",
        "records.search",
        "records.read",
        "records.count",
        "records.aggregate",
        "attachments.read",
        "reports.render",
    }
)


# Where the connector repository keeps the addon, relative to its root.
_MODULE = Path("addons", "odumcp")
_MANIFEST = _MODULE / "__manifest__.py"


def addon_checkout(team: TeamSettings, name: str) -> Path:
    return (
        Path(get_workspace_path(prod_env_name(name), team.workspaces_dir))
        / "odumcp-addons"
    )


def managed_module(team: TeamSettings, name: str) -> Path:
    """The managed checkout's odumcp module, mounted read-only at MOUNT."""
    return addon_checkout(team, name) / _MODULE


def _addons_root(repo: Path) -> Path:
    """The single directory Odoo scans for a checkout.

    Mirrors :func:`resolve_main_addons_path` / :func:`resolve_extra_addons_path`:
    a top-level ``addons/`` directory wins, otherwise the repo root. Odoo scans
    addons_path non-recursively, so a module in the other directory is invisible.
    """
    nested = repo / "addons"
    return nested if nested.is_dir() else repo


# Oldest Odoo series the connector repository publishes odumcp for.
MIN_ODOO_MAJOR = 16


class UnsupportedOdooVersion(PrerequisiteNotMetError):
    """No odumcp exists for this Odoo version; synchronizing cannot help."""


def odoo_major(
    settings: Settings, odoo_image: str, container: Any | None = None
) -> int:
    """The production's Odoo series, which selects the odumcp branch.

    The image reference answers for official and versioned tags. A custom tag
    without a version is asked directly: the running container when there is
    one, otherwise a throwaway container of the same (already pulled) image.
    """
    from oduflow.docker_ops.client import get_client, run_for_output
    from oduflow.odoo_version import (
        detect_odoo_major,
        major_from_image_reference,
        major_from_version_output,
    )

    major = major_from_image_reference(odoo_image)
    if major is None and container is not None:
        major = detect_odoo_major(container, settings.image_label)
    if major is None and odoo_image:
        try:
            output = run_for_output(
                get_client(),
                odoo_image,
                ["--version"],
                entrypoint="odoo",
                network_disabled=True,
            )
            major = major_from_version_output(output.decode("utf-8", "replace"))
        except Exception:
            logger.warning(
                "Could not read the Odoo version of image %s", odoo_image, exc_info=True
            )
    if major is None:
        raise PrerequisiteNotMetError(
            f"Cannot determine the Odoo version of image '{odoo_image}', so the "
            "matching OduMCP addon cannot be selected."
        )
    if major < MIN_ODOO_MAJOR:
        raise UnsupportedOdooVersion(
            f"Automatic OduMCP provisioning supports Odoo {MIN_ODOO_MAJOR} and "
            f"later; this production runs Odoo {major}."
        )
    return major


def _series_major(version: Any) -> int | None:
    """``19.0.1.4.2`` → 19; ``None`` for a version without a series prefix."""
    from oduflow.module_versions import odoo_series

    series = odoo_series(str(version or ""))
    return int(series.split(".")[0]) if series else None


def _checkout_major(checkout: Path) -> int | None:
    """Odoo series of a managed checkout, from its odumcp manifest version."""
    from oduflow.git_ops import parse_manifest

    try:
        return _series_major(parse_manifest(str(checkout / _MANIFEST)).get("version"))
    except Exception:
        return None


def _mounted_major(container: Any) -> int | None:
    """Odoo series of the odumcp the container actually sees at MOUNT.

    ``None`` when nothing readable is mounted there, e.g. the bind mount still
    points at a checkout that was replaced and removed on the host.
    """
    try:
        code, output = container.exec_run(
            ["cat", f"{MOUNT}/odumcp/__manifest__.py"], user="root"
        )
        if code != 0:
            return None
        text = output.decode("utf-8", "replace") if isinstance(output, bytes) else ""
        return _series_major(ast.literal_eval(text).get("version"))
    except Exception:
        return None


def _has_managed_mount(container: Any) -> bool:
    mounts = (getattr(container, "attrs", None) or {}).get("Mounts") or []
    return any(mount.get("Destination") == MOUNT + "/odumcp" for mount in mounts)


def _record_status(team: TeamSettings, name: str, status: str) -> None:
    try:
        production_registry.update_production(team, name, {"mcp": {"status": status}})
    except Exception:
        # Bookkeeping must never replace the real failure: the production
        # can disappear between the call and this handler.
        logger.warning(
            "Could not record OduMCP status '%s' for '%s'", status, name, exc_info=True
        )


@dataclass(frozen=True)
class AddonPreparation:
    """Outcome of :func:`prepare_addon`.

    ``managed`` tells where odumcp comes from: Oduflow's managed checkout, or
    the client's own repositories (main or extra), which Oduflow never touches.
    ``action`` is what happened to the managed checkout:

    - ``"repository"``: the client repository provides odumcp; no checkout.
    - ``"removed"``: the client repository provides odumcp now, so the stale
      managed checkout was removed and its mount must go.
    - ``"kept"``: the existing checkout still serves (same Odoo series).
    - ``"cloned"``: a first checkout was published.
    - ``"replaced"``: the checkout was replaced for another Odoo series.
    - ``"refreshed"``: the checkout was re-cloned on explicit request.

    ``major`` is the Odoo series the managed checkout targets (``None`` when
    there is no managed checkout or its version is unknown).
    """

    managed: bool
    action: str
    major: int | None = None

    @property
    def recreate(self) -> bool:
        """A running container must be recreated: what it mounts changed."""
        return self.action in ("cloned", "replaced", "refreshed", "removed")


def _retire_checkout(checkout: Path, temporary: Path) -> None:
    """Take *checkout* out of the addons path atomically; *temporary* (a
    directory deleted by the caller) takes it away.

    The rename makes the removal atomic for the host: no half-deleted module
    is ever visible at the checkout path. It does not shield a running
    container: its bind mount follows the inode, so the files vanish from the
    mount when the temporary directory is deleted. Every caller therefore
    recreates the container (or has it recreated) afterwards.
    """
    checkout.rename(temporary / "previous")


def extra_worktree_paths(
    team: TeamSettings, name: str, record: dict[str, Any] | None = None
) -> list[str]:
    """Host paths of the production's extra-addon worktrees, from its record."""
    if record is None:
        record = production_registry.get_production(team, name)
    workspace = Path(get_workspace_path(prod_env_name(name), team.workspaces_dir))
    return [
        str(workspace / "extra" / repo) for repo in record.get("extra_addons") or {}
    ]


def repository_provides_addon(
    team: TeamSettings, name: str, extra_host_paths: Iterable[str] | None = None
) -> bool:
    """Whether the client's own repositories ship odumcp where Odoo scans.

    The single probe behind every decision about the managed copy: whether to
    clone it, whether to mount it, whether to retire it. *extra_host_paths*
    are the extra-addon worktrees; by default those the registry record lists.
    """
    main = Path(get_repo_path(prod_env_name(name), team.workspaces_dir))
    if extra_host_paths is None:
        extra_host_paths = extra_worktree_paths(team, name)
    roots = [main, *(Path(host) for host in extra_host_paths)]
    return any(
        (_addons_root(root) / "odumcp" / "__manifest__.py").is_file() for root in roots
    )


def mounts_managed_module(
    team: TeamSettings, name: str, extra_host_paths: Iterable[str] | None = None
) -> bool:
    """Whether a container built now mounts the managed odumcp copy.

    Decided by repository state, not by the checkout's mere presence: once the
    client repositories ship the module, any recreate drops the mount even
    before the checkout itself is retired.
    """
    return (
        managed_module(team, name) / "__manifest__.py"
    ).is_file() and not repository_provides_addon(team, name, extra_host_paths)


def retire_managed_checkout(team: TeamSettings, name: str) -> bool:
    """Remove the managed checkout atomically; False when there was none.

    Callers make sure no running container still mounts it (see
    :func:`_retire_checkout`).
    """
    checkout = addon_checkout(team, name)
    if not checkout.exists():
        return False
    logger.info(
        "Removing the managed OduMCP checkout of '%s': the production "
        "repository provides odumcp",
        name,
    )
    with tempfile.TemporaryDirectory(
        prefix=".odumcp-retired-", dir=checkout.parent
    ) as temporary:
        _retire_checkout(checkout, Path(temporary))
    return True


def prepare_addon(
    settings: Settings,
    team: TeamSettings,
    name: str,
    extra_paths: list[tuple[str, str]],
    odoo_image: str,
    container: Any | None = None,
    *,
    refresh: bool = False,
) -> AddonPreparation:
    """Make odumcp available to the production, or step aside when the
    client ships it.

    Oduflow ships odumcp as a managed component by default: without the
    module in the main or extra repositories, the connector repository is
    cloned at the branch of the production's Odoo series (``<major>.0``, see
    :func:`odoo_major`) and only its odumcp addon is mounted, not the other
    addons of that repository. A client takes the module over by committing
    it into their own repository: the managed checkout is then removed and
    Oduflow never touches the module again.

    The managed checkout is never silently updated. It is replaced when it
    was made for another Odoo series (the production was upgraded since), or
    on an explicit ``refresh``, which re-clones the same series. Raises
    UnsupportedOdooVersion before Odoo 16.
    """
    checkout = addon_checkout(team, name)
    if repository_provides_addon(team, name, [host for host, _ in extra_paths]):
        # The client took the module over. The repository path comes first in
        # addons_path, so the managed copy shadows nothing, but it must not
        # linger there nor be mounted into the next container.
        if retire_managed_checkout(team, name):
            return AddonPreparation(managed=False, action="removed")
        return AddonPreparation(managed=False, action="repository")
    from oduflow.docker_ops.env_ops import _clone_repo

    current = (checkout / _MANIFEST).is_file()
    try:
        major = odoo_major(settings, odoo_image, container)
    except UnsupportedOdooVersion:
        raise
    except PrerequisiteNotMetError:
        if not current or refresh:
            # Nothing to serve, or an explicit refresh that cannot pick a
            # connector branch blindly.
            raise
        # Without a readable version the existing checkout keeps serving.
        logger.warning(
            "Keeping the OduMCP checkout of '%s': the Odoo version of image %s "
            "is unknown",
            name,
            odoo_image,
        )
        return AddonPreparation(
            managed=True, action="kept", major=_checkout_major(checkout)
        )
    same_series = current and _checkout_major(checkout) in (None, major)
    if same_series and not refresh:
        return AddonPreparation(managed=True, action="kept", major=major)
    action = "refreshed" if same_series else ("replaced" if current else "cloned")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a complete checkout: a failed download must not poison
    # production's addons path or prevent a subsequent synchronization.
    with tempfile.TemporaryDirectory(
        prefix=".odumcp-download-", dir=checkout.parent
    ) as temporary:
        source = Path(temporary) / "repo"
        _clone_repo(
            settings.prod_odumcp_repo_url,
            f"{major}.0",
            str(source),
            team,
            depth=1,
            timeout=300,
        )
        if not (source / _MANIFEST).is_file():
            raise PrerequisiteNotMetError(
                "Configured OduMCP repository does not contain addons/odumcp."
            )
        if checkout.exists():
            # Retired with the temporary directory.
            _retire_checkout(checkout, Path(temporary))
        source.rename(checkout)
    return AddonPreparation(managed=True, action=action, major=major)


def checkout_commit(checkout: Path) -> str | None:
    """HEAD of the managed checkout; ``None`` when it is not a Git clone."""
    from oduflow.git_ops import rev_parse

    try:
        return rev_parse(str(checkout))
    except Exception:
        return None


_INSTALL = """
Module = env["ir.module.module"]
Module.update_list()
module = Module.search([("name", "=", "odumcp")], limit=1)
if not module:
    raise RuntimeError("odumcp is missing from the production addons path")
changed = False
if module.state != "installed":
    module.button_immediate_install()
    changed = True
elif payload.get("upgrade") or module.installed_version != module.latest_version:
    # A refreshed checkout may carry data/view changes without a version bump.
    module.button_immediate_upgrade()
    changed = True
env.cr.commit()
print("ODUFLOW_MCP_RESULT=" + json.dumps({"installed": True, "module_changed": changed}))
"""
_CONFIGURE = """
if not hasattr(env["res.users.apikeys"], "_set_oduflow_key"):
    raise RuntimeError("Upgrade odumcp to a release supporting Oduflow managed keys")
result = env["res.users.apikeys"]._set_oduflow_key(payload["key"])
env.cr.commit()
print("ODUFLOW_MCP_RESULT=" + json.dumps(result))
"""


def _shell(
    settings: Settings,
    team: TeamSettings,
    name: str,
    container: Any,
    script: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Fixed internal scripts only. Secrets are private temporary files, never argv."""
    from oduflow.docker_ops.client import get_client, get_odoo_uid_gid
    from oduflow.docker_ops.production_ops import prod_db_name
    from oduflow.env_credentials import load_credentials

    creds = load_credentials(
        prod_env_name(name),
        team.workspaces_dir,
        settings.db_user,
        settings.db_password,
        allow_fallback=False,
    )
    uid, gid = map(
        int,
        get_odoo_uid_gid(get_client(), container.labels[settings.image_label]).split(
            ":"
        ),
    )
    stem = "oduflow-mcp-" + uuid.uuid4().hex
    script_path, payload_path = f"/tmp/{stem}.py", f"/tmp/{stem}.json"
    source = (
        "import json\nwith open("
        + repr(payload_path)
        + ") as source:\n    payload = json.load(source)\n"
        + script
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for path, data in (
            (script_path, source.encode()),
            (payload_path, json.dumps(payload).encode()),
        ):
            info = tarfile.TarInfo(Path(path).name)
            info.size, info.mode, info.uid, info.gid = len(data), 0o600, uid, gid
            tar.addfile(info, io.BytesIO(data))
    archive.seek(0)
    try:
        container.put_archive("/tmp", archive)
        argv = [
            "odoo",
            "shell",
            "--no-http",
            "--stop-after-init",
            "--workers=0",
            "--max-cron-threads=0",
            "--db_host=" + settings.prod_db_container,
            "-r",
            creds["pg_user"],
            "-d",
            prod_db_name(team, name),
        ]
        code, output = container.exec_run(
            ["sh", "-c", shlex.join(argv) + " < " + shlex.quote(script_path)],
            user="odoo",
            environment={"PGPASSWORD": creds["pg_password"]},
        )
        text = (
            output.decode("utf-8", errors="replace")
            if isinstance(output, bytes)
            else str(output)
        )
        if code == 0:
            for line in reversed(text.splitlines()):
                if line.startswith("ODUFLOW_MCP_RESULT="):
                    result = json.loads(line.partition("=")[2])
                    if isinstance(result, dict):
                        return result
        # Do not expose arbitrary Odoo logs: custom addons can print payloads.
        raise PrerequisiteNotMetError(
            "OduMCP provisioning failed. Check production logs, addon compatibility, "
            "and that the deployed odumcp supports managed Oduflow keys."
        )
    finally:
        container.exec_run(["rm", "-f", script_path, payload_path], user="root")


def provision(
    settings: Settings, team: TeamSettings, name: str, *, upgrade: bool = False
) -> dict[str, Any]:
    """Install odumcp when missing, upgrade it when its version changed, and
    set the production key. ``upgrade`` forces the module upgrade even at an
    unchanged version: after a refreshed checkout, the code on disk changed
    without a manifest bump being guaranteed.
    """
    from oduflow.docker_ops import production_ops
    from oduflow.docker_ops.client import get_client
    from oduflow.wal_monitor import assert_writable

    if not team.production_token:
        raise PrerequisiteNotMetError(
            "Set this team's production_token before provisioning OduMCP."
        )
    assert_writable(settings)
    production_registry.get_production(team, name)
    container = production_ops._get_container(get_client(), settings, team, name)
    if container is None:
        raise PrerequisiteNotMetError(
            "Production container is missing; recreate it before synchronizing OduMCP."
        )
    container.reload()
    if container.status != "running":
        raise PrerequisiteNotMetError(
            "Start the production before synchronizing OduMCP."
        )
    try:
        if _has_managed_mount(container):
            # Never install an addon built for another Odoo series, e.g.
            # after the image changed without a synchronization.
            from oduflow.odoo_version import detect_odoo_major

            mounted = _mounted_major(container)
            running = detect_odoo_major(container, settings.image_label)
            if mounted and running and mounted != running:
                raise PrerequisiteNotMetError(
                    f"The mounted OduMCP addon is for Odoo {mounted}, but this "
                    f"production runs Odoo {running}. Run sync_production_mcp "
                    "to fetch the matching addon."
                )
        installed = _shell(
            settings, team, name, container, _INSTALL, {"upgrade": upgrade}
        )
        if installed["module_changed"]:
            container.restart()
        result = _shell(
            settings, team, name, container, _CONFIGURE, {"key": team.production_token}
        )
        production_registry.update_production(team, name, {"mcp": {"status": "ready"}})
        return {"name": name, "status": "ready", **installed, **result}
    except Exception:
        _record_status(team, name, "sync_failed")
        raise


def execute(
    settings: Settings,
    team: TeamSettings,
    name: str,
    operation: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """No write retries: after an uncertain execute, retrieve changes.status."""
    from oduflow.docker_ops.production_ops import prod_url

    if not team.production_token:
        raise PrerequisiteNotMetError(
            "Production MCP is not configured: set production_token."
        )
    record = production_registry.get_production(team, name)
    status = (record.get("mcp") or {}).get("status")
    if status == "unsupported":
        raise PrerequisiteNotMetError(
            "Production OduMCP is not available: automatic provisioning supports "
            f"Odoo {MIN_ODOO_MAJOR} and later. Production infrastructure tools "
            "remain available."
        )
    if status == "sync_failed":
        raise PrerequisiteNotMetError(
            "Production OduMCP is unavailable: addon installation or key setup "
            "did not complete. Retry sync_production_mcp for this production. "
            "Production infrastructure tools remain available."
        )
    request_id = str(uuid.uuid4())
    try:
        with httpx.Client(
            timeout=httpx.Timeout(60, connect=10),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                prod_url(settings, team, record).rstrip("/") + "/odumcp/v1/execute",
                headers={
                    "Authorization": "Bearer " + team.production_token,
                    "X-Request-ID": request_id,
                    # Lands in the client's production access log.
                    "User-Agent": f"{branding.current().slug}-production",
                },
                json={"operation": operation, "params": params or {}},
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise FlowError(
                            "OduMCP response exceeds 4 MB; narrow the query. For changes, check approval status."
                        )
                if response.is_redirect:
                    raise FlowError(
                        "Production OduMCP redirected the request; check its configured domain."
                    )
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise FlowError(
                        "Production did not return an OduMCP response; verify module installation and routing."
                    ) from None
                if not isinstance(body, dict):
                    raise FlowError("Invalid OduMCP response.")
                if response.status_code >= 400 or body.get("ok") is not True:
                    error = body.get("error") or {}
                    # Preserve field/policy error details without turning a failed call into success.
                    raise FlowError("OduMCP: " + json.dumps(error, ensure_ascii=False))
                data = body.get("data")
                if not isinstance(data, dict):
                    raise FlowError("Invalid OduMCP response data.")
                if data.get("state") == "pending":
                    data["next_step"] = (
                        "Approve in Odoo, then call production_odoo_execute_change with this approval_id."
                    )
                return {
                    "production": name,
                    "request_id": body.get("request_id", request_id),
                    **data,
                }
    except httpx.TransportError:
        raise FlowError(
            "Cannot reach production OduMCP. Execution outcome may be unknown; check changes.status before resubmitting a change."
        ) from None


def synchronize(
    settings: Settings, team: TeamSettings, name: str, *, refresh: bool = False
) -> dict[str, Any]:
    """Adopt an existing production; the caller holds its production lock.

    ``refresh`` re-clones the managed checkout from the connector repository
    even when its Odoo series is unchanged — the only way the managed module
    is ever updated. It is a documented no-op for a module the client ships
    in their own repository, which Oduflow does not touch.
    """
    from oduflow.docker_ops import production_ops
    from oduflow.docker_ops.client import get_client

    record = production_registry.get_production(team, name)
    if not team.production_token:
        raise PrerequisiteNotMetError("Set this team's production_token first.")
    container = production_ops._get_container(get_client(), settings, team, name)
    if container is None:
        raise PrerequisiteNotMetError("Production container is missing.")
    container.reload()
    if container.status != "running":
        raise PrerequisiteNotMetError(
            "Start the production before synchronizing OduMCP."
        )
    workspace = Path(get_workspace_path(prod_env_name(name), team.workspaces_dir))
    extras = [
        (str(workspace / "extra" / repo), "") for repo in record.get("extra_addons", {})
    ]
    try:
        prepared = prepare_addon(
            settings,
            team,
            name,
            extras,
            record.get("odoo_image", ""),
            container,
            refresh=refresh,
        )
    except UnsupportedOdooVersion:
        _record_status(team, name, "unsupported")
        raise
    checkout = addon_checkout(team, name)
    recreate = False
    if prepared.managed and checkout.is_dir():
        # A replaced or refreshed checkout is a new directory: the running
        # container still sees the old one through its bind mount. Comparing
        # what the container sees also repairs a previous synchronization
        # whose container recreation failed.
        recreate = (
            prepared.recreate
            or not _has_managed_mount(container)
            or _mounted_major(container) != _checkout_major(checkout)
        )
    elif not prepared.managed:
        # The client repository provides odumcp: a managed mount (of the
        # checkout removed just now, or by a synchronization whose recreation
        # failed) must disappear from the container. What the container
        # mounts decides, not the removal itself: a checkout the container
        # never mounted (its recreation failed) needs no downtime now.
        recreate = _has_managed_mount(container)
    if recreate:
        production_ops.reconfigure_production(settings, team, name, force_recreate=True)
    # A refreshed checkout is re-cloned code of the same series; the manifest
    # version need not have moved, so the upgrade is forced.
    result = provision(settings, team, name, upgrade=prepared.action == "refreshed")
    result["addon_source"] = "managed" if prepared.managed else "repository"
    result["checkout"] = prepared.action
    result["refreshed"] = prepared.action == "refreshed"
    result["container_recreated"] = recreate
    if prepared.managed:
        result["connector_branch"] = (
            f"{prepared.major}.0" if prepared.major is not None else None
        )
        result["connector_commit"] = checkout_commit(checkout)
    elif refresh:
        result["note"] = (
            "odumcp comes from the production repository, which Oduflow does "
            "not touch: nothing was refreshed. Update the module by deploying "
            "your repository."
        )
    return result
