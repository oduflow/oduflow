"""Scoped single-environment dashboard access (``/env/<name>``).

The UI counterpart of the scoped MCP endpoint (see ``oduflow.scoped_access``
and ``specs/0028-scoped-environment-mcp-access.md``): an operator shares one
environment with a client, who gets the normal dashboard reduced to that
environment — its card, logs, dev-loop actions, consoles and Agent Chat — and
nothing else of the team.

Two pieces cooperate, both in ``web_ui``:

- the ``/env/<name>`` route exchanges a share link's ``?key=`` (verified
  against ``oduflow.env_share``) for a signed, host-only cookie and renders the
  dashboard in scoped mode;
- ``UIAuthMiddleware`` resolves that cookie to a *scoped principal* — the
  owning team plus one environment name — and runs every subsequent request
  through :func:`is_allowed` here.

The policy is **default-deny**: a request is refused unless it matches this
table, and any env-addressed request must address the cookie's own
environment. So a scoped visitor cannot reach another environment, another
team's resources, or the team-wide surfaces (templates, services, volumes,
extra repos, credentials, productions, host stats) even by typing the URL.

Deliberately denied although the environment is theirs to work in:

- ``WS .../agent`` — **Agent CLI**. It is a PTY in the *per-team* agent
  container, whose ``/workspace`` holds a checkout of every environment of the
  team; a shell there is not confined to one environment. Agent Chat
  (``.../agent-acp``) is exposed instead: same agent, but driven over ACP at
  this environment's checkout with its scoped MCP token.
- everything that creates, destroys or re-provisions: create, delete, update,
  recreate, switch-branch, protect/unprotect, save-as-template.
"""

from __future__ import annotations

import re

# Cookie holding the scoped session (signed team + env + share-secret
# fingerprint). Host-only and SameSite=Strict, like the operator's own session
# cookie. One scoped session per browser and host at a time: opening a second
# share link on the same host replaces the first.
SHARE_COOKIE = "oduflow_env_auth"

# Prefix of the scoped dashboard page.
PAGE_PREFIX = "/env/"

_ENV_API_PREFIX = "/api/environments/"

# Team-wide paths a scoped session may still reach. `/api/agent` reports only
# whether the team's coding agent is enabled and its default type — no secrets
# — and the dashboard needs it to decide whether to offer Agent Chat. Feedback
# returns only a prefilled public GitHub issue URL.
_GLOBAL_ALLOWED: dict[str, frozenset[str]] = {
    "GET": frozenset({"/api/agent"}),
    # Ends the scoped session by clearing the cookie; the visitor gets back in
    # with the share link they were sent.
    "POST": frozenset({"/logout", "/api/feedback/link"}),
}

# Per-environment paths, keyed by method, matched against the part of the path
# that follows `/api/environments/<env>/`.
_ENV_ALLOWED: dict[str, frozenset[str]] = {
    "GET": frozenset(
        {
            "logs",
            "modules",
            "users",
            "connect-open",
            "mcp-access",
            "agent-acp/info",
        }
    ),
    "POST": frozenset(
        {
            "start",
            "stop",
            "restart",
            "sync",
            "modules",
            "storage/refresh",
            "connect-as",
            "agent-acp/session",
            "agent-acp/attachments",
        }
    ),
    "WEBSOCKET": frozenset({"terminal", "sql", "agent-acp"}),
}

# Agent Chat attachment removal: the id is generated per upload.
_ENV_ALLOWED_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "DELETE": (re.compile(r"^agent-acp/attachments/[^/]+$"),),
}

# The environment list: allowed because the scoped dashboard polls it for its
# one card. `api_list` filters the response down to the scoped environment.
_ENV_LIST_PATH = "/api/environments"


def _normalize(path: str) -> str:
    """Drop a single trailing slash so `/env/x/` and `/env/x` behave alike."""
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def is_scoped_page(path: str, env: str) -> bool:
    """Whether ``path`` is the scoped dashboard page of ``env``."""
    return _normalize(path) == PAGE_PREFIX + env


def is_allowed(method: str, path: str, env: str) -> bool:
    """Whether a scoped session for ``env`` may make this request.

    ``method`` is the HTTP method, or ``"WEBSOCKET"`` for a WebSocket
    handshake. Default-deny: anything not matched here is refused.
    """
    if not env:
        return False
    method = method.upper()
    path = _normalize(path)

    if is_scoped_page(path, env):
        return method == "GET"
    if path in _GLOBAL_ALLOWED.get(method, frozenset()):
        return True
    if path == _ENV_LIST_PATH:
        return method == "GET"
    if not path.startswith(_ENV_API_PREFIX):
        return False

    # `<env>/<suffix>`: environment names may contain slashes (feature/x), so
    # match the name explicitly rather than splitting on the next separator.
    rest = path[len(_ENV_API_PREFIX) :]
    if not rest.startswith(env + "/"):
        return False
    suffix = rest[len(env) + 1 :]
    if suffix in _ENV_ALLOWED.get(method, frozenset()):
        return True
    return any(p.match(suffix) for p in _ENV_ALLOWED_PATTERNS.get(method, ()))
