"""Load, validate, hash, and resolve declarative Stack manifests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from oduflow.stack_models import EnvValue, StackManifest, ValueFrom

_MAX_MANIFEST_BYTES = 1_000_000
_MAX_FILE_BYTES = 1_000_000


class StackValidationError(ValueError):
    """A manifest is malformed, invalid, or references unsafe local input."""


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


def _construct_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise StackValidationError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load_stack(path: str | os.PathLike[str]) -> StackManifest:
    """Load one Stack YAML file with duplicate-key and typed validation."""
    manifest_path = Path(path).expanduser().resolve()
    try:
        size = manifest_path.stat().st_size
    except OSError as exc:
        raise StackValidationError(f"cannot read stack manifest: {exc}") from exc
    if size > _MAX_MANIFEST_BYTES:
        raise StackValidationError(
            f"stack manifest exceeds {_MAX_MANIFEST_BYTES} byte limit"
        )
    try:
        raw = yaml.load(manifest_path.read_text(encoding="utf-8"), _UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, StackValidationError) as exc:
        if isinstance(exc, StackValidationError):
            raise
        raise StackValidationError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise StackValidationError("stack manifest root must be a mapping")
    try:
        return StackManifest.model_validate(raw)
    except ValidationError as exc:
        raise StackValidationError(str(exc)) from exc


def manifest_hash(manifest: StackManifest) -> str:
    """Stable hash of declarative input; contains references, never resolved secrets."""
    canonical = json.dumps(
        manifest.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def resolve_env_values(
    values: Mapping[str, EnvValue],
    *,
    settings: Any = None,
    team: Any = None,
    env_name: str = "",
    production_name: str = "",
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve literal, host-env, Odoo, and database values at apply time."""
    source_env = os.environ if environ is None else environ
    result: dict[str, str] = {}
    # One lookup per database, not per variable: a full PG* set is six keys
    # pointing at the same resource, and each lookup costs several round trips
    # into the PostgreSQL container.
    databases: dict[str, dict[str, Any]] = {}
    for key, raw in values.items():
        if isinstance(raw, str):
            result[key] = raw
            continue
        if not isinstance(raw, ValueFrom):  # defensive for callers constructing data
            raise StackValidationError(f"unsupported value source for '{key}'")
        if raw.from_env is not None:
            if raw.from_env not in source_env:
                raise StackValidationError(
                    f"environment variable '{raw.from_env}' required by '{key}' is not set"
                )
            result[key] = source_env[raw.from_env]
            continue
        if raw.database is not None:
            if settings is None or team is None:
                raise StackValidationError(
                    f"'{key}' requires managed database '{raw.database}'"
                )
            from oduflow.docker_ops import service_database_ops

            if raw.database not in databases:
                databases[raw.database] = service_database_ops.get_database(
                    settings, team, raw.database, reveal_password=True
                )
            database = databases[raw.database]
            if database.get("status") != "ready":
                raise StackValidationError(
                    f"managed database '{raw.database}' required by '{key}' is not ready"
                )
            field = raw.database_field or ""
            result[key] = str(database[field])
            continue
        if raw.production_field is not None:
            if settings is None or team is None or not production_name:
                raise StackValidationError(f"'{key}' requires an existing production")
            from oduflow import production_registry
            from oduflow.docker_ops import production_ops

            record = production_registry.get_production(team, production_name)
            if raw.production_field == "url":
                result[key] = production_ops.prod_url(settings, team, record)
            elif raw.production_field == "database":
                result[key] = production_ops.prod_db_name(team, production_name)
            else:
                result[key] = production_ops._odoo_container_name(
                    settings, team, production_name
                )
            continue
        if settings is None or team is None or not env_name:
            raise StackValidationError(f"'{key}' requires an existing Odoo environment")
        from oduflow.docker_ops import env_ops

        if raw.environment_field == "url":
            result[key] = env_ops.get_env_base_url(settings, team, env_name)[0]
        elif raw.environment_field == "token":
            token = env_ops.get_env_token(settings, team, env_name)
            if not token:
                raise StackValidationError(
                    f"environment '{env_name}' has no scoped MCP token"
                )
            result[key] = token
    return result


def read_stack_file(manifest_path: str | os.PathLike[str], source: str) -> str:
    """Read a UTF-8 source contained inside the manifest directory."""
    base = Path(manifest_path).expanduser().resolve().parent
    candidate = (base / source).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise StackValidationError(
            f"file source '{source}' escapes the stack directory"
        ) from exc
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise StackValidationError(
            f"cannot read file source '{source}': {exc}"
        ) from exc
    if not candidate.is_file():
        raise StackValidationError(f"file source '{source}' is not a regular file")
    if size > _MAX_FILE_BYTES:
        raise StackValidationError(
            f"file source '{source}' exceeds {_MAX_FILE_BYTES} byte limit"
        )
    try:
        return candidate.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise StackValidationError(
            f"file source '{source}' is not UTF-8 text; V1 supports text files only"
        ) from exc


def validate_stack_files(
    manifest: StackManifest, manifest_path: str | os.PathLike[str]
) -> None:
    """Validate every local file referenced by a manifest without mutating state."""
    for item in manifest.spec.files:
        read_stack_file(manifest_path, item.source)


def write_json_schema(path: str | os.PathLike[str]) -> None:
    """Write the public v1alpha1 JSON Schema generated from typed models."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(StackManifest.model_json_schema(by_alias=True), indent=2) + "\n",
        encoding="utf-8",
    )
