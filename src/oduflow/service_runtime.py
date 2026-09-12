"""Explicit Docker lifecycle settings, preserved across service replacement."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ServiceRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

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
    import json

    if "oduflow.runtime" not in container.labels:
        return {}
    return normalize_runtime(json.loads(container.labels.get("oduflow.runtime", "{}")))
