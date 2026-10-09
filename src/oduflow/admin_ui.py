"""Server settings console: the deployment-wide oduflow.toml editor at /admin.

Teams are isolated tenants, and the config holds every team's credentials,
so the console is not part of the team dashboard: it has its own credential
(``[admin] password``, optionally with TOTP via ``oduflow ui-2fa setup
--admin``), its own signed session cookie, and it protects its own routes —
the team auth middleware lets ``/admin`` through untouched. While the
password is empty the console does not exist (every path is a 404).

All reads and writes go through :mod:`oduflow.config_store`; this module is
only HTTP plumbing. See specs/0077-server-settings-console.md.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import pathlib
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import BaseRoute, Route

from oduflow import config_schema as schema
from oduflow import config_store, feedback, ui_totp
from oduflow.errors import ConfigError, ConflictError, FlowError
from oduflow.settings import Settings, TeamSettings, secret_matches

logger = logging.getLogger("oduflow")

PREFIX = "/admin"
ADMIN_COOKIE = "oduflow_admin_auth"
_ADMIN_SALT = "oduflow.admin-auth.v1"
# Shorter than a team session: this one unlocks every team's secrets.
_ADMIN_MAX_AGE = 12 * 3600
_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent / "templates"
_DISABLED = "Not found"


def is_admin_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def admin_principal(settings: Settings) -> TeamSettings:
    """The console credential in the shape ``ui_totp`` stores factors for.

    Not a team: it never enters ``settings.teams``. Its TOTP state lives in
    ``<data_dir>/admin/`` (team data dirs are ``team_<id>``, so no clash).
    """
    data_dir = (
        os.path.join(settings.base_data_dir, "admin") if settings.base_data_dir else ""
    )
    return TeamSettings(
        team_id="admin",
        hostname="Server settings",
        ui_password=settings.admin_password,
        data_dir=data_dir,
    )


def _secret(settings: Settings) -> str:
    # Same persistent server secret as team sessions, different salt: an admin
    # cookie can never be replayed as a team session or vice versa.
    from oduflow.web_ui import _get_secret

    return _get_secret(settings)


def _signer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(settings), salt=_ADMIN_SALT)


def _fingerprint(settings: Settings) -> str:
    return hmac.new(
        _secret(settings).encode("utf-8"),
        settings.admin_password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_admin_token(settings: Settings, mfa_version: str) -> str:
    """Signed console session; changing the password or TOTP revokes it."""
    return _signer(settings).dumps(["admin", _fingerprint(settings), mfa_version])


def check_admin_token(token: str, settings: Settings) -> bool:
    if not token or not settings.admin_password:
        return False
    try:
        data = _signer(settings).loads(token, max_age=_ADMIN_MAX_AGE)
    except BadData:
        return False
    if not (isinstance(data, list) and len(data) == 3 and data[0] == "admin"):
        return False
    _, fingerprint, mfa_version = data
    if not isinstance(fingerprint, str):
        return False
    try:
        if mfa_version != ui_totp.version(settings, admin_principal(settings)):
            return False
    except ui_totp.TOTPStateError:
        return False
    return hmac.compare_digest(fingerprint, _fingerprint(settings))


def has_admin_session(request: Request, settings: Settings) -> bool:
    return check_admin_token(request.cookies.get(ADMIN_COOKIE, ""), settings)


def _render_login(error: str = "") -> str:
    page = (_TEMPLATE_DIR / "login.html").read_text(encoding="utf-8")
    banner = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return (
        page.replace('action="/login"', f'action="{PREFIX}/login"')
        .replace("<h1>Sign in</h1>", "<h1>Server settings</h1>")
        .replace(
            "<title>Oduflow — Sign in</title>",
            "<title>Oduflow — Server settings</title>",
        )
        .replace("<!--ERROR-->", banner)
    )


def _json_error(message: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ConfigError("Request body must be JSON.") from None
    if not isinstance(body, dict):
        raise ConfigError("Request body must be a JSON object.")
    return body


def _parse_or_empty(text: str) -> dict[str, Any]:
    """The current document, or {} when a hand edit left it unparseable."""
    try:
        return config_store.parse_text(text)
    except ConfigError:
        return {}


def _teams_of(raw: dict[str, Any]) -> dict[str, Any]:
    teams = raw.get("team")
    return teams if isinstance(teams, dict) else {}


def _next_team_suggestion(raw: dict[str, Any]) -> dict[str, Any]:
    """Defaults for "Add team": next numeric id, next free 100-port block."""
    teams = _teams_of(raw)
    numeric = [int(k) for k in teams if str(k).isdigit()]
    next_id = str(max(numeric) + 1) if numeric else str(len(teams) + 1)
    while next_id in teams:
        next_id = str(int(next_id) + 1)
    ends = []
    for cfg in teams.values():
        pr = cfg.get("port_range") if isinstance(cfg, dict) else None
        if (
            isinstance(pr, list)
            and len(pr) == 2
            and all(isinstance(v, int) for v in pr)
        ):
            ends.append(pr[1])
        else:
            ends.append(50100)
    start = max(ends) if ends else 50000
    return {"team_id": next_id, "port_range": [start, start + 100]}


def build_admin_routes(
    get_settings: Callable[[], Settings],
    runtime: config_store.ConfigRuntime | None,
) -> list[BaseRoute]:
    from oduflow.web_ui import _is_cross_origin, _is_secure_request, _LoginRateLimiter

    limiter = _LoginRateLimiter()

    def config_path() -> str:
        settings = get_settings()
        if not settings.toml_path:
            raise ConfigError("This server was not started from an oduflow.toml.")
        return settings.toml_path

    def guarded(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            settings = get_settings()
            if not settings.admin_password:
                return _json_error(_DISABLED, 404)
            if not has_admin_session(request, settings):
                return _json_error("Unauthorized", 401)
            if request.method != "GET" and _is_cross_origin(request.headers):
                return _json_error("Cross-origin request blocked", 403)
            try:
                return await handler(request)
            except ConflictError as exc:
                return _json_error(str(exc), 409)
            except FlowError as exc:
                return _json_error(str(exc), 400)
            except OSError as exc:
                logger.exception("Server settings console I/O failed")
                return _json_error(f"Cannot access the config: {exc.strerror}", 500)

        return wrapped

    # ── pages ──

    async def page(request: Request) -> Response:
        settings = get_settings()
        if not settings.admin_password:
            return Response(_DISABLED, status_code=404, media_type="text/plain")
        if not has_admin_session(request, settings):
            return RedirectResponse(f"{PREFIX}/login", status_code=302)
        html_text = (_TEMPLATE_DIR / "admin.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html_text.replace(
                "__ODUFLOW_VERSION__", html.escape(feedback.oduflow_version())
            )
        )

    async def login(request: Request) -> Response:
        settings = get_settings()
        if not settings.admin_password:
            return Response(_DISABLED, status_code=404, media_type="text/plain")
        if has_admin_session(request, settings):
            return RedirectResponse(PREFIX, status_code=302)
        if request.method != "POST":
            return HTMLResponse(_render_login())
        ip = _client_ip(request)
        if limiter.is_limited(ip):
            return HTMLResponse(
                _render_login("Too many failed attempts. Try again later."),
                status_code=429,
            )
        if _is_cross_origin(request.headers):
            return HTMLResponse(
                _render_login("Cross-origin request blocked."), status_code=403
            )
        from oduflow.web_ui import _read_login_credentials

        password, code = await _read_login_credentials(request)
        if password and secret_matches(settings.admin_password, password):
            principal = admin_principal(settings)
            try:
                mfa_version = await run_in_threadpool(
                    ui_totp.verify, settings, principal, code
                )
            except ui_totp.TOTPThrottled:
                return HTMLResponse(
                    _render_login("Too many failed attempts. Try again later."),
                    status_code=429,
                )
            except (ui_totp.TOTPStateError, OSError):
                logger.exception("Server settings console 2FA state unavailable")
                return HTMLResponse(
                    _render_login("Sign-in unavailable. Check the server log."),
                    status_code=503,
                )
            if mfa_version is not None:
                limiter.clear(ip)
                logger.info("Server settings console: signed in from %s", ip)
                response: Response = RedirectResponse(PREFIX, status_code=303)
                response.set_cookie(
                    ADMIN_COOKIE,
                    make_admin_token(settings, mfa_version),
                    max_age=_ADMIN_MAX_AGE,
                    httponly=True,
                    samesite="strict",
                    secure=_is_secure_request(request),
                    path="/",
                )
                return response
        limiter.record_failure(ip)
        logger.warning("Server settings console: failed sign-in from %s", ip)
        return HTMLResponse(
            _render_login("Invalid password or authenticator code."), status_code=401
        )

    async def logout(request: Request) -> Response:
        response = RedirectResponse(f"{PREFIX}/login", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/", samesite="strict")
        return response

    # ── API ──

    def _boot_settings() -> Settings:
        """The Settings this process started from, for change classification.

        Live swaps move ``get_settings()`` forward; what a restart-class key
        needs a restart *for* was decided at startup.
        """
        return runtime.boot_settings() if runtime is not None else get_settings()

    def _state() -> dict[str, Any]:
        settings = get_settings()
        path = config_path()
        snapshot = config_store.read_snapshot(path)
        file_error = ""
        file_raw: dict[str, Any] = {}
        try:
            file_raw = config_store.parse_text(snapshot.text)
            config_store.validate_text(snapshot.text, path)
        except ConfigError as exc:
            file_error = str(exc)
        pending = (
            config_store.pending_changes(runtime, file_raw)
            if runtime is not None and file_raw
            else []
        )
        try:
            totp = ui_totp.enabled(settings, admin_principal(settings))
        except ui_totp.TOTPStateError:
            totp = False
        return {
            "ok": True,
            "path": snapshot.path,
            "revision": snapshot.revision,
            "boot_id": runtime.boot_id if runtime else "",
            "schema": schema.schema_json(),
            # None when the file does not even parse: the forms would show
            # empty values, so the page sends the admin to Raw TOML instead.
            "values": config_store.mask_raw(file_raw) if file_raw else None,
            "file_error": file_error,
            "pending": [c.to_json() for c in pending],
            "unknown": config_store.unknown_keys(file_raw),
            "totp": totp,
            "restart_available": bool(runtime and runtime.restart),
            "history": len(config_store.list_history(path)),
            "suggest": _next_team_suggestion(file_raw),
        }

    async def api_state(request: Request) -> Response:
        return JSONResponse(await run_in_threadpool(_state))

    def _candidate(body: dict[str, Any], current: str) -> tuple[str, str, str]:
        """``(text, note, source)`` for a preview/apply request body."""
        if "raw" in body:
            raw = body["raw"]
            if not isinstance(raw, str) or not raw.strip():
                raise ConfigError("The raw config is empty.")
            return raw.replace("\r\n", "\n"), "Raw TOML edit", "raw"
        if "restore" in body:
            entry_id = str(body["restore"])
            _, before, _ = config_store.read_history(config_path(), entry_id)
            return before, f"Revert of {entry_id}", "restore"
        changes = config_store.parse_changes(body.get("changes"))
        return config_store.apply_changes(current, changes), "Console edit", "form"

    def _preview(body: dict[str, Any]) -> dict[str, Any]:
        path = config_path()
        snapshot = config_store.read_snapshot(path)
        base = str(body.get("revision") or "")
        if base and base != snapshot.revision:
            raise ConflictError(
                "oduflow.toml changed since you opened it. Reload to see the "
                "current file, then redo the change."
            )
        text, _, _ = _candidate(body, snapshot.text)
        diff = config_store.masked_diff(snapshot.text, text)
        old_raw = _parse_or_empty(snapshot.text)
        try:
            new_settings, new_raw = config_store.validate_text(text, path)
            config_store.check_console_policy(old_raw, new_raw)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc), "diff": diff}
        running = get_settings()
        changes = config_store.diff_raw(old_raw, new_raw, _boot_settings())
        warnings = _warnings(old_raw, new_raw, running)
        return {
            "ok": True,
            "revision": snapshot.revision,
            "diff": diff,
            "changes": [c.to_json() for c in changes],
            "apply": config_store.worst_apply(changes),
            "warnings": warnings,
            "signs_out": new_settings.admin_password != running.admin_password,
        }

    def _warnings(
        old_raw: dict[str, Any], new_raw: dict[str, Any], running: Settings
    ) -> list[str]:
        out: list[str] = []
        old_teams = _teams_of(old_raw)
        new_teams = _teams_of(new_raw)
        for team_id in sorted(set(old_teams) - set(new_teams)):
            team = running.teams.get(team_id)
            where = f" ({team.data_dir})" if team and team.data_dir else ""
            out.append(
                f"Removing team {team_id} does not delete anything: its data "
                f"directory{where}, databases and running containers stay until "
                "you clean them up."
            )
        return out

    async def api_preview(request: Request) -> Response:
        body = await _json_body(request)
        result = await run_in_threadpool(_preview, body)
        return JSONResponse(result, status_code=200)

    def _apply(body: dict[str, Any], actor: str) -> dict[str, Any]:
        path = config_path()
        base = str(body.get("revision") or "")
        if not base:
            raise ConfigError("Missing the revision the change was made against.")
        snapshot = config_store.read_snapshot(path)
        text, note, source = _candidate(body, snapshot.text)
        _, new_raw = config_store.validate_text(text, path)
        old_raw = _parse_or_empty(snapshot.text)
        config_store.check_console_policy(old_raw, new_raw)
        changes = config_store.diff_raw(old_raw, new_raw, _boot_settings())
        paths = [".".join(c.path) for c in changes]
        revision = config_store.write_config(
            path, text, base, note=note, paths=paths, actor=actor
        )
        logger.info(
            "Server settings console: %s saved by %s (%s)",
            note,
            actor,
            ", ".join(paths) or "no effective change",
        )
        applied: list[config_store.LeafChange] = []
        problem = ""
        pending: list[config_store.LeafChange] = []
        if runtime is not None:
            applied, problem = config_store.apply_live(runtime, new_raw)
            pending = config_store.pending_changes(runtime, new_raw)
        return {
            "ok": True,
            "revision": revision,
            "source": source,
            "applied": [c.to_json() for c in applied],
            "pending": [c.to_json() for c in pending],
            "problem": problem,
        }

    async def api_apply(request: Request) -> Response:
        body = await _json_body(request)
        actor = f"admin@{_client_ip(request)}"
        return JSONResponse(await run_in_threadpool(_apply, body, actor))

    def _reload() -> dict[str, Any]:
        if runtime is None:
            raise ConfigError("Live reload is only available in HTTP mode.")
        path = config_path()
        snapshot = config_store.read_snapshot(path)
        _, raw = config_store.validate_text(snapshot.text, path)
        applied, problem = config_store.apply_live(runtime, raw)
        return {
            "ok": True,
            "applied": [c.to_json() for c in applied],
            "pending": [
                c.to_json() for c in config_store.pending_changes(runtime, raw)
            ],
            "problem": problem,
        }

    async def api_reload(request: Request) -> Response:
        return JSONResponse(await run_in_threadpool(_reload))

    def _reveal(body: dict[str, Any], ip: str) -> dict[str, Any]:
        path = body.get("path")
        if not isinstance(path, list) or not all(isinstance(p, str) for p in path):
            raise ConfigError("Missing path.")
        key = tuple(path)
        if not schema.is_secret_path(key):
            raise ConfigError("Not a secret.")
        snapshot = config_store.read_snapshot(config_path())
        value = config_store._get(config_store.parse_text(snapshot.text), key)
        if not isinstance(value, str):
            raise ConfigError("No stored value.")
        logger.info("Server settings console: %s revealed from %s", ".".join(key), ip)
        return {"ok": True, "value": value}

    async def api_reveal(request: Request) -> Response:
        body = await _json_body(request)
        return JSONResponse(await run_in_threadpool(_reveal, body, _client_ip(request)))

    async def api_raw(request: Request) -> Response:
        snapshot = await run_in_threadpool(config_store.read_snapshot, config_path())
        return JSONResponse(
            {"ok": True, "text": snapshot.text, "revision": snapshot.revision}
        )

    async def api_history(request: Request) -> Response:
        entries = await run_in_threadpool(config_store.list_history, config_path())
        return JSONResponse({"ok": True, "entries": entries})

    def _history_entry(entry_id: str) -> dict[str, Any]:
        entry, before, after = config_store.read_history(config_path(), entry_id)
        return {
            "ok": True,
            "entry": entry,
            "diff": config_store.masked_diff(before, after, labels=("before", "after")),
        }

    async def api_history_entry(request: Request) -> Response:
        entry_id = request.path_params["entry_id"]
        return JSONResponse(await run_in_threadpool(_history_entry, entry_id))

    async def api_restart(request: Request) -> Response:
        if runtime is None or runtime.restart is None:
            return _json_error("Restart is only available in HTTP mode.", 400)
        busy = runtime.busy() if runtime.busy else []
        if busy:
            return _json_error(
                "Operations are running; restarting now would cut them off "
                "half-way. Wait for them to finish.",
                409,
                busy=busy,
            )
        logger.warning(
            "Server settings console: restart requested from %s", _client_ip(request)
        )
        # Answer first; the restart closes this connection.
        threading.Timer(0.5, runtime.restart).start()
        return JSONResponse({"ok": True, "boot_id": runtime.boot_id})

    return [
        Route(PREFIX, page, methods=["GET"]),
        Route(f"{PREFIX}/login", login, methods=["GET", "POST"]),
        Route(f"{PREFIX}/logout", logout, methods=["POST"]),
        Route(f"{PREFIX}/api/state", guarded(api_state), methods=["GET"]),
        Route(f"{PREFIX}/api/preview", guarded(api_preview), methods=["POST"]),
        Route(f"{PREFIX}/api/apply", guarded(api_apply), methods=["POST"]),
        Route(f"{PREFIX}/api/reload", guarded(api_reload), methods=["POST"]),
        Route(f"{PREFIX}/api/reveal", guarded(api_reveal), methods=["POST"]),
        Route(f"{PREFIX}/api/raw", guarded(api_raw), methods=["GET"]),
        Route(f"{PREFIX}/api/history", guarded(api_history), methods=["GET"]),
        Route(
            f"{PREFIX}/api/history/{{entry_id}}",
            guarded(api_history_entry),
            methods=["GET"],
        ),
        Route(f"{PREFIX}/api/restart", guarded(api_restart), methods=["POST"]),
    ]
