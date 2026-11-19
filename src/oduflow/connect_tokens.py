"""One-time, short-lived tokens for the cross-subdomain Connect As landing.

Under traefik routing an environment lives on its own host
(``<slug>.<team-host>``), so the dashboard cannot set the env's ``session_id``
cookie directly: a parent-domain cookie would *not* override a stale host-only
``session_id`` Odoo may have left on the env host (they are distinct cookies per
RFC 6265, and the older one is sent first). Instead the browser is sent to
``https://<env-host>/oduflow-connect?token=...``. Traefik routes that path to
Oduflow (not Odoo), which redeems the token and sets ``session_id`` HOST-ONLY on
the env host — overriding any stale cookie.

A token stands for one of two things:

- **A minted session** (:func:`issue` / :func:`consume`) — the dashboard's
  "Open" button. The browser follows a redirect straight to the landing, so the
  session is minted up front and the first GET consumes the token.
- **A deferred grant** (:func:`issue_link` / :func:`peek_link` /
  :func:`discard`) — the ``connect_link`` MCP tool. The link travels through
  another application to a human and may never be opened, or be fetched first by
  a link scanner. So the token carries only *who to sign in as*; the Odoo session
  is minted when a browser actually redeems the link, and the token is removed
  only once that has succeeded.

The store is in-process (the server is single-process, like ``agent_sessions``);
entries expire after a short TTL and are bound to the env host they were issued
for.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from oduflow.env_tokens import generate_token

# Long enough to survive a couple of redirects and a slow tab open, short enough
# that a leaked link is useless almost immediately.
_TTL_SECONDS = 120.0

# A connect_link URL is relayed by an MCP client to a person, who has to notice
# and click it, so it gets a little longer than the dashboard's own redirect.
LINK_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class LinkGrant:
    """What a ``connect_link`` token entitles its holder to.

    ``user`` is the selector exactly as :func:`odoo_ops.connect_as_user` takes it
    (a login, a numeric id, or ``""`` for the database's admin), so redemption
    resolves the user by the same rule that validated it when the link was
    issued. ``login`` is what that resolved to then, for display only.
    """

    team_id: str
    env_name: str
    user: str
    login: str


_lock = threading.Lock()
# token -> (env_host, minted session_id or deferred grant, expiry_monotonic)
_store: dict[str, tuple[str, str | LinkGrant, float]] = {}


def _now(now: float | None) -> float:
    return time.monotonic() if now is None else now


def _prune(now: float) -> None:
    for token in [t for t, (_, _, exp) in _store.items() if exp <= now]:
        _store.pop(token, None)


def _put(env_host: str, payload: str | LinkGrant, ttl: float, now: float | None) -> str:
    ts = _now(now)
    token = generate_token()
    with _lock:
        _prune(ts)
        _store[token] = (env_host.lower(), payload, ts + ttl)
    return token


def issue(env_host: str, session_id: str, *, now: float | None = None) -> str:
    """Store ``session_id`` bound to ``env_host`` and return a one-time token."""
    return _put(env_host, session_id, _TTL_SECONDS, now)


def consume(token: str, env_host: str, *, now: float | None = None) -> str | None:
    """Return the ``session_id`` for a valid session token, once.

    Returns ``None`` if the token is unknown, already used, expired, or was
    issued for a different host than the one now presenting it. A deferred
    ``connect_link`` token is not a session: it also yields ``None`` here and is
    left in place for :func:`peek_link`.
    """
    ts = _now(now)
    with _lock:
        _prune(ts)
        entry = _store.get(token)
        if entry is None or isinstance(entry[1], LinkGrant):
            return None
        del _store[token]
    stored_host, session_id, expiry = entry
    if expiry <= ts or stored_host != env_host.lower():
        return None
    assert isinstance(session_id, str)
    return session_id


def issue_link(env_host: str, grant: LinkGrant, *, now: float | None = None) -> str:
    """Store a deferred sign-in grant bound to ``env_host``; return its token."""
    return _put(env_host, grant, LINK_TTL_SECONDS, now)


def peek_link(
    token: str, env_host: str, *, now: float | None = None
) -> LinkGrant | None:
    """Return the grant behind a valid ``connect_link`` token without using it up.

    Reading is deliberately free of side effects: showing the landing page (or a
    link scanner fetching it) must not spend the link. ``None`` if the token is
    unknown, already redeemed, expired, presented on another host, or is a
    session token.
    """
    ts = _now(now)
    with _lock:
        _prune(ts)
        entry = _store.get(token)
    if entry is None:
        return None
    stored_host, payload, expiry = entry
    if expiry <= ts or stored_host != env_host.lower():
        return None
    return payload if isinstance(payload, LinkGrant) else None


def discard(token: str) -> None:
    """Remove a token: its link has been redeemed, or can never be."""
    with _lock:
        _store.pop(token, None)
