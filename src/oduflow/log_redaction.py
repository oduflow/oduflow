"""Keeping credentials out of the server log.

Two places write caller-visible data to the log on every request, and both can
carry a live credential:

- ``handle_errors`` logs a preview of each MCP tool result. Some results *are*
  credentials: a minted Odoo ``session_id``, a freshly set password, a service
  database's ``DATABASE_URL``, a one-time sign-in link.
- uvicorn's access log records the request line including the query string,
  which is where the one-time links carry their token
  (``/oduflow-connect?token=…``, ``/oduflow-artifact?token=…``) and a share link
  carries its key (``/env/<name>?key=…``).

One mechanism covers each:

- A tool wraps every credential it puts in its result in :func:`secret`. The
  value is returned unchanged for the caller and remembered for the duration of
  the call; :func:`scrub` then masks it in anything ``handle_errors`` logs. A
  tool that hands out a new kind of credential only has to mark it.
- :class:`AccessLogQueryFilter` masks the values of credential-bearing query
  parameters in access-log records, whatever the path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

REDACTED = "[redacted]"

# The credentials marked during the current tool call. None outside a call, so
# secret() is a harmless no-op for code paths that never reach handle_errors
# (the CLI helpers, tests calling an inner function directly).
_marked: ContextVar[list[str] | None] = ContextVar("oduflow_log_secrets", default=None)


@contextmanager
def collecting() -> Iterator[None]:
    """Scope within which :func:`secret` values are remembered for :func:`scrub`."""
    token = _marked.set([])
    try:
        yield
    finally:
        _marked.reset(token)


def secret(value: str) -> str:
    """Mark *value* as a credential and return it unchanged.

    Use it at the point a credential is placed in a tool result:
    ``f"Password: {secret(password)}"``.
    """
    marked = _marked.get()
    if marked is not None and value:
        marked.append(str(value))
    return value


def scrub(text: str) -> str:
    """Return *text* with every credential marked so far replaced."""
    marked = _marked.get()
    if not marked:
        return text
    # Longest first: a URL that embeds a password must go as a whole before the
    # password alone is replaced inside it.
    for value in sorted(set(marked), key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


# Query parameters that carry a credential on some Oduflow route: one-time
# link tokens, the share-link key, and the conventional OAuth bearer name.
_SENSITIVE_QUERY_PARAMS = ("token", "key", "access_token")
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:" + "|".join(_SENSITIVE_QUERY_PARAMS) + r")=)[^&#\s\"']+",
    re.IGNORECASE,
)

_ACCESS_LOGGER_NAME = "uvicorn.access"


def redact_query(text: str) -> str:
    """Mask the values of credential-bearing query parameters in *text*."""
    return _QUERY_SECRET_RE.sub(r"\1" + REDACTED, text)


class AccessLogQueryFilter(logging.Filter):
    """Mask credential query parameters in uvicorn access-log records.

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with the path and query string as
    one argument. The arguments are rewritten in place, before any handler
    formats them, so every handler and formatter sees the masked request line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_query(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = redact_query(record.msg)
        return True


def install_access_log_filter() -> None:
    """Attach :class:`AccessLogQueryFilter` to uvicorn's access logger (idempotent).

    Safe to call before uvicorn configures logging: its ``dictConfig`` replaces
    a logger's handlers but leaves the filters attached to it.
    """
    access_logger = logging.getLogger(_ACCESS_LOGGER_NAME)
    if any(isinstance(f, AccessLogQueryFilter) for f in access_logger.filters):
        return
    access_logger.addFilter(AccessLogQueryFilter())
