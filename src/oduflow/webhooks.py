"""GitHub push webhooks → automatic production deploys.

``POST /api/webhooks/github`` (public path, no UI session): the request is
authenticated by its ``X-Hub-Signature-256`` HMAC instead. Each team's
secret is auto-generated with its first production and doubles as the team
resolver — the signature is verified against every team's secret and the
first match wins (team counts are single digits; the cost is nil).

On a ``push`` event the matching team's productions with the same
(normalized) repo URL + branch AND ``auto_update`` enabled are deployed in
background threads via :func:`production_ops.update_production` — with its
automatic code rollback. Dev environments are NEVER touched by webhooks.

Coalescing: pushes arriving while a deploy runs queue behind the
production's lock (blocking acquire with a timeout), and at most ONE
queued run is kept — the queued deploy pulls the latest state anyway, so
N rapid pushes collapse into at most one running + one pending deploy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from typing import Any
from urllib.parse import urlparse

from oduflow import production_registry
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

# How long a webhook-triggered deploy waits for the production's lock
# before giving up (a snapshot or manual deploy may be running).
_LOCK_TIMEOUT_SECONDS = 30 * 60

# Productions with a deploy already queued (coalescing). Guarded by _guard.
_pending: set[str] = set()
_guard = threading.Lock()


def normalize_repo_url(url: str) -> str:
    """Canonical form for repo matching: lowercase host, no scheme, no
    credentials, no trailing .git, no trailing slash.

    Handles https URLs and scp-like ssh syntax (git@host:owner/repo.git) —
    GitHub payloads carry both forms.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url and ":" in url and "@" in url.split(":", 1)[0]:
        # scp-like: git@github.com:owner/repo.git
        userhost, _, path = url.partition(":")
        host = userhost.rpartition("@")[2]
        url = f"https://{host}/{path}"
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}{path}"


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Validate GitHub's ``X-Hub-Signature-256: sha256=<hex>`` header."""
    if not secret or not signature_header:
        return False
    scheme, _, digest = signature_header.partition("=")
    if scheme.lower() != "sha256" or not digest:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest.strip().lower())


def resolve_team(
    settings: Settings, body: bytes, signature_header: str
) -> TeamSettings | None:
    """The team whose webhook secret signed this request (None = no match)."""
    for team in settings.teams.values():
        try:
            secret = production_registry.get_webhook_secret(team)
        except Exception:
            continue
        if secret and verify_signature(secret, body, signature_header):
            return team
    return None


def match_productions(team: TeamSettings, payload: dict[str, Any]) -> list[str]:
    """Production names to auto-deploy for a push payload.

    Match rule: same branch (payload ref ``refs/heads/{branch}``), same
    normalized repo URL (any of clone_url / html_url / ssh_url / url), and
    ``auto_update`` enabled.
    """
    ref = str(payload.get("ref", ""))
    if not ref.startswith("refs/heads/"):
        return []
    branch = ref[len("refs/heads/") :]
    repo = payload.get("repository") or {}
    push_urls = {
        normalize_repo_url(str(repo.get(key, "")))
        for key in ("clone_url", "html_url", "ssh_url", "url", "git_url")
    }
    push_urls.discard("")
    if not push_urls:
        return []

    names = []
    for name, record in production_registry.list_productions(team).items():
        if not record.get("auto_update"):
            continue
        if record.get("branch") != branch:
            continue
        if normalize_repo_url(str(record.get("repo_url", ""))) in push_urls:
            names.append(name)
    return sorted(names)


def _deploy_in_background(
    settings: Settings, team: TeamSettings, locks: LockManager, name: str
) -> None:
    from oduflow.docker_ops import production_ops
    from oduflow.locking import prod_lock_key

    key = prod_lock_key(team.team_id, name)
    pending_key = f"{team.team_id}/{name}"
    try:
        if not locks.acquire_env_blocking(
            key, _LOCK_TIMEOUT_SECONDS, operation="webhook deploy"
        ):
            logger.warning(
                "Webhook deploy of production '%s' gave up waiting for its "
                "lock (%.0f min)",
                name,
                _LOCK_TIMEOUT_SECONDS / 60,
            )
            return
        # Holding the lock: this run is no longer "pending" — a new push
        # arriving from here on may queue the next one.
        with _guard:
            _pending.discard(pending_key)
        try:
            result = production_ops.update_production(
                settings, team, name, trigger="webhook"
            )
            logger.info(
                "Webhook deploy of production '%s': %s",
                name,
                result.get("action"),
            )
        finally:
            locks.release_env(key)
    except Exception:
        logger.exception("Webhook deploy of production '%s' failed", name)
    finally:
        # Safety net: the lock-timeout early return (and any exception raised
        # before the discard above) must never leak the coalescing slot — a
        # stuck key would make dispatch_push drop every future push for this
        # production until the server restarts. discard is idempotent.
        with _guard:
            _pending.discard(pending_key)


def dispatch_push(
    settings: Settings,
    team: TeamSettings,
    locks: LockManager,
    payload: dict[str, Any],
) -> list[str]:
    """Start background deploys for a verified push; returns queued names."""
    queued = []
    for name in match_productions(team, payload):
        pending_key = f"{team.team_id}/{name}"
        with _guard:
            if pending_key in _pending:
                # One deploy is already waiting; it will pull this push too.
                continue
            _pending.add(pending_key)
        thread = threading.Thread(
            target=_deploy_in_background,
            args=(settings, team, locks, name),
            name=f"oduflow-webhook-{team.team_id}-{name}",
            daemon=True,
        )
        thread.start()
        queued.append(name)
    return queued


def handle_github_event(
    settings: Settings,
    locks: LockManager,
    *,
    event: str,
    body: bytes,
    signature_header: str,
) -> tuple[int, dict[str, Any]]:
    """Process a GitHub webhook request; returns (http_status, json_body)."""
    if not settings.prod_enabled:
        return 404, {"ok": False, "error": "production hosting disabled"}
    team = resolve_team(settings, body, signature_header)
    if team is None:
        return 401, {"ok": False, "error": "invalid signature"}

    if event == "ping":
        return 200, {"ok": True, "pong": True}
    if event != "push":
        return 200, {"ok": True, "ignored": event}

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "malformed payload"}

    queued = dispatch_push(settings, team, locks, payload)
    return 202, {"ok": True, "queued": queued}
