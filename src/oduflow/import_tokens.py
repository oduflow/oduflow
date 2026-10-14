"""Short-lived, file-backed tokens for pushing an Odoo.sh backup into a template.

The dashboard mints a token (15 min TTL) bound to a target template name; the
`import-odoo.sh` client running in the Odoo.sh shell presents that token on every
ingest call. Because the token expires quickly, a copy left in the terminal
scrollback is useless afterwards.

Each token is one JSON file under ``<team.data_dir>/import_tokens/<token>.json``.
The token carries auth, the target template, the selected addon error policy and
whether the filestore is skipped (``without_filestore``); it deliberately does NOT store upload progress. Resume is instead derived from
what is actually staged on disk in the template directory (see ``web_ui``), so a
re-run — even with a freshly minted token after the previous one expired
mid-upload — continues where it left off instead of restarting.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time

from oduflow.errors import NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

# token_urlsafe(24) yields 32 url-safe chars; accept that shape only so a token
# taken from the URL/header can never be a path-traversal segment.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_DEFAULT_TTL_SECONDS = 15 * 60
_lock = threading.Lock()

ADDON_ERROR_POLICY_STRICT = "strict"
ADDON_ERROR_POLICY_BEST_EFFORT = "best_effort"
ADDON_ERROR_POLICIES = frozenset(
    {ADDON_ERROR_POLICY_STRICT, ADDON_ERROR_POLICY_BEST_EFFORT}
)


def _tokens_dir(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, "import_tokens")


def _token_path(team: TeamSettings, token: str) -> str:
    return os.path.join(_tokens_dir(team), f"{token}.json")


def _write(team: TeamSettings, record: dict[str, object]) -> None:
    path = _token_path(team, str(record["token"]))
    tmp = f"{path}.tmp"
    with _lock:
        os.makedirs(_tokens_dir(team), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, path)


def _remove(team: TeamSettings, token: str) -> None:
    try:
        os.remove(_token_path(team, token))
    except OSError:
        pass


def _cleanup_expired(team: TeamSettings, *, now: float) -> None:
    directory = _tokens_dir(team)
    if not os.path.isdir(directory):
        return
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path) as f:
                record = json.load(f)
            expired = float(record.get("expires_at", 0)) < now
        except (OSError, ValueError):
            expired = True  # unreadable/garbage token file — drop it
        if expired:
            try:
                os.remove(path)
            except OSError:
                pass


def create_token(
    team: TeamSettings,
    template_name: str,
    *,
    addon_error_policy: str = ADDON_ERROR_POLICY_STRICT,
    without_filestore: bool = False,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> dict[str, object]:
    """Mint a token bound to ``template_name`` and persist it. Also reaps any
    expired tokens for this team so the directory does not grow unbounded."""
    from oduflow.naming import validate_template_name

    validate_template_name(template_name)
    if addon_error_policy not in ADDON_ERROR_POLICIES:
        raise ValueError("addon_error_policy must be 'strict' or 'best_effort'.")
    now = time.time() if now is None else now
    _cleanup_expired(team, now=now)
    record = {
        "token": secrets.token_urlsafe(24),
        "team_id": team.team_id,
        "template_name": template_name,
        "addon_error_policy": addon_error_policy,
        "without_filestore": bool(without_filestore),
        "created_at": now,
        "expires_at": now + ttl_seconds,
    }
    _write(team, record)
    return record


def load_token(
    settings: Settings, token: str, *, now: float | None = None
) -> tuple[TeamSettings, dict[str, object]]:
    """Resolve a token to ``(team, record)``, searching across all teams.

    Raises NotFoundError for an unknown/malformed token and
    PrerequisiteNotMetError for an expired one (which is also deleted).
    """
    if not token or not _TOKEN_RE.match(token):
        raise NotFoundError("Invalid or unknown import token.")
    now = time.time() if now is None else now
    for team in settings.teams.values():
        path = _token_path(team, token)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                record = json.load(f)
        except (OSError, ValueError):
            raise NotFoundError("Invalid or unknown import token.")
        if float(record.get("expires_at", 0)) < now:
            _remove(team, token)
            raise PrerequisiteNotMetError(
                "Import token has expired. Generate a new one from the dashboard."
            )
        return team, record
    raise NotFoundError("Invalid or unknown import token.")


def invalidate(team: TeamSettings, token: str) -> None:
    """Delete a token (used once the import is finalized)."""
    _remove(team, token)
