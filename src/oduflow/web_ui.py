from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote, urlsplit

from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, HTTPConnection, Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from oduflow import (
    activity,
    agent_config,
    agent_uploads,
    artifact_tokens,
    connect_tokens,
    env_share,
    feedback,
    fsutil,
    git_ops,
    import_tokens,
    production_registry,
    secret_store,
    ui_scope,
    ui_totp,
    updates,
)
from oduflow.docker_ops import (
    env_ops,
    odoo_ops,
    production_ops,
    service_database_ops,
    service_ops,
    service_presets,
    system_ops,
    volume_ops,
)
from oduflow.docker_ops.odoo_ops import get_environment_logs
from oduflow.docker_ops.stats import (
    get_container_stats,
    get_system_stats,
    read_storage_cache,
    refresh_env_storage,
    refresh_team_storage,
)
from oduflow.errors import (
    BusyError,
    ConflictError,
    ExternalCommandError,
    FlowError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.licensing import (
    get_license_checkout_url,
    get_license_info,
    install_license_from_text,
    refresh_license,
)
from oduflow.locking import (
    LockManager,
    credentials_lock_key,
    prod_backups_lock_key,
    prod_lock_key,
    service_database_lock_key,
    service_lock_key,
    service_preset_lock_key,
    template_lock_key,
    volume_lock_key,
)
from oduflow.naming import (
    PROD_ENV_PREFIX,
    parse_env_vars,
    parse_service_command,
    slugify_branch,
    validate_env_hostname,
    validate_env_name,
    validate_template_name,
)
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_AUTH_COOKIE = "oduflow_ui_auth"
# Reachable without authentication: the login flow and static brand assets
# (so the login page can render its logo/favicon/fonts). /static/ serves only
# vetted extensions from the packaged assets dir (fonts, icons, xterm).
# /import-odoo.sh (the Odoo.sh client script) and the five import ingest
# endpoints authenticate with a short-lived import token, not the UI password,
# so they bypass UI session authentication. They are EXACT paths (not prefixes):
# a prefix like "/api/templates/import/" would also expose sibling routes such
# as /api/templates/{name}/delete with name="import" to unauthenticated calls.
# NOTE: /api/templates/import-token (which mints the token) is deliberately NOT
# public — it stays behind the UI login.
_PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/logout",
        "/favicon.ico",
        "/logo.png",
        "/import-odoo.sh",
        "/api/templates/import/status",
        "/api/templates/import/manifest",
        "/api/templates/import/dump",
        "/api/templates/import/filestore",
        "/api/templates/import/addon",
        "/api/templates/import/addon-remote",
        "/api/templates/import/finalize",
        # Uptime-monitor endpoint: no auth, no secrets in the response.
        "/healthz",
        # GitHub can't carry a UI session; the handler verifies its own
        # X-Hub-Signature-256 HMAC against per-team webhook secrets.
        "/api/webhooks/github",
        # Cross-subdomain Connect As landing: reached on an ENV host (no
        # dashboard session there), authenticated by its own one-time token.
        "/oduflow-connect",
        # Generated-artifact download: fetched by an agent with curl, which has
        # no dashboard session. Its one-time token is the sole credential.
        "/oduflow-artifact",
    }
)
# /env/<name> is the scoped single-environment dashboard reached from a share
# link. It bypasses the team login because it authenticates itself: the link's
# ?key= (verified against the team's share registry) or the scoped cookie it
# exchanges that key for. Only the page lives under this prefix — every API
# call it makes goes to the normal /api/... routes, where the middleware
# resolves the scoped cookie and applies the ui_scope allowlist.
_PUBLIC_PREFIXES = ("/static/", ui_scope.PAGE_PREFIX)
_SESSION_SALT = "oduflow.ui-auth.v1"
_SHARE_SALT = "oduflow.env-share.v1"
_SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days
_SECRET_FILENAME = ".ui_session_secret"

# Cached per data dir (process-wide; the server runs against a single one).
_secrets_cache: dict[str, str] = {}
_signers: dict[str, URLSafeTimedSerializer] = {}
_share_signers: dict[str, URLSafeTimedSerializer] = {}


def _load_or_create_secret(data_dir: str) -> str:
    """Load the persistent UI-session signing secret from the data dir, creating
    it on first use. Persisting it means a server restart does not invalidate
    live sessions."""
    path = os.path.join(data_dir, _SECRET_FILENAME)
    try:
        existing = pathlib.Path(path).read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    os.makedirs(data_dir, exist_ok=True)
    secret = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another worker created it between our read and write; use theirs.
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secret)
    return secret


def _get_secret(settings: Settings) -> str:
    """Return the cached persistent server-side signing secret for this server."""
    key = settings.base_data_dir or ""
    secret = _secrets_cache.get(key)
    if secret is None:
        secret = _load_or_create_secret(key) if key else secrets.token_hex(32)
        _secrets_cache[key] = secret
    return secret


def _get_signer(settings: Settings) -> URLSafeTimedSerializer:
    """Return the cached itsdangerous signer for this server's session cookies.

    The signing key is a persistent server-side secret (NOT the team password),
    so a leaked cookie reveals nothing about the password and cannot be
    brute-forced offline.
    """
    key = settings.base_data_dir or ""
    signer = _signers.get(key)
    if signer is None:
        signer = URLSafeTimedSerializer(_get_secret(settings), salt=_SESSION_SALT)
        _signers[key] = signer
    return signer


def _password_fingerprint(secret: str, ui_password: str) -> str:
    """A fingerprint of the team password, keyed by the server secret. Embedded
    in the token so that changing ui_password invalidates outstanding cookies
    immediately, while a leaked cookie still cannot be used to brute-force the
    password offline (the key is the server secret, not public)."""
    return hmac.new(
        secret.encode("utf-8"), ui_password.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _make_ui_token(
    team: "TeamSettings", settings: Settings, *, mfa_version: str | None = None
) -> str:
    """Signed, timestamped session token for a team, stored as a cookie so
    WebSocket handshakes (which cannot send an Authorization header) can
    authenticate. Expires after `_SESSION_MAX_AGE`; carries a password
    fingerprint so a password change revokes it at once."""
    fingerprint = _password_fingerprint(_get_secret(settings), team.ui_password)
    if mfa_version is None:
        # Convenience for password-only callers. MFA sessions may only be minted
        # with the generation returned by a successful TOTP verification.
        mfa_version = ui_totp.version(settings, team, password_only=True)
    return _get_signer(settings).dumps([team.team_id, fingerprint, mfa_version])


def _check_cookie_token(token: str, settings: Settings) -> "TeamSettings | None":
    """Validate a cookie token from `_make_ui_token`: verify its signature, that
    it is within `_SESSION_MAX_AGE`, and that the embedded password fingerprint
    still matches the team's current ui_password, then return its team."""
    if not token:
        return None
    try:
        data = _get_signer(settings).loads(token, max_age=_SESSION_MAX_AGE)
    except BadData:
        return None
    if not (isinstance(data, list) and len(data) == 3):
        return None
    team_id, fingerprint, mfa_version = data
    if not isinstance(team_id, str) or not isinstance(fingerprint, str):
        return None
    team = settings.teams.get(team_id)
    if not team or not team.ui_password:
        return None
    try:
        if mfa_version != ui_totp.version(settings, team):
            return None
    except ui_totp.TOTPStateError:
        return None
    expected = _password_fingerprint(_get_secret(settings), team.ui_password)
    if not hmac.compare_digest(fingerprint, expected):
        return None
    return team


def _get_share_signer(settings: Settings) -> URLSafeTimedSerializer:
    """Signer for scoped-environment share cookies.

    Same server secret as the operator session signer but a different salt, so
    a scoped cookie can never be replayed as a full team session (or the other
    way round) even if the payload shapes ever converge.
    """
    key = settings.base_data_dir or ""
    signer = _share_signers.get(key)
    if signer is None:
        signer = URLSafeTimedSerializer(_get_secret(settings), salt=_SHARE_SALT)
        _share_signers[key] = signer
    return signer


def _share_fingerprint(secret: str, share_secret: str) -> str:
    """A fingerprint of an environment's share secret, keyed by the server
    secret. Embedded in the scoped cookie so that rotating or revoking the
    share link invalidates every session opened with the old link."""
    return hmac.new(
        secret.encode("utf-8"), share_secret.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _make_share_token(
    settings: Settings, team: "TeamSettings", env_name: str, share_secret: str
) -> str:
    """Signed, timestamped scoped session token for one environment."""
    fingerprint = _share_fingerprint(_get_secret(settings), share_secret)
    return _get_share_signer(settings).dumps([team.team_id, env_name, fingerprint])


def _check_share_token(
    token: str, settings: Settings
) -> "tuple[TeamSettings, str] | None":
    """Validate a scoped cookie from `_make_share_token` and return its
    ``(team, env_name)``, or None when it is invalid, expired, or its share
    link has since been rotated or revoked."""
    if not token:
        return None
    try:
        data = _get_share_signer(settings).loads(token, max_age=_SESSION_MAX_AGE)
    except BadData:
        return None
    if not (isinstance(data, list) and len(data) == 3):
        return None
    team_id, env_name, fingerprint = data
    if not all(isinstance(v, str) for v in (team_id, env_name, fingerprint)):
        return None
    team = settings.teams.get(team_id)
    if not team:
        return None
    record = env_share.get(team, env_name)
    if not record:
        return None
    expected = _share_fingerprint(_get_secret(settings), str(record["secret"]))
    if not hmac.compare_digest(fingerprint, expected):
        return None
    return (team, env_name)


def _is_cross_origin(headers: Headers) -> bool:
    """Whether Origin/Referer mark this as a cross-site request (CSRF).

    Compares the Origin (or, absent that, Referer) host:port against the
    request's own Host header. Returns False when neither header is present —
    non-browser clients (curl, the import shell script) carry no ambient cookie
    and cannot be driven cross-site by a victim's browser, so there is nothing
    to forge."""
    host = headers.get("host", "")
    source = headers.get("origin", "") or headers.get("referer", "")
    if not source:
        return False
    try:
        netloc = urlsplit(source).netloc
    except Exception:
        return True
    return netloc != host


class UIAuthMiddleware:
    def __init__(self, app: ASGIApp, get_settings: Callable[[], Settings]) -> None:
        self._app = app
        self._get_settings = get_settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope["type"] == "http" and (
            path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)
        ):
            await self._app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        # A valid share cookie deliberately wins over full credentials. This
        # keeps a share link scoped even when an operator previews it in a
        # browser that already has a team session.
        share = _check_share_token(
            conn.cookies.get(ui_scope.SHARE_COOKIE, ""), self._get_settings()
        )
        team: TeamSettings | None = None
        scoped_env: str | None = None
        if share:
            team = share[0]
            scoped_env = share[1]
        if not team:
            token = conn.cookies.get(_AUTH_COOKIE)
            if token:
                team = _check_cookie_token(token, self._get_settings())
        if team:
            # CSRF: browsers authenticate with an ambient cookie, so reject
            # cross-site state-changing requests (unsafe HTTP methods and every
            # WebSocket handshake — cross-site WS hijacking targets the shell/SQL
            # terminals). SameSite=Strict is the first line; this is the
            # server-side backstop. Non-browser clients send no Origin/Referer
            # and are unaffected.
            method = scope.get("method", "")
            is_unsafe = scope["type"] == "websocket" or method in (
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            )
            if is_unsafe and _is_cross_origin(conn.headers):
                if scope["type"] == "websocket":
                    await WebSocket(scope, receive, send).close(code=1008)
                else:
                    forbidden: Response = JSONResponse(
                        {"ok": False, "error": "Cross-origin request blocked"},
                        status_code=403,
                    )
                    await forbidden(scope, receive, send)
                return
            # Scoped sessions are default-deny: only the single-environment
            # surface of ui_scope, and only for their own environment.
            if scoped_env is not None and not ui_scope.is_allowed(
                "WEBSOCKET" if scope["type"] == "websocket" else method,
                path,
                scoped_env,
            ):
                if scope["type"] == "websocket":
                    await WebSocket(scope, receive, send).close(code=1008)
                else:
                    denied: Response = JSONResponse(
                        {"ok": False, "error": "Not available for a shared link."},
                        status_code=403,
                    )
                    await denied(scope, receive, send)
                return
            scope.setdefault("state", {})["team"] = team
            scope["state"]["scoped_env"] = scoped_env
            await self._app(scope, receive, send)
            return

        # Unauthenticated. Browsers get a login page (no Basic dialog); API
        # clients and WebSocket handshakes get a machine-readable rejection.
        if scope["type"] == "websocket":
            ws = WebSocket(scope, receive, send)
            await ws.close(code=1008)
        elif path.startswith("/api/"):
            response: Response = JSONResponse(
                {"ok": False, "error": "Unauthorized"}, status_code=401
            )
            await response(scope, receive, send)
        else:
            response = RedirectResponse("/login", status_code=302)
            await response(scope, receive, send)


def _is_secure_request(request: Request) -> bool:
    """Whether the browser sees this connection as HTTPS, honouring a single
    X-Forwarded-Proto hop (first value, case-insensitive). Drives the cookie
    Secure attribute without guessing from routing_mode."""
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent / "templates"


def _render_login(error: str = "") -> str:
    """Render the login page, optionally with a server-controlled error banner."""
    page = (_TEMPLATE_DIR / "login.html").read_text(encoding="utf-8")
    # Escape the message so the banner stays safe even if a future caller passes
    # user-influenced text (today's callers pass static literals).
    banner = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return page.replace("<!--ERROR-->", banner)


async def _read_login_credentials(request: Request) -> tuple[str, str]:
    """Read form/JSON credentials without a multipart dependency."""
    import urllib.parse

    body = await request.body()
    if "application/json" in request.headers.get("content-type", ""):
        try:
            data = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return "", ""
        if not isinstance(data, dict):
            return "", ""
        return str(data.get("password") or "").strip(), str(
            data.get("otp") or ""
        ).strip()
    parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    return (parsed.get("password", [""])[0]).strip(), (
        parsed.get("otp", [""])[0]
    ).strip()


_EXTERNAL_COMMAND_UI_ERROR = "Operation failed. Check server logs for details."


def _flow_error_status(e: FlowError) -> int:
    if isinstance(e, NotFoundError):
        return 404
    if isinstance(e, BusyError):
        return 409
    if isinstance(e, ExternalCommandError):
        return 500
    return 400


def _public_flow_error(e: FlowError, *, context: str = "Dashboard operation") -> str:
    if isinstance(e, ExternalCommandError):
        # Command output can contain tracebacks, filesystem paths, or connection
        # details. Keep it in the server log instead of returning it to browsers.
        logger.error("%s failed: %s", context, e)
        return _EXTERNAL_COMMAND_UI_ERROR
    return str(e)


def _error_response(e: FlowError) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": _public_flow_error(e)},
        status_code=_flow_error_status(e),
    )


def _validate_dev_env_name(env_name: str) -> str:
    """Validate a dev environment name without entering production namespace."""
    validate_env_name(env_name)
    if slugify_branch(env_name).startswith(PROD_ENV_PREFIX):
        raise ValueError(
            f"'{env_name}' is a production environment. Use the production "
            "deployment workflow instead of dev module operations."
        )
    return env_name


async def _offload(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run blocking dashboard work without stalling the ASGI event loop."""
    return await run_in_threadpool(func, *args, **kwargs)


def _normalize_extra_addons(raw_addons: object) -> dict[str, str]:
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        logger.warning(
            "Legacy list format for extra_addons (no branch info), skipping: %s",
            raw_addons,
        )
        return {}
    return {}


def _normalize_domain_list(raw: object) -> list[str]:
    """Read an extra-domains field as a JSON list or a comma/whitespace
    separated string (the dashboard sends a plain text input)."""
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part for part in re.split(r"[,\s]+", raw) if part]
    return []


def _env_vars_from_body(raw: object) -> dict[str, str]:
    """Read an "env_vars" request field in either supported shape.

    A mapping is taken verbatim: the dashboard sends one so that a value
    holding commas ("OPTIONS=a,b", routine for stack-managed environments)
    cannot be re-split on its way back in. The KEY=VALUE string form is kept
    for existing REST clients and parsed with parse_env_vars.
    """
    if isinstance(raw, dict):
        return {str(k).strip(): str(v) for k, v in raw.items() if str(k).strip()}
    return parse_env_vars(str(raw or "").strip())


def _command_from_body(raw: object) -> list[str]:
    """Read a "command" request field in either supported shape.

    A list is taken verbatim as argv (no re-splitting, so an argument may hold
    spaces); the shell-quoted string form is parsed like the MCP tool does.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return parse_service_command(raw)
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    raise ValueError("command must be a shell-quoted string or an array of strings")


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


def _guide_title(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# "):
                    return line[2:].strip()
    except Exception:
        pass
    return os.path.basename(filepath).replace("_", " ").replace(".md", "").title()


def _acp_adapter_cmd(agent_type: str) -> list[str]:
    """Command that starts the ACP (Agent Client Protocol) adapter for the given
    agent inside the coder container. These speak JSON-RPC over stdio (unlike the
    interactive CLIs the terminal console runs), which the ``ws_agent_acp`` relay
    bridges to the browser chat. Bin names come from the packages installed in
    ``docker/agent/Dockerfile``."""
    if agent_type == "codex":
        return ["codex-acp"]
    if agent_type == "opencode":
        return ["opencode", "acp"]
    return ["claude-agent-acp"]


_CLAUDE_AUTH_GUIDANCE_MARKER = "Oduflow authentication guidance:"
_OPENCODE_AUTH_GUIDANCE_MARKER = "Oduflow OpenCode authentication guidance:"


def _annotate_acp_auth_error(
    frame: str, agent_type: str, auth_mode: str, team_id: str
) -> str:
    """Add mode-specific recovery to recognized ACP authentication errors.

    Adapters return provider failures as JSON-RPC response frames. Keep the
    provider's original code/data/message intact and append guidance only for
    recognizable authentication failures. In particular, quota/spend-limit
    failures must remain untouched: silently trying a different credential
    could switch accounts or billing modes.
    """
    if agent_type not in {"claude", "opencode"}:
        return frame
    try:
        message = json.loads(frame)
    except (TypeError, ValueError):
        return frame
    if not isinstance(message, dict) or not isinstance(message.get("error"), dict):
        return frame
    error = message["error"]
    error_message = error.get("message")
    if not isinstance(error_message, str):
        return frame

    lowered = error_message.lower()
    if agent_type == "opencode":
        if "provider authentication required" not in lowered:
            return frame
        if _OPENCODE_AUTH_GUIDANCE_MARKER in error_message:
            return frame
        guidance = (
            "Open Agent CLI and run `opencode auth login`, or configure the "
            "selected provider's credential in "
            f"[team.{team_id}.agent_env] (or the single-team server environment), "
            "then restart Oduflow."
        )
        error["message"] = (
            f"{error_message}\n\n{_OPENCODE_AUTH_GUIDANCE_MARKER} {guidance}"
        )
        return json.dumps(message, separators=(",", ":"), ensure_ascii=False)

    specific_markers = (
        "invalid bearer token",
        "invalid api key",
        "oauth token has expired",
        "oauth token expired",
        "oauth token revoked",
    )
    generic_401 = "401" in lowered and (
        "failed to authenticate" in lowered or "authentication_error" in lowered
    )
    if not generic_401 and not any(marker in lowered for marker in specific_markers):
        return frame
    if _CLAUDE_AUTH_GUIDANCE_MARKER in error_message:
        return frame

    if auth_mode == "setup_token":
        guidance = (
            "Claude is using CLAUDE_CODE_OAUTH_TOKEN, which overrides interactive "
            "/login. Run `claude setup-token` while signed in to the intended "
            "account, replace the token in "
            f"[team.{team_id}.agent_env] (or the single-team server environment), "
            "then restart Oduflow."
        )
    elif auth_mode == "api_key":
        guidance = (
            "Claude is using ANTHROPIC_API_KEY (Console API billing). Update or "
            "remove that key in "
            f"[team.{team_id}.agent_env] (or the single-team server environment), "
            "then restart Oduflow."
        )
    else:
        guidance = (
            "No Claude environment credential is configured. Open Agent CLI for "
            "this team, run `/login`, complete the interactive sign-in, then "
            "reopen Agent Chat."
        )

    error["message"] = f"{error_message}\n\n{_CLAUDE_AUTH_GUIDANCE_MARKER} {guidance}"
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


def _codex_cli_cmd(mcp_url: str, model: str = "") -> list[str]:
    """Build the hosted Codex CLI command.

    Docker is the security boundary for hosted agents: the container is
    per-team and runs as the unprivileged ``agent`` user. Disabling Codex's
    nested Linux sandbox avoids requiring user namespaces (bubblewrap), which
    Docker's default seccomp profile intentionally blocks.
    """
    cmd = [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        f'mcp_servers.oduflow.url="{mcp_url}"',
        "-c",
        'mcp_servers.oduflow.bearer_token_env_var="ODUFLOW_MCP_TOKEN"',
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def _opencode_cli_cmd(model: str = "") -> list[str]:
    """Build the hosted OpenCode TUI command.

    Docker and the unprivileged ``agent`` user are the security boundary, so
    permission requests are auto-approved inside the container.
    """
    cmd = ["opencode", "--auto"]
    if model:
        cmd += ["--model", model]
    return cmd


def _opencode_config(
    mcp_url: str = "",
    *,
    include_browser: bool = True,
    include_oduflow: bool = False,
    model: str = "",
) -> str:
    """Return session-local OpenCode config with no embedded credential.

    ``OPENCODE_CONFIG_CONTENT`` has high precedence, so Oduflow's approval-free
    trust model and scoped MCP wiring cannot be silently shadowed by a checkout
    config. The bearer remains an environment placeholder resolved only inside
    this docker-exec process.
    """
    config: dict[str, Any] = {
        "autoupdate": False,
        "permission": "allow",
    }
    if include_browser:
        config["mcp"] = {
            "agent_browser": {
                "type": "local",
                "command": ["agent-browser", "mcp", "--tools", "all"],
                "environment": {
                    "AGENT_BROWSER_SESSION": "{env:AGENT_BROWSER_SESSION}",
                    "AGENT_BROWSER_EXECUTABLE_PATH": "/usr/bin/chromium",
                },
            },
        }
    if model:
        config["model"] = model
    if include_oduflow:
        config.setdefault("mcp", {})["oduflow"] = {
            "type": "remote",
            "url": mcp_url,
            "enabled": True,
            "oauth": False,
            "headers": {
                "Authorization": "Bearer {env:ODUFLOW_MCP_TOKEN}",
            },
        }
    return json.dumps(config, separators=(",", ":"))


def _wire_client_acp_mcp(
    frame: str, mcp_url: str, mcp_token: str, browser_session: str
) -> str:
    """Inject built-in MCP servers into client-configurable ACP sessions.

    The browser intentionally knows neither URL nor token. The WebSocket relay
    augments only ``session/new``/``session/load``/``session/resume`` frames on
    their way to Codex/OpenCode, using ACP's client-provided MCP contract. Agent
    Browser is a local credentialless stdio server. Oduflow is added only when a
    scoped token exists; that credential lives only in this exec's environment
    and stdio and is never returned to the browser or disk.
    """
    try:
        message = json.loads(frame)
    except json.JSONDecodeError:
        return frame
    if not isinstance(message, dict) or message.get("method") not in {
        "session/new",
        "session/load",
        "session/resume",
    }:
        return frame
    params = message.get("params")
    if not isinstance(params, dict):
        return frame
    existing = params.get("mcpServers")
    servers = existing if isinstance(existing, list) else []
    servers = [
        server
        for server in servers
        if not isinstance(server, dict)
        or server.get("name") not in {"agent_browser", "oduflow"}
    ]
    servers.append(
        {
            "name": "agent_browser",
            "command": "agent-browser",
            "args": ["mcp", "--tools", "all"],
            "env": [
                {"name": "AGENT_BROWSER_SESSION", "value": browser_session},
                {
                    "name": "AGENT_BROWSER_EXECUTABLE_PATH",
                    "value": "/usr/bin/chromium",
                },
            ],
        }
    )
    if mcp_token:
        servers.append(
            {
                "type": "http",
                "name": "oduflow",
                "url": mcp_url,
                "headers": [{"name": "Authorization", "value": f"Bearer {mcp_token}"}],
            }
        )
    params["mcpServers"] = servers
    return json.dumps(message, separators=(",", ":"))


class _LoginRateLimiter:
    """Best-effort in-memory throttle for failed logins (issue #56).

    Tracks failed attempts per client IP in a sliding window; once the threshold
    is reached the IP is locked out for the rest of the window. A successful
    login clears the IP. The dashboard runs in a single uvicorn process, so an
    in-memory store is sufficient.
    """

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        cutoff = now - self.window
        kept = [t for t in self._failures.get(key, []) if t > cutoff]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)

    def is_limited(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now)
            return len(self._failures.get(key, [])) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._failures.setdefault(key, []).append(now)
            self._prune(key, now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class _ImportStagingLocks:
    """Serialize finalizers that mutate one template import staging tree."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._map_lock = threading.Lock()

    def for_path(self, staging: str) -> threading.Lock:
        with self._map_lock:
            if staging not in self._locks:
                self._locks[staging] = threading.Lock()
            return self._locks[staging]


def _build_routes(
    get_settings: Callable[[], Settings],
    locks: LockManager,
) -> list[BaseRoute]:
    # Per-app so test apps and real deployments don't share failure counters.
    login_limiter = _LoginRateLimiter()
    import_staging_locks = _ImportStagingLocks()

    def _set_session_cookie(
        response: Response, team: TeamSettings, request: Request, mfa_version: str
    ) -> None:
        response.set_cookie(
            _AUTH_COOKIE,
            _make_ui_token(team, get_settings(), mfa_version=mfa_version),
            max_age=_SESSION_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=_is_secure_request(request),
            path="/",
        )

    def _render_dashboard(
        settings: Settings, scoped_env: str = "", team: TeamSettings | None = None
    ) -> str:
        """Render the dashboard page. With ``scoped_env`` set it renders in
        shared single-environment mode (see oduflow.ui_scope): the client-side
        surface collapses to that one environment's card. The server-side
        allowlist, not this rendering, is the boundary."""
        html_path = _TEMPLATE_DIR / "dashboard.html"
        return (
            html_path.read_text(encoding="utf-8")
            .replace(
                "__PRODUCTION_TAB_HIDDEN__",
                "" if settings.prod_enabled and not scoped_env else "hidden",
            )
            .replace("__ODUFLOW_VERSION__", html.escape(feedback.oduflow_version()))
            .replace(
                "__ODUFLOW_DIAGNOSTICS__",
                html.escape(
                    feedback.format_diagnostics(feedback.diagnostics(settings))
                ),
            )
            # Read back from a body data attribute, never inlined into a script
            # literal: environment names are git branch names and may carry
            # quotes. Attribute escaping is exactly the right encoding there.
            .replace("__SCOPED_ENV__", html.escape(scoped_env, quote=True))
            # The create-production modal needs the team zone whichever tab it
            # is opened from, so it ships with the page instead of arriving as
            # a side effect of loading the productions list.
            .replace(
                "__TEAM_BASE_DOMAIN__",
                html.escape(team.base_domain if team else "", quote=True),
            )
        )

    def dashboard(request: Request) -> HTMLResponse:
        settings = get_settings()
        team = getattr(request.state, "team", None)
        page = _render_dashboard(settings, team=team)
        return HTMLResponse(page)

    def _team_for_share(
        settings: Settings, env_name: str, key: str, host: str
    ) -> TeamSettings | None:
        """The team whose environment ``env_name`` is shared under ``key``.

        The share secret is the only credential a link carries, so the team is
        resolved from it. In traefik mode the request host names the team, so
        try that one first; the scan is the fallback for port mode (one host,
        many teams)."""
        by_host = settings.get_team_by_hostname(host) if host else None
        candidates = [by_host] if by_host else []
        candidates += [t for t in settings.teams.values() if t is not by_host]
        for team in candidates:
            if team is not None and env_share.verify(team, env_name, key):
                return team
        return None

    def _share_link_error(message: str, status_code: int) -> Response:
        return Response(message, status_code=status_code, media_type="text/plain")

    def scoped_env_page(request: Request) -> Response:
        """The shared single-environment dashboard at ``/env/<name>``.

        This route authenticates itself (it is outside the team login): either
        the share link's ``?key=``, which is exchanged for a scoped cookie and
        dropped from the URL, or that cookie on later visits. An operator with
        a full session can also open it to preview what the client sees.
        """
        env_name = request.path_params["name"]
        settings = get_settings()
        key = request.query_params.get("key") or ""
        if key:
            client_ip = request.client.host if request.client else "unknown"
            if login_limiter.is_limited(client_ip):
                return _share_link_error(
                    "Too many failed attempts. Try again later.", 429
                )
            team = _team_for_share(settings, env_name, key, request.url.hostname or "")
            if team is None:
                login_limiter.record_failure(client_ip)
                return _share_link_error(
                    "This share link is invalid or has been revoked. "
                    "Ask for a new one.",
                    403,
                )
            login_limiter.clear(client_ip)
            # Land on the clean URL so the key leaves the address bar, browser
            # history, and any Referer sent by the page's own requests.
            target = ui_scope.PAGE_PREFIX + quote(env_name, safe="/")
            response: Response = RedirectResponse(target, status_code=303)
            response.set_cookie(
                ui_scope.SHARE_COOKIE,
                _make_share_token(settings, team, env_name, key),
                max_age=_SESSION_MAX_AGE,
                httponly=True,
                samesite="strict",
                secure=_is_secure_request(request),
                path="/",
            )
            return response

        share = _check_share_token(
            request.cookies.get(ui_scope.SHARE_COOKIE, ""), settings
        )
        if share and share[1] == env_name:
            return HTMLResponse(_render_dashboard(settings, scoped_env=env_name))

        auth_enabled = any(t.ui_password for t in settings.teams.values())
        session = request.cookies.get(_AUTH_COOKIE, "")
        operator = _check_cookie_token(session, settings) if session else None
        if operator is not None or not auth_enabled:
            return HTMLResponse(_render_dashboard(settings, scoped_env=env_name))
        return _share_link_error(
            "This link is missing its key. Open the full share link you were "
            "sent (it ends with ?key=...).",
            401,
        )

    async def login(request: Request) -> Response:
        settings = get_settings()
        # Auth disabled entirely -> the dashboard is open, no login needed.
        if not any(t.ui_password for t in settings.teams.values()):
            return RedirectResponse("/", status_code=302)
        # Already signed in (valid session cookie) -> go straight to the app.
        token = request.cookies.get(_AUTH_COOKIE)
        if token and _check_cookie_token(token, settings) is not None:
            return RedirectResponse("/", status_code=302)
        if request.method == "POST":
            client_ip = request.client.host if request.client else "unknown"
            if login_limiter.is_limited(client_ip):
                return HTMLResponse(
                    _render_login("Too many failed attempts. Try again later."),
                    status_code=429,
                )
            if _is_cross_origin(request.headers):
                return HTMLResponse(
                    _render_login("Cross-origin request blocked."), status_code=403
                )
            password, code = await _read_login_credentials(request)
            team = settings.get_team_by_ui_password(password) if password else None
            if team is not None:
                try:
                    mfa_version = await run_in_threadpool(
                        ui_totp.verify, settings, team, code
                    )
                except ui_totp.TOTPThrottled:
                    return HTMLResponse(
                        _render_login("Too many failed attempts. Try again later."),
                        status_code=429,
                    )
                except (ui_totp.TOTPStateError, OSError):
                    # Fail-closed: log the cause, or the operator sees a 503
                    # with nothing in the log explaining what to restore/reset.
                    logger.exception(
                        "UI 2FA state unavailable for team %s", team.team_id
                    )
                    return HTMLResponse(
                        _render_login(
                            "Sign-in unavailable. Contact the server administrator."
                        ),
                        status_code=503,
                    )
                if mfa_version is not None:
                    login_limiter.clear(client_ip)
                    response: Response = RedirectResponse("/", status_code=303)
                    _set_session_cookie(response, team, request, mfa_version)
                    return response
            login_limiter.record_failure(client_ip)
            return HTMLResponse(
                _render_login("Invalid password or authenticator code."),
                status_code=401,
            )
        return HTMLResponse(_render_login())

    def logout(request: Request) -> RedirectResponse:
        # /logout is public, so the scoped session is read from its cookie
        # rather than request.state. When an operator opened a share link in an
        # already authenticated browser, leaving scoped mode restores that
        # operator session instead of signing it out too.
        settings = get_settings()
        share_token = request.cookies.get(ui_scope.SHARE_COOKIE, "")
        share = _check_share_token(share_token, settings)
        session = request.cookies.get(_AUTH_COOKIE, "")
        operator = _check_cookie_token(session, settings) if session else None
        if share_token and operator is not None:
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie(ui_scope.SHARE_COOKIE, path="/", samesite="strict")
            return response
        if share:
            target = ui_scope.PAGE_PREFIX + quote(share[1], safe="/")
            response = RedirectResponse(target, status_code=303)
            response.delete_cookie(ui_scope.SHARE_COOKIE, path="/", samesite="strict")
            return response

        # A normal operator logout clears the full session. Also remove any
        # stale scoped cookie that failed validation.
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(_AUTH_COOKIE, path="/", samesite="strict")
        response.delete_cookie(ui_scope.SHARE_COOKIE, path="/", samesite="strict")
        return response

    def favicon(request: Request) -> Response:
        ico_path = _TEMPLATE_DIR / "favicon.ico"
        return Response(ico_path.read_bytes(), media_type="image/x-icon")

    def logo(request: Request) -> Response:
        logo_path = _TEMPLATE_DIR / "logo.png"
        return Response(logo_path.read_bytes(), media_type="image/png")

    _STATIC_MEDIA_TYPES = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".woff2": "font/woff2",
        ".png": "image/png",
        ".md": "text/markdown; charset=utf-8",
    }

    def static_file(request: Request) -> Response:
        filename = request.path_params["filename"]
        static_dir = (_TEMPLATE_DIR / "static").resolve()
        file_path = (static_dir / filename).resolve()
        media_type = _STATIC_MEDIA_TYPES.get(file_path.suffix)
        if (
            not file_path.is_relative_to(static_dir)
            or media_type is None
            or not file_path.is_file()
        ):
            return Response("Not found", status_code=404)
        return Response(
            file_path.read_bytes(),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def _get_ui_team(request: HTTPConnection) -> TeamSettings:
        """Get the team from request state (set by auth middleware) or fallback.

        Accepts any Starlette connection (HTTP ``Request`` or ``WebSocket``);
        both carry the ``state`` populated by the auth middleware.
        """
        if hasattr(request.state, "team"):
            team: TeamSettings = request.state.team
            return team
        settings = get_settings()
        # When auth is enforced (any team has a ui_password), the middleware
        # always populates request.state.team for non-public paths. Reaching
        # here means the request bypassed auth — default-deny instead of silently
        # acting as team "1" (tenant-isolation hazard). The single-team fallback
        # is only for the open (auth-disabled) server.
        if any(t.ui_password for t in settings.teams.values()):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if len(settings.teams) == 1:
            return next(iter(settings.teams.values()))
        return settings.get_team("1")

    def api_list(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            envs = env_ops.list_environments(settings, team)
            # A share link sees exactly its own environment: the scoped
            # dashboard polls this route for its single card.
            scoped_env = getattr(request.state, "scoped_env", None)
            if scoped_env:
                envs = [e for e in envs if e.get("env_name") == scoped_env]
            return JSONResponse({"ok": True, "environments": envs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_list")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_start(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = env_ops.start_environment(get_settings(), branch, team)
            activity.mark_started(team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_start")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_stop(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            result = env_ops.stop_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_stop")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_restart(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = env_ops.restart_environment(get_settings(), branch, team)
            activity.mark_started(team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_restart")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_sync(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            activity.touch(team, branch)
            result = env_ops.pull_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_sync")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_modules(request: Request) -> JSONResponse:
        """Installed modules, for the dashboard's Upgrade modules picker."""
        try:
            branch = _validate_dev_env_name(request.path_params["branch"])
            team = _get_ui_team(request)
            modules = odoo_ops.list_installed_module_records(
                get_settings(), team, branch
            )
            return JSONResponse({"ok": True, "modules": modules})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_modules")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_modules_apply(request: Request) -> JSONResponse:
        try:
            branch = _validate_dev_env_name(request.path_params["branch"])
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "Request body must be a JSON object."},
                status_code=400,
            )
        raw_action = body.get("action")
        action = raw_action.strip() if isinstance(raw_action, str) else ""
        if action not in {"install", "upgrade"}:
            return JSONResponse(
                {"ok": False, "error": "Action must be 'install' or 'upgrade'."},
                status_code=400,
            )
        raw_modules = body.get("modules", "")
        if not isinstance(raw_modules, str):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Modules must be a comma-separated string.",
                },
                status_code=400,
            )
        modules = [m.strip() for m in raw_modules.split(",")]
        modules = [m for m in modules if m]
        if not modules:
            return JSONResponse(
                {"ok": False, "error": "At least one module name is required."},
                status_code=400,
            )
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            activity.touch(team, branch)
            operation = (
                odoo_ops.install_odoo_modules
                if action == "install"
                else odoo_ops.upgrade_odoo_modules
            )
            result = await _offload(operation, settings, team, branch, *modules)
            exit_code = result["exit_code"]
            applied = result.get("modules", modules)
            # "all" is Odoo's keyword for every installed module, not a module
            # name, so the result reads as a phrase instead of a one-item list.
            applied_label = (
                "all installed modules"
                if action == "upgrade" and applied == [odoo_ops.ALL_MODULES]
                else ", ".join(applied)
            )
            restart_warning = ""
            container_restarted: bool | None = None
            # A failed run is still a completed request: the Odoo log is the
            # answer the developer came for, so it goes to the result modal
            # instead of a one-line toast.
            if exit_code == 0:
                completed_verb = "Installed" if action == "install" else "Upgraded"
                try:
                    await _offload(env_ops.restart_environment, settings, branch, team)
                    container_restarted = True
                    message = (
                        f"{completed_verb}: {applied_label}. Odoo container restarted."
                    )
                except FlowError as e:
                    container_restarted = False
                    restart_error = _public_flow_error(
                        e, context=f"Restart after module {action} in {branch}"
                    )
                    restart_warning = (
                        f"Modules were {action}d, but the Odoo container could not "
                        f"be restarted. {restart_error}"
                    )
                    message = f"{completed_verb}: {applied_label}. Restart failed."
                except Exception:
                    container_restarted = False
                    logger.exception(
                        "Unexpected restart error after module %s in %s",
                        action,
                        branch,
                    )
                    restart_warning = (
                        f"Modules were {action}d, but the Odoo container could not "
                        "be restarted. Check server logs for details."
                    )
                    message = f"{completed_verb}: {applied_label}. Restart failed."
            else:
                verb = "Install" if action == "install" else "Upgrade"
                message = f"{verb} failed: {applied_label}."
            payload: dict[str, Any] = {
                "action": action,
                "message": message,
                "exit_code": exit_code,
                "output": result.get("output", ""),
                "modules_attempted": applied,
            }
            if exit_code == 0:
                success_key = (
                    "modules_installed" if action == "install" else "modules_upgraded"
                )
                payload[success_key] = applied
                payload["container_restarted"] = container_restarted
                if restart_warning:
                    payload["warnings"] = [restart_warning]
            return JSONResponse({"ok": True, "result": payload})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_modules_apply")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    async def api_switch_branch(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        target = ((body or {}).get("branch") or "").strip()
        if not target:
            return JSONResponse(
                {"ok": False, "error": "A target branch is required."}, status_code=400
            )
        new_name = ((body or {}).get("new_name") or "").strip()
        if new_name == branch:
            new_name = ""
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        # The rename target is locked too, so a concurrent create cannot claim
        # the name while the environment is moving onto it.
        if new_name:
            try:
                locks.acquire_env(new_name, team.team_id)
            except BusyError as e:
                locks.release_env(branch)
                return _error_response(e)
        try:
            settings = get_settings()
            activity.touch(team, branch)
            result = await _offload(
                env_ops.switch_environment_branch,
                settings,
                team,
                branch,
                target,
                new_name=new_name or None,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_switch_branch")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            if new_name:
                locks.release_env(new_name)
            locks.release_env(branch)

    def api_delete(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            env_ops.delete_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": {"deleted": branch}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    async def api_update(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        odoo_image = (body.get("odoo_image") or "").strip() if body else ""
        new_name = (body.get("new_name") or "").strip() if body else ""
        if new_name == branch:
            new_name = ""
        # "env_vars" present in the body is a full replacement (an empty value
        # clears every user-supplied var); an absent key keeps the current ones.
        env_override = None
        if body and "env_vars" in body:
            env_override = _env_vars_from_body(body.get("env_vars"))
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        # The rename target is locked too, so a concurrent create cannot claim
        # the name while the environment is moving onto it.
        if new_name:
            try:
                locks.acquire_env(new_name, team.team_id)
            except BusyError as e:
                locks.release_env(branch)
                return _error_response(e)
        try:
            settings = get_settings()
            activity.touch(team, branch)
            result = await _offload(
                env_ops.update_environment,
                settings,
                team,
                branch,
                env_override=env_override,
                image_override=odoo_image or None,
                rename_to=new_name or None,
                hostname_override=(body.get("hostname") or "").strip() or None
                if body
                else None,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            # A rejected new_name (validate_env_name); the message is written
            # for the operator reading it.
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_update")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            if new_name:
                locks.release_env(new_name)
            locks.release_env(branch)

    def api_env_vars(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import get_resource_name

            settings = get_settings()
            client = _get_client()
            odoo_container_name = get_resource_name(
                branch, "odoo", settings.prefix, team.team_id
            )
            try:
                container = client.containers.get(odoo_container_name)
            except _docker.errors.NotFound:
                return _error_response(
                    NotFoundError(f"Environment '{branch}' not found.")
                )
            env_vars = json.loads(container.labels.get("oduflow.env_vars", "{}"))
            return JSONResponse({"ok": True, "env_vars": env_vars})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_env_vars")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_recreate(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import get_resource_name

            settings = get_settings()
            activity.touch(team, branch)
            client = _get_client()
            odoo_container_name = get_resource_name(
                branch, "odoo", settings.prefix, team.team_id
            )
            try:
                container = client.containers.get(odoo_container_name)
                labels = container.labels
            except _docker.errors.NotFound:
                return _error_response(
                    NotFoundError(f"Environment '{branch}' not found.")
                )

            repo_url = labels.get(settings.repo_label, "")
            odoo_image = labels.get(settings.image_label, "")
            template_raw = labels.get("oduflow.template", "")
            template_name = (
                template_raw if template_raw and template_raw != "none" else None
            )
            extra_addons_raw = labels.get("oduflow.extra_addons", "{}")
            extra_addons = json.loads(extra_addons_raw) or None
            git_user = labels.get("oduflow.git_user", "")
            env_vars = json.loads(labels.get("oduflow.env_vars", "{}")) or None
            local_path = labels.get("oduflow.local_path", "")
            # The environment name and the git branch it tracks can differ
            # (several environments off one branch), so recreate from the
            # recorded branch label rather than from the environment name.
            git_branch = labels.get("oduflow.git_branch", branch)
            recreate_labels = dict(labels)
            env_ops._reconcile_environment_hostname_for_update(
                client, settings, team, branch, recreate_labels
            )
            hostname = recreate_labels.get(env_ops.ENV_HOSTNAME_LABEL, "")
            hostname_source = recreate_labels.get(env_ops.ENV_HOSTNAME_SOURCE_LABEL, "")

            # Check disk space BEFORE deleting the old environment: refusing
            # here loses nothing, while failing after delete_environment would
            # destroy a working environment. Conservative on purpose — the old
            # environment's own space is not yet freed at measurement time.
            from oduflow.docker_ops import system_ops

            est_db_bytes = system_ops.estimate_new_db_bytes(
                client, settings, team, template_name
            )
            system_ops.check_disk_space(
                client,
                settings,
                team,
                template_name,
                estimated_db_bytes=est_db_bytes,
                local_mount=bool(local_path),
                env_name=branch,
            )

            # Same reasoning as the disk-space check: a dangling secret
            # reference must refuse the recreate here, while refusing loses
            # nothing — create_environment would only re-check after
            # delete_environment has already destroyed the working environment.
            secret_store.resolve_env_secrets(team, env_vars)

            env_ops.delete_environment(settings, team, branch, preserve_share=True)
            result = env_ops.create_environment(
                settings,
                team,
                git_branch,
                repo_url,
                odoo_image,
                env_name=branch,
                template_name=template_name,
                extra_addons=extra_addons,
                git_user=git_user,
                env_vars=env_vars,
                local_path=local_path,
                hostname=hostname,
                hostname_source=hostname_source,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_recreate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    @contextlib.contextmanager
    def _template_locks(
        team: TeamSettings, *names: str, operation: str, team_wide: bool
    ) -> Iterator[None]:
        """Take the same locks a template mutation takes on the MCP side.

        Every mutation holds each touched template's own key, so the dashboard
        and the MCP tools exclude each other on the template itself. Only
        ``team_wide=True`` adds the team lock, and only two kinds of operation
        need it: those that remount live environments' overlay filestores
        (publish, refresh, attach, finalize) and delete/rename, whose
        dependent-environment scan is a check-then-act against a concurrent
        create_environment. Importing a brand-new template and editing
        metadata.json are neither, and take the key alone.
        """
        with contextlib.ExitStack() as stack:
            if team_wide:
                stack.enter_context(locks.team_lock(team.team_id, operation=operation))
            # dict.fromkeys de-duplicates while keeping order: a rename to the
            # template's own name passes the same name twice, and entering one
            # key twice would report a phantom concurrent operation instead of
            # the ConflictError that rename really deserves.
            for name in dict.fromkeys(names):
                stack.enter_context(
                    locks.env_lock(
                        template_lock_key(team.team_id, name), operation=operation
                    )
                )
            yield

    async def api_save_as_template(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        template_name = str((body or {}).get("template_name") or "").strip()
        if not template_name:
            return JSONResponse(
                {"ok": False, "error": "template_name is required"}, status_code=400
            )
        try:
            validate_template_name(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        # Team lock (not just the env): publishing can remount other envs' overlay
        # filestores, so it must serialize against the whole team like the MCP tool.
        try:
            with _template_locks(
                team, template_name, operation="save_as_template", team_wide=True
            ):
                activity.touch(team, branch)
                # No overwrite from the UI: publishing over an existing template
                # is a deliberate re-baseline reserved for the MCP tool, so a
                # duplicate name here raises ConflictError (surfaced to the
                # client by _error_response).
                result = await _offload(
                    system_ops.publish_env_as_template,
                    get_settings(),
                    team,
                    branch,
                    template_name=template_name,
                )
            return JSONResponse(
                {
                    "ok": True,
                    "result": {
                        "status": result.get("status"),
                        "env_name": result.get("env_name"),
                        "template_db": result.get("template_db"),
                        "affected_envs": result.get("affected_envs", []),
                        "remount_failures": result.get("remount_failures", []),
                    },
                }
            )
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_save_as_template")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def _template_from_production(team: TeamSettings, prod_name: str) -> str:
        """Resolve ``from_production`` to its managed ``prod-<name>`` template.

        The dashboard counterpart of the MCP resolver, minus its
        ``allow_copy_to_dev_mcp`` gate: that flag only governs agent-initiated
        copies, and the dashboard administrator can always copy a production
        into dev.
        """
        from oduflow.server import (
            _PRODUCTION_DISABLED_MESSAGE,
            ensure_production_template,
        )

        settings = get_settings()
        if not settings.prod_enabled:
            raise PrerequisiteNotMetError(_PRODUCTION_DISABLED_MESSAGE)
        # Raises NotFoundError for an unknown production.
        production_registry.get_production(team, prod_name)
        managed, _published = await _offload(
            ensure_production_template,
            settings,
            team,
            prod_name,
            locks=locks,
            operation="create_environment",
        )
        return str(managed)

    async def api_create(request: Request) -> JSONResponse:
        import json as _json

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body."}, status_code=400
            )

        env_name = (body.get("env_name") or "").strip()
        # Older dashboards (and cached ones) send only env_name and mean the
        # branch by it; new ones send both so one branch can back several
        # environments.
        branch = (body.get("branch") or "").strip() or env_name
        env_name = env_name or branch
        repo_url = (body.get("repo_url") or "").strip()
        odoo_image = (body.get("odoo_image") or "").strip()
        git_user = (body.get("git_user") or "").strip()
        template_name_raw = (body.get("template_name") or "").strip()
        extra_addons_raw = body.get("extra_addons")
        auto_install_raw = (body.get("auto_install_modules") or "").strip()
        hostname = (body.get("hostname") or "").strip()
        from_production = (body.get("from_production") or "").strip()
        env_vars = _env_vars_from_body(body.get("env_vars"))
        if from_production and template_name_raw:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "from_production cannot be combined with a template: it "
                        "supplies the database, filestore and code origin itself."
                    ),
                },
                status_code=400,
            )
        if not env_name:
            return JSONResponse(
                {"ok": False, "error": "branch is required."},
                status_code=400,
            )
        try:
            env_name = validate_env_name(env_name)
            # Validated here as well as in create_environment: rejecting a bad
            # DNS label before the lock is taken keeps the message immediate
            # and starts no work that has to be unwound.
            if hostname:
                hostname = validate_env_hostname(hostname)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        team = _get_ui_team(request)
        # Acquire the env lock in its own try so a BusyError returns WITHOUT
        # entering the try/finally below — otherwise the finally would release a
        # lock that another in-flight request holds (issue #42).
        try:
            locks.acquire_env(env_name, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            # Publishing dumps a live production database, so it happens only
            # once the request is past validation and holds the env lock.
            if from_production:
                template_name_raw = await _template_from_production(
                    team, from_production
                )

            resolved_template: str | None
            if not template_name_raw or template_name_raw.lower() == "none":
                resolved_template = None
            else:
                resolved_template = template_name_raw

            # Load metadata from template
            settings = get_settings()
            extra_dict = None
            if isinstance(extra_addons_raw, dict):
                extra_dict = extra_addons_raw or None
            elif isinstance(extra_addons_raw, str) and extra_addons_raw.strip():
                extra_dict = _parse_extra_addons(extra_addons_raw.strip()) or None
            local_path_from_meta = ""
            template_env_vars: dict[str, str] = {}
            if resolved_template:
                metadata_path = team.get_template_metadata_path(resolved_template)
                if os.path.isfile(metadata_path):
                    with open(metadata_path) as f:
                        metadata = _json.load(f)
                    if not repo_url:
                        repo_url = metadata.get("repo_url", "")
                    if not odoo_image:
                        odoo_image = metadata.get("odoo_image", "")
                    if not git_user:
                        git_user = metadata.get("git_user", "")
                    if extra_dict is None:
                        raw = metadata.get("extra_addons")
                        if raw:
                            extra_dict = _normalize_extra_addons(raw) or None
                    if not auto_install_raw:
                        auto_install_raw = metadata.get("auto_install_modules", "")
                    template_env_vars = system_ops._template_env_vars(
                        metadata, resolved_template
                    )
                    if not repo_url and metadata.get("local_path"):
                        if settings.allow_local_path:
                            local_path_from_meta = metadata["local_path"]
                        else:
                            return JSONResponse(
                                {
                                    "ok": False,
                                    "error": (
                                        f"Template '{resolved_template}' was saved "
                                        "from a live-mounted environment and has no "
                                        "repo_url. Set allow_local_path = true in "
                                        "oduflow.toml [server], or provide "
                                        "repo_url explicitly."
                                    ),
                                },
                                status_code=400,
                            )

            if not repo_url and not local_path_from_meta:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "repo_url is required (not found in template metadata either).",
                    },
                    status_code=400,
                )
            if not odoo_image:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "odoo_image is required (not found in template metadata either).",
                    },
                    status_code=400,
                )
            auto_install_list = (
                [m.strip() for m in auto_install_raw.split(",") if m.strip()]
                if auto_install_raw
                else []
            )
            result = await _offload(
                env_ops.create_environment,
                settings,
                team,
                branch,
                repo_url,
                odoo_image,
                env_name=env_name,
                template_name=resolved_template,
                extra_addons=extra_dict,
                git_user=git_user,
                auto_install_modules=auto_install_list or None,
                # Template env vars are the baseline; the request overrides
                # only the keys it names (same merge as the MCP tool).
                env_vars={**template_env_vars, **env_vars} or None,
                local_path=local_path_from_meta,
                hostname=hostname,
            )
            payload: dict[str, Any] = {"ok": True, "result": result}
            if from_production:
                # Which managed template the copy went through, so the dashboard
                # can name it (and the refresh path) in its confirmation.
                payload["from_production"] = from_production
                payload["template_name"] = resolved_template
            return JSONResponse(payload)
        except FlowError as e:
            # FlowError is an "expected" business error, but for create it is the
            # only record of WHY the environment failed to build (overlay mount,
            # disk/quota, git auth, …): the response often never reaches the
            # browser on multi-minute creates, so log it here to keep the reason
            # in the server journal.
            logger.warning("create_environment failed for %s: %s", env_name, e)
            return _error_response(e)
        except ValueError as e:
            # Deliberate input validation raised deeper in the create path
            # (unsupported hostname for the routing mode, malformed team
            # hostname, …). Same policy as the MCP tools' handle_errors: these
            # messages are authored for the user, so surface them as 400
            # instead of masking them behind a 500.
            logger.warning("create_environment rejected %s: %s", env_name, e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(env_name)

    def api_logs(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            n = int(request.query_params.get("n", "200"))
        except (ValueError, TypeError):
            n = 200
        container = request.query_params.get("container", "")
        try:
            logs = get_environment_logs(
                get_settings(),
                branch,
                n_lines=n,
                container_name=container,
                team=_get_ui_team(request),
            )
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_logs")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_stats(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            containers = get_container_stats(settings, team)
            system = get_system_stats()
            # Cached only — one small JSON read; recomputing storage is an
            # explicit action (the refresh button / the refresh endpoints).
            storage = read_storage_cache(team)
            return JSONResponse(
                {
                    "ok": True,
                    "containers": containers,
                    "system": system,
                    "storage": storage,
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_stats")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_storage_refresh(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            entry = refresh_env_storage(get_settings(), _get_ui_team(request), branch)
            return JSONResponse({"ok": True, "storage": entry})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_storage_refresh")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_usage(request: Request) -> JSONResponse:
        """Cached per-team usage + quotas — the read side for external
        billing/quota tooling (see api_usage_refresh for recomputation)."""
        try:
            team = _get_ui_team(request)
            return JSONResponse(
                {
                    "ok": True,
                    "team_id": team.team_id,
                    "quotas": {
                        "db_quota_gb": team.db_quota_gb,
                        "disk_quota_gb": team.disk_quota_gb,
                    },
                    "usage": read_storage_cache(team),
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_usage")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_usage_refresh(request: Request) -> JSONResponse:
        """Recompute storage for every environment plus team totals. Heavy
        (walks every workspace); meant for operator tooling on a schedule."""
        try:
            team = _get_ui_team(request)
            usage = refresh_team_storage(get_settings(), team)
            return JSONResponse(
                {
                    "ok": True,
                    "team_id": team.team_id,
                    "quotas": {
                        "db_quota_gb": team.db_quota_gb,
                        "disk_quota_gb": team.disk_quota_gb,
                    },
                    "usage": usage,
                }
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_usage_refresh")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_templates(request: Request) -> JSONResponse:
        # Read-only: no lock, so listing templates never bounces off (or delays)
        # a publish. Mirrors the list_templates MCP tool.
        team = _get_ui_team(request)
        try:
            templates = system_ops.list_templates(get_settings(), team)
            return JSONResponse({"ok": True, "templates": templates})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_templates")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_import_template_from_odoo(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "JSON body must be an object"},
                status_code=400,
            )

        # "odoo_url" is the legacy field name the dashboard form still posts.
        source = str(body.get("source") or body.get("odoo_url") or "").strip()
        master_pwd = str(body.get("master_pwd") or "")
        db_name = str(body.get("db_name") or "").strip()
        template_name = str(body.get("template_name") or "").strip()
        without_filestore = body.get("without_filestore", False)
        overwrite = body.get("overwrite", False)
        refresh = body.get("refresh", False)
        s3_endpoint = str(body.get("s3_endpoint") or "").strip()
        s3_access_key = str(body.get("s3_access_key") or "")
        s3_secret_key = str(body.get("s3_secret_key") or "")
        s3_region = str(body.get("s3_region") or "").strip()
        for flag_name, flag in (
            ("without_filestore", without_filestore),
            ("overwrite", overwrite),
            ("refresh", refresh),
        ):
            if not isinstance(flag, bool):
                return JSONResponse(
                    {"ok": False, "error": f"{flag_name} must be a boolean"},
                    status_code=400,
                )
        if not source and not refresh:
            return JSONResponse(
                {"ok": False, "error": "source is required"}, status_code=400
            )
        # Only the Odoo database-manager path needs the master password.
        if not master_pwd and source.startswith(("http://", "https://")):
            return JSONResponse(
                {"ok": False, "error": "master_pwd is required"}, status_code=400
            )
        if not template_name:
            return JSONResponse(
                {"ok": False, "error": "template_name is required"}, status_code=400
            )
        try:
            validate_template_name(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        # New templates need only their own key; replacements also protect
        # environments while changing their overlay lower layer.
        try:
            with _template_locks(
                team,
                template_name,
                operation="import_template",
                team_wide=overwrite or refresh,
            ):
                result = await _offload(
                    system_ops.import_template,
                    get_settings(),
                    team,
                    source=source,
                    master_pwd=master_pwd,
                    db_name=db_name,
                    template_name=template_name,
                    without_filestore=without_filestore,
                    overwrite=overwrite,
                    refresh=refresh,
                    s3_endpoint=s3_endpoint,
                    s3_access_key=s3_access_key,
                    s3_secret_key=s3_secret_key,
                    s3_region=s3_region,
                )
            payload = {
                "template_name": result.get("template_name"),
                "source_url": result.get("source_url"),
                "source_db": result.get("source_db"),
                "odoo_version": result.get("odoo_version"),
                "odoo_image": result.get("odoo_image"),
                "template_db": result.get("template_db"),
                "includes_filestore": result.get("includes_filestore"),
                "zip_size_mb": result.get("zip_size_mb"),
                "restore_seconds": result.get("restore_seconds"),
                "affected_envs": result.get("affected_envs", []),
                "remount_failures": result.get("remount_failures", []),
            }
            if "dump_reloaded" in result:  # s3 / local-path / refresh engine
                payload.update(
                    {
                        "status": result.get("status"),
                        "dump_reloaded": result.get("dump_reloaded"),
                        "downloaded_files": result.get("downloaded_files"),
                        "downloaded_mb": result.get("downloaded_mb"),
                        "reused_files": result.get("reused_files"),
                        "removed_files": result.get("removed_files"),
                    }
                )
            return JSONResponse({"ok": True, "result": payload})
        except FlowError as e:
            logger.warning(
                "import_template failed for template %s: %s", template_name, e
            )
            return _error_response(e)
        except Exception:
            logger.exception(
                "Unexpected error in api_import_template_from_odoo for template %s",
                template_name,
            )
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_template_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            with _template_locks(
                team, name, operation="delete_template", team_wide=True
            ):
                result = system_ops.delete_template(get_settings(), team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_template_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_template_rename(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        new_name = str((body or {}).get("new_name") or "").strip()
        if not new_name:
            return JSONResponse(
                {"ok": False, "error": "new_name is required"}, status_code=400
            )
        # Both names: the destination needs a key too, or an import into
        # `new_name` could create that template between rename_template's
        # "does the target exist?" check and its os.rename.
        try:
            with _template_locks(
                team, name, new_name, operation="rename_template", team_wide=True
            ):
                result = await _offload(
                    system_ops.rename_template, get_settings(), team, name, new_name
                )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_template_rename")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_template_metadata(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            result = system_ops.get_template_metadata(team, name)
            return JSONResponse({"ok": True, **result})
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except UnicodeDecodeError:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Template metadata is not valid UTF-8.",
                },
                status_code=400,
            )
        except Exception:
            logger.exception("Unexpected error in api_template_metadata")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_template_metadata_update(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            validate_template_name(name)
            body = await request.json()
        except (TypeError, ValueError) as e:
            message = str(e) or "Invalid JSON body"
            return JSONResponse({"ok": False, "error": message}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "JSON body must be an object"},
                status_code=400,
            )

        content = body.get("content")
        revision = body.get("revision")
        if not isinstance(content, str):
            return JSONResponse(
                {"ok": False, "error": "content must be a string"}, status_code=400
            )
        if not isinstance(revision, str):
            return JSONResponse(
                {"ok": False, "error": "revision must be a string"}, status_code=400
            )

        # Template key, not the team lock: this rewrites one metadata.json and
        # remounts nothing. Concurrent editors are already rejected by the
        # revision check inside update_template_metadata; the key is what keeps
        # a publish or an import into this template from interleaving.
        try:
            with _template_locks(
                team, name, operation="update_template_metadata", team_wide=False
            ):
                result = system_ops.update_template_metadata(
                    team, name, content, revision
                )
            return JSONResponse({"ok": True, **result})
        except ConflictError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        except (TypeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_template_metadata_update")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    # --- Import from Odoo.sh (push-based template ingest) ------------------

    def _import_token_value(request: Request) -> str:
        """Read the import token from the Authorization: Bearer header only.

        A ``?token=`` query param is deliberately NOT accepted: these endpoints
        bypass UI session authentication, so the token is the sole credential.
        Tokens in URLs leak into proxy/CDN access logs and Referer headers. The
        official ``import-odoo.sh`` client always sends the Bearer header."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer ") :].strip()
        return ""

    def _resolve_import_token(
        request: Request,
    ) -> tuple[TeamSettings, dict[str, object]]:
        return import_tokens.load_token(get_settings(), _import_token_value(request))

    def _import_progress(team: TeamSettings, template_name: str) -> dict[str, object]:
        """Derive resumable upload progress from the import staging directory.

        Before promotion, live template files are deliberately ignored so an
        overwrite cannot fake progress from old data. A ``.promoted`` marker
        explicitly switches missing artifacts to the live copy, allowing a
        failed final restore to retry without re-uploading moved files.
        """
        staging = team.get_import_staging_dir(template_name)  # validates name
        promoted = os.path.isfile(os.path.join(staging, ".promoted"))
        live_dir = team.get_template_dir(template_name)
        staged_meta = os.path.join(staging, "metadata.json")
        staged_dump = os.path.join(staging, "dump.sql.gz")
        live_meta = team.get_template_metadata_path(template_name)
        live_dump = os.path.join(live_dir, "dump.sql.gz")
        manifest = os.path.isfile(staged_meta) or (
            promoted and os.path.isfile(live_meta)
        )
        dump = os.path.isfile(staged_dump) or (promoted and os.path.isfile(live_dump))
        # dump_bytes lets a chunked upload resume mid-file: bytes already on the
        # server, whether the dump is complete (final file) or partial (.part).
        if dump:
            dump_path = staged_dump if os.path.isfile(staged_dump) else live_dump
            dump_bytes = os.path.getsize(dump_path)
        else:
            dpart = os.path.join(staging, "dump.sql.gz.part")
            dump_bytes = os.path.getsize(dpart) if os.path.isfile(dpart) else 0
        staged_fs_dir = os.path.join(staging, "filestore")
        fs_dir = (
            staged_fs_dir
            if os.path.isdir(staged_fs_dir)
            else team.get_template_filestore_path(template_name)
            if promoted
            else staged_fs_dir
        )
        chunks: list[str] = []
        if os.path.isdir(fs_dir):
            # A chunk directory appears only after its tar was extracted in
            # full and atomically renamed into place (extract_filestore_chunk),
            # so its presence means that chunk is complete.
            for entry in os.listdir(fs_dir):
                if (
                    re.fullmatch(r"[0-9a-f]{2}", entry) or entry == "checklist"
                ) and os.path.isdir(os.path.join(fs_dir, entry)):
                    chunks.append(entry)
        # File-backed addons (Enterprise/Themes/private extras): a dir under
        # addons/ appears only after its tar fully extracted (extract_addon_dir).
        addons_dir = os.path.join(staging, "addons")
        addons: list[str] = []
        if os.path.isdir(addons_dir):
            for entry in os.listdir(addons_dir):
                if not entry.startswith(".") and os.path.isdir(
                    os.path.join(addons_dir, entry)
                ):
                    addons.append(entry)
        # Remote addons (cloned server-side) are announced in addons.json only.
        remote_addons: list[str] = []
        addons_manifest = os.path.join(staging, "addons.json")
        if os.path.isfile(addons_manifest):
            try:
                with open(addons_manifest) as f:
                    for e in json.load(f):
                        if (
                            isinstance(e, dict)
                            and e.get("kind") == "remote"
                            and e.get("name")
                        ):
                            remote_addons.append(str(e["name"]))
            except (OSError, ValueError):
                pass
        # Partial addon tars (chunked upload in progress): received bytes per
        # addon, so a resumed run can restart an incomplete one.
        addon_bytes: dict[str, int] = {}
        if os.path.isdir(staging):
            for entry in os.listdir(staging):
                if entry.startswith(".addon_") and entry.endswith(".tar.part"):
                    nm = entry[len(".addon_") : -len(".tar.part")]
                    addon_bytes[nm] = os.path.getsize(os.path.join(staging, entry))
        return {
            "manifest": manifest,
            "dump": dump,
            "dump_bytes": dump_bytes,
            "filestore_chunks": sorted(chunks),
            "addons": sorted(addons),
            "addon_bytes": addon_bytes,
            "remote_addons": sorted(set(remote_addons)),
        }

    def import_odoo_script(request: Request) -> Response:
        script = (_TEMPLATE_DIR / "import-odoo.sh").read_text(encoding="utf-8")
        return Response(
            script,
            media_type="text/x-shellscript; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def api_import_token(request: Request) -> JSONResponse:
        """Mint a short-lived import token for a target template (UI-authed).

        Optional booleans ``with_enterprise`` / ``with_themes`` /
        ``with_extra_addons`` append the matching ``--with-*`` flags to the
        returned command so the checkboxes in the import dialog drive what the
        Odoo.sh client downloads. ``addon_error_policy`` is stored with the
        token and controls strict versus best-effort addon wiring at finalize.
        """
        team = _get_ui_team(request)
        with_flags: list[str] = []
        try:
            body = await request.body()
            data = json.loads(body or b"{}") or {}
            template_name = str(data.get("template_name") or "").strip()
            if not template_name:
                return JSONResponse(
                    {"ok": False, "error": "template_name is required"},
                    status_code=400,
                )
            from oduflow.naming import validate_template_name

            validate_template_name(template_name)
            if data.get("with_enterprise"):
                with_flags.append("--with-enterprise")
            if data.get("with_themes"):
                with_flags.append("--with-themes")
            if data.get("with_extra_addons"):
                with_flags.append("--with-extra-addons")
            addon_error_policy = str(
                data.get("addon_error_policy")
                or import_tokens.ADDON_ERROR_POLICY_STRICT
            )
            record = import_tokens.create_token(
                team,
                template_name,
                addon_error_policy=addon_error_policy,
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_import_token")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

        base = str(request.base_url).rstrip("/")
        # Behind a TLS-terminating proxy request.base_url is http://; honour
        # X-Forwarded-Proto so the pasted command never sends the Bearer token
        # over plaintext (and -L follows any residual http->https redirect).
        if _is_secure_request(request) and base.startswith("http://"):
            base = "https://" + base[len("http://") :]
        command = (
            f"curl -sSfL {base}/import-odoo.sh | bash -s -- "
            f"--server {base} --token {record['token']}"
        )
        if with_flags:
            command += " " + " ".join(with_flags)
        return JSONResponse(
            {
                "ok": True,
                "token": record["token"],
                "template_name": template_name,
                "expires_at": record["expires_at"],
                "command": command,
            }
        )

    def api_import_status(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
            progress = _import_progress(team, str(record["template_name"]))
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "template_name": record["template_name"],
                "progress": progress,
                "expires_at": record["expires_at"],
            }
        )

    async def api_import_manifest(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        body = await request.body()
        try:
            manifest = json.loads(body or b"{}")
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid manifest JSON"}, status_code=400
            )

        template_name = str(record["template_name"])
        branch = str(manifest.get("odoo_branch") or "").strip()  # e.g. "18.0"
        metadata = {
            "odoo_image": f"odoo:{branch}" if branch else "",
            "repo_url": "",
            "source": "odoo.sh",
            "source_db": manifest.get("name", ""),
            "source_revision": manifest.get("revision", ""),
            "source_repository": manifest.get("repository", ""),
            "odoo_version": branch,
            "modules": manifest.get("installed_modules", {}),
            "backup_datetime_utc": manifest.get("backup_datetime_utc", ""),
        }
        try:
            staging = team.get_import_staging_dir(template_name)
            os.makedirs(staging, exist_ok=True)
            # 0600 from birth: the staged file is promoted into the live
            # template dir with its mode, and template metadata is owner-only.
            fsutil.atomic_write_private_json(
                os.path.join(staging, "metadata.json"), metadata, sort_keys=False
            )
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    async def _receive_offset_chunk(
        request: Request, part_path: str, offset: int
    ) -> tuple[str, int]:
        """Append a byte-range chunk to ``part_path`` at ``offset`` (in order).

        Chunked uploads work around proxies that cap request body size (e.g.
        Cloudflare's 100 MB limit) by splitting a large payload into parts the
        server concatenates. Returns ``(state, size)`` where state is
        ``"written"`` (appended), ``"duplicate"`` (offset already covered — a
        resent part, discarded) or ``"gap"`` (offset beyond current size —
        caller should 409). ``offset == 0`` truncates/starts fresh. The request
        body is always drained so the connection closes cleanly.
        """
        cur = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        if offset > cur:
            async for _ in request.stream():
                pass
            return "gap", cur
        if 0 < offset < cur:
            async for _ in request.stream():
                pass
            return "duplicate", cur
        mode = "wb" if offset == 0 else "ab"
        with open(part_path, mode) as f:
            async for data in request.stream():
                f.write(data)
        return "written", os.path.getsize(part_path)

    async def api_import_dump(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        template_name = str(record["template_name"])
        try:
            staging = team.get_import_staging_dir(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        os.makedirs(staging, exist_ok=True)
        dest = os.path.join(staging, "dump.sql.gz")
        part = f"{dest}.part"

        offset_raw = request.query_params.get("offset")
        if offset_raw is None:
            # Legacy single-shot upload (no chunking): stream straight through.
            try:
                with open(part, "wb") as f:
                    async for chunk in request.stream():
                        f.write(chunk)
                os.replace(part, dest)
            except Exception:
                if os.path.exists(part):
                    os.remove(part)
                logger.exception("Failed to receive dump for %s", template_name)
                return JSONResponse(
                    {"ok": False, "error": "Internal server error."}, status_code=500
                )
            return JSONResponse({"ok": True})

        # Chunked upload: append at `offset`, complete when the part hits `total`.
        if os.path.isfile(dest):  # already assembled — retry after completion
            return JSONResponse(
                {"ok": True, "received": os.path.getsize(dest), "complete": True}
            )
        try:
            offset = int(offset_raw)
            total = int(request.query_params.get("total", "0"))
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "offset and total must be integers"},
                status_code=400,
            )
        try:
            state, size = await _receive_offset_chunk(request, part, offset)
        except Exception:
            logger.exception("Failed to receive dump chunk for %s", template_name)
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        if state == "gap":
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"unexpected offset {offset}, expected {size}",
                    "expected": size,
                },
                status_code=409,
            )
        complete = total > 0 and size >= total
        if complete:
            os.replace(part, dest)
        return JSONResponse({"ok": True, "received": size, "complete": complete})

    async def api_import_filestore(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        chunk = request.query_params.get("chunk", "").strip()
        if not re.fullmatch(r"[0-9a-f]{2}|checklist", chunk):
            return JSONResponse(
                {"ok": False, "error": f"Invalid filestore chunk '{chunk}'"},
                status_code=400,
            )
        template_name = str(record["template_name"])
        try:
            staging = team.get_import_staging_dir(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        fs_dir = os.path.join(staging, "filestore")
        os.makedirs(staging, exist_ok=True)
        fd, tmp_tar = tempfile.mkstemp(
            dir=staging, prefix=f".chunk_{chunk}_", suffix=".tar"
        )
        try:
            with os.fdopen(fd, "wb") as f:
                async for data in request.stream():
                    f.write(data)

            # Atomic: the chunk dir appears in staging only after the whole tar
            # extracted, so a truncated upload is never mistaken for a complete
            # chunk on resume.
            def _finish_filestore_chunk() -> None:
                with import_staging_locks.for_path(staging):
                    if os.path.isdir(os.path.join(fs_dir, chunk)):
                        return
                    system_ops.extract_filestore_chunk(tmp_tar, fs_dir, chunk)

            await _offload(_finish_filestore_chunk)
        except Exception:
            logger.exception(
                "Failed to receive filestore chunk %s for %s", chunk, template_name
            )
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            if os.path.exists(tmp_tar):
                os.remove(tmp_tar)
        return JSONResponse({"ok": True})

    _ADDON_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,63}$")

    def _record_import_addon(
        team: TeamSettings, template_name: str, entry: dict[str, object]
    ) -> None:
        """Append/replace an addon descriptor in the staging addons.json.

        Keyed by name so a resumed upload updates rather than duplicates.
        """
        staging = team.get_import_staging_dir(template_name)  # validates name
        os.makedirs(staging, exist_ok=True)
        path = os.path.join(staging, "addons.json")
        entries: list[Any] = []
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    entries = [
                        e
                        for e in loaded
                        if isinstance(e, dict) and e.get("name") != entry["name"]
                    ]
            except (OSError, ValueError):
                entries = []
        entries.append(entry)
        fd, staged_path = tempfile.mkstemp(
            dir=staging, prefix=".addons_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(entries, f, indent=2)
            os.replace(staged_path, path)
        finally:
            if os.path.exists(staged_path):
                os.remove(staged_path)

    async def api_import_addon(request: Request) -> JSONResponse:
        """Receive one addon directory (tar stream) that becomes a local
        (remote-less) extra-addons repo — Enterprise, Themes or a private extra
        repo that cannot be cloned."""
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        name = request.query_params.get("name", "").strip()
        if not _ADDON_NAME_RE.match(name):
            return JSONResponse(
                {"ok": False, "error": f"Invalid addon name '{name}'"},
                status_code=400,
            )
        branch = request.query_params.get("branch", "").strip()
        category = request.query_params.get("category", "").strip()
        template_name = str(record["template_name"])
        try:
            staging = team.get_import_staging_dir(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        addons_dir = os.path.join(staging, "addons")
        os.makedirs(staging, exist_ok=True)
        part = os.path.join(staging, f".addon_{name}.tar.part")

        def _finish(final_tar: str) -> None:
            """Extract the assembled tar and record the addon; clean up."""
            try:
                with import_staging_locks.for_path(staging):
                    if not os.path.isdir(os.path.join(addons_dir, name)):
                        system_ops.extract_addon_dir(final_tar, addons_dir, name)
                    _record_import_addon(
                        team,
                        template_name,
                        {
                            "name": name,
                            "kind": "local",
                            "branch": branch,
                            "origin_url": "",
                            "category": category,
                        },
                    )
            finally:
                if os.path.exists(final_tar):
                    os.remove(final_tar)

        offset_raw = request.query_params.get("offset")
        if offset_raw is None:
            # Legacy single-shot upload (no chunking).
            fd, final_tar = tempfile.mkstemp(
                dir=staging, prefix=f".addon_{name}_", suffix=".tar"
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    async for data in request.stream():
                        f.write(data)
                await _offload(_finish, final_tar)
            except Exception:
                logger.exception(
                    "Failed to receive addon %s for %s", name, template_name
                )
                return JSONResponse(
                    {"ok": False, "error": "Internal server error."}, status_code=500
                )
            finally:
                if os.path.exists(final_tar):
                    os.remove(final_tar)
            return JSONResponse({"ok": True})

        # Chunked upload: append at `offset`, extract when the part hits `total`.
        if os.path.isdir(os.path.join(addons_dir, name)):  # already extracted
            return JSONResponse({"ok": True, "complete": True})
        try:
            offset = int(offset_raw)
            total = int(request.query_params.get("total", "0"))
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "offset and total must be integers"},
                status_code=400,
            )
        try:
            state, size = await _receive_offset_chunk(request, part, offset)
        except Exception:
            logger.exception(
                "Failed to receive addon chunk %s for %s", name, template_name
            )
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        if state == "gap":
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"unexpected offset {offset}, expected {size}",
                    "expected": size,
                },
                status_code=409,
            )
        complete = total > 0 and size >= total
        if complete:
            fd, final_tar = tempfile.mkstemp(
                dir=staging, prefix=f".addon_{name}_", suffix=".tar"
            )
            os.close(fd)
            try:
                os.replace(part, final_tar)
                await _offload(_finish, final_tar)
            except Exception:
                logger.exception(
                    "Failed to finalize addon %s for %s", name, template_name
                )
                return JSONResponse(
                    {"ok": False, "error": "Internal server error."}, status_code=500
                )
            finally:
                if os.path.exists(final_tar):
                    os.remove(final_tar)
        return JSONResponse({"ok": True, "received": size, "complete": complete})

    async def api_import_addon_remote(request: Request) -> JSONResponse:
        """Announce a reachable extra repo (no files uploaded) — it is cloned
        from its origin at finalize so it stays updatable via Oduflow."""
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        name = str((body or {}).get("name") or "").strip()
        if not _ADDON_NAME_RE.match(name):
            return JSONResponse(
                {"ok": False, "error": f"Invalid addon name '{name}'"},
                status_code=400,
            )
        origin_url = str((body or {}).get("origin_url") or "").strip()
        if not origin_url:
            return JSONResponse(
                {"ok": False, "error": "origin_url is required"}, status_code=400
            )
        try:
            from oduflow.url_safety import assert_allowed_url

            assert_allowed_url(origin_url, require_https=True, allow_private=True)
        except FlowError as e:
            return _error_response(e)
        branch = str((body or {}).get("branch") or "").strip()
        template_name = str(record["template_name"])
        try:
            staging = team.get_import_staging_dir(template_name)

            def _record_remote_addon() -> None:
                with import_staging_locks.for_path(staging):
                    _record_import_addon(
                        team,
                        template_name,
                        {
                            "name": name,
                            "kind": "remote",
                            "branch": branch,
                            "origin_url": origin_url,
                            "category": "extra",
                        },
                    )

            await _offload(_record_remote_addon)
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    def api_import_finalize(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        template_name = str(record["template_name"])
        try:
            progress = _import_progress(team, template_name)
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if not progress["manifest"] or not progress["dump"]:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Manifest and SQL dump must be uploaded before finalize.",
                },
                status_code=400,
            )
        # Team lock: unlike the pull-based import, finalize can promote into an
        # existing template and so remounts live environments' overlays. The
        # uploads that precede it stay lock-free — only this swap is team-wide.
        try:
            with _template_locks(
                team,
                template_name,
                operation="finalize_imported_template",
                team_wide=True,
            ):
                result = system_ops.finalize_imported_template(
                    get_settings(),
                    team,
                    template_name,
                    staging_dir=team.get_import_staging_dir(template_name),
                    addon_error_policy=str(
                        record.get("addon_error_policy")
                        or import_tokens.ADDON_ERROR_POLICY_STRICT
                    ),
                )
                import_tokens.invalidate(team, str(record["token"]))
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_import_finalize")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_service_databases(request: Request) -> JSONResponse:
        try:
            databases = service_database_ops.list_databases(
                get_settings(), _get_ui_team(request)
            )
            return JSONResponse({"ok": True, "databases": databases})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_databases")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_service_database_create(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            cluster = (body.get("cluster") or "dev").strip()
            if not name:
                return JSONResponse(
                    {"ok": False, "error": "name is required."}, status_code=400
                )
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "A JSON body is required."}, status_code=400
            )
        if cluster not in service_database_ops.CLUSTERS:
            return JSONResponse(
                {"ok": False, "error": "cluster must be 'dev' or 'prod'."},
                status_code=400,
            )
        key = service_database_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="create_service_database")
        except BusyError as e:
            return _error_response(e)
        try:
            result = await _offload(
                service_database_ops.create_database,
                get_settings(),
                team,
                name,
                cluster=cluster,
            )
            return JSONResponse(
                {"ok": True, "result": result},
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_database_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_service_database_credentials(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_database_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="get_service_database")
        except BusyError as e:
            return _error_response(e)
        try:
            result = service_database_ops.get_database(
                get_settings(),
                team,
                name,
                reveal_password=True,
            )
            return JSONResponse(
                {"ok": True, "result": result},
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_database_credentials")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    async def api_service_database_rotate(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_database_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="rotate_service_database_password")
        except BusyError as e:
            return _error_response(e)
        try:
            result = await _offload(
                service_database_ops.rotate_password, get_settings(), team, name
            )
            return JSONResponse(
                {"ok": True, "result": result},
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_database_rotate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    async def api_service_database_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_database_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="delete_service_database")
        except BusyError as e:
            return _error_response(e)
        try:
            result = await _offload(
                service_database_ops.delete_database, get_settings(), team, name
            )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_database_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def _set_service_database_protection(
        request: Request, protected: bool
    ) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_database_lock_key(team.team_id, name)
        try:
            locks.acquire_env(
                key,
                operation=(
                    "protect_service_database"
                    if protected
                    else "unprotect_service_database"
                ),
            )
        except BusyError as e:
            return _error_response(e)
        try:
            result = service_database_ops.set_protected(team, name, protected)
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error while setting database protection")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_service_database_protect(request: Request) -> JSONResponse:
        return _set_service_database_protection(request, True)

    def api_service_database_unprotect(request: Request) -> JSONResponse:
        return _set_service_database_protection(request, False)

    def api_services(request: Request) -> JSONResponse:
        try:
            services = service_ops.list_services(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "services": services})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_services")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_service_create(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        # The body carries the service name, and the lock key is per service —
        # so it is read before the lock is taken (the MCP tool gets the name as
        # an argument and locks in its decorator).
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            image = (body.get("image") or "").strip()
            port = body.get("port")
            routes = body.get("routes")
            hostname = (body.get("hostname") or "").strip() or None
            host_mode = bool(body.get("host_mode", False))
            if not name or not image or (not port and not routes):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "name, image and either port or routes are required.",
                    },
                    status_code=400,
                )
            env_vars = _env_vars_from_body(body.get("env_vars")) or None
            volumes_raw = (body.get("volumes") or "").strip()
            parsed_volumes = (
                volume_ops.parse_volume_mounts(volumes_raw) if volumes_raw else None
            )
            privileged = bool(body.get("privileged", False))
            net_admin = bool(body.get("net_admin", False))
            cap_add = ["NET_ADMIN"] if net_admin else None
            command = _command_from_body(body.get("command")) or None
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        key = service_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="create_service")
        except BusyError as e:
            return _error_response(e)
        try:
            result = await _offload(
                service_ops.create_service,
                get_settings(),
                team,
                name,
                image,
                int(port) if port else None,
                hostname=hostname,
                env_vars=env_vars,
                host_mode=host_mode,
                volumes=parsed_volumes,
                cap_add=cap_add,
                privileged=privileged,
                routes=routes,
                command=command,
                runtime=body.get("runtime"),
            )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    async def api_service_update(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="update_service")
        except BusyError as e:
            return _error_response(e)
        try:
            # Body is optional; if missing or not JSON, treat as no overrides
            try:
                body = await request.json()
            except Exception:
                body = {}

            # "env_vars" present in the body is a full replacement (an empty
            # value clears every variable); an absent key keeps the current
            # ones — see api_update.
            env_override = (
                _env_vars_from_body(body.get("env_vars"))
                if body and "env_vars" in body
                else None
            )

            # "volumes" follows the same rule as "env_vars": present in the
            # body means a full replacement, and an empty value unmounts every
            # volume — that is the only way the edit form can remove one.
            volume_override = None
            if body and "volumes" in body:
                volume_override = volume_ops.parse_volume_mounts(
                    (body.get("volumes") or "").strip()
                )

            image_override = (body.get("image") or "").strip() or None if body else None
            hostname_override = (
                (body.get("hostname") or "").strip() or None if body else None
            )
            port_raw = body.get("port") if body else None
            port_override = int(port_raw) if port_raw else None
            routes_override = body.get("routes") if body and "routes" in body else None
            host_mode_override = (
                bool(body["host_mode"])
                if body and "host_mode" in body and body["host_mode"] is not None
                else None
            )
            privileged_override = (
                bool(body["privileged"])
                if body and "privileged" in body and body["privileged"] is not None
                else None
            )
            cap_add_override = None
            if body and "net_admin" in body and body["net_admin"] is not None:
                cap_add_override = ["NET_ADMIN"] if bool(body["net_admin"]) else []

            command_override = None
            if body and "command" in body and body["command"] is not None:
                command_override = _command_from_body(body["command"])

            result = await _offload(
                service_ops.update_service,
                get_settings(),
                team,
                name,
                env_override=env_override,
                image_override=image_override,
                port_override=port_override,
                hostname_override=hostname_override,
                host_mode_override=host_mode_override,
                volume_override=volume_override,
                cap_add_override=cap_add_override,
                privileged_override=privileged_override,
                routes_override=routes_override,
                command_override=command_override,
                runtime_override=body.get("runtime") if body else None,
            )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_update")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_service_config(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            config = service_ops.get_service_config(get_settings(), team, name)
            return JSONResponse({"ok": True, "config": config})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_config")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_service_restart(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="restart_service")
        except BusyError as e:
            return _error_response(e)
        try:
            result = service_ops.restart_service(get_settings(), team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_restart")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    async def api_service_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        # The body is optional, and an absent "save_preset" keeps the preset —
        # what deleting a service has always done.
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        # An explicit null counts as absent: only a real false drops the preset.
        save_preset_raw = body.get("save_preset")
        save_preset = True if save_preset_raw is None else bool(save_preset_raw)
        key = service_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="delete_service")
        except BusyError as e:
            return _error_response(e)
        try:
            result = await _offload(
                service_ops.delete_service,
                get_settings(),
                team,
                name,
                save_preset=save_preset,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def _set_service_protection(request: Request, protected: bool) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_lock_key(team.team_id, name)
        try:
            locks.acquire_env(
                key,
                operation="protect_service" if protected else "unprotect_service",
            )
        except BusyError as e:
            return _error_response(e)
        try:
            result = service_ops.set_service_protected(
                get_settings(), team, name, protected
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error while setting service protection")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_service_protect(request: Request) -> JSONResponse:
        return _set_service_protection(request, True)

    def api_service_unprotect(request: Request) -> JSONResponse:
        return _set_service_protection(request, False)

    def api_service_logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            n = int(request.query_params.get("n", "200"))
        except (ValueError, TypeError):
            n = 200
        try:
            logs = service_ops.get_service_logs(
                get_settings(), _get_ui_team(request), name, n
            )
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_logs")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_service_presets(request: Request) -> JSONResponse:
        try:
            presets = service_presets.list_presets(_get_ui_team(request))
            return JSONResponse({"ok": True, "presets": presets})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_presets")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_service_restore(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        # Body first, then the per-service lock — see api_service_create.
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            image = (body.get("image") or "").strip()
            port = body.get("port")
            routes = body.get("routes")
            hostname = (body.get("hostname") or "").strip() or None
            host_mode = bool(body.get("host_mode", False))
            if not name or not image or (not port and not routes):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "name, image and either port or routes are required.",
                    },
                    status_code=400,
                )
            env_vars = _env_vars_from_body(body.get("env_vars")) or None
            volumes_raw = (body.get("volumes") or "").strip()
            parsed_volumes = (
                volume_ops.parse_volume_mounts(volumes_raw) if volumes_raw else None
            )
            privileged = bool(body.get("privileged", False))
            net_admin = bool(body.get("net_admin", False))
            cap_add = ["NET_ADMIN"] if net_admin else None
            command = _command_from_body(body.get("command")) or None
            runtime = body.get("runtime")
            if runtime is None:
                try:
                    runtime = service_presets.get_preset(team, name).get("runtime")
                except NotFoundError:
                    pass
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_restore")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        key = service_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="restore_service")
        except BusyError as e:
            return _error_response(e)
        try:
            service_ops.assert_service_not_protected(team, name, "restoring")
            result = await _offload(
                service_ops.create_service,
                get_settings(),
                team,
                name,
                image,
                int(port) if port else None,
                hostname=hostname,
                env_vars=env_vars,
                host_mode=host_mode,
                volumes=parsed_volumes,
                cap_add=cap_add,
                privileged=privileged,
                routes=routes,
                command=command,
                runtime=runtime,
            )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_restore")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_service_preset_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = service_preset_lock_key(team.team_id)
        try:
            locks.acquire_env(key, operation="delete_service_preset")
        except BusyError as e:
            return _error_response(e)
        try:
            service_presets.delete_preset(team, name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_preset_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_volumes(request: Request) -> JSONResponse:
        try:
            vols = volume_ops.list_volumes(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "volumes": vols})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_volumes")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_volume_create(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        # Body first, then the per-volume lock — see api_service_create.
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            description = (body.get("description") or "").strip()
            if not name:
                return JSONResponse(
                    {"ok": False, "error": "Volume name is required."},
                    status_code=400,
                )
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body."}, status_code=400
            )
        key = volume_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="create_volume")
        except BusyError as e:
            return _error_response(e)
        try:
            result = await _offload(
                volume_ops.create_volume,
                get_settings(),
                team,
                name,
                description=description,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_volume_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_volume_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        key = volume_lock_key(team.team_id, name)
        try:
            locks.acquire_env(key, operation="delete_volume")
        except BusyError as e:
            return _error_response(e)
        try:
            result = volume_ops.delete_volume(get_settings(), team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_volume_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_agent_guides_list(request: Request) -> JSONResponse:
        try:
            guides = []
            guides_dir = os.path.join(_get_ui_team(request).data_dir, "agent_guides")
            bundled_dir = _TEMPLATE_DIR / "agent_guides"
            seen = set()
            if os.path.isdir(guides_dir):
                for fname in sorted(os.listdir(guides_dir)):
                    if fname.endswith(".md"):
                        seen.add(fname)
                        guides.append(
                            {
                                "filename": fname,
                                "title": _guide_title(os.path.join(guides_dir, fname)),
                            }
                        )
            if bundled_dir.is_dir():
                for fpath in sorted(bundled_dir.iterdir()):
                    if fpath.suffix == ".md" and fpath.name not in seen:
                        guides.append(
                            {"filename": fpath.name, "title": _guide_title(str(fpath))}
                        )
            return JSONResponse({"ok": True, "guides": guides})
        except Exception:
            logger.exception("Unexpected error in api_agent_guides_list")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_agent_guide_get(request: Request) -> JSONResponse:
        try:
            filename = request.path_params["filename"]
            if not filename.endswith(".md") or "/" in filename or "\\" in filename:
                return JSONResponse(
                    {"ok": False, "error": "Invalid filename"}, status_code=400
                )
            guides_dir = os.path.join(_get_ui_team(request).data_dir, "agent_guides")
            guide_path = os.path.join(guides_dir, filename)
            content = ""
            if os.path.isfile(guide_path):
                with open(guide_path, "r", encoding="utf-8") as f:
                    content = f.read()
            elif (_TEMPLATE_DIR / "agent_guides" / filename).is_file():
                content = (_TEMPLATE_DIR / "agent_guides" / filename).read_text(
                    encoding="utf-8"
                )
            else:
                return JSONResponse(
                    {"ok": False, "error": "Guide not found"}, status_code=404
                )
            return JSONResponse({"ok": True, "content": content, "filename": filename})
        except Exception:
            logger.exception("Unexpected error in api_agent_guide_get")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_agent_info(request: Request) -> JSONResponse:
        """Whether the coding agent is enabled for this team, and its default
        type. The dashboard fetches this at boot to decide if the Agent Chat /
        Agent CLI actions are shown at all. Configuration lives in oduflow.toml
        ([team.X] agent_* keys); there is no runtime editing."""
        try:
            team = _get_ui_team(request)
            return JSONResponse(
                {
                    "ok": True,
                    "enabled": team.agent_enabled,
                    "default": agent_config.effective_agent_default(team),
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_agent_info")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_agent_acp_info(request: Request) -> JSONResponse:
        """Info the browser chat needs before connecting: the in-container
        working dir (used as ACP ``cwd``) and the stored session id to resume
        (or null to start fresh). See specs/0029-agent-console-and-chat.md."""
        try:
            from oduflow import agent_sessions
            from oduflow.naming import get_agent_checkout_dir

            branch = request.path_params["branch"]
            team = _get_ui_team(request)
            agent_type = agent_config.resolve_agent_type(
                request.query_params.get("type"), team
            )
            return JSONResponse(
                {
                    "ok": True,
                    "cwd": get_agent_checkout_dir(branch),
                    "type": agent_type,
                    "session_id": agent_sessions.get_session(team, branch, agent_type),
                    "history": agent_sessions.get_history(team, branch, agent_type),
                    "attachment_limits": {
                        "max_file_bytes": agent_uploads.MAX_FILE_BYTES,
                        "max_files": agent_uploads.MAX_FILES_PER_PROMPT,
                    },
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_agent_acp_info")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_agent_acp_session(request: Request) -> JSONResponse:
        """Persist or select a chat session for this environment and agent.

        An empty session id clears only the current selection, preserving the
        recent history for later recovery (including degenerate Codex
        ``session/new`` responses).
        """
        try:
            from oduflow import agent_sessions

            branch = request.path_params["branch"]
            team = _get_ui_team(request)
            body = await request.json()
            agent_type = agent_config.resolve_agent_type(body.get("type"), team)
            session_id = body.get("session_id")
            if session_id:
                title = str(body.get("title") or "").strip() or None
                agent_sessions.set_session(
                    team, branch, agent_type, str(session_id), title=title
                )
            else:
                agent_sessions.clear_current(team, branch, agent_type)
            return JSONResponse(
                {
                    "ok": True,
                    "session_id": agent_sessions.get_session(team, branch, agent_type),
                    "history": agent_sessions.get_history(team, branch, agent_type),
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_agent_acp_session")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_agent_acp_attachment_upload(request: Request) -> JSONResponse:
        """Stream one chat attachment, then copy it into the agent workspace."""
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        filename = request.query_params.get("name", "")
        try:
            agent_uploads.normalize_filename(filename)
        except agent_uploads.AttachmentError as e:
            return JSONResponse(
                {"ok": False, "error": str(e)}, status_code=e.status_code
            )

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > agent_uploads.MAX_FILE_BYTES:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": (
                                "Files must be no larger than "
                                f"{agent_uploads.MAX_FILE_BYTES // (1024 * 1024)} MiB."
                            ),
                        },
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"ok": False, "error": "Invalid Content-Length header."},
                    status_code=400,
                )

        size = 0
        too_large = False
        with tempfile.TemporaryFile() as source:
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > agent_uploads.MAX_FILE_BYTES:
                        too_large = True
                        continue
                    source.write(chunk)
            except ClientDisconnect:
                # Normal outcome, not an error: the browser aborts the XHR when
                # the user removes an in-flight attachment or closes the tab.
                return JSONResponse(
                    {"ok": False, "error": "Upload cancelled."}, status_code=400
                )
            if too_large:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            "Files must be no larger than "
                            f"{agent_uploads.MAX_FILE_BYTES // (1024 * 1024)} MiB."
                        ),
                    },
                    status_code=413,
                )

            try:
                locks.acquire_env(branch, team.team_id)
            except BusyError as e:
                return _error_response(e)
            try:
                source.seek(0)
                attachment = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: agent_uploads.store_attachment(
                        get_settings(),
                        team,
                        branch,
                        filename,
                        request.headers.get("content-type"),
                        source,
                        size,
                    ),
                )
                return JSONResponse({"ok": True, "attachment": attachment})
            except agent_uploads.AttachmentError as e:
                return JSONResponse(
                    {"ok": False, "error": str(e)}, status_code=e.status_code
                )
            except FlowError as e:
                return _error_response(e)
            except Exception:
                logger.exception("Unexpected Agent Chat attachment upload error")
                return JSONResponse(
                    {"ok": False, "error": "Internal server error."},
                    status_code=500,
                )
            finally:
                locks.release_env(branch)

    async def api_agent_acp_attachment_delete(request: Request) -> JSONResponse:
        """Remove one attachment that has not been sent in a prompt."""
        branch = request.path_params["branch"]
        upload_id = request.path_params["upload_id"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: agent_uploads.delete_attachment(
                    get_settings(), team, branch, upload_id
                ),
            )
            return JSONResponse({"ok": True})
        except agent_uploads.AttachmentError as e:
            return JSONResponse(
                {"ok": False, "error": str(e)}, status_code=e.status_code
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected Agent Chat attachment delete error")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_license(request: Request) -> JSONResponse:
        settings = get_settings()
        info = get_license_info(settings.etc_dir)
        return JSONResponse({"ok": True, "license": info.to_dict()})

    async def api_license_refresh(request: Request) -> JSONResponse:
        try:
            info, renewed = await _offload(refresh_license, get_settings().etc_dir)
            return JSONResponse(
                {"ok": True, "license": info.to_dict(), "renewed": renewed}
            )
        except (ValueError, OSError):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Unable to update the license. Check your payment and retry, or contact support.",
                },
                status_code=400,
            )

    async def api_license_checkout(request: Request) -> JSONResponse:
        try:
            url = await _offload(get_license_checkout_url, get_settings().etc_dir)
            return JSONResponse({"ok": True, "checkout_url": url})
        except (ValueError, OSError):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Unable to open checkout. Update license status or contact support.",
                },
                status_code=400,
            )

    async def api_version(request: Request) -> JSONResponse:
        """Compare the running version with the latest GitHub release.

        Called only when a user clicks the version in the header, so the
        outbound request to github.com is always something a person asked
        for. Failures are reported in the payload, never as a 5xx: "could
        not reach GitHub" is an answer the dialog shows, not a server error.
        """
        result = await _offload(updates.check_for_update)
        return JSONResponse({"ok": True, "version": result.to_dict()})

    async def api_license_activate(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            key_text = (body.get("key") or "").strip()
            if not key_text:
                return JSONResponse(
                    {"ok": False, "error": "License key is required."}, status_code=400
                )
            settings = get_settings()
            info = install_license_from_text(key_text, settings.etc_dir)
            return JSONResponse({"ok": True, "license": info.to_dict()})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_license_activate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_feedback_link(request: Request) -> JSONResponse:
        """Build the prefilled GitHub issue URL the dashboard opens in a tab.

        Built server-side so the issue forms, the diagnostics block, and the URL
        length budget have one owner (oduflow.feedback) shared with the
        report_issue MCP tool.
        """
        try:
            try:
                body = await request.json()
            except (UnicodeDecodeError, ValueError):
                return JSONResponse(
                    {"ok": False, "error": "Request body must be valid JSON."},
                    status_code=400,
                )

            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "error": "Request body must be a JSON object."},
                    status_code=400,
                )

            for field in ("details", "kind", "title"):
                if field in body and not isinstance(body[field], str):
                    return JSONResponse(
                        {"ok": False, "error": f"{field} must be a string."},
                        status_code=400,
                    )

            details = body.get("details", "").strip()
            if not details:
                return JSONResponse(
                    {"ok": False, "error": "Details are required."}, status_code=400
                )
            url = feedback.build_issue_url(
                kind=(body.get("kind") or feedback.DEFAULT_KIND).strip(),
                title=body.get("title", "").strip(),
                details=details,
                settings=get_settings(),
            )
            return JSONResponse({"ok": True, "url": url})
        except Exception:
            logger.exception("Unexpected error in api_feedback_link")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_extra_repos(request: Request) -> JSONResponse:
        from oduflow.extra_addons import list_extra_repos

        try:
            repos = list_extra_repos(_get_ui_team(request))
            return JSONResponse({"ok": True, "repos": repos})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repos")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_extra_repo_add(request: Request) -> JSONResponse:
        # No lock: extra_addons serialises every mutator per repo (see the MCP
        # extra-repo tools).
        team = _get_ui_team(request)
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            repo_url = (body.get("repo_url") or "").strip()
            git_user = (body.get("git_user") or "").strip()
            branches = body.get("branches") or []
            if not name or not repo_url:
                return JSONResponse(
                    {"ok": False, "error": "name and repo_url are required."},
                    status_code=400,
                )
            if not isinstance(branches, list) or any(
                not isinstance(b, str) for b in branches
            ):
                return JSONResponse(
                    {"ok": False, "error": "branches must be a list of names."},
                    status_code=400,
                )
            # Same URL validation + SSRF guard as the MCP add_extra_repo path.
            from oduflow.git_ops import validate_repo_url

            validate_repo_url(repo_url)

            from oduflow.extra_addons import clone_extra_repo

            result = await _offload(
                clone_extra_repo,
                team,
                name,
                repo_url,
                git_user=git_user,
                branches=branches,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_add")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_extra_repo_ls_remote(request: Request) -> JSONResponse:
        """List remote branches before cloning (add-repo wizard branch picker)."""
        team = _get_ui_team(request)
        try:
            body = await request.json()
            repo_url = (body.get("repo_url") or "").strip()
            git_user = (body.get("git_user") or "").strip()
            if not repo_url:
                return JSONResponse(
                    {"ok": False, "error": "repo_url is required."}, status_code=400
                )
            # SSRF guard: ls-remote runs server-side with team credentials, so
            # apply the same URL validation as the MCP add_extra_repo path.
            from oduflow.git_ops import validate_repo_url

            validate_repo_url(repo_url)

            from oduflow.extra_addons import list_remote_branches

            branches = await _offload(
                list_remote_branches, team, repo_url, git_user=git_user
            )
            return JSONResponse({"ok": True, "branches": branches})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_ls_remote")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_extra_repo_pull(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            from oduflow.extra_addons import fetch_extra_repo

            summary = await _offload(fetch_extra_repo, team, name)
            return JSONResponse({"ok": True, "result": summary})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_pull")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_extra_repo_protect(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            from oduflow.extra_addons import protect_extra_repo

            team = _get_ui_team(request)
            result = protect_extra_repo(team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_protect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_extra_repo_unprotect(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            from oduflow.extra_addons import unprotect_extra_repo

            team = _get_ui_team(request)
            result = unprotect_extra_repo(team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_unprotect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_extra_repo_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            from oduflow.extra_addons import delete_extra_repo

            delete_extra_repo(get_settings(), team, name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_credentials(request: Request) -> JSONResponse:
        from oduflow.git_ops import list_credentials

        try:
            team = _get_ui_team(request)
            creds = list_credentials(cred_file=team.git_credentials_file())
            return JSONResponse({"ok": True, "credentials": creds})
        except Exception:
            logger.exception("Unexpected error in api_credentials")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_credential_add(request: Request) -> JSONResponse:
        """Store a git credential.

        Two body shapes are accepted:

        * token-first (dashboard form): ``token`` plus ``host`` (default
          ``github.com``), optional ``username`` and optional ``repo_url`` to
          verify against with ``git ls-remote``;
        * legacy: ``repo_url`` carrying ``user:PAT@`` inline credentials.
        """
        team = _get_ui_team(request)
        try:
            body = await request.json()
            repo_url = (body.get("repo_url") or "").strip()
            token = (body.get("token") or "").strip()
            if not repo_url and not token:
                return JSONResponse(
                    {"ok": False, "error": "token (or repo_url) is required."},
                    status_code=400,
                )
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body."}, status_code=400
            )
        from oduflow import git_ops

        # Same key as the setup_repo_auth MCP tool: one writer at a time on the
        # team's credential store.
        key = credentials_lock_key(team.team_id)
        try:
            locks.acquire_env(key, operation="setup_repo_auth")
        except BusyError as e:
            return _error_response(e)
        try:
            if token:
                result = await _offload(
                    git_ops.store_credential,
                    host=(body.get("host") or "").strip() or "github.com",
                    token=token,
                    username=(body.get("username") or "").strip(),
                    verify_repo_url=repo_url,
                    cred_file=team.git_credentials_file(),
                )
            else:
                result = await _offload(
                    git_ops.setup_repo_auth,
                    repo_url,
                    cred_file=team.git_credentials_file(),
                )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_credential_add")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    async def api_credential_delete(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        key = credentials_lock_key(team.team_id)
        try:
            body = await request.json()
            host = (body.get("host") or "").strip()
            username = (body.get("username") or "").strip()
            if not host or not username:
                return JSONResponse(
                    {"ok": False, "error": "host and username are required."},
                    status_code=400,
                )
            from oduflow.git_ops import delete_credential

            # Rewrites the same store setup_repo_auth appends to.
            try:
                locks.acquire_env(key, operation="delete_credential")
            except BusyError as e:
                return _error_response(e)
            try:
                removed = delete_credential(
                    host, username, cred_file=team.git_credentials_file()
                )
            finally:
                locks.release_env(key)
            if not removed:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"Credential not found for {host}/{username}.",
                    },
                    status_code=404,
                )
            return JSONResponse(
                {"ok": True, "result": {"host": host, "username": username}}
            )
        except Exception:
            logger.exception("Unexpected error in api_credential_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_credential_validate(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            host = (body.get("host") or "").strip()
            username = (body.get("username") or "").strip()
            if not host or not username:
                return JSONResponse(
                    {"ok": False, "error": "host and username are required."},
                    status_code=400,
                )
            from oduflow.git_ops import validate_credential

            team = _get_ui_team(request)
            status = await _offload(
                validate_credential,
                host,
                username,
                cred_file=team.git_credentials_file(),
            )
            return JSONResponse({"ok": True, "status": status})
        except Exception:
            logger.exception("Unexpected error in api_credential_validate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_ssh_key(request: Request) -> JSONResponse:
        """The team's SSH public key (the response carries no private material)."""
        from oduflow import git_ops

        try:
            team = _get_ui_team(request)
            public_key, fingerprint = git_ops.ssh_key_info(team.ssh_dir())
            return JSONResponse(
                {"ok": True, "public_key": public_key, "fingerprint": fingerprint}
            )
        except Exception:
            logger.exception("Unexpected error in api_ssh_key")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_ssh_key_generate(request: Request) -> JSONResponse:
        """Create the team SSH key if absent; ``{"force": true}`` regenerates.

        Regeneration invalidates the old key on every host where it was
        registered — the dashboard confirms before sending force.
        """
        from oduflow import git_ops

        team = _get_ui_team(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        force = bool(body.get("force"))
        key = credentials_lock_key(team.team_id)
        try:
            locks.acquire_env(key, operation="generate_ssh_key")
        except BusyError as e:
            return _error_response(e)
        try:
            # One offloaded unit: keygen is a blocking subprocess, and reading
            # the result inside it keeps the ASGI event loop untouched.
            def _generate() -> tuple[str, str]:
                comment = git_ops.ssh_key_comment(team.team_id)
                git_ops.ensure_ssh_key(team.ssh_dir(), comment=comment, force=force)
                return git_ops.ssh_key_info(team.ssh_dir())

            public_key, fingerprint = await _offload(_generate)
            return JSONResponse(
                {"ok": True, "public_key": public_key, "fingerprint": fingerprint}
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_ssh_key_generate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(key)

    def api_secrets(request: Request) -> JSONResponse:
        """Names, types and timestamps only — never secret values."""
        try:
            team = _get_ui_team(request)
            return JSONResponse(
                {"ok": True, "secrets": secret_store.list_secrets(team)}
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_secrets")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_secret_set(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
            value = body.get("value")
            if not isinstance(value, str) or not value:
                return JSONResponse(
                    {"ok": False, "error": "A non-empty value is required."},
                    status_code=400,
                )
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body."}, status_code=400
            )
        try:
            result = secret_store.set_secret(team, name, value, body.get("value_type"))
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_secret_set")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_secret_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            secret_store.delete_secret(team, name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_secret_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_protect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            result = env_ops.protect_environment(get_settings(), team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_protect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_unprotect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            result = env_ops.unprotect_environment(get_settings(), team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_unprotect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_set_note(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            body = await request.json()
            note = body.get("note", "")
            result = env_ops.set_note(get_settings(), team, branch, note)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_set_note")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_mcp_access(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            token = env_ops.get_env_token(settings, team, branch)
            # The MCP endpoint is advertised on the same public origin as share
            # links: the team's own hostname in traefik mode, and the actual
            # request origin (including an upstream tunnel) in port mode.
            base = _public_base_url(request, settings, team)
            url = f"{base}/mcp/{quote(branch, safe='/')}"
            return JSONResponse(
                {"ok": True, "result": {"url": url, "token": token}},
                headers={"Cache-Control": "no-store"},
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_mcp_access")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def _public_base_url(
        request: Request, settings: Settings, team: TeamSettings
    ) -> str:
        """Origin share links and MCP endpoints should be opened on. In traefik
        mode that is the team's own hostname (the env pages are served there);
        port mode keeps the configured base or the request's own."""
        if settings.routing_mode == "traefik":
            return f"{settings.public_scheme_for(team)}://{team.hostname}"
        return str(request.base_url).rstrip("/")

    def _share_payload(
        request: Request,
        settings: Settings,
        team: TeamSettings,
        branch: str,
        record: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not record:
            return {"shared": False, "url": None, "created_at": None}
        page = ui_scope.PAGE_PREFIX + quote(branch, safe="/")
        key = quote(str(record["secret"]), safe="")
        return {
            "shared": True,
            "url": f"{_public_base_url(request, settings, team)}{page}?key={key}",
            "created_at": record.get("created_at"),
        }

    def _share_endpoint(
        request: Request, action: Callable[[TeamSettings, str], dict[str, Any] | None]
    ) -> JSONResponse:
        """Shared plumbing for the four share routes. Sharing is an operator
        action: a scoped session must not be able to read, mint or revoke the
        link it came in on (the ui_scope allowlist denies these paths too)."""
        branch = request.path_params["branch"]
        try:
            if getattr(request.state, "scoped_env", None):
                return JSONResponse(
                    {"ok": False, "error": "Not available for a shared link."},
                    status_code=403,
                )
            settings = get_settings()
            team = _get_ui_team(request)
            record = action(team, branch)
            return JSONResponse(
                {
                    "ok": True,
                    "result": _share_payload(request, settings, team, branch, record),
                },
                headers={"Cache-Control": "no-store"},
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in share endpoint")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_share_get(request: Request) -> JSONResponse:
        return _share_endpoint(
            request, lambda team, branch: env_share.get(team, branch)
        )

    def api_share_create(request: Request) -> JSONResponse:
        def action(team: TeamSettings, branch: str) -> dict[str, Any] | None:
            env_ops.require_environment(get_settings(), team, branch)
            env_share.create_or_get(team, branch)
            return env_share.get(team, branch)

        return _share_endpoint(request, action)

    def api_share_rotate(request: Request) -> JSONResponse:
        def action(team: TeamSettings, branch: str) -> dict[str, Any] | None:
            env_ops.require_environment(get_settings(), team, branch)
            env_share.rotate(team, branch)
            return env_share.get(team, branch)

        return _share_endpoint(request, action)

    def api_share_revoke(request: Request) -> JSONResponse:
        def action(team: TeamSettings, branch: str) -> dict[str, Any] | None:
            env_share.revoke(team, branch)
            return None

        return _share_endpoint(request, action)

    def api_env_users(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            users = odoo_ops.list_env_users(settings, team, branch)
            return JSONResponse({"ok": True, "result": {"users": users}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_env_users")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_connect_as(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        lock_acquired = False
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            # Read the body before taking the env lock (same order as
            # api_update): a client that stalls mid-body would otherwise hold
            # the branch lock for as long as it likes, 409-ing every other
            # operation on the branch and blocking team-level ones.
            body = await request.json()
            user = (body.get("user") or "admin").strip() or "admin"
            locks.acquire_env(branch, team.team_id, operation="connect_as_user")
            lock_acquired = True
            activity.touch(team, branch)
            result = await _offload(
                odoo_ops.connect_as_user, settings, team, branch, user
            )
            return JSONResponse(
                {
                    "ok": True,
                    "result": {
                        "url": result["url"],
                        "login": result["login"],
                        "uid": result["uid"],
                        "expires_at": result["expires_at"],
                        "cookie": {
                            "name": "session_id",
                            "value": result["sid"],
                            "domain": result["cookie_domain"],
                            "path": "/",
                        },
                    },
                }
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_connect_as")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            if lock_acquired:
                locks.release_env(branch)

    async def api_connect_open(request: Request) -> Response:
        """ "Open": mint a session and land the browser in the env already
        logged in.

        The dashboard's Open button can't set the session_id cookie from
        JavaScript: Odoo issues session_id as HttpOnly and cookies are shared
        across localhost ports, so a ``document.cookie`` write is silently
        dropped and the env keeps the browser's stale session.

        Port / same-host mode: the dashboard and env share a host, so an HTTP
        Set-Cookie here (which overrides the HttpOnly cookie) reaches the env and
        the redirect lands authenticated.

        Traefik mode: the env lives on its own host, so a cookie set here would
        not reach it (and a parent-domain cookie would not override a stale
        host-only session_id on the env host). Instead mint the session, stash it
        behind a one-time token, and 303 the browser to the env's own
        ``/oduflow-connect`` (routed to Oduflow by Traefik), which sets the cookie
        host-only there.
        """
        branch = request.path_params["branch"]
        lock_acquired = False
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            locks.acquire_env(branch, team.team_id, operation="connect_as_user")
            lock_acquired = True
            user = (request.query_params.get("user") or "admin").strip() or "admin"
            activity.touch(team, branch)
            result = await _offload(
                odoo_ops.connect_as_user, settings, team, branch, user
            )
            if settings.routing_mode == "traefik":
                env_host = result["cookie_domain"]
                token = connect_tokens.issue(env_host, result["sid"])
                landing = (
                    f"{settings.public_scheme_for(team)}://{env_host}"
                    f"/oduflow-connect?token={token}"
                )
                return RedirectResponse(landing, status_code=303)
            response: Response = RedirectResponse(result["url"], status_code=303)
            # Host-only cookie (no domain): scoped to the dashboard's host and,
            # since cookies ignore ports, sent to the env on the same host.
            # HttpOnly mirrors Odoo's own session_id so it overrides it cleanly.
            response.set_cookie(
                "session_id",
                result["sid"],
                path="/",
                httponly=True,
                samesite="lax",
            )
            return response
        except FlowError as e:
            return Response(
                f"Connect failed: {_public_flow_error(e, context='Connect As')}",
                status_code=_flow_error_status(e),
                media_type="text/plain",
            )
        except Exception:
            logger.exception("Unexpected error in api_connect_open")
            return Response(
                "Connect failed: Internal server error.",
                status_code=500,
                media_type="text/plain",
            )
        finally:
            if lock_acquired:
                locks.release_env(branch)

    async def api_connect_land(request: Request) -> Response:
        """Traefik cross-subdomain Connect As landing, served ON the env host.

        Consumes the one-time token minted by :func:`api_connect_open`, sets the
        env's ``session_id`` cookie HOST-ONLY (no ``Domain`` → scoped to this env
        host, so it overrides any stale host-only cookie Odoo left here), and
        303-redirects to ``/web`` already authenticated. No dashboard session is
        required or present on this host; the token is the sole credential.
        """
        token = request.query_params.get("token") or ""
        env_host = request.url.hostname or ""
        sid = connect_tokens.consume(token, env_host)
        if not sid:
            return Response(
                "Connect link is invalid or expired. Reopen it from the dashboard.",
                status_code=400,
                media_type="text/plain",
            )
        # Same-host redirect: a relative Location keeps whatever scheme and
        # host the browser really reached us on, with no header interpretation
        # — the one-time token is already consumed, so a wrong absolute scheme
        # (e.g. a terminator that doesn't send X-Forwarded-Proto) would leave
        # the user with a dead link.
        response: Response = RedirectResponse("/web", status_code=303)
        response.set_cookie(
            "session_id",
            sid,
            path="/",
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
        )
        return response

    async def api_artifact_download(request: Request) -> Response:
        """Serve a file an MCP tool generated inside an environment, once.

        This is the way an artifact reaches an agent without passing through its
        context window: the tool stashes the bytes and hands back a URL, the
        agent pipes it straight to disk. There is no dashboard session on the
        agent side, so the one-time token in the query string is the credential.
        """
        artifact = artifact_tokens.consume(request.query_params.get("token") or "")
        if artifact is None:
            return Response(
                "Download link is invalid, expired, or already used.",
                status_code=404,
                media_type="text/plain",
            )
        filename, content = artifact
        return Response(
            content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def ws_terminal(websocket: WebSocket) -> None:
        branch = websocket.path_params["branch"]
        await websocket.accept()
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import get_db_name, get_resource_name, validate_env_name

            try:
                validate_env_name(branch)
            except ValueError as e:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1008)
                return

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()
            container_name = get_resource_name(
                branch, "odoo", settings.prefix, team.team_id
            )
            db_name = get_db_name(branch, team.team_id)

            try:
                container = client.containers.get(container_name)
            except _docker.errors.NotFound:
                await websocket.send_text(
                    "\x1b[31mError: environment not found\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            if container.status != "running":
                await websocket.send_text(
                    "\x1b[31mError: container is not running\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            exec_id = client.api.exec_create(
                container.id,
                [
                    "/entrypoint.sh",
                    "odoo",
                    "shell",
                    "-d",
                    db_name,
                    "--no-http",
                    "-c",
                    "/etc/odoo/odoo.conf",
                ],
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                try:
                    while not closed.is_set():
                        data = await loop.run_in_executor(None, raw_sock.recv, 4096)
                        if not data:
                            break
                        await websocket.send_text(
                            data.decode("utf-8", errors="replace")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        msg = json.loads(text)
                        if msg.get("type") == "input":
                            await loop.run_in_executor(
                                None, raw_sock.sendall, msg["data"].encode("utf-8")
                            )
                        elif msg.get("type") == "resize":
                            cols = msg.get("cols", 80)
                            rows = msg.get("rows", 24)
                            try:
                                client.api.exec_resize(exec_id, height=rows, width=cols)
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    closed.set()

            tasks = [
                asyncio.ensure_future(docker_to_browser()),
                asyncio.ensure_future(browser_to_docker()),
            ]
            try:
                # Return as soon as EITHER side ends so a browser-closed/idle
                # terminal never parks docker_to_browser in recv() forever,
                # leaking the executor thread that blocks interpreter shutdown.
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                closed.set()
                # shutdown() reliably wakes a thread blocked in recv (a bare
                # close() need not) and gives the exec'd process EOF on stdin so
                # it exits.
                try:
                    raw_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.exception("WebSocket terminal error for branch %s", branch)
            try:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
            except Exception:
                pass

    async def ws_sql_terminal(websocket: WebSocket) -> None:
        branch = websocket.path_params["branch"]
        await websocket.accept()
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import get_db_name, validate_env_name

            # Validate before deriving DB/credential paths, matching the REST
            # create handler: a name like ".." would otherwise resolve
            # get_workspace_path one level above the workspaces dir when loading
            # scoped credentials. Reject it explicitly instead of failing opaquely.
            try:
                validate_env_name(branch)
            except ValueError as e:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1008)
                return

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()
            db_name = get_db_name(branch, team.team_id)

            try:
                db_container = client.containers.get(settings.shared_db_container)
            except _docker.errors.NotFound:
                await websocket.send_text(
                    "\x1b[31mError: database container not found\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            if db_container.status != "running":
                await websocket.send_text(
                    "\x1b[31mError: database container is not running\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            from oduflow.env_credentials import (
                MissingCredentialsError,
                load_credentials,
            )

            # Never open an interactive psql as the cluster superuser: that would
            # allow \c into another team's database and COPY ... FROM PROGRAM
            # (RCE). Require the environment's scoped role.
            try:
                creds = load_credentials(
                    branch,
                    team.workspaces_dir,
                    settings.db_user,
                    settings.db_password,
                    allow_fallback=False,
                )
            except MissingCredentialsError as e:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
                return

            exec_id = client.api.exec_create(
                db_container.id,
                ["psql", "-U", creds["pg_user"], "-d", db_name],
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                try:
                    while not closed.is_set():
                        data = await loop.run_in_executor(None, raw_sock.recv, 4096)
                        if not data:
                            break
                        await websocket.send_text(
                            data.decode("utf-8", errors="replace")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        msg = json.loads(text)
                        if msg.get("type") == "input":
                            await loop.run_in_executor(
                                None, raw_sock.sendall, msg["data"].encode("utf-8")
                            )
                        elif msg.get("type") == "resize":
                            cols = msg.get("cols", 80)
                            rows = msg.get("rows", 24)
                            try:
                                client.api.exec_resize(exec_id, height=rows, width=cols)
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    closed.set()

            tasks = [
                asyncio.ensure_future(docker_to_browser()),
                asyncio.ensure_future(browser_to_docker()),
            ]
            try:
                # Return as soon as EITHER side ends so a browser-closed/idle
                # terminal never parks docker_to_browser in recv() forever,
                # leaking the executor thread that blocks interpreter shutdown.
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                closed.set()
                # shutdown() reliably wakes a thread blocked in recv (a bare
                # close() need not) and gives the exec'd process EOF on stdin so
                # it exits.
                try:
                    raw_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.exception("WebSocket SQL terminal error for branch %s", branch)
            try:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
            except Exception:
                pass

    async def ws_agent_console(websocket: WebSocket) -> None:
        branch = websocket.path_params["branch"]
        await websocket.accept()
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import (
                get_agent_checkout_dir,
                get_agent_container_name,
                validate_env_name,
            )

            try:
                validate_env_name(branch)
            except ValueError as e:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1008)
                return

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()

            if not team.agent_enabled:
                await websocket.send_text(
                    "\x1b[31mError: the coding agent is disabled for this team. "
                    "Set agent_enabled = true in the [team."
                    + team.team_id
                    + "] section of oduflow.toml and restart the server.\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            # Which agent to run (claude | codex | opencode); default from config.
            agent_type = agent_config.resolve_agent_type(
                websocket.query_params.get("type"), team
            )

            container_name = get_agent_container_name(team.team_id, settings.prefix)
            try:
                container = client.containers.get(container_name)
            except _docker.errors.NotFound:
                await websocket.send_text(
                    "\x1b[31mError: agent container not found "
                    "(it is created on server start; check the logs).\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            if container.status != "running":
                await websocket.send_text(
                    "\x1b[31mError: agent container is not running\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            # The console attaches at the environment's checkout. If it is
            # missing (an installation upgraded to the agent feature, or a
            # setup that failed at creation) heal it on demand by cloning from
            # the Odoo container labels.
            checkout = get_agent_checkout_dir(branch)

            def _checkout_missing() -> bool:
                code, _ = container.exec_run(
                    ["test", "-d", checkout], user=env_ops.AGENT_USER
                )
                return bool(code != 0)

            missing = await asyncio.get_event_loop().run_in_executor(
                None, _checkout_missing
            )
            if missing:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: env_ops.ensure_agent_env_checkout(settings, team, branch),
                )
                missing = await asyncio.get_event_loop().run_in_executor(
                    None, _checkout_missing
                )
            if missing:
                await websocket.send_text(
                    "\x1b[31mError: no checkout at "
                    + checkout
                    + " — could not build it automatically (live-mount "
                    "environments have no repo to clone); recreate the "
                    "environment or check the server logs.\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            # Persistent checkouts may carry an MCP URL generated by an older
            # Oduflow version. Refresh only that generated entry before Claude
            # reads .mcp.json; no git fetch/reset and no token is written.
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: env_ops.refresh_agent_mcp_config(
                    container, settings, team, branch
                ),
            )

            # SCOPED per-env MCP credentials, injected into THIS session's exec
            # env only (never the container env): the token grants the ADR-0028
            # allowlist for this one environment, so a console user/agent can
            # only ever see a credential for the environment they already hold.
            mcp_url = env_ops.get_agent_mcp_url(settings, team, branch)
            try:
                mcp_token = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: env_ops.get_env_token(settings, team, branch)
                )
            except Exception:
                mcp_token = None
            if not mcp_token:
                await websocket.send_text(
                    "\x1b[33mWarning: this environment has no scoped MCP token "
                    "(created before MCP Access?) — the agent cannot drive it "
                    "over MCP. Update or recreate the environment.\x1b[0m\r\n"
                )
            exec_env = {
                "ODUFLOW_MCP_URL": mcp_url,
                "ODUFLOW_MCP_TOKEN": mcp_token or "",
                "AGENT_BROWSER_SESSION": os.path.basename(checkout),
            }

            if agent_type == "codex":
                # Codex has no project-scoped config; wire the Oduflow MCP
                # server via CLI overrides. Docker is the outer sandbox, so
                # Codex does not attempt a nested bubblewrap sandbox.
                cmd = _codex_cli_cmd(mcp_url, settings.agent_codex_model)
            elif agent_type == "opencode":
                # OpenCode's high-precedence inline config carries only
                # placeholders, never the scoped bearer itself. It also locks
                # the hosted trust model and disables runtime self-updates.
                exec_env["OPENCODE_CONFIG_CONTENT"] = _opencode_config(
                    mcp_url,
                    include_oduflow=bool(mcp_token),
                    model=settings.agent_opencode_model,
                )
                cmd = _opencode_cli_cmd(settings.agent_opencode_model)
            else:
                # Approval-free like Codex: Docker + the unprivileged `agent`
                # user are the security boundary, so the console skips per-tool
                # prompts. Safe under the non-root agent user (the CLI's own root
                # guard on this flag does not trip). Agent Chat gets the same via
                # the seeded user-tier settings.json (permissions.defaultMode).
                cmd = ["claude", "--dangerously-skip-permissions"]
                if settings.agent_claude_model:
                    cmd += ["--model", settings.agent_claude_model]

            exec_id = client.api.exec_create(
                container.id,
                cmd,
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
                workdir=checkout,
                environment=exec_env,
                user=env_ops.AGENT_USER,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                try:
                    while not closed.is_set():
                        data = await loop.run_in_executor(None, raw_sock.recv, 4096)
                        if not data:
                            break
                        await websocket.send_text(
                            data.decode("utf-8", errors="replace")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        msg = json.loads(text)
                        if msg.get("type") == "input":
                            await loop.run_in_executor(
                                None, raw_sock.sendall, msg["data"].encode("utf-8")
                            )
                        elif msg.get("type") == "resize":
                            cols = msg.get("cols", 80)
                            rows = msg.get("rows", 24)
                            try:
                                client.api.exec_resize(exec_id, height=rows, width=cols)
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    closed.set()

            tasks = [
                asyncio.ensure_future(docker_to_browser()),
                asyncio.ensure_future(browser_to_docker()),
            ]
            try:
                # Return as soon as EITHER side ends. Unlike ws_terminal (whose
                # exec dies with the environment container), the agent container
                # is long-lived: waiting for BOTH would park docker_to_browser
                # in recv() forever after the browser closes an idle console,
                # leaking the exec'd agent process and the executor thread.
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                closed.set()
                # shutdown() reliably wakes a thread blocked in recv (a bare
                # close() need not) and gives the agent EOF on stdin so the
                # `docker exec` process exits instead of leaking.
                try:
                    raw_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.exception("WebSocket agent console error for branch %s", branch)
            try:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
            except Exception:
                pass

    async def ws_agent_acp(websocket: WebSocket) -> None:
        """Bridge the browser ACP chat to an ACP adapter in the coder container.

        Unlike ``ws_agent_console`` (an interactive PTY terminal), this is a dumb
        line-framed relay: the adapter speaks JSON-RPC over stdio. We exec it
        WITHOUT a TTY, demux docker's multiplexed stdout/stderr, forward one
        complete stdout line (= one JSON-RPC frame) per WebSocket message, and
        write inbound frames to stdin. See specs/0029-agent-console-and-chat.md."""
        branch = websocket.path_params["branch"]
        await websocket.accept()

        async def _err(msg: str, close_code: int = 1011) -> None:
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "_chat/error",
                            "params": {"message": msg},
                        }
                    )
                )
            except Exception:
                pass
            try:
                await websocket.close(code=close_code)
            except Exception:
                pass

        async def _notice(msg: str) -> None:
            """Non-fatal system line in the chat (unlike _err, keeps going)."""
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "_chat/notice",
                            "params": {"message": msg},
                        }
                    )
                )
            except Exception:
                pass

        from oduflow.naming import validate_env_name

        try:
            validate_env_name(branch)
        except ValueError as e:
            await _err(str(e), close_code=1008)
            return

        try:
            from docker.utils.socket import (
                STDERR,
                next_frame_header,
                read_exactly,
            )

            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import (
                get_agent_checkout_dir,
                get_agent_container_name,
            )

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()

            if not team.agent_enabled:
                await _err(
                    "the coding agent is disabled for this team. Set "
                    f"agent_enabled = true in the [team.{team.team_id}] section "
                    "of oduflow.toml and restart the server."
                )
                return

            agent_type = agent_config.resolve_agent_type(
                websocket.query_params.get("type"), team
            )
            claude_auth_mode = env_ops._claude_auth_mode(settings, team)

            container_name = get_agent_container_name(team.team_id, settings.prefix)
            try:
                container = client.containers.get(container_name)
            except _docker.errors.NotFound:
                await _err(
                    "agent container not found "
                    "(it is created on server start; check the logs)."
                )
                return
            if container.status != "running":
                await _err("agent container is not running")
                return

            checkout = get_agent_checkout_dir(branch)

            def _checkout_missing() -> bool:
                code, _ = container.exec_run(
                    ["test", "-d", checkout], user=env_ops.AGENT_USER
                )
                return bool(code != 0)

            missing = await asyncio.get_event_loop().run_in_executor(
                None, _checkout_missing
            )
            if missing:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: env_ops.ensure_agent_env_checkout(settings, team, branch),
                )
                missing = await asyncio.get_event_loop().run_in_executor(
                    None, _checkout_missing
                )
            if missing:
                await _err(
                    f"no checkout at {checkout} — could not build it "
                    "automatically (live-mount environments have no repo to "
                    "clone); recreate the environment or check the logs."
                )
                return

            # Keep Claude ACP's project-scoped .mcp.json current for persistent
            # checkouts. Codex/OpenCode receive the same URL through session
            # wiring.
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: env_ops.refresh_agent_mcp_config(
                    container, settings, team, branch
                ),
            )

            # SCOPED per-env MCP credentials for THIS session only (see the
            # matching block in ws_agent_console). Claude's adapter picks them
            # up via the ${VAR} placeholders in the checkout's .mcp.json;
            # Codex/OpenCode ACP receive the same server through their native
            # session request contract in browser_to_docker below.
            try:
                mcp_token = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: env_ops.get_env_token(settings, team, branch)
                )
            except Exception:
                mcp_token = None
            if not mcp_token:
                await _notice(
                    "This environment has no scoped MCP token (created before "
                    "MCP Access existed) — the agent cannot drive it over MCP. "
                    "Update or recreate the environment."
                )
            mcp_url = env_ops.get_agent_mcp_url(settings, team, branch)
            exec_env = {
                "ODUFLOW_MCP_URL": mcp_url,
                "ODUFLOW_MCP_TOKEN": mcp_token or "",
                "AGENT_BROWSER_SESSION": os.path.basename(checkout),
            }
            if agent_type == "codex":
                # codex-acp maps this official mode to approval_policy=never
                # and dangerFullAccess. Docker + the unprivileged agent user
                # remain the outer security boundary for browser chat.
                exec_env["INITIAL_AGENT_MODE"] = "agent-full-access"
            elif agent_type == "opencode":
                # MCP servers arrive through the ACP session-open frame below,
                # avoiding duplicate registrations. Runtime policy and the
                # optional default model still come from inline config.
                exec_env["OPENCODE_CONFIG_CONTENT"] = _opencode_config(
                    include_browser=False,
                    model=settings.agent_opencode_model,
                )

            exec_id = client.api.exec_create(
                container.id,
                _acp_adapter_cmd(agent_type),
                stdin=True,
                tty=False,
                stdout=True,
                stderr=True,
                workdir=checkout,
                environment=exec_env,
                user=env_ops.AGENT_USER,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=False, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                buf = b""
                try:
                    while not closed.is_set():
                        stream, length = await loop.run_in_executor(
                            None, next_frame_header, raw_sock
                        )
                        if stream == -1 or length < 0:
                            break
                        if length == 0:
                            continue
                        payload = await loop.run_in_executor(
                            None, read_exactly, raw_sock, length
                        )
                        if stream == STDERR:
                            logger.info(
                                "acp[%s/%s] stderr: %s",
                                branch,
                                agent_type,
                                payload.decode("utf-8", "replace").rstrip(),
                            )
                            continue
                        # stdout carries NDJSON JSON-RPC. Frame boundaries do NOT
                        # align with lines, so buffer and emit one complete line
                        # (one JSON-RPC frame) per WebSocket message.
                        buf += payload
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.strip()
                            if text:
                                frame = text.decode("utf-8", "replace")
                                frame = _annotate_acp_auth_error(
                                    frame,
                                    agent_type,
                                    claude_auth_mode,
                                    team.team_id,
                                )
                                await websocket.send_text(frame)
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        if not text:
                            continue
                        if agent_type in {"codex", "opencode"}:
                            text = _wire_client_acp_mcp(
                                text,
                                mcp_url,
                                mcp_token or "",
                                os.path.basename(checkout),
                            )
                        frame = text if text.endswith("\n") else text + "\n"
                        await loop.run_in_executor(
                            None, raw_sock.sendall, frame.encode("utf-8")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            tasks = [
                asyncio.ensure_future(docker_to_browser()),
                asyncio.ensure_future(browser_to_docker()),
            ]
            try:
                # Return as soon as EITHER side ends — the browser closed the
                # chat, or the adapter exited.
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                closed.set()
                # Tear the exec down so it never outlives the chat. A thread
                # parked in next_frame_header/recv is only reliably woken by
                # shutdown() (a bare close() need not interrupt a blocked recv);
                # dropping the socket also gives the adapter EOF on stdin so the
                # `docker exec` process exits instead of leaking. Then cancel the
                # still-parked coroutine (receive_text is cancellable) and reap.
                try:
                    raw_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception:
            logger.exception("WebSocket ACP chat error for branch %s", branch)
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Production hosting
    # ------------------------------------------------------------------

    def _prod_lock_key(team: TeamSettings, name: str) -> str:
        return prod_lock_key(team.team_id, name)

    def api_productions(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            prods = production_ops.list_productions(settings, team)
            webhook_secret = production_registry.get_webhook_secret(team)
            return JSONResponse(
                {
                    "ok": True,
                    "productions": prods,
                    "base_domain": team.base_domain,
                    "backup_configured": settings.backup is not None,
                    "webhook": {
                        "path": "/api/webhooks/github",
                        "secret": webhook_secret,
                    },
                }
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_productions failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_production_create(request: Request) -> JSONResponse:
        settings = get_settings()
        team = _get_ui_team(request)
        try:
            data = await request.json()
            name = str(data.get("name", "")).strip()
            repo_url = str(data.get("repo_url", "")).strip()
            from_environment = str(data.get("from_environment", "")).strip()
            if repo_url:
                git_ops.validate_repo_url(repo_url)
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        raw_extra = data.get("extra_addons")
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        # The source env's lock is scoped inside create_production to the
        # brief stop/copy/restart slice (not the whole provisioning).
        env_lock = (
            (
                lambda: locks.env_lock(
                    from_environment, team.team_id, operation="create_production"
                )
            )
            if from_environment
            else None
        )
        try:
            result = await _offload(
                production_ops.create_production,
                settings,
                team,
                name,
                repo_url,
                str(data.get("branch", "")).strip(),
                str(data.get("domain", "")).strip(),
                str(data.get("odoo_image", "")).strip(),
                extra_domains=_normalize_domain_list(data.get("extra_domains")),
                git_user=str(data.get("git_user", "")).strip(),
                extra_addons=(
                    _normalize_extra_addons(raw_extra)
                    if raw_extra is not None
                    else None
                ),
                auto_update=bool(data.get("auto_update")),
                template_name=str(data.get("template_name", "")).strip() or None,
                from_environment=from_environment or None,
                env_lock=env_lock,
            )
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("api_production_create failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    def _production_action(
        request: Request,
        action: Callable[[Settings, TeamSettings, str], dict[str, Any]],
        *,
        with_backup_store: bool = False,
    ) -> JSONResponse:
        """Shared lock/error wrapper for simple per-production POST actions.

        with_backup_store additionally takes the team's backup-store lock in
        the same order as the MCP tools (production first, then the store), so
        a prune cannot run mid-snapshot/restore.
        """
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        if with_backup_store:
            try:
                locks.acquire_env(prod_backups_lock_key(team.team_id))
            except FlowError as e:
                locks.release_env(_prod_lock_key(team, name))
                return _error_response(e)
        try:
            result = action(settings, team, name)
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("production action failed for '%s'", name)
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            if with_backup_store:
                locks.release_env(prod_backups_lock_key(team.team_id))
            locks.release_env(_prod_lock_key(team, name))

    async def _production_body_action(
        request: Request,
        action: Callable[..., dict[str, Any]],
    ) -> JSONResponse:
        """Async sibling of _production_action for POST handlers with a JSON
        body: same lock/error contract, action(settings, team, name, data)
        runs off the event loop via _offload."""
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            data = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        if not isinstance(data, dict):
            return JSONResponse(
                {"ok": False, "error": "Body must be a JSON object."},
                status_code=400,
            )
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = await _offload(action, settings, team, name, data)
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("production action failed for '%s'", name)
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    def api_production_start(request: Request) -> JSONResponse:
        return _production_action(request, production_ops.start_production)

    def api_production_stop(request: Request) -> JSONResponse:
        return _production_action(request, production_ops.stop_production)

    def api_production_restart(request: Request) -> JSONResponse:
        return _production_action(request, production_ops.restart_production)

    def api_production_info(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            name = request.path_params["name"]
            info = production_ops.get_production_info(settings, team, name)
            return JSONResponse({"ok": True, **info})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_info failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_update(request: Request) -> JSONResponse:
        """Deploy in a background thread: deploys can run for minutes and
        would time browsers out; the dashboard polls status instead."""
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            production_registry.get_production(team, name)
        except FlowError as e:
            return _error_response(e)

        def _run() -> None:
            key = _prod_lock_key(team, name)
            if not locks.acquire_env_blocking(key, 300):
                logger.warning("UI deploy of '%s' timed out on lock", name)
                return
            try:
                production_ops.update_production(settings, team, name, trigger="ui")
            except Exception:
                logger.exception("UI deploy of production '%s' failed", name)
            finally:
                locks.release_env(key)

        threading.Thread(
            target=_run, name=f"oduflow-ui-deploy-{name}", daemon=True
        ).start()
        return JSONResponse({"ok": True, "started": True}, status_code=202)

    def api_production_rollback(request: Request) -> JSONResponse:
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        to_commit = request.query_params.get("to_commit", "")
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = production_ops.rollback_production(
                settings, team, name, to_commit, trigger="ui"
            )
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_rollback failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    async def api_production_auto_update(request: Request) -> JSONResponse:
        try:
            team = _get_ui_team(request)
            name = request.path_params["name"]
            data = await request.json()
            production_registry.get_production(team, name)
            production_registry.update_production(
                team, name, {"auto_update": bool(data.get("enabled"))}
            )
            return JSONResponse({"ok": True})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_auto_update failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_production_copy_to_dev_mcp(request: Request) -> JSONResponse:
        """Flip ``allow_copy_to_dev_mcp`` — the MCP-only gate on prod→dev copies.

        Deliberately dashboard-only: no MCP tool can change this flag, so an
        agent cannot lift its own restriction.
        """
        try:
            team = _get_ui_team(request)
            name = request.path_params["name"]
            try:
                data = await request.json()
            except ValueError:
                data = None
            enabled = data.get("enabled") if isinstance(data, dict) else None
            if not isinstance(enabled, bool):
                return JSONResponse(
                    {"ok": False, "error": 'Body must be {"enabled": true|false}.'},
                    status_code=400,
                )
            production_registry.update_production(
                team, name, {"allow_copy_to_dev_mcp": enabled}
            )
            return JSONResponse({"ok": True})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_copy_to_dev_mcp failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def _reconfigure_action(
        settings: Settings, team: TeamSettings, name: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        repo_url = str(data.get("repo_url", "")).strip()
        if repo_url:
            git_ops.validate_repo_url(repo_url)
        raw_extra = data.get("extra_addons")
        return production_ops.reconfigure_production(
            settings,
            team,
            name,
            domain=str(data.get("domain", "")).strip() or None,
            # Present-but-empty means "remove all extras"; absent = unchanged.
            extra_domains=(
                _normalize_domain_list(data["extra_domains"])
                if "extra_domains" in data
                else None
            ),
            odoo_image=str(data.get("odoo_image", "")).strip() or None,
            branch=str(data.get("branch", "")).strip() or None,
            repo_url=repo_url or None,
            # Present-but-empty means "clear" (the dashboard always submits
            # the field); only an absent key means "leave unchanged".
            git_user=(str(data["git_user"]).strip() if "git_user" in data else None),
            extra_addons=(
                _normalize_extra_addons(raw_extra) if raw_extra is not None else None
            ),
        )

    async def api_production_reconfigure(request: Request) -> JSONResponse:
        """Change infra settings (domain/image/branch/repo/extra addons) and
        recreate the production container to match; DB/filestore preserved."""
        return await _production_body_action(request, _reconfigure_action)

    def _odoo_conf_action(
        settings: Settings, team: TeamSettings, name: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        options = data.get("options") or {}
        unset = data.get("unset") or []
        if not isinstance(options, dict) or not isinstance(unset, list):
            raise ValueError('Body must be {"options": {...}, "unset": [...]}.')
        return production_ops.set_production_odoo_conf(
            settings,
            team,
            name,
            set_options={str(k): str(v) for k, v in options.items()},
            unset_options=[str(k) for k in unset],
            restart=bool(data.get("restart", True)),
            replace=bool(data.get("replace", False)),
        )

    async def api_production_odoo_conf(request: Request) -> JSONResponse:
        """Set/remove per-production odoo.conf [options] overrides. With
        "replace": true the passed options become the complete override set."""
        return await _production_body_action(request, _odoo_conf_action)

    async def api_production_save_as_template(request: Request) -> JSONResponse:
        """Publish a production's database and filestore as a dev template.

        Never consults ``allow_copy_to_dev_mcp``: that flag gates agents, and
        the dashboard is the administrator's own console.
        """
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        template_name = str((body or {}).get("template_name") or "").strip()
        # Unlike the environment publish, overwrite is offered here: refreshing
        # the managed prod-<name> template from the live production is the
        # normal case, and it stays an explicit choice.
        overwrite = bool((body or {}).get("overwrite"))
        if not template_name:
            return JSONResponse(
                {"ok": False, "error": "template_name is required"}, status_code=400
            )
        try:
            validate_template_name(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        # Fail fast on an unknown production before taking a team-wide lock.
        try:
            production_registry.get_production(team, name)
        except FlowError as e:
            return _error_response(e)
        # Team lock: publishing can remount other envs' overlay filestores, so it
        # serializes against the whole team like the save_production_as_template
        # MCP tool. The production's own key on top keeps deploys, restores and
        # deletes of this production out of the dump.
        prod_key = _prod_lock_key(team, name)
        try:
            with (
                _template_locks(
                    team,
                    template_name,
                    operation="save_production_as_template",
                    team_wide=True,
                ),
                locks.env_lock(prod_key, operation="save_production_as_template"),
            ):
                result = await _offload(
                    system_ops.publish_production_as_template,
                    get_settings(),
                    team,
                    name,
                    template_name=template_name,
                    overwrite=overwrite,
                )
            return JSONResponse(
                {
                    "ok": True,
                    "result": {
                        "status": result.get("status"),
                        "prod_name": result.get("prod_name"),
                        "template_name": template_name,
                        "template_db": result.get("template_db"),
                        "affected_envs": result.get("affected_envs", []),
                        "remount_failures": result.get("remount_failures", []),
                    },
                }
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except ConflictError as e:
            # The dashboard offers a re-baseline instead of a dead end, so it
            # has to tell "that name is taken" apart from every other 400.
            return JSONResponse(
                {"ok": False, "error": _public_flow_error(e), "conflict": True},
                status_code=_flow_error_status(e),
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_save_as_template failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_logs(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            name = request.path_params["name"]
            n_lines = int(request.query_params.get("lines", "200"))
            logs = production_ops.production_logs(
                settings, team, name, n_lines=min(n_lines, 2000)
            )
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_logs failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_deploys(request: Request) -> JSONResponse:
        try:
            team = _get_ui_team(request)
            name = request.path_params["name"]
            production_registry.get_production(team, name)
            deploys = production_ops.read_deploys(team, name, limit=20)
            return JSONResponse({"ok": True, "deploys": deploys})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_deploys failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_production_delete(request: Request) -> JSONResponse:
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            data = await request.json()
        except Exception:
            data = {}
        if str(data.get("confirm", "")) != name:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Confirmation failed: type the production name.",
                },
                status_code=400,
            )
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = await _offload(
                production_ops.delete_production,
                settings,
                team,
                name,
                drop_database=bool(data.get("drop_database")),
            )
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_delete failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    def api_production_snapshots(request: Request) -> JSONResponse:
        try:
            from oduflow import backup_ops

            settings = get_settings()
            team = _get_ui_team(request)
            name = request.path_params["name"]
            refresh = request.query_params.get("refresh") == "true"
            manifests = backup_ops.list_snapshots(settings, team, name, refresh=refresh)
            return JSONResponse({"ok": True, "snapshots": manifests})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_snapshots failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_snapshot_now(request: Request) -> JSONResponse:
        from oduflow import backup_ops

        def _snapshot(
            settings: Settings, team: TeamSettings, name: str
        ) -> dict[str, Any]:
            return backup_ops.snapshot_production(settings, team, name, trigger="ui")

        return _production_action(request, _snapshot, with_backup_store=True)

    async def api_production_restore(request: Request) -> JSONResponse:
        from oduflow import backup_ops

        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            data = await request.json()
        except Exception:
            data = {}
        snapshot_id = str(data.get("snapshot_id", "")).strip()
        if str(data.get("confirm", "")) != name:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Confirmation failed: type the production name.",
                },
                status_code=400,
            )
        if not snapshot_id:
            return JSONResponse(
                {"ok": False, "error": "snapshot_id is required"}, status_code=400
            )
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        # Same order as the MCP tool: the production first, then the team's
        # backup store, so a prune cannot delete artifacts mid-restore.
        try:
            locks.acquire_env(prod_backups_lock_key(team.team_id))
        except FlowError as e:
            locks.release_env(_prod_lock_key(team, name))
            return _error_response(e)
        try:
            result = await _offload(
                backup_ops.restore_production, settings, team, name, snapshot_id
            )
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_restore failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(prod_backups_lock_key(team.team_id))
            locks.release_env(_prod_lock_key(team, name))

    async def api_production_backup_schedule(request: Request) -> JSONResponse:
        try:
            team = _get_ui_team(request)
            name = request.path_params["name"]
            data = await request.json()
            schedule = str(data.get("schedule", "")).strip().lower()
            if schedule != "off" and not re.match(
                r"^([01]\d|2[0-3]):[0-5]\d$", schedule
            ):
                return JSONResponse(
                    {"ok": False, "error": 'schedule must be "HH:MM" or "off"'},
                    status_code=400,
                )
            production_registry.get_production(team, name)
            production_registry.set_nested(team, name, "backup", {"schedule": schedule})
            return JSONResponse({"ok": True})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_backup_schedule failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_backup_status(request: Request) -> JSONResponse:
        try:
            from oduflow import backup_ops

            settings = get_settings()
            team = _get_ui_team(request)
            return JSONResponse(
                {"ok": True, **backup_ops.backup_status(settings, team)}
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_backup_status failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    # ------------------------------------------------------------------
    # Health + GitHub webhook (both PUBLIC paths with their own auth)
    # ------------------------------------------------------------------

    def api_production_wal_status(request: Request) -> JSONResponse:
        try:
            from oduflow import wal_monitor

            _get_ui_team(request)
            return JSONResponse({"ok": True, **wal_monitor.status(get_settings())})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_wal_status failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_production_wal_control(request: Request) -> JSONResponse:
        from oduflow import wal_monitor
        from oduflow.docker_ops.client import get_client

        try:
            _get_ui_team(request)
            data = await request.json()
            locks.acquire_system(operation="control_production_wal")
            try:
                result = await _offload(
                    wal_monitor.control,
                    get_settings(),
                    get_client(),
                    str(data.get("action", "")),
                    str(data.get("confirm", "")),
                )
                return JSONResponse({"ok": True, **result})
            finally:
                locks.release_system()
        except FlowError as exc:
            return _error_response(exc)
        except Exception:
            logger.exception("Production WAL control failed")
            return JSONResponse(
                {"ok": False, "error": "WAL control failed; check server logs"},
                status_code=500,
            )

    def healthz(request: Request) -> JSONResponse:
        from oduflow.health import collect_health

        result = collect_health(get_settings())
        return JSONResponse(result, status_code=200 if result["ok"] else 503)

    async def webhook_github(request: Request) -> JSONResponse:
        from oduflow import webhooks

        body = await request.body()
        status, payload = webhooks.handle_github_event(
            get_settings(),
            locks,
            event=request.headers.get("x-github-event", ""),
            body=body,
            signature_header=request.headers.get("x-hub-signature-256", ""),
        )
        return JSONResponse(payload, status_code=status)

    production_routes: list[BaseRoute] = []
    if get_settings().prod_enabled:
        production_routes = [
            Route("/api/productions", api_productions, methods=["GET"]),
            Route("/api/productions/create", api_production_create, methods=["POST"]),
            Route(
                "/api/productions/wal-status",
                api_production_wal_status,
                methods=["GET"],
            ),
            Route(
                "/api/productions/wal-control",
                api_production_wal_control,
                methods=["POST"],
            ),
            Route(
                "/api/productions/backup-status",
                api_production_backup_status,
                methods=["GET"],
            ),
            Route("/api/productions/{name}", api_production_info, methods=["GET"]),
            Route(
                "/api/productions/{name}/start",
                api_production_start,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/stop",
                api_production_stop,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/restart",
                api_production_restart,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/update",
                api_production_update,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/rollback",
                api_production_rollback,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/auto-update",
                api_production_auto_update,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/copy-to-dev-mcp",
                api_production_copy_to_dev_mcp,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/reconfigure",
                api_production_reconfigure,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/odoo-conf",
                api_production_odoo_conf,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/save-as-template",
                api_production_save_as_template,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/logs",
                api_production_logs,
                methods=["GET"],
            ),
            Route(
                "/api/productions/{name}/deploys",
                api_production_deploys,
                methods=["GET"],
            ),
            Route(
                "/api/productions/{name}/delete",
                api_production_delete,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/snapshots",
                api_production_snapshots,
                methods=["GET"],
            ),
            Route(
                "/api/productions/{name}/snapshot",
                api_production_snapshot_now,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/restore",
                api_production_restore,
                methods=["POST"],
            ),
            Route(
                "/api/productions/{name}/backup-schedule",
                api_production_backup_schedule,
                methods=["POST"],
            ),
            Route("/api/webhooks/github", webhook_github, methods=["POST"]),
        ]

    return [
        Route("/", dashboard, methods=["GET"]),
        # Shared single-environment dashboard; authenticates itself with the
        # share link's ?key= or the scoped cookie (see oduflow.ui_scope).
        Route(ui_scope.PAGE_PREFIX + "{name:path}", scoped_env_page, methods=["GET"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/logo.png", logo, methods=["GET"]),
        Route("/static/{filename}", static_file, methods=["GET"]),
        Route("/api/version", api_version, methods=["GET"]),
        Route("/api/license", api_license, methods=["GET"]),
        Route("/api/license/activate", api_license_activate, methods=["POST"]),
        Route("/api/license/refresh", api_license_refresh, methods=["POST"]),
        Route("/api/license/checkout", api_license_checkout, methods=["POST"]),
        Route("/api/feedback/link", api_feedback_link, methods=["POST"]),
        Route("/api/templates", api_templates, methods=["GET"]),
        Route("/import-odoo.sh", import_odoo_script, methods=["GET"]),
        Route("/api/templates/import-token", api_import_token, methods=["POST"]),
        Route(
            "/api/templates/import-from-odoo",
            api_import_template_from_odoo,
            methods=["POST"],
        ),
        Route("/api/templates/import/status", api_import_status, methods=["GET"]),
        Route("/api/templates/import/manifest", api_import_manifest, methods=["POST"]),
        Route("/api/templates/import/dump", api_import_dump, methods=["POST"]),
        Route(
            "/api/templates/import/filestore", api_import_filestore, methods=["POST"]
        ),
        Route("/api/templates/import/addon", api_import_addon, methods=["POST"]),
        Route(
            "/api/templates/import/addon-remote",
            api_import_addon_remote,
            methods=["POST"],
        ),
        Route("/api/templates/import/finalize", api_import_finalize, methods=["POST"]),
        Route(
            "/api/templates/{name:path}/metadata",
            api_template_metadata,
            methods=["GET"],
        ),
        Route(
            "/api/templates/{name:path}/metadata",
            api_template_metadata_update,
            methods=["PUT"],
        ),
        Route("/api/templates/{name}/delete", api_template_delete, methods=["POST"]),
        Route("/api/templates/{name}/rename", api_template_rename, methods=["POST"]),
        Route("/api/environments", api_list, methods=["GET"]),
        Route("/api/environments/create", api_create, methods=["POST"]),
        *production_routes,
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/stats", api_stats, methods=["GET"]),
        Route("/api/usage", api_usage, methods=["GET"]),
        Route("/api/usage/refresh", api_usage_refresh, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/storage/refresh",
            api_storage_refresh,
            methods=["POST"],
        ),
        Route("/api/agent-guides", api_agent_guides_list, methods=["GET"]),
        Route("/api/agent-guides/{filename}", api_agent_guide_get, methods=["GET"]),
        Route("/api/agent", api_agent_info, methods=["GET"]),
        Route(
            "/api/environments/{branch:path}/agent-acp/info",
            api_agent_acp_info,
            methods=["GET"],
        ),
        Route(
            "/api/environments/{branch:path}/agent-acp/session",
            api_agent_acp_session,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/agent-acp/attachments",
            api_agent_acp_attachment_upload,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/agent-acp/attachments/{upload_id}",
            api_agent_acp_attachment_delete,
            methods=["DELETE"],
        ),
        Route("/api/environments/{branch:path}/start", api_start, methods=["POST"]),
        Route("/api/environments/{branch:path}/stop", api_stop, methods=["POST"]),
        Route("/api/environments/{branch:path}/restart", api_restart, methods=["POST"]),
        Route("/api/environments/{branch:path}/sync", api_sync, methods=["POST"]),
        Route("/api/environments/{branch:path}/modules", api_modules, methods=["GET"]),
        Route(
            "/api/environments/{branch:path}/modules",
            api_modules_apply,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/switch-branch",
            api_switch_branch,
            methods=["POST"],
        ),
        Route("/api/environments/{branch:path}/protect", api_protect, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/unprotect", api_unprotect, methods=["POST"]
        ),
        Route("/api/environments/{branch:path}/note", api_set_note, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/mcp-access",
            api_mcp_access,
            methods=["GET"],
        ),
        Route(
            "/api/environments/{branch:path}/share",
            api_share_get,
            methods=["GET"],
        ),
        Route(
            "/api/environments/{branch:path}/share",
            api_share_create,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/share/rotate",
            api_share_rotate,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/share/revoke",
            api_share_revoke,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/users",
            api_env_users,
            methods=["GET"],
        ),
        Route(
            "/api/environments/{branch:path}/connect-as",
            api_connect_as,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/connect-open",
            api_connect_open,
            methods=["GET"],
        ),
        # Served on an env host (routed here by Traefik's PathPrefix router);
        # public + token-authenticated. See api_connect_land.
        Route("/oduflow-connect", api_connect_land, methods=["GET"]),
        Route("/oduflow-artifact", api_artifact_download, methods=["GET"]),
        Route("/api/environments/{branch:path}/update", api_update, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/env-vars", api_env_vars, methods=["GET"]
        ),
        Route(
            "/api/environments/{branch:path}/recreate", api_recreate, methods=["POST"]
        ),
        Route(
            "/api/environments/{branch:path}/save-as-template",
            api_save_as_template,
            methods=["POST"],
        ),
        Route("/api/environments/{branch:path}/delete", api_delete, methods=["POST"]),
        Route("/api/service-databases", api_service_databases, methods=["GET"]),
        Route(
            "/api/service-databases/create",
            api_service_database_create,
            methods=["POST"],
        ),
        Route(
            # POST, not GET: this is the only endpoint that returns an
            # unmasked secret, and the CSRF backstop in UIAuthMiddleware
            # only guards unsafe methods.
            "/api/service-databases/{name}/credentials",
            api_service_database_credentials,
            methods=["POST"],
        ),
        Route(
            "/api/service-databases/{name}/rotate",
            api_service_database_rotate,
            methods=["POST"],
        ),
        Route(
            "/api/service-databases/{name}/protect",
            api_service_database_protect,
            methods=["POST"],
        ),
        Route(
            "/api/service-databases/{name}/unprotect",
            api_service_database_unprotect,
            methods=["POST"],
        ),
        Route(
            "/api/service-databases/{name}/delete",
            api_service_database_delete,
            methods=["POST"],
        ),
        Route("/api/services", api_services, methods=["GET"]),
        Route("/api/services/create", api_service_create, methods=["POST"]),
        Route("/api/services/{name}/update", api_service_update, methods=["POST"]),
        Route("/api/services/{name}/config", api_service_config, methods=["GET"]),
        Route("/api/services/{name}/restart", api_service_restart, methods=["POST"]),
        Route("/api/services/{name}/delete", api_service_delete, methods=["POST"]),
        Route("/api/services/{name}/protect", api_service_protect, methods=["POST"]),
        Route(
            "/api/services/{name}/unprotect", api_service_unprotect, methods=["POST"]
        ),
        Route("/api/services/{name}/logs", api_service_logs, methods=["GET"]),
        Route("/api/service-presets", api_service_presets, methods=["GET"]),
        Route("/api/service-presets/restore", api_service_restore, methods=["POST"]),
        Route(
            "/api/service-presets/{name}/delete",
            api_service_preset_delete,
            methods=["POST"],
        ),
        Route("/api/volumes", api_volumes, methods=["GET"]),
        Route("/api/volumes/create", api_volume_create, methods=["POST"]),
        Route("/api/volumes/{name}/delete", api_volume_delete, methods=["POST"]),
        Route("/api/extra-repos", api_extra_repos, methods=["GET"]),
        Route("/api/extra-repos/add", api_extra_repo_add, methods=["POST"]),
        Route("/api/extra-repos/ls-remote", api_extra_repo_ls_remote, methods=["POST"]),
        Route("/api/extra-repos/{name}/pull", api_extra_repo_pull, methods=["POST"]),
        Route(
            "/api/extra-repos/{name}/protect", api_extra_repo_protect, methods=["POST"]
        ),
        Route(
            "/api/extra-repos/{name}/unprotect",
            api_extra_repo_unprotect,
            methods=["POST"],
        ),
        Route(
            "/api/extra-repos/{name}/delete", api_extra_repo_delete, methods=["POST"]
        ),
        Route("/api/credentials", api_credentials, methods=["GET"]),
        Route("/api/credentials/add", api_credential_add, methods=["POST"]),
        Route("/api/credentials/delete", api_credential_delete, methods=["POST"]),
        Route("/api/credentials/validate", api_credential_validate, methods=["POST"]),
        Route("/api/ssh-key", api_ssh_key, methods=["GET"]),
        Route("/api/ssh-key/generate", api_ssh_key_generate, methods=["POST"]),
        # Secrets are write-only: the list returns names + timestamps, and no
        # endpoint anywhere returns a stored value. Deliberately absent from
        # the ui_scope allowlist, so scoped share sessions cannot touch them.
        Route("/api/secrets", api_secrets, methods=["GET"]),
        Route("/api/secrets/{name}/set", api_secret_set, methods=["POST"]),
        Route("/api/secrets/{name}/delete", api_secret_delete, methods=["POST"]),
        Route("/api/environments/{branch:path}/logs", api_logs, methods=["GET"]),
        WebSocketRoute("/api/environments/{branch:path}/terminal", ws_terminal),
        WebSocketRoute("/api/environments/{branch:path}/sql", ws_sql_terminal),
        WebSocketRoute("/api/environments/{branch:path}/agent", ws_agent_console),
        WebSocketRoute("/api/environments/{branch:path}/agent-acp", ws_agent_acp),
    ]


def mount_web_ui(
    app: Starlette,
    get_settings: Callable[[], Settings],
    locks: LockManager,
) -> None:
    from starlette.routing import Router

    routes = _build_routes(get_settings, locks)
    sub_app: ASGIApp = Router(routes=routes)

    settings = get_settings()
    has_ui_passwords = any(t.ui_password for t in settings.teams.values())
    if has_ui_passwords:
        sub_app = UIAuthMiddleware(sub_app, get_settings)
        logger.info("Web UI session authentication ENABLED")
    else:
        logger.warning("Web UI auth DISABLED (no ui_password set in any team)")

    from starlette.routing import Mount

    app.routes.append(Mount("/", app=sub_app))
