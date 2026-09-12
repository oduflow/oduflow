"""Durable credentials for PostgreSQL databases used by auxiliary services."""

from __future__ import annotations

import json
import os
from typing import Any

from oduflow.errors import ConflictError, NotFoundError, PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import validate_service_database_name
from oduflow.settings import TeamSettings

_VERSION = 1
_REQUIRED_STRING_FIELDS = {
    "name",
    "database",
    "username",
    "password",
    "created_at",
}


def credentials_dir(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, "service_databases")


def credentials_path(team: TeamSettings, name: str) -> str:
    validate_service_database_name(name)
    return os.path.join(credentials_dir(team), f"{name}.json")


def exists(team: TeamSettings, name: str) -> bool:
    return os.path.isfile(credentials_path(team, name))


def save(
    team: TeamSettings,
    name: str,
    record: dict[str, Any],
    *,
    overwrite: bool = False,
) -> None:
    """Atomically persist one credential record with owner-only permissions."""
    path = credentials_path(team, name)
    if not overwrite and os.path.exists(path):
        raise ConflictError(f"Service database '{name}' already exists.")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    payload = {"version": _VERSION, **record}
    atomic_write_private_json(path, payload)


def load(team: TeamSettings, name: str) -> dict[str, Any]:
    """Load and strictly validate one credential record."""
    path = credentials_path(team, name)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise NotFoundError(f"Service database '{name}' not found.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PrerequisiteNotMetError(
            f"Credentials for service database '{name}' cannot be read safely."
        ) from exc

    if not isinstance(data, dict) or data.get("version") != _VERSION:
        raise PrerequisiteNotMetError(
            f"Credentials for service database '{name}' have an unsupported format."
        )
    if any(
        not isinstance(data.get(key), str) or not data[key]
        for key in _REQUIRED_STRING_FIELDS
    ):
        raise PrerequisiteNotMetError(
            f"Credentials for service database '{name}' are incomplete."
        )
    if data["name"] != name:
        raise PrerequisiteNotMetError(
            f"Credentials for service database '{name}' do not match their filename."
        )
    return data


def list_names(team: TeamSettings) -> list[str]:
    root = credentials_dir(team)
    if not os.path.isdir(root):
        return []
    names: list[str] = []
    for entry in os.listdir(root):
        if not entry.endswith(".json"):
            continue
        name = entry[:-5]
        try:
            validate_service_database_name(name)
        except ValueError:
            continue
        names.append(name)
    return sorted(names)


def delete(team: TeamSettings, name: str) -> None:
    path = credentials_path(team, name)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
