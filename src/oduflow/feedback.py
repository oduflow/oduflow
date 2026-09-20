"""Prefilled GitHub issue links for user feedback.

Oduflow never files issues on the user's behalf: it builds a link to the
project's "new issue" form with the description and a short diagnostics block
prefilled, and the user submits it from their own GitHub account. That keeps
the report attributable to a real person, keeps Oduflow free of any GitHub
credentials, and lets the user read and edit everything before it becomes
public.

Two consequences shape this module:

* ``labels=`` is deliberately absent from the query string. GitHub rejects that
  parameter (404) for users without triage rights on the repository, so labels
  live in the issue *form* YAML (``.github/ISSUE_TEMPLATE/``) instead and are
  applied on submission regardless of permissions.
* The diagnostics block carries only non-identifying deployment facts — the
  same bar as the anonymous telemetry in ``telemetry.py``. No hostnames, team
  names, repository URLs, branch or database names ever go into a link.
"""

from __future__ import annotations

import platform
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from oduflow.settings import Settings

REPO = "oduflow/oduflow"
NEW_ISSUE_URL = f"https://github.com/{REPO}/issues/new"
ISSUES_URL = f"https://github.com/{REPO}/issues"

# Issue form templates shipped in .github/ISSUE_TEMPLATE/. Each form defines the
# `details` and `environment` field ids this module prefills, and carries its
# own labels.
KINDS: dict[str, str] = {
    "bug": "bug.yml",
    "feature": "feature.yml",
    "feedback": "feedback.yml",
}
DEFAULT_KIND = "feedback"

# GitHub answers 414 for over-long URLs; browsers and proxies add their own
# limits. Stay well below any of them and truncate the free-form text instead.
MAX_URL_LEN = 4000
MAX_TITLE_LEN = 256
_TRUNCATION_NOTE = "\n\n[truncated — please paste the rest here]"


def oduflow_version() -> str:
    """Return the installed package version, or "dev" from a source checkout."""
    try:
        return version("oduflow")
    except PackageNotFoundError:
        return "dev"


def diagnostics(settings: Settings | None = None) -> dict[str, str]:
    """Collect the non-identifying deployment facts worth having in a report.

    Everything here describes *how Oduflow is running*, never *what it runs*:
    no hostnames, team names, repo URLs, branch or database names.
    """
    from oduflow import settings as settings_module

    info = {
        "Oduflow": oduflow_version(),
        "Python": platform.python_version(),
        "Platform": f"{platform.system()} {platform.machine()}",
        "Transport": settings_module.TRANSPORT,
    }
    if settings is not None:
        info["Routing"] = settings.routing_mode
        info["Production"] = "enabled" if settings.prod_enabled else "disabled"
    return info


def format_diagnostics(info: dict[str, str]) -> str:
    """Render diagnostics as the plain ``key: value`` block the forms expect."""
    return "\n".join(f"{key}: {value}" for key, value in info.items())


def build_issue_url(
    kind: str = DEFAULT_KIND,
    title: str = "",
    details: str = "",
    settings: Settings | None = None,
    environment: str = "",
) -> str:
    """Build a prefilled "new issue" URL for oduflow/oduflow.

    ``kind`` selects the issue form (and with it the labels applied on
    submission). ``environment`` overrides the auto-collected diagnostics block;
    pass an empty string to collect it here.
    """
    template = KINDS.get(kind, KINDS[DEFAULT_KIND])
    if not environment:
        environment = format_diagnostics(diagnostics(settings))

    params = {"template": template, "environment": environment}
    title = title.strip()
    if title:
        params["title"] = title[:MAX_TITLE_LEN].rstrip()
    params["details"] = details.strip()

    # Percent-encoding can triple a character, so shrink the free-form text by
    # measuring the encoded URL instead of trusting the character count.
    keep = len(params["details"])
    while len(_encode(params)) > MAX_URL_LEN and keep > 0:
        keep = int(keep * 0.8)
        params["details"] = (
            details.strip()[:keep].rstrip() + _TRUNCATION_NOTE if keep else ""
        )
    return _encode(params)


def _encode(params: dict[str, str]) -> str:
    # quote_via=quote keeps spaces as %20 rather than "+", which GitHub renders
    # literally in a prefilled textarea.
    return f"{NEW_ISSUE_URL}?{urlencode(params, quote_via=quote)}"


def report_issue_message(url: str, kind: str) -> str:
    """Human-facing instructions returned by the MCP tool / CLI."""
    return (
        f"Open this link to file a {kind} report on {REPO} from your own GitHub\n"
        f"account. The form is prefilled — review and edit it before submitting;\n"
        f"nothing is sent until you press 'Create'.\n\n"
        f"{url}\n"
    )
