"""Separate production Bearer credentials and MCP surface from developer access."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

from oduflow.settings import Settings

PRODUCTION_SCOPE = "oduflow_production"
SHARED_TOOLS = frozenset({"read_output"})
_HTTP_ACTIVE: ContextVar[bool] = ContextVar("production_http_active", default=False)
_PRODUCTION_ROUTE: ContextVar[bool] = ContextVar("production_route", default=False)

# Explicit surfaces: adding a new tool does not automatically grant prod access.
PRODUCTION_TOOLS = frozenset(
    {
        "create_production",
        "list_productions",
        "get_production_info",
        "production_logs",
        "start_production",
        "stop_production",
        "restart_production",
        "set_production_auto_update",
        "reconfigure_production",
        "set_production_odoo_conf",
        "update_production",
        "rollback_production",
        "production_deploys",
        "snapshot_production",
        "list_production_snapshots",
        "restore_production",
        "production_backup_status",
        "production_wal_status",
        "control_production_wal",
        "set_production_backup_schedule",
        "prune_production_backups",
        "restore_cluster_pitr",
        "delete_production",
        "save_production_as_template",
        "sync_production_mcp",
        "production_odoo_info",
        "production_odoo_read",
        "production_odoo_preview_change",
        "production_odoo_change_status",
        "production_odoo_execute_change",
    }
)


def require_production_access() -> None:
    """Local CLI is owner access; HTTP requires the separate production route/key."""
    if not _HTTP_ACTIVE.get():
        return
    token = get_access_token()
    if (
        not _PRODUCTION_ROUTE.get()
        or token is None
        or PRODUCTION_SCOPE not in token.scopes
    ):
        raise ToolError(
            "Production access requires /production and the team's production_token."
        )


def _reject_production_service_database(name: str) -> None:
    """Keep prod-cluster service databases off the development surface.

    The team is resolved exactly as the tool will resolve it, so the gate also
    holds where no access token exists at all: stdio, and HTTP with
    allow_insecure_http. A record that cannot be read is not a production
    database — leave it to the tool, whose errors pass through
    ``handle_errors`` and stay actionable under mask_error_details.
    """
    from oduflow.errors import FlowError
    from oduflow.server import _resolve_team
    from oduflow.service_database_credentials import load

    try:
        record = load(_resolve_team(None), name)
    except (FlowError, ValueError):
        return
    if record.get("cluster") == "prod":
        raise ToolError("Production service databases require administrator access.")


class ProductionASGI:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        production = path.rstrip("/") == "/production"
        if production:
            scope = dict(scope, path="/mcp", raw_path=b"/mcp")
        active = _HTTP_ACTIVE.set(True)
        route = _PRODUCTION_ROUTE.set(production)
        try:
            await self.app(scope, receive, send)
        finally:
            _PRODUCTION_ROUTE.reset(route)
            _HTTP_ACTIVE.reset(active)


class ProductionAccessMiddleware(Middleware):
    def __init__(self, settings_getter: Callable[[], Settings]) -> None:
        self._settings = settings_getter

    def _production(self) -> bool:
        token = get_access_token()
        is_prod = token is not None and PRODUCTION_SCOPE in token.scopes
        if _PRODUCTION_ROUTE.get() != is_prod:
            raise ToolError(
                "Use the matching MCP endpoint and credential: /mcp for development, /production for production."
            )
        if is_prod:
            assert token is not None
            team = self._settings().get_team_by_production_token(token.token)
            if team is None or team.team_id != token.client_id:
                raise ToolError("Production credential is no longer valid.")
        return is_prod

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        production = self._production()
        tools = await call_next(context)
        # No dynamic hiding based on production.enabled or existing deployments.
        return [
            tool
            for tool in tools
            if tool.name in SHARED_TOOLS
            or (tool.name in PRODUCTION_TOOLS) == production
        ]

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        production = self._production()
        name = context.message.name
        if name not in SHARED_TOOLS and (name in PRODUCTION_TOOLS) != production:
            raise ToolError(
                "This tool is not available with this development/production credential."
            )
        if (
            production
            and name in {"restore_cluster_pitr", "control_production_wal"}
            and len(self._settings().teams) > 1
        ):
            raise ToolError(
                "This operation affects every team's shared production cluster. Use the owner CLI or dashboard on a multi-team server."
            )
        if not production:
            args = context.message.arguments or {}
            from oduflow.naming import slugify_branch

            if name in {
                "get_service_database",
                "rotate_service_database_password",
                "delete_service_database",
            }:
                _reject_production_service_database(str(args.get("name", "")))
            if (
                slugify_branch(str(args.get("env_name", ""))).startswith("prod-")
                or args.get("cluster") == "prod"
            ):
                raise ToolError(
                    "Production access requires /production and a production credential."
                )
        return await call_next(context)


def check_cached_output_access(production: bool, team_id: str) -> None:
    if not _HTTP_ACTIVE.get():
        return
    if production != _PRODUCTION_ROUTE.get():
        raise ToolError("Cached output belongs to a different access scope.")
    if production:
        require_production_access()
        token = get_access_token()
        if token is None or token.client_id != team_id:
            raise ToolError("Cached output belongs to another team.")
