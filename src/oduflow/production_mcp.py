"""Production OduMCP provisioning and policy-governed HTTP access."""

from __future__ import annotations

import io
import json
import logging
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx

from oduflow import production_registry
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


def addon_checkout(team: TeamSettings, name: str) -> Path:
    return (
        Path(get_workspace_path(prod_env_name(name), team.workspaces_dir))
        / "odumcp-addons"
    )


def _addons_root(repo: Path) -> Path:
    """The single directory Odoo scans for a checkout.

    Mirrors :func:`resolve_main_addons_path` / :func:`resolve_extra_addons_path`:
    a top-level ``addons/`` directory wins, otherwise the repo root. Odoo scans
    addons_path non-recursively, so a module in the other directory is invisible.
    """
    nested = repo / "addons"
    return nested if nested.is_dir() else repo


def prepare_addon(
    settings: Settings,
    team: TeamSettings,
    name: str,
    extra_paths: list[tuple[str, str]],
) -> None:
    """Use existing addon code when provided; otherwise fetch the configured release.

    Only odumcp is mounted from the fallback repository, not its other addons.
    Reconfiguration reuses this checkout; it never silently pulls newer code.
    """
    main = Path(get_repo_path(prod_env_name(name), team.workspaces_dir))
    roots = [main, *(Path(host) for host, _ in extra_paths)]
    if any(
        (_addons_root(root) / "odumcp" / "__manifest__.py").is_file() for root in roots
    ):
        return
    from oduflow.docker_ops.env_ops import _clone_repo

    checkout = addon_checkout(team, name)
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        # Publish only a complete checkout: a failed download must not poison
        # production's addons path or prevent a subsequent synchronization.
        with tempfile.TemporaryDirectory(
            prefix=".odumcp-download-", dir=checkout.parent
        ) as temporary:
            source = Path(temporary) / "repo"
            _clone_repo(
                settings.prod_odumcp_repo_url,
                settings.prod_odumcp_ref,
                str(source),
                team,
                depth=1,
                timeout=300,
            )
            if not (source / "addons/odumcp/__manifest__.py").is_file():
                raise PrerequisiteNotMetError(
                    "Configured OduMCP repository does not contain addons/odumcp."
                )
            source.rename(checkout)
    if not (checkout / "addons/odumcp/__manifest__.py").is_file():
        raise PrerequisiteNotMetError(
            "Configured OduMCP repository does not contain addons/odumcp."
        )


_INSTALL = """
import odoo
if odoo.release.version_info[0] != 19:
    raise RuntimeError("Automatic OduMCP provisioning currently supports Odoo 19 only")
Module = env["ir.module.module"]
Module.update_list()
module = Module.search([("name", "=", "odumcp")], limit=1)
if not module:
    raise RuntimeError("odumcp is missing from the production addons path")
changed = False
if module.state != "installed":
    module.button_immediate_install()
    changed = True
elif module.installed_version != module.latest_version:
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
            "OduMCP provisioning failed. Check production logs, Odoo 19 compatibility, "
            "and that the deployed odumcp supports managed Oduflow keys."
        )
    finally:
        container.exec_run(["rm", "-f", script_path, payload_path], user="root")


def provision(settings: Settings, team: TeamSettings, name: str) -> dict[str, Any]:
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
        installed = _shell(settings, team, name, container, _INSTALL, {})
        if installed["module_changed"]:
            container.restart()
        result = _shell(
            settings, team, name, container, _CONFIGURE, {"key": team.production_token}
        )
        production_registry.update_production(team, name, {"mcp": {"status": "ready"}})
        return {"name": name, "status": "ready", **installed, **result}
    except Exception:
        try:
            production_registry.update_production(
                team, name, {"mcp": {"status": "sync_failed"}}
            )
        except Exception:
            # Bookkeeping must never replace the real failure: the production
            # can disappear between the call and this handler.
            logger.warning(
                "Could not record OduMCP sync failure for '%s'", name, exc_info=True
            )
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
    if record.get("mcp", {}).get("status") == "sync_failed":
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
                    "User-Agent": "oduflow-production",
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


def synchronize(settings: Settings, team: TeamSettings, name: str) -> dict[str, Any]:
    """Adopt an existing production; the caller holds its production lock."""
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
    prepare_addon(settings, team, name, extras)
    if addon_checkout(team, name).is_dir():
        mounts = container.attrs.get("Mounts", [])
        if not any(mount.get("Destination") == MOUNT + "/odumcp" for mount in mounts):
            production_ops.reconfigure_production(
                settings, team, name, force_recreate=True
            )
    return provision(settings, team, name)
