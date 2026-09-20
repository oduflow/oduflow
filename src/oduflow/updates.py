"""Manual release check against the project's GitHub Releases.

Nothing here runs on its own. The dashboard calls it only when someone clicks
the version in the header, so this is an explicit, user-initiated lookup — not
telemetry, not a background poll, and not something an idle browser tab ever
triggers. One click is one request; no result is kept between clicks.

Only the public, unauthenticated Releases endpoint is used, and the reply is
read for four facts: the tag, the release title, its publication date, and the
release page URL. Nothing about the installation is sent — a GET carries no
payload and the request goes out with the package's own User-Agent.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

from oduflow import feedback

REPO = "oduflow/oduflow"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{REPO}/releases"
TIMEOUT = 8
# A release payload is a few kilobytes; this only caps a hostile or broken reply.
MAX_BODY_BYTES = 512 * 1024

# Status values the dashboard renders as text (never as color alone).
STATUS_CURRENT = "up-to-date"
STATUS_UPDATE = "update-available"
STATUS_UNKNOWN = "unknown"
STATUS_ERROR = "error"


@dataclass
class UpdateCheck:
    """Outcome of one release check, as shown in the version dialog."""

    current: str
    status: str
    latest: str = ""
    release_title: str = ""
    published_at: str = ""
    release_url: str = RELEASES_PAGE_URL
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Release:
    tag: str = ""
    title: str = ""
    published_at: str = ""
    url: str = RELEASES_PAGE_URL


def _fetch_latest_release() -> _Release:
    """Read the latest published release. Raises URLError/HTTPError/ValueError."""
    request = Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"oduflow/{feedback.oduflow_version()}",
        },
        method="GET",
    )
    with urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read(MAX_BODY_BYTES).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Unexpected reply from GitHub.")
    return _Release(
        tag=str(payload.get("tag_name") or "").strip(),
        title=str(payload.get("name") or "").strip(),
        published_at=str(payload.get("published_at") or "").strip(),
        url=str(payload.get("html_url") or "").strip() or RELEASES_PAGE_URL,
    )


def _http_error_message(error: HTTPError) -> str:
    if error.code in (403, 429):
        # Unauthenticated GitHub API calls are limited per source IP.
        return "GitHub rate limit reached — try again later."
    if error.code == 404:
        return "No published release found."
    return f"GitHub returned HTTP {error.code}."


def check_for_update(current: str | None = None) -> UpdateCheck:
    """Compare the installed version with the latest published release.

    ``current`` defaults to the installed package version. A source checkout
    reports ``dev``, which is not a release version: such a build is reported
    as ``unknown`` with the latest release still shown, because "older" and
    "newer" are both wrong answers for unreleased code.
    """
    installed = current or feedback.oduflow_version()
    try:
        release = _fetch_latest_release()
    except HTTPError as error:
        return UpdateCheck(
            current=installed, status=STATUS_ERROR, error=_http_error_message(error)
        )
    except URLError as error:
        return UpdateCheck(
            current=installed,
            status=STATUS_ERROR,
            error=f"Could not reach github.com: {error.reason}.",
        )
    except (OSError, ValueError):
        return UpdateCheck(
            current=installed,
            status=STATUS_ERROR,
            error="Could not read the release information from GitHub.",
        )

    latest = release.tag[1:] if release.tag[:1] == "v" else release.tag
    result = UpdateCheck(
        current=installed,
        status=STATUS_UNKNOWN,
        latest=latest,
        release_title=release.title,
        published_at=release.published_at,
        release_url=release.url,
    )
    if not latest:
        result.status = STATUS_ERROR
        result.error = "GitHub reported no release tag."
        return result
    try:
        result.status = (
            STATUS_UPDATE if Version(latest) > Version(installed) else STATUS_CURRENT
        )
    except InvalidVersion:
        result.status = STATUS_UNKNOWN
    return result
