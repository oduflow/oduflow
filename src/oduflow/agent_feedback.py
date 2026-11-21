"""Anonymous agent feedback on the Oduflow MCP tool surface.

Off by default and deliberately undocumented: when ``[server] agent_feedback``
is not enabled the ``submit_agent_feedback`` tool is neither listed nor
callable, and nothing is appended to the agent instructions. Operators of an
Oduflow instance turn it on by hand when they want the coding agents working
against that instance to report where the tool surface got in their way.

The channel is independent of :mod:`oduflow.telemetry` — different flag,
different endpoint, different payload — so disabling one never implies the
other.

Anonymity is enforced twice: the injected instructions tell the agent to keep
business context out of the text, and :func:`scrub` mechanically redacts what
slips through anyway (hosts, e-mails, paths, tokens, env/team names).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Collection
from urllib.request import Request, urlopen

from oduflow.errors import FlowError

logger = logging.getLogger("oduflow")

_ENDPOINT = os.environ.get(
    "ODUFLOW_AGENT_FEEDBACK_URL", "https://oduflow.dev/agent-feedback"
)
_TIMEOUT = 5

# Categories the agent may pick. Anything else is rejected at the tool boundary
# so the server side can index on a small closed set.
CATEGORIES = ("friction", "missing_tool", "unclear_error", "docs")

# Hard caps — a feedback note is a paragraph, not a transcript.
MAX_SUGGESTION_CHARS = 2000
MAX_TOOLS = 10
MAX_TOOL_NAME_CHARS = 60

_REDACTED = "[redacted]"

# Ordered: URLs before bare hosts, so a URL is redacted whole rather than
# leaving its scheme behind.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # e-mail
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # URL with scheme (incl. git@host:path style already caught by e-mail rule)
    re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE),
    # absolute POSIX path (/usr/lib/... , /home/max/work/...)
    re.compile(r"(?<![\w.])/(?:[\w.-]+/)+[\w.-]*"),
    # relative POSIX path. Require either an explicit ./ or ../ prefix, at
    # least two separators, or a filename extension so ordinary phrases such
    # as "limit/offset" keep their meaning.
    re.compile(
        r"(?<![\w./-])(?:"
        r"(?:\.{1,2}/)+(?:[\w.-]+/)*[\w.-]+"
        r"|(?:[\w.-]+/){2,}[\w.-]+"
        r"|[\w.-]+/[\w.-]+\.[A-Za-z0-9]{1,10}"
        r")(?![\w./-])"
    ),
    # bare hostname with a TLD-ish tail (example.com, srv.company.io:8069)
    re.compile(r"\b(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?\b", re.IGNORECASE),
    # IPv4, optionally with a port
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"),
    # Windows path
    re.compile(r"\b[A-Za-z]:\\[\\\w.-]+"),
    # Odoo databases provisioned by Oduflow
    re.compile(r"\boduflow_\w+\b"),
    # Long opaque strings — tokens, hashes, secrets. Two tiers on purpose:
    # tool names are what this channel collects, and several are 20+ chars
    # (set_production_backup_schedule is 30). A digit inside a long run marks a
    # secret; snake_case identifiers have none, so they survive up to 31 chars.
    re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}\b|\b[A-Za-z0-9_-]{32,}\b"),
)


def scrub(text: str, known_names: tuple[str, ...] = ()) -> str:
    """Redact anything that could identify the installation or its business.

    ``known_names`` carries instance-specific identifiers (environment names,
    team ids) that look like ordinary words and therefore cannot be matched by
    a generic pattern.
    """
    if not text:
        return ""
    cleaned = text
    for name in sorted({n for n in known_names if len(n) >= 3}, key=len, reverse=True):
        cleaned = re.sub(
            rf"(?<!\w){re.escape(name)}(?!\w)",
            _REDACTED,
            cleaned,
            flags=re.IGNORECASE,
        )
    for pattern in _PATTERNS:
        cleaned = pattern.sub(_REDACTED, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > MAX_SUGGESTION_CHARS:
        cleaned = cleaned[:MAX_SUGGESTION_CHARS].rstrip() + "…"
    return cleaned


def normalize_tools(
    tools: str | list[str] | None,
    allowed_names: Collection[str] | None = None,
) -> list[str]:
    """Parse reported tool names into a bounded, optionally closed-set list."""
    if not tools:
        return []
    raw = tools if isinstance(tools, list) else re.split(r"[,\s]+", tools)
    names: list[str] = []
    for item in raw:
        name = re.sub(r"[^\w]", "", str(item))[:MAX_TOOL_NAME_CHARS]
        if (
            name
            and (allowed_names is None or name in allowed_names)
            and name not in names
        ):
            names.append(name)
    return names[:MAX_TOOLS]


def build_payload(
    *,
    category: str,
    tools: list[str],
    suggestion: str,
    version: str,
    instance_id: str,
) -> dict[str, object]:
    """Assemble the wire payload. Contains nothing beyond these five fields."""
    return {
        "category": category,
        "tools": tools,
        "suggestion": suggestion,
        "version": version,
        "instance_id": instance_id,
    }


def send(payload: dict[str, object]) -> None:
    """POST the payload. Logs the outcome only — never the feedback text."""
    from oduflow.telemetry import SHARED_TOKEN as token

    try:
        headers = {
            "Content-Type": "application/json",
            # A branded UA avoids edge bot-protection rules that block the
            # default ``Python-urllib/x.y`` signature (Cloudflare error 1010).
            "User-Agent": f"oduflow/{payload.get('version') or ''}".rstrip("/"),
        }
        if token:
            headers["X-Oduflow-Telemetry"] = token
        req = Request(
            _ENDPOINT,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=_TIMEOUT) as resp:
            logger.info(
                "Agent feedback sent (category=%s, status=%s)",
                payload.get("category"),
                resp.status,
            )
    except Exception as exc:
        # Do not include the endpoint, response body or submitted text.
        raise FlowError(
            f"Agent feedback delivery failed ({type(exc).__name__})."
        ) from None


# --- Instruction text injected only while the feature is enabled -------------

# One line appended to the MCP server instructions, which every client sees in
# the initialize handshake — agents that never re-read the guide still learn
# about the tool.
MCP_HINT = (
    "When a task is finished, call submit_agent_feedback once if the Oduflow "
    "tools got in your way or something was missing. Keep it anonymous: no "
    "names, paths, hosts, repos or business details."
)

INSTRUCTIONS_SECTION = """
---

## Session Feedback (after the task is done)

When a task reaches its end, call `submit_agent_feedback` **once** — but only
if you actually have something to report about the Oduflow MCP tools
themselves: friction you hit, an error message that did not tell you what went
wrong, a workflow that took more calls than it should, or a tool you wished
existed. Skip the call when the session was uneventful; silence is a valid
result.

Arguments:

- `category` — one of `friction`, `missing_tool`, `unclear_error`, `docs`.
- `tools` — the Oduflow tool names involved, comma-separated (e.g.
  `pull_and_apply, get_environment_logs`).
- `suggestion` — one short English paragraph: what happened and what would
  have helped.

**This report leaves the machine, so it must be anonymous.** Write about the
tools, never about the work. Do not include branch, environment, database,
module, company or people names, repository URLs, hostnames, IP addresses,
file paths, credentials, or excerpts of the user's code and data. Describe the
shape of the problem instead.

- ❌ "pull_and_apply failed for branch feature-invoice-pdf in
  github.com/acme/addons — the invoice_pdf module of ACME broke"
- ✅ "pull_and_apply reported a failed upgrade without naming the module that
  failed; I had to reconstruct it from the traceback"

- ❌ "run_db_query returned 812 rows of customer orders and truncated them"
- ✅ "run_db_query truncates large result sets with no way to page through
  them; a limit/offset argument would remove the guesswork"
""".lstrip()
