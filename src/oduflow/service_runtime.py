"""Explicit Docker lifecycle settings, preserved across service replacement."""

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger("oduflow")

RUNTIME_LABEL = "oduflow.runtime"


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ServiceRuntime(BaseModel):
    # camelCase aliases keep Stack manifests on the manifest-wide convention
    # (stopSignal/stopTimeout) while MCP/REST keep passing snake_case.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        strict=True,
    )

    tmpfs: dict[str, str] = Field(default_factory=dict)
    cgroupns: Literal["private"] | None = None
    stop_signal: Literal["SIGTERM", "SIGRTMIN+3"] | None = None
    stop_timeout: int | None = Field(default=None, ge=1, le=3600)

    @field_validator("tmpfs")
    @classmethod
    def temporary_mounts(cls, value: dict[str, str]) -> dict[str, str]:
        for path, options in value.items():
            if path not in {"/run", "/run/lock", "/tmp"}:
                raise ValueError("Runtime tmpfs is limited to /run, /run/lock and /tmp")
            if any(char in options for char in "\n\r\x00"):
                raise ValueError("Invalid tmpfs options")
        return value


def normalize_runtime(value: dict[str, Any] | None) -> dict[str, Any]:
    parsed = ServiceRuntime.model_validate(value or {})
    result = parsed.model_dump(exclude_none=True)
    if not result["tmpfs"]:
        result.pop("tmpfs")
    return result


def inspect_runtime(container: Any) -> dict[str, Any]:
    """Read only settings explicitly managed by Oduflow, ignoring image defaults."""
    raw = container.labels.get(RUNTIME_LABEL)
    if not raw:
        return {}
    try:
        return normalize_runtime(json.loads(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        # A label written by another Oduflow version (or edited by hand) must
        # not break describe/stop/delete for this container.
        logger.warning("Ignoring invalid %s label", RUNTIME_LABEL, exc_info=True)
        return {}


def stop_kwargs(container: Any) -> dict[str, Any]:
    """Stop/restart arguments honoring the container's configured stop timeout."""
    timeout = inspect_runtime(container).get("stop_timeout")
    return {"timeout": timeout} if timeout else {}
