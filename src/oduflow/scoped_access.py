"""Scoped single-environment MCP access (``/mcp/<env>``).

The same FastMCP server serves both the full ``/mcp`` endpoint and a scoped
``/mcp/<env>`` endpoint that exposes only the tools needed to work *inside* one
environment. Three pieces cooperate:

- :class:`ScopedEnvASGI` — an outer ASGI shim that recognises ``/mcp/<env>``,
  stashes the env in the request scope, and rewrites the path to the canonical
  ``/mcp`` route so the existing streamable transport + auth handle it.
- :class:`OduflowTokenVerifier` — a Bearer verifier that accepts both team
  ``auth_token`` values (full access) and per-environment tokens (scoped), the
  latter carrying the env in an ``oduflow_env:<env>`` scope.
- :class:`ScopedAccessMiddleware` — a FastMCP middleware enforcing, default-deny,
  which tools are visible/callable and injecting the resolved ``env_name``.

Access policy (see :func:`decide`):

============== ================== ============================
Token          URL ``/mcp``       URL ``/mcp/<env>``
============== ================== ============================
team token     full (unchanged)   scoped to ``<env>``
env-A token    deny               scoped iff ``<env> == A``
============== ================== ============================
"""

from __future__ import annotations

import copy
import logging
from contextvars import ContextVar
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from oduflow import env_tokens
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

# ASGI scope key holding the env parsed from a /mcp/<env> URL.
SCOPE_KEY = "oduflow_scoped_env"
# Prefix of the access-token scope that binds a per-env token to its env.
ENV_SCOPE_PREFIX = "oduflow_env:"
_HTTP_REQUEST_ACTIVE: ContextVar[bool] = ContextVar(
    "oduflow_http_request_active", default=False
)
_HTTP_URL_ENV: ContextVar[str | None] = ContextVar("oduflow_http_url_env", default=None)

# Tools exposed on the scoped endpoint: full dev loop + restart, read-only and
# diagnostics. Everything else (create/delete/stop/start/update/recreate,
# list_environments, templates, services, volumes, extra repos, repo auth,
# save/import template) is denied — default-deny, not enumerated here.
SCOPED_ALLOWLIST = frozenset(
    {
        "get_agent_instructions",
        "get_odoo_development_guide",
        "report_issue",
        # Present only while the hidden [server] agent_feedback option is on;
        # otherwise the tool is disabled and never reaches this allowlist.
        "submit_agent_feedback",
        "read_output",
        "get_environment_info",
        "get_environment_logs",
        "pull_and_apply",
        "install_odoo_modules",
        "upgrade_odoo_modules",
        "export_module_translations",
        "translation_status",
        "run_odoo_tests",
        "run_odoo_shell",
        "run_odoo_command",
        "run_db_query",
        "odoo_search_read",
        "odoo_create",
        "odoo_write",
        "odoo_unlink",
        "odoo_call",
        "odoo_schema",
        "reset_admin_password",
        "connect_as_user",
        "write_file_in_odoo",
        "read_file_in_odoo",
        "search_in_odoo",
        "http_request_to_odoo",
        "list_installed_modules",
        "restart_environment",
        # Image builds: available only when the team has [team.X.image_registry]
        # configured; otherwise the tools raise a prerequisite error.
        "start_image_build",
        "get_image_build",
        "publish_image_build",
        "cancel_image_build",
    }
)


# -- Pure helpers (unit-tested directly) --


def decide(url_env: str | None, token_env: str | None) -> tuple[str, str | None]:
    """Decide access mode from the URL env and the token-bound env.

    Returns ``(mode, env)`` where ``mode`` is ``"full"``, ``"scoped"`` or
    ``"deny"``. In ``"scoped"`` mode ``env`` is the environment to operate on.
    """
    if url_env is None:
        # Root /mcp: a per-env token must use its own /mcp/<env> URL.
        if token_env is not None:
            return ("deny", None)
        return ("full", None)
    # Scoped /mcp/<env>: a per-env token must match the URL's env.
    if token_env is not None and token_env != url_env:
        return ("deny", None)
    return ("scoped", url_env)


def strip_env_name(parameters: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a tool's JSON schema with ``env_name`` removed.

    On the scoped endpoint the env is implied by the URL, so the agent neither
    sees nor supplies ``env_name``.
    """
    params = copy.deepcopy(parameters)
    props = params.get("properties")
    if isinstance(props, dict):
        props.pop("env_name", None)
    required = params.get("required")
    if isinstance(required, list):
        params["required"] = [r for r in required if r != "env_name"]
    return params


def build_env_param_tools(mcp: Any) -> set[str]:
    """Allowlisted tool names that declare an ``env_name`` parameter.

    Computed once from the registered tools so the env is injected only where
    the tool actually accepts it (avoids drift in a hand-kept mapping).
    """
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", {}) or {}
    result: set[str] = set()
    for name in SCOPED_ALLOWLIST:
        tool = tools.get(name)
        if tool is None:
            continue
        props = (getattr(tool, "parameters", None) or {}).get("properties", {})
        if "env_name" in props:
            result.add(name)
    return result


# -- Request-context readers (thin; monkeypatched in tests) --


def scoped_env_from_request() -> str | None:
    """The env parsed from a /mcp/<env> URL for the current request, if any."""
    if _HTTP_REQUEST_ACTIVE.get():
        return _HTTP_URL_ENV.get()
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        return request.scope.get(SCOPE_KEY)
    except Exception:
        return None


def env_from_access_token() -> str | None:
    """The env a per-env token is bound to, from its access-token scopes."""
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:
        if _HTTP_REQUEST_ACTIVE.get():
            raise
        return None
    if not token:
        return None
    for scope in token.scopes or []:
        if scope.startswith(ENV_SCOPE_PREFIX):
            return scope[len(ENV_SCOPE_PREFIX) :]
    return None


# -- Auth: Bearer verifier accepting team + per-env tokens --


class OduflowTokenVerifier(TokenVerifier):
    """Verify Bearer tokens against team ``auth_token`` and per-env tokens."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        from oduflow.production_access import PRODUCTION_SCOPE

        team = self._settings.get_team_by_production_token(token)
        if team is not None:
            return AccessToken(
                token=token, client_id=team.team_id, scopes=[PRODUCTION_SCOPE]
            )
        resolved = await env_tokens.resolve_token_async(self._settings, token)
        if resolved is None:
            return None
        team_id, env_name = resolved
        scopes = [f"{ENV_SCOPE_PREFIX}{env_name}"] if env_name else []
        return AccessToken(
            token=token, client_id=team_id, scopes=scopes, expires_at=None
        )


# -- Routing: /mcp/<env> -> /mcp + scope tag --


class ScopedEnvASGI:
    """Route ``/mcp/<env>`` to the canonical ``/mcp`` endpoint, tagging the env.

    The env is stashed in the ASGI scope (read back via
    ``get_http_request().scope``) and the path rewritten so the existing
    streamable transport + auth + OAuth resource metadata serve it unchanged.
    Non-HTTP scopes (lifespan, websocket) and other paths pass through untouched.
    """

    _WELL_KNOWN_PREFIX = "/.well-known/oauth-protected-resource"

    # OAuth/discovery endpoints that a *path-relative* MCP client (e.g. the
    # claude.ai custom connector) requests under the ``/mcp`` mount instead of at
    # the origin root — it appends ``/authorize`` etc. to the connector URL
    # ``https://<host>/mcp`` rather than following the (correct, root) endpoints
    # advertised by discovery. Without this table ``_scoped_env`` would treat
    # ``authorize`` as an environment name and rewrite ``/mcp/authorize`` to the
    # auth-protected ``/mcp`` route → ``401 invalid_token``. We alias them back to
    # the real root routes the OAuth provider registers. Exact-match only, so an
    # environment literally named e.g. ``token`` is a (negligible) blind spot;
    # OAuth endpoints win.
    _OAUTH_SUBPATHS = {
        "/mcp/authorize": "/authorize",
        "/mcp/token": "/token",
        "/mcp/register": "/register",
        "/mcp/.well-known/oauth-authorization-server": (
            "/.well-known/oauth-authorization-server"
        ),
        "/mcp/.well-known/oauth-protected-resource": (
            "/.well-known/oauth-protected-resource/mcp"
        ),
    }

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        url_env: str | None = None
        if scope.get("type") == "http":
            path = scope.get("path", "")
            oauth_path = self._OAUTH_SUBPATHS.get(path)
            if oauth_path is not None:
                # OAuth/discovery endpoint requested under /mcp/ — route it to the
                # real root route (query string in scope["query_string"] is kept).
                scope = dict(scope)
                scope["path"] = oauth_path
                scope["raw_path"] = oauth_path.encode()
            elif (env := self._scoped_env(path)) is not None:
                # ASGI ``path`` is already percent-decoded, so ``env`` is the
                # plain environment name (slashes in branch names included).
                scope = dict(scope)
                scope[SCOPE_KEY] = env
                url_env = env
                scope["path"] = "/mcp"
                scope["raw_path"] = b"/mcp"
            else:
                rewritten = self._well_known(path)
                if rewritten is not None:
                    scope = dict(scope)
                    scope["path"] = rewritten
                    scope["raw_path"] = rewritten.encode()
            active_token = _HTTP_REQUEST_ACTIVE.set(True)
            env_token = _HTTP_URL_ENV.set(url_env)
            try:
                await self.app(scope, receive, send)
            finally:
                _HTTP_URL_ENV.reset(env_token)
                _HTTP_REQUEST_ACTIVE.reset(active_token)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _scoped_env(path: str) -> str | None:
        """Return the env segment of ``/mcp/<env>`` (anything after ``/mcp/``)."""
        prefix = "/mcp/"
        if path.startswith(prefix) and len(path) > len(prefix):
            return path[len(prefix) :]
        return None

    @classmethod
    def _well_known(cls, path: str) -> str | None:
        """Map a scoped OAuth resource-metadata path back to the ``/mcp`` one."""
        base = cls._WELL_KNOWN_PREFIX
        prefix = base + "/mcp/"
        if path.startswith(prefix) and len(path) > len(prefix):
            return base + "/mcp"
        return None


# -- Enforcement: tool list/call gating --


class ScopedAccessMiddleware(Middleware):
    """Default-deny the tool surface and inject ``env_name`` on scoped requests.

    On the full ``/mcp`` endpoint with a team token this is a no-op. The same
    policy is enforced on both ``tools/list`` and ``tools/call`` so a leaked
    listing can never enable a call.
    """

    def __init__(
        self,
        env_param_tools: set[str],
        allowlist: frozenset[str] = SCOPED_ALLOWLIST,
    ) -> None:
        self._allow = frozenset(allowlist)
        self._env_param = frozenset(env_param_tools)

    def _decide(self) -> tuple[str, str | None]:
        try:
            return decide(scoped_env_from_request(), env_from_access_token())
        except Exception:
            logger.exception("Could not resolve scoped MCP request context")
            return ("deny", None)

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        tools = await call_next(context)
        mode, _env = self._decide()
        if mode == "full":
            return tools
        if mode == "deny":
            return []
        out = []
        for tool in tools:
            if tool.name not in self._allow:
                continue
            if tool.name in self._env_param:
                tool = tool.model_copy(
                    update={"parameters": strip_env_name(tool.parameters)}
                )
            out.append(tool)
        return out

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        mode, env = self._decide()
        if mode == "full":
            return await call_next(context)
        name = context.message.name
        if mode == "deny" or name not in self._allow:
            raise ToolError(
                f"Tool '{name}' is not available on this single-environment "
                "MCP endpoint."
            )
        if name in self._env_param:
            args = dict(context.message.arguments or {})
            args["env_name"] = env
            context.message.arguments = args
        return await call_next(context)
