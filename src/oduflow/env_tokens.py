"""Per-environment MCP access tokens.

Each environment gets a personal token stored in the Docker label
``oduflow.mcp_token`` at creation time (see ``env_ops.create_environment``).
The token is a Bearer token authorizing the scoped MCP endpoint ``/mcp/<env>``
(single-environment access). That endpoint is Bearer-only — the OAuth flow
(client_id/secret) is wired only for the team-wide ``/mcp`` endpoint.

``resolve_token`` maps an incoming token to a ``(team_id, env_name)`` pair:

- a team ``auth_token`` from settings -> ``(team_id, None)``  [full access]
- a container's ``oduflow.mcp_token`` label -> ``(team_id, env_name)``  [scoped]

Resolution must stay cheap on the hot path: token verification runs on *every*
HTTP request, and an unauthenticated caller can spam invalid Bearer tokens at
the public endpoint. So env-token lookups are served from an in-memory cache
that is refreshed by scanning Docker at most once per ``_MIN_SCAN_INTERVAL``;
within that window unknown tokens are rejected without a rescan (implicit
negative cache), which bounds a token flood to one scan per interval. The
blocking Docker scan is offloaded to a thread by ``resolve_token_async`` so it
never stalls the event loop.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time

from oduflow.settings import Settings, secret_matches

logger = logging.getLogger("oduflow")

MCP_TOKEN_LABEL = "oduflow.mcp_token"

# Minimum seconds between full Docker scans. Bounds a flood of invalid tokens to
# one scan per interval and caps how long a freshly created env token waits to
# become resolvable (create/delete call invalidate_cache to force an early scan).
_MIN_SCAN_INTERVAL = 5.0

_lock = threading.Lock()
_scan_condition = threading.Condition(_lock)
# token -> (team_id, env_name) for env-scoped tokens. Team tokens are resolved
# directly from settings and are never cached here.
_env_tokens: dict[str, tuple[str, str]] = {}
_last_scan: float = 0.0
_scan_in_progress = False


def generate_token() -> str:
    """Generate a fresh per-environment access token."""
    return secrets.token_urlsafe(32)


def _scan_env_tokens(settings: Settings) -> dict[str, tuple[str, str]]:
    """Scan managed containers and build ``{token: (team_id, env_name)}``."""
    from oduflow.docker_ops.client import get_client

    result: dict[str, tuple[str, str]] = {}
    try:
        client = get_client()
        containers = client.containers.list(
            all=True, filters={"label": f"{settings.managed_label}=true"}
        )
    except Exception as e:  # pragma: no cover - docker unavailable
        logger.debug("env-token scan failed: %s", e)
        return result
    for container in containers:
        labels = container.labels or {}
        token = labels.get(MCP_TOKEN_LABEL)
        env_name = labels.get(settings.branch_label)
        team_id = labels.get(settings.team_label)
        if token and env_name and team_id:
            result[token] = (team_id, env_name)
    return result


def resolve_token(settings: Settings, token: str) -> tuple[str, str | None] | None:
    """Resolve a presented token to ``(team_id, env_name)``.

    Returns ``(team_id, None)`` for a team ``auth_token`` (full access),
    ``(team_id, env_name)`` for a per-environment token (scoped access), or
    ``None`` if the token is unknown.

    May block on a Docker scan (rate-limited); async callers should prefer
    :func:`resolve_token_async` to keep the event loop free.
    """
    global _last_scan, _scan_in_progress
    if not token:
        return None
    # 1. Team tokens (static, from settings) — never touches Docker.
    for team_id, team in settings.teams.items():
        if team.auth_token and secret_matches(team.auth_token, token):
            return (team_id, None)
    # 2. Env tokens: serve from cache; rescan at most once per interval.
    with _scan_condition:
        hit = _env_tokens.get(token)
        if hit is not None:
            return hit
        fresh_enough = (time.monotonic() - _last_scan) < _MIN_SCAN_INTERVAL
        if fresh_enough:
            return None  # unknown token, rate-limited: don't rescan
        if _scan_in_progress:
            while _scan_in_progress:
                _scan_condition.wait()
            return _env_tokens.get(token)
        _scan_in_progress = True
    try:
        fresh = _scan_env_tokens(settings)  # outside lock (blocking Docker call)
    except Exception:
        with _scan_condition:
            _scan_in_progress = False
            _scan_condition.notify_all()
        raise
    with _scan_condition:
        _env_tokens.clear()
        _env_tokens.update(fresh)
        _last_scan = time.monotonic()
        _scan_in_progress = False
        _scan_condition.notify_all()
    return fresh.get(token)


async def resolve_token_async(
    settings: Settings, token: str
) -> tuple[str, str | None] | None:
    """Async wrapper that offloads :func:`resolve_token` to a worker thread."""
    import anyio

    return await anyio.to_thread.run_sync(resolve_token, settings, token)


def invalidate_cache() -> None:
    """Drop the cached env-token map and force the next lookup to rescan."""
    global _last_scan
    with _scan_condition:
        _env_tokens.clear()
        _last_scan = 0.0
