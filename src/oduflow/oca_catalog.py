"""Public OCA catalogue, shared across teams without credentials or team state."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from typing import Any, TypedDict

import httpx

from oduflow.errors import BusyError, PrerequisiteNotMetError
from oduflow.webhooks import normalize_repo_url

logger = logging.getLogger("oduflow")


class OcaRepository(TypedDict):
    name: str
    extra_name: str
    repo_url: str
    description: str
    stars: int
    archived: bool


class OcaCatalogue(TypedDict):
    repos: list[OcaRepository]
    # True when GitHub could not be reached and an expired copy is served.
    stale: bool


_URL = "https://api.github.com/orgs/OCA/repos"
_PER_PAGE = 100
_TTL = 15 * 60
# A failed refresh is remembered briefly, so an outage or rate limit does not
# turn every dialog open into another request against the shared quota.
_ERROR_TTL = 60
# OCA has ~300 repositories (3 pages); the caps only stop a broken reply.
_MAX_PAGES = 10
_MAX_PAGE_BYTES = 4 * 1024 * 1024
_REQUEST_TIMEOUT = 15
_DEADLINE = 45
# Matches the extra repo name limit in extra_addons.
_NAME_MAX = 63
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+")

# Held for a whole refresh: concurrent callers wait for its outcome instead of
# starting their own GitHub request.
_lock = threading.Lock()
_cached: list[OcaRepository] = []
_expires = 0.0
_error = ""
_error_until = 0.0


def list_oca_repositories() -> OcaCatalogue:
    """Return the catalogue, refreshing it at most once per TTL.

    Only fixed GitHub endpoints are requested. Branch discovery uses the existing
    git ls-remote API, avoiding one GitHub API request per selected repository.
    """
    global _cached, _expires, _error, _error_until
    if not _lock.acquire(timeout=_DEADLINE):
        return _stale_or_raise(
            BusyError("The OCA catalogue is still loading. Try again shortly.")
        )
    try:
        now = time.monotonic()
        if now < _expires:
            return _copy(stale=False)
        if now < _error_until:
            return _stale_or_raise(PrerequisiteNotMetError(_error))
        try:
            repos = _fetch()
        except PrerequisiteNotMetError as exc:
            _error = str(exc)
            _error_until = time.monotonic() + _ERROR_TTL
            logger.warning("OCA catalogue refresh failed: %s", exc)
            return _stale_or_raise(exc)
        _cached = repos
        _expires = time.monotonic() + _TTL
        _error, _error_until = "", 0.0
        return _copy(stale=False)
    finally:
        _lock.release()


def _copy(stale: bool) -> OcaCatalogue:
    return {"repos": [repo.copy() for repo in _cached], "stale": stale}


def _stale_or_raise(exc: Exception) -> OcaCatalogue:
    if _cached:
        return _copy(stale=True)
    raise exc


def _fetch() -> list[OcaRepository]:
    """Read every catalogue page; any failure discards the partial result."""
    deadline = time.monotonic() + _DEADLINE
    found: dict[str, dict[str, Any]] = {}
    try:
        with httpx.Client(
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=False,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Oduflow-OCA-Import",
            },
        ) as client:
            for page in range(1, _MAX_PAGES + 1):
                items = _read_page(client, page, deadline)
                for item in items:
                    repo = _parse_item(item)
                    if repo:
                        found[repo["name"]] = repo
                if len(items) < _PER_PAGE:
                    break
            else:
                raise ValueError("Catalogue has more pages than expected")
    except httpx.HTTPError as exc:
        raise PrerequisiteNotMetError(
            "Could not load the OCA catalogue from GitHub. Try again shortly."
        ) from exc
    except ValueError as exc:
        raise PrerequisiteNotMetError(
            "GitHub returned an invalid OCA catalogue. Try again shortly."
        ) from exc
    if not found:
        raise PrerequisiteNotMetError(
            "GitHub returned an empty OCA catalogue. Try again shortly."
        )
    return _assign_import_names(found.values())


def _read_page(client: httpx.Client, page: int, deadline: float) -> list[Any]:
    with client.stream(
        "GET",
        _URL,
        params={"per_page": _PER_PAGE, "page": page, "sort": "full_name"},
    ) as response:
        if response.status_code in (403, 429):
            raise PrerequisiteNotMetError(
                "GitHub has limited catalogue requests. Try again later."
            )
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > _MAX_PAGE_BYTES:
                raise ValueError("Catalogue page is too large")
            if time.monotonic() > deadline:
                raise PrerequisiteNotMetError(
                    "GitHub is answering too slowly. Try again shortly."
                )
    items = json.loads(body)
    if not isinstance(items, list):
        raise ValueError("Expected a repository list")
    return items


def _parse_item(item: object) -> dict[str, Any] | None:
    """Treat remote metadata as untrusted: skip malformed entries, never accept
    clone URLs from it, nor unsafe names as path components."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    owner = item.get("owner")
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        return None
    if not isinstance(owner, dict) or str(owner.get("login") or "").lower() != "oca":
        return None
    try:
        stars = max(int(item.get("stargazers_count") or 0), 0)
    except (TypeError, ValueError):
        stars = 0
    return {
        "name": name,
        "repo_url": f"https://github.com/OCA/{name}.git",
        "description": str(item.get("description") or ""),
        "stars": stars,
        "archived": bool(item.get("archived")),
    }


def _assign_import_names(found: Iterable[dict[str, Any]]) -> list[OcaRepository]:
    """Give every entry a distinct extra repo name.

    Names that need no sanitising or truncation keep the plain ``oca-<name>``;
    any other entry colliding with a taken name gets a numeric suffix, so
    ``foo-bar`` stays ``oca-foo-bar`` and ``foo.bar`` becomes ``oca-foo-bar-2``.
    """
    entries = list(found)

    def stem(name: str) -> str:
        return "oca-" + re.sub(r"[^A-Za-z0-9_-]", "-", name)

    def exact_first(entry: dict[str, Any]) -> tuple[bool, str]:
        plain = stem(entry["name"])
        return (
            plain != "oca-" + entry["name"] or len(plain) > _NAME_MAX,
            entry["name"],
        )

    taken: set[str] = set()
    names: dict[str, str] = {}
    for entry in sorted(entries, key=exact_first):
        base = stem(entry["name"])[:_NAME_MAX]
        candidate, number = base, 1
        while candidate in taken:
            number += 1
            suffix = f"-{number}"
            candidate = base[: _NAME_MAX - len(suffix)] + suffix
        taken.add(candidate)
        names[entry["name"]] = candidate
    return [
        {
            "name": entry["name"],
            "extra_name": names[entry["name"]],
            "repo_url": entry["repo_url"],
            "description": entry["description"],
            "stars": entry["stars"],
            "archived": entry["archived"],
        }
        for entry in entries
    ]


def _origin_key(url: str) -> str:
    # GitHub paths are case-insensitive, and www.github.com redirects.
    key = normalize_repo_url(url).lower()
    return key.removeprefix("www.")


def annotate_existing(
    repos: list[OcaRepository], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mark rows the team already has (same origin in any URL form) and rows
    whose import name is occupied by an unrelated repository."""
    origins: dict[str, str] = {}
    for current in existing:
        key = _origin_key(str(current.get("repo_url") or ""))
        if key:
            origins.setdefault(key, str(current.get("name") or ""))
    names = {current.get("name") for current in existing}
    rows: list[dict[str, Any]] = []
    for repo in repos:
        existing_as = origins.get(_origin_key(repo["repo_url"]), "")
        rows.append(
            {
                **repo,
                "existing_as": existing_as,
                "name_taken": not existing_as and repo["extra_name"] in names,
            }
        )
    return rows
