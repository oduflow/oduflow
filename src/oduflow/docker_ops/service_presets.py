"""Manage service preset configurations (save / restore / list / delete).

Presets are persisted as a single JSON file at ``{team.data_dir}/service_presets.json``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from oduflow.errors import NotFoundError
from oduflow.settings import TeamSettings

logger = logging.getLogger("oduflow")


def _presets_path(team: TeamSettings) -> str:
    """Return the absolute path to the presets JSON file."""
    return os.path.join(team.data_dir, "service_presets.json")


def _load_presets(team: TeamSettings) -> dict[str, Any]:
    """Read and return all presets from disk.

    Returns an empty dict when the file does not exist or contains invalid JSON.
    """
    path = _presets_path(team)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
            return data
    except json.JSONDecodeError:
        logger.warning("Corrupt presets file at %s – returning empty dict", path)
        return {}


def _save_presets(team: TeamSettings, data: dict[str, Any]) -> None:
    """Persist *data* to the presets JSON file, creating parent dirs if needed.

    Writes atomically (temp file + os.replace) so a crash mid-write cannot leave
    a truncated file that _load_presets would discard as corrupt.
    """
    path = _presets_path(team)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # Presets carry service env vars, which historically include API keys, so
    # they get the same owner-only mode as the credential stores (migration
    # 0006 brings pre-existing files forward).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_preset(
    team: TeamSettings,
    name: str,
    image: str,
    port: int | None,
    hostname: str | None = None,
    env_vars: dict[str, str] | None = None,
    base_hostname: str = "",
    host_mode: bool = False,
    volumes: list[dict[str, str]] | None = None,
    cap_add: list[str] | None = None,
    privileged: bool = False,
    routes: list[dict[str, object]] | None = None,
    command: list[str] | None = None,
) -> dict[str, Any]:
    """Save (or overwrite) a single service preset and return it."""
    short_hostname = hostname or ""
    if short_hostname and base_hostname:
        suffix = f".{base_hostname}"
        if short_hostname.endswith(suffix):
            short_hostname = short_hostname[: -len(suffix)]
    preset: dict[str, Any] = {
        "image": image,
        "port": port or 0,
        "hostname": short_hostname,
        "env_vars": env_vars if env_vars is not None else {},
    }
    if host_mode:
        preset["host_mode"] = True
    if volumes:
        preset["volumes"] = volumes
    if cap_add:
        preset["cap_add"] = list(cap_add)
    if privileged:
        preset["privileged"] = True
    if routes:
        preset["routes"] = routes
    if command:
        preset["command"] = list(command)
    data = _load_presets(team)
    data[name] = preset
    _save_presets(team, data)
    return preset


def list_presets(team: TeamSettings) -> list[dict[str, Any]]:
    """Return all presets as a sorted list of dicts (each includes a ``name`` key)."""
    data = _load_presets(team)
    return sorted(
        [{"name": name, **preset} for name, preset in data.items()],
        key=lambda p: p["name"],
    )


def get_preset(team: TeamSettings, name: str) -> dict[str, Any]:
    """Return a single preset dict (with ``name`` key) or raise :class:`NotFoundError`."""
    data = _load_presets(team)
    if name not in data:
        raise NotFoundError(f"Service preset '{name}' not found")
    return {"name": name, **data[name]}


def delete_preset(team: TeamSettings, name: str) -> dict[str, Any]:
    """Remove a preset from disk and return ``{"name": name}``.

    Raises :class:`NotFoundError` if the preset does not exist.
    """
    data = _load_presets(team)
    if name not in data:
        raise NotFoundError(f"Service preset '{name}' not found")
    del data[name]
    _save_presets(team, data)
    return {"name": name}
