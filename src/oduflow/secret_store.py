"""Team-scoped named secrets for environment variables.

Secret values are set by a human operator in the dashboard and are never
returned by any MCP tool or REST endpoint — only the names are listable.
Env vars reference a secret as ``secret:<name>``; the reference is what gets
persisted everywhere a configuration travels (service presets, the
``oduflow.env_vars`` container label, template metadata), and the real value
is substituted only at container-creation time. That way restore/rename/
template flows migrate secrets for free, and an agent reading service or
environment info sees the reference, not the value.

The store is one plaintext JSON file per team at
``{team.data_dir}/secrets.json``, protected the same way as the other
credential files (0600, atomic replace). A process inside a container can
still read its own environment — this guards the MCP/REST read surfaces, not
the container boundary.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any

from oduflow.errors import NotFoundError, PrerequisiteNotMetError
from oduflow.locking import keyed_mutex, team_secrets_lock_key
from oduflow.settings import TeamSettings

_VERSION = 1
SECRET_REF_PREFIX = "secret:"
_SECRET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def secrets_path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, "secrets.json")


def validate_secret_name(name: str) -> str:
    if not name or not _SECRET_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid secret name '{name}': must start with a lowercase letter "
            "or digit and contain only lowercase letters, digits, dots, hyphens "
            "and underscores (max 64 characters)."
        )
    return name


def is_secret_ref(value: object) -> bool:
    """Whether an env-var value is a ``secret:<name>`` reference."""
    return isinstance(value, str) and value.startswith(SECRET_REF_PREFIX)


def secret_ref_name(value: str) -> str:
    """The secret name inside a reference; raises ValueError if malformed."""
    name = value[len(SECRET_REF_PREFIX) :].strip()
    return validate_secret_name(name)


def _load(team: TeamSettings) -> dict[str, Any]:
    path = secrets_path(team)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"version": _VERSION, "secrets": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise PrerequisiteNotMetError(
            "The team secrets store cannot be read safely."
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != _VERSION
        or not isinstance(data.get("secrets"), dict)
    ):
        raise PrerequisiteNotMetError(
            "The team secrets store has an unsupported format."
        )
    return data


def _save(team: TeamSettings, data: dict[str, Any]) -> None:
    path = secrets_path(team)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def list_secrets(team: TeamSettings) -> list[dict[str, str]]:
    """Names and timestamps only — the values never leave this module."""
    records = _load(team)["secrets"]
    return [
        {
            "name": name,
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
        }
        for name, record in sorted(records.items())
    ]


def set_secret(team: TeamSettings, name: str, value: str) -> dict[str, Any]:
    validate_secret_name(name)
    if not isinstance(value, str) or not value:
        raise ValueError("A secret value must be a non-empty string.")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with keyed_mutex(team_secrets_lock_key(team.team_id)):
        data = _load(team)
        existing = data["secrets"].get(name)
        data["secrets"][name] = {
            "value": value,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
        _save(team, data)
    return {"name": name, "created": existing is None}


def delete_secret(team: TeamSettings, name: str) -> None:
    validate_secret_name(name)
    with keyed_mutex(team_secrets_lock_key(team.team_id)):
        data = _load(team)
        if name not in data["secrets"]:
            raise NotFoundError(f"Secret '{name}' not found.")
        del data["secrets"][name]
        _save(team, data)


def resolve_env_secrets(
    team: TeamSettings, env_vars: dict[str, str] | None
) -> dict[str, str] | None:
    """Substitute ``secret:<name>`` references with their stored values.

    Returns a new mapping safe to hand to Docker as the container environment;
    the caller keeps persisting the original (reference-carrying) mapping.
    Raises PrerequisiteNotMetError when a referenced secret does not exist, so
    a dangling reference aborts before any resource is created.
    """
    if not env_vars:
        return env_vars
    refs: dict[str, str] = {}
    for key, value in env_vars.items():
        if is_secret_ref(value):
            try:
                refs[key] = secret_ref_name(value)
            except ValueError as exc:
                raise PrerequisiteNotMetError(
                    f"Environment variable {key} holds a malformed secret "
                    f"reference: {exc}"
                ) from exc
    if not refs:
        return dict(env_vars)
    records = _load(team)["secrets"]
    missing = sorted({name for name in refs.values() if name not in records})
    if missing:
        raise PrerequisiteNotMetError(
            "Undefined secret(s): " + ", ".join(missing) + ". A human operator "
            "must set them in the Oduflow dashboard (Credentials tab, Secrets "
            "section) before this configuration can be applied."
        )
    resolved = dict(env_vars)
    for key, name in refs.items():
        resolved[key] = str(records[name]["value"])
    return resolved
