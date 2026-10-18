"""``execute_kw``-equivalent ORM access over Odoo's authenticated web JSON-RPC.

Calls ``/web/dataset/call_kw`` over HTTP from *inside* the environment's Odoo
container (``http://127.0.0.1:8069``) via a one-shot ``python3`` helper delivered
by ``put_archive`` — the same delivery mechanism as
:func:`odoo_ops.run_odoo_shell`, and the same in-container HTTP approach as
:func:`production_ops._probe_odoo_health`.

In-container on purpose: the call is then independent of ``routing_mode`` (port
vs traefik), needs no DNS, no TLS and no published port, and behaves identically
on Linux and macOS. It also costs a container exec instead of an ``odoo shell``
registry boot.

Authentication is passwordless: a session is minted server-side once per
(team, environment, user) by :func:`odoo_ops.connect_as_user` and cached in this
process. Because the session belongs to a real user, ACLs and record rules are
enforced exactly as they are for that user in the UI.

The route, the JSON-RPC envelope and the error payload shape are stable across
the Odoo versions Oduflow supports (15-19). Odoo 19 renamed the route's
``type='json'`` to ``type='jsonrpc'``, but that declaration never crosses the
wire. ``/jsonrpc`` and ``/xmlrpc/2`` are deliberately *not* used: they are
deprecated in 19, scheduled for removal in 20, and each call logs a warning into
the very environment log an agent reads while debugging.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import secrets
import tarfile
import threading
import time
from dataclasses import dataclass
from typing import Any

import docker
from oduflow.docker_ops.client import get_client
from oduflow.errors import ExternalCommandError, NotFoundError
from oduflow.naming import get_resource_name
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

# Loopback inside the Odoo container. Every conf Oduflow generates pins
# http_interface = 0.0.0.0 (Odoo 20 alone would bind only 127.0.0.1), and
# loopback is reachable under either binding, so this works in every
# environment.
ODOO_ORIGIN = "http://127.0.0.1:8069"
# Cookie name Odoo has used for the session id since well before 15.0. Also the
# load-bearing assumption of connect_as_user (spec 0031).
SESSION_COOKIE = "session_id"

RPC_TIMEOUT = 120
READY_TIMEOUT = 60
# Sessions are capped well below Odoo's own SESSION_LIFETIME: many things
# silently invalidate a session token (a password reset, a template restore, a
# DB restore), and Odoo 19 rotates session ids every 3 hours. A short cap bounds
# how long a doomed sid is retried, and keeps a live credential out of process
# memory for days.
SESSION_TTL = 3600.0
MAX_SESSIONS = 200
# Guards the Oduflow process against a fields=""-style query pulling binaries.
MAX_RESPONSE_BYTES = 4_000_000

# Odoo model names are dotted lowercase identifiers. Validated before entering a
# URL path segment.
_MODEL_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# Operators that identify a bare domain leaf such as ["name", "=", "x"], which
# models routinely pass where a list of leaves is expected.
_DOMAIN_OPERATORS = frozenset(
    {
        "=",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "=?",
        "=like",
        "=ilike",
        "like",
        "not like",
        "ilike",
        "not ilike",
        "in",
        "not in",
        "child_of",
        "parent_of",
        "any",
        "not any",
    }
)


# ---------------------------------------------------------------------------
# Argument parsing (pure — unit-tested directly)
# ---------------------------------------------------------------------------


def _jsonable(value: Any, what: str) -> Any:
    """Return *value* with tuples flattened to lists, rejecting non-JSON types.

    ``ast.literal_eval`` accepts tuples and sets; the former are a normal way to
    write an Odoo domain by hand, the latter cannot be sent over JSON-RPC at all.
    """
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, what) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v, what) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(
        f"{what} contains a {type(value).__name__} value, which cannot be sent "
        "over JSON-RPC. Use JSON types only."
    )


def parse_json_arg(raw: str, what: str, default: Any = None) -> Any:
    """Parse a JSON argument, tolerating a Python literal.

    Models frequently emit Python syntax (single quotes, tuples, ``True``/
    ``None``) where JSON is expected. ``ast.literal_eval`` accepts those without
    evaluating code — a plain ``eval`` here would be arbitrary code execution.
    """
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        literal = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        raise ValueError(
            f"{what} is neither valid JSON nor a Python literal: {text[:120]!r}"
        )
    # Outside the try: _jsonable's own rejection (a set, say) is a precise
    # message that must not be flattened into "not a literal".
    return _jsonable(literal, what)


def parse_domain(raw: str) -> list[Any]:
    """Parse a search domain, wrapping a bare leaf into a list of leaves."""
    value = parse_json_arg(raw, "domain", [])
    if not isinstance(value, list):
        raise ValueError(
            "domain must be a JSON array of leaves, e.g. "
            '[["state", "=", "sale"]]. Got: ' + type(value).__name__
        )
    if (
        len(value) == 3
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and value[1] in _DOMAIN_OPERATORS
    ):
        # ["name", "=", "x"] → [["name", "=", "x"]]. Prefix operators ("&", "|",
        # "!") are not wrapped because their operands are lists, not strings.
        return [value]
    return value


def parse_fields(raw: str) -> list[str] | None:
    """Parse ``fields`` from a comma-separated list or a JSON array."""
    text = (raw or "").strip()
    if not text:
        return None
    value = parse_json_arg(text, "fields") if text.startswith("[") else None
    if value is None:
        value = [f.strip() for f in text.split(",") if f.strip()]
    if not isinstance(value, list) or not all(isinstance(f, str) for f in value):
        raise ValueError(
            "fields must be a comma-separated list of field names "
            '("name,email") or a JSON array of strings.'
        )
    return value


def parse_ids(raw: str, what: str = "ids") -> list[int]:
    """Parse record ids from "42", "1,2,3" or "[1,2,3]" — order-preserving."""
    text = (raw or "").strip()
    if not text:
        raise ValueError(
            f'{what} is required: pass a single id ("42"), a comma-separated '
            'list ("1,2,3") or a JSON array ("[1,2,3]").'
        )
    value = parse_json_arg(text, what) if text.startswith("[") else None
    if value is None:
        value = [p.strip() for p in text.split(",") if p.strip()]
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list of integer record ids.")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValueError(f"{what} must contain integers; got {item!r}.")
        try:
            number = int(item)
        except ValueError:
            raise ValueError(f"{what} must contain integers; got {item!r}.")
        if number not in out:
            out.append(number)
    if not out:
        raise ValueError(f"{what} is required and must contain at least one id.")
    return out


def parse_values(raw: str, *, allow_list: bool = False) -> Any:
    """Parse ``values`` — a field/value object, or a list of them."""
    value = parse_json_arg(raw, "values")
    if value is None:
        raise ValueError(
            'values is required: a JSON object of field values, e.g. {"name": "Acme"}.'
        )
    value = _jsonable(value, "values")
    if isinstance(value, dict):
        return value
    if (
        allow_list
        and isinstance(value, list)
        and all(isinstance(v, dict) for v in value)
    ):
        if not value:
            raise ValueError("values must contain at least one record.")
        return value
    if allow_list:
        raise ValueError(
            "values must be a JSON object of field values, or a JSON array of "
            "such objects to create several records at once."
        )
    raise ValueError(
        'values must be a JSON object of field values, e.g. {"name": "Acme"}.'
    )


def parse_context(raw: str) -> dict[str, Any] | None:
    """Parse an optional context override."""
    value = parse_json_arg(raw, "context")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(
            'context must be a JSON object, e.g. {"lang": "fr_FR", "active_test": false}.'
        )
    return value


def validate_model(model: str) -> str:
    """Validate an Odoo model name before it enters a URL path segment."""
    name = (model or "").strip()
    if not _MODEL_RE.match(name):
        raise ValueError(
            f"Invalid model name {model!r}: expected a dotted lowercase "
            'technical name such as "res.partner".'
        )
    return name


def validate_method(method: str) -> str:
    """Validate a model method name, rejecting private ones."""
    name = (method or "").strip()
    if name.startswith("_"):
        raise ValueError(
            f"Method {method!r} is private and is not callable over RPC "
            "(Odoo 19 rejects it server-side too). Use run_odoo_shell for "
            "private methods and registry internals."
        )
    if not _METHOD_RE.match(name):
        raise ValueError(
            f"Invalid method name {method!r}: expected a plain identifier such "
            'as "search_read".'
        )
    return name


# ---------------------------------------------------------------------------
# In-container helper
# ---------------------------------------------------------------------------

# Runs as root so it can unlink itself from the sticky /tmp: the payload embeds a
# live session id, and a crashed Oduflow must not leave that credential readable
# on the container filesystem. CPython compiles the whole file before executing
# the first statement, so the unlink is safe.
#
# The response is framed with an explicit byte length rather than sentinels
# because ORM data may contain any string, including a sentinel.
_HELPER_TEMPLATE = """\
import json, os, sys, urllib.error, urllib.request

try:
    os.unlink(__file__)
except OSError:
    pass

P = json.loads(__ODUFLOW_PAYLOAD__)
request = urllib.request.Request(
    P["url"],
    data=json.dumps(P["body"]).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": P["origin"],
        "Referer": P["origin"] + "/web",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": P["cookie"] + "=" + P["sid"],
    },
    method="POST",
)
try:
    response = urllib.request.urlopen(request, timeout=P["timeout"])
    status, raw = response.status, response.read(P["max_bytes"] + 1)
except urllib.error.HTTPError as exc:
    status, raw = exc.code, exc.read(P["max_bytes"] + 1)
except urllib.error.URLError as exc:
    sys.stderr.write("cannot reach Odoo at %s: %s" % (P["origin"], exc.reason))
    raise SystemExit(4)
if len(raw) > P["max_bytes"]:
    sys.stderr.write("response is %d bytes" % len(raw))
    raise SystemExit(5)
out = sys.stdout.buffer
out.write(("ODUFLOW-RPC %d %d\\n" % (status, len(raw))).encode("ascii"))
out.write(raw)
out.flush()
"""

_READY_PROBE = [
    "python3",
    "-c",
    "import sys,urllib.request;"
    f"sys.exit(0 if urllib.request.urlopen('{ODOO_ORIGIN}/web/health',"
    "timeout=5).status == 200 else 1)",
]


def build_helper_script(payload: dict[str, Any]) -> str:
    """Render the in-container helper with *payload* embedded as a JSON literal."""
    return _HELPER_TEMPLATE.replace("__ODUFLOW_PAYLOAD__", repr(json.dumps(payload)))


def parse_helper_stdout(stdout: bytes) -> tuple[int, bytes]:
    """Split the helper's framed stdout into ``(http_status, body)``."""
    marker = b"ODUFLOW-RPC "
    start = stdout.find(marker)
    newline = stdout.find(b"\n", start) if start >= 0 else -1
    if start < 0 or newline < 0:
        raise ExternalCommandError(
            "odoo call_kw",
            0,
            "The in-container helper produced no framed response: "
            f"{stdout[:2000].decode('utf-8', 'replace')}",
        )
    header = stdout[start + len(marker) : newline].split()
    try:
        status, length = int(header[0]), int(header[1])
    except (IndexError, ValueError):
        raise ExternalCommandError(
            "odoo call_kw", 0, f"Malformed response header: {header!r}"
        )
    body = stdout[newline + 1 : newline + 1 + length]
    if len(body) != length:
        raise ExternalCommandError(
            "odoo call_kw",
            0,
            f"Truncated response: expected {length} bytes, got {len(body)}.",
        )
    return status, body


@dataclass
class _RawResponse:
    status: int
    payload: Any


def _put_helper(container: Any, script: str) -> str:
    """Write the helper into the container's /tmp and return its path."""
    basename = f"_oduflow_rpc_{secrets.token_hex(16)}.py"
    data = script.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=basename)
        info.size = len(data)
        # Root-owned and unreadable by the odoo user: the payload carries a live
        # session id, and tenant code runs as odoo inside this container.
        info.mode = 0o600
        info.uid = 0
        info.gid = 0
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive("/tmp", tar_stream)
    return f"/tmp/{basename}"


def _exec_rpc(
    container: Any,
    sid: str,
    model: str,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any],
    timeout: int = RPC_TIMEOUT,
) -> _RawResponse:
    """POST one call_kw request from inside *container* and return the response."""
    payload = {
        "url": f"{ODOO_ORIGIN}/web/dataset/call_kw/{model}/{method}",
        "origin": ODOO_ORIGIN,
        "cookie": SESSION_COOKIE,
        "sid": sid,
        "timeout": timeout,
        "max_bytes": MAX_RESPONSE_BYTES,
        "body": {
            "jsonrpc": "2.0",
            "method": "call",
            "id": 1,
            "params": {
                "model": model,
                "method": method,
                "args": args,
                "kwargs": kwargs,
            },
        },
    }
    script_path = _put_helper(container, build_helper_script(payload))
    try:
        # The session id lives only inside the script file: never in argv (`ps`)
        # and never in the exec environment — the same rule PGPASSWORD follows in
        # run_odoo_shell.
        exit_code, streams = container.exec_run(
            ["python3", script_path], user="root", demux=True
        )
    finally:
        container.exec_run(["rm", "-f", script_path], user="root")

    stdout, stderr = streams if isinstance(streams, tuple) else (streams, b"")
    stdout = stdout or b""
    stderr_text = (stderr or b"").decode("utf-8", "replace").strip()

    if exit_code == 4:
        raise ConnectionRefusedError(stderr_text or "Odoo is not answering on 8069.")
    if exit_code == 5:
        raise ValueError(
            f"The Odoo response is larger than {MAX_RESPONSE_BYTES} bytes "
            f"({stderr_text}). Narrow the query: pass `fields` explicitly and "
            "lower `limit`."
        )
    if exit_code != 0:
        raise ExternalCommandError(
            "odoo call_kw", exit_code, stderr_text or stdout.decode("utf-8", "replace")
        )

    status, body = parse_helper_stdout(stdout)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except ValueError:
        raise ExternalCommandError(
            "odoo call_kw",
            status,
            "Odoo returned a non-JSON response (HTTP "
            f"{status}): {body[:2000].decode('utf-8', 'replace')}",
        )
    return _RawResponse(status=status, payload=parsed)


def _wait_http_ready(container: Any, timeout: int = READY_TIMEOUT) -> bool:
    """Poll ``/web/health`` from inside the container until it answers 200.

    Probing in-container rather than through :func:`env_ops.wait_for_odoo_ready`
    on purpose: the latter goes through the environment's public URL, which in
    traefik mode depends on DNS and TLS being live — the exact dependency this
    module exists to avoid.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if container.exec_run(_READY_PROBE)[0] == 0:
                return True
        except docker.errors.APIError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------


@dataclass
class _CachedSession:
    sid: str
    login: str
    uid: int
    expires_at: float


_SESSIONS: dict[tuple[str, str, str], _CachedSession] = {}
_MINT_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_LOCK = threading.Lock()


def _prune_locked() -> None:
    now = time.time()
    for key in [k for k, s in _SESSIONS.items() if s.expires_at <= now]:
        _SESSIONS.pop(key, None)
        _MINT_LOCKS.pop(key, None)
    while len(_SESSIONS) >= MAX_SESSIONS:
        oldest = min(_SESSIONS, key=lambda k: _SESSIONS[k].expires_at)
        _SESSIONS.pop(oldest, None)
        _MINT_LOCKS.pop(oldest, None)


def invalidate_sessions(team_id: str, env_name: str = "") -> int:
    """Drop cached sessions for a team, or for one environment of it.

    Called after operations that make Odoo reject existing sessions — notably
    ``reset_admin_password``, since the session token is derived from the user's
    password hash.
    """
    with _LOCK:
        keys = [
            k
            for k in _SESSIONS
            if k[0] == team_id and (not env_name or k[1] == env_name)
        ]
        for key in keys:
            _SESSIONS.pop(key, None)
            _MINT_LOCKS.pop(key, None)
    return len(keys)


def _get_session(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    as_user: str,
    *,
    force: bool = False,
) -> tuple[_CachedSession, bool]:
    """Return a cached or freshly minted session, plus whether it was minted."""
    from oduflow.docker_ops.odoo_ops import connect_as_user

    key = (team.team_id, env_name, as_user.strip())
    with _LOCK:
        if not force:
            cached = _SESSIONS.get(key)
            if cached is not None and cached.expires_at > time.time():
                return cached, False
        _SESSIONS.pop(key, None)
        mint_lock = _MINT_LOCKS.setdefault(key, threading.Lock())

    # Minting boots an Odoo registry (seconds). Holding only the per-key lock
    # keeps other environments' calls moving.
    with mint_lock:
        with _LOCK:
            cached = _SESSIONS.get(key)
            if not force and cached is not None and cached.expires_at > time.time():
                return cached, False
        info = connect_as_user(settings, team, env_name, as_user)
        uid = int(info["uid"] or 0)
        if uid == 1:
            raise ValueError(
                "as_user resolved to the superuser (uid 1), which is not a real "
                "web-session actor. Use run_odoo_shell for sudo() access."
            )
        session = _CachedSession(
            sid=info["sid"],
            login=info["login"],
            uid=uid,
            expires_at=time.time() + SESSION_TTL,
        )
        with _LOCK:
            _prune_locked()
            _SESSIONS[key] = session
        return session, True


def _is_session_invalid(raw: _RawResponse) -> bool:
    """True when Odoo rejected the session rather than the call itself."""
    if raw.status in (401, 403):
        return True
    error = raw.payload.get("error") if isinstance(raw.payload, dict) else None
    if not isinstance(error, dict):
        return False
    if error.get("code") == 100:  # "Odoo Session Expired"
        return True
    data = error.get("data")
    name = str(data.get("name", "")) if isinstance(data, dict) else ""
    return "SessionExpired" in name


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class RpcResult:
    """Outcome of one ``call_kw``.

    Odoo-side failures are *results*, not exceptions: "may this user write to
    this record" is exactly the question ``as_user`` exists to answer, and the
    server traceback in :attr:`error_debug` is the most useful thing this module
    can hand an agent.
    """

    ok: bool
    value: Any = None
    error_name: str = ""
    error_message: str = ""
    error_debug: str = ""
    login: str = ""
    uid: int = 0
    minted: bool = False

    def error_text(self) -> str:
        short = self.error_name.rsplit(".", 1)[-1] or "Error"
        lines = [f"{short}: {self.error_message}".rstrip(": ").rstrip()]
        if self.error_debug:
            lines.append("")
            lines.append(self.error_debug.rstrip())
        return "\n".join(lines)


def _to_result(raw: _RawResponse, session: _CachedSession, minted: bool) -> RpcResult:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    error = payload.get("error")
    if isinstance(error, dict):
        raw_data = error.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        return RpcResult(
            ok=False,
            error_name=str(data.get("name") or error.get("message") or "Odoo error"),
            error_message=str(data.get("message") or error.get("message") or ""),
            error_debug=str(data.get("debug") or ""),
            login=session.login,
            uid=session.uid,
            minted=minted,
        )
    return RpcResult(
        ok=True,
        value=payload.get("result"),
        login=session.login,
        uid=session.uid,
        minted=minted,
    )


def call_kw(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    model: str,
    method: str,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    as_user: str = "",
) -> RpcResult:
    """Run one ``execute_kw``-equivalent call against the environment's Odoo.

    *as_user* is a login or a numeric user id; empty means the environment's
    admin. The call runs inside a real session for that user, so ACLs and record
    rules apply exactly as they do in the web client.
    """
    model = validate_model(model)
    method = validate_method(method)
    args = list(args or [])
    kwargs = dict(kwargs or {})

    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    minted = False
    for attempt in (0, 1):
        session, was_minted = _get_session(
            settings, team, env_name, as_user, force=bool(attempt)
        )
        minted = minted or was_minted
        try:
            raw = _exec_rpc(container, session.sid, model, method, args, kwargs)
        except ConnectionRefusedError as exc:
            # The environment was probably just woken: Odoo serves HTTP a while
            # after the container starts.
            if attempt or not _wait_http_ready(container):
                raise ExternalCommandError("odoo call_kw", 0, str(exc))
            raw = _exec_rpc(container, session.sid, model, method, args, kwargs)

        if _is_session_invalid(raw):
            if attempt == 0:
                logger.info(
                    "Odoo rejected the cached session; re-minting",
                    extra={"env_name": env_name, "login": session.login},
                )
                continue
            raise ExternalCommandError(
                "odoo call_kw",
                0,
                f"Odoo rejected a freshly minted session for '{session.login}' in "
                f"'{env_name}'. The session store or session_token computation may "
                "differ in this Odoo version — use run_odoo_shell instead.",
            )
        return _to_result(raw, session, minted)

    raise AssertionError("unreachable")  # pragma: no cover
