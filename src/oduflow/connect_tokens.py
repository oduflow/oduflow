"""One-time, short-lived tokens for the cross-subdomain Connect As "Open" flow.

Under traefik routing an environment lives on its own host
(``<slug>.<team-host>``), so the dashboard cannot set the env's ``session_id``
cookie directly: a parent-domain cookie would *not* override a stale host-only
``session_id`` Odoo may have left on the env host (they are distinct cookies per
RFC 6265, and the older one is sent first). Instead the dashboard mints the
session, stashes the resulting ``session_id`` behind a one-time token here, and
303-redirects the browser to ``https://<env-host>/oduflow-connect?token=...``.
Traefik routes that path to Oduflow (not Odoo), which consumes the token and
sets ``session_id`` HOST-ONLY on the env host — overriding any stale cookie.

A token carries either a ready ``session_id`` (the dashboard, which redirects
the browser at once) or a :class:`LoginGrant` (the ``connect_link`` MCP tool):
a grant holds no Odoo session yet — the landing mints it only when the browser
actually opens the link, so a link that is never used leaves nothing behind.

The store is in-process (the server is single-process, like ``agent_sessions``);
entries are consumed exactly once and expire after a short TTL.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from oduflow.env_tokens import generate_token

# Long enough to survive a couple of redirects and a slow tab open, short enough
# that a leaked link is useless almost immediately.
_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class LoginGrant:
    """Permission to mint a session as *user* in an environment, on redemption."""

    team_id: str
    env_name: str
    user: str


Payload = str | LoginGrant

_lock = threading.Lock()
# token -> (env_host, session_id or grant, expiry_monotonic)
_store: dict[str, tuple[str, Payload, float]] = {}


def _now(now: float | None) -> float:
    return time.monotonic() if now is None else now


def _prune(now: float) -> None:
    for token in [t for t, (_, _, exp) in _store.items() if exp <= now]:
        _store.pop(token, None)


def issue(env_host: str, payload: Payload, *, now: float | None = None) -> str:
    """Store a ``session_id`` or :class:`LoginGrant` bound to ``env_host``.

    Returns the one-time token.
    """
    ts = _now(now)
    token = generate_token()
    with _lock:
        _prune(ts)
        _store[token] = (env_host.lower(), payload, ts + _TTL_SECONDS)
    return token


def consume(token: str, env_host: str, *, now: float | None = None) -> Payload | None:
    """Return the ``session_id`` or :class:`LoginGrant` for a valid token, once.

    Returns ``None`` if the token is unknown, already used, expired, or was
    issued for a different host than the one now presenting it.
    """
    ts = _now(now)
    with _lock:
        _prune(ts)
        entry = _store.pop(token, None)
    if entry is None:
        return None
    stored_host, payload, expiry = entry
    if expiry <= ts or stored_host != env_host.lower():
        return None
    return payload


def login_link(base_url: str, payload: Payload) -> str:
    """Stash *payload* behind a one-time token and return the landing URL.

    *base_url* is the environment's own URL (``scheme://host``, from
    ``env_ops.get_env_base_url`` — the single source of the env URL rule). The
    result, ``<base_url>/oduflow-connect?token=...``, logs the browser that
    opens it into the environment: the env host's ``/oduflow-connect`` route
    consumes the token and sets ``session_id`` host-only there. Shared by the
    dashboard's "Open" button and the ``connect_link`` MCP tool. Traefik
    routing only — in port mode there is no per-env host serving
    ``/oduflow-connect``.

    The returned URL is a bearer credential until consumed or expired: never
    log it.
    """
    env_host = urlsplit(base_url).hostname or ""
    token = issue(env_host, payload)
    return f"{base_url.rstrip('/')}/oduflow-connect?token={token}"
