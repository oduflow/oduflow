"""Production target reconciliation for Stack; registry ownership survives restarts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from oduflow import production_registry, secret_store
from oduflow.docker_ops import production_ops, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.errors import ConflictError, NotFoundError
from oduflow.naming import sanitize_repo_url
from oduflow.settings import Settings, TeamSettings
from oduflow.stack_loader import resolve_env_values
from oduflow.stack_models import StackManifest

ROUTE_DRIFT = "container route"


def spec_hash(manifest: StackManifest) -> str:
    target = manifest.spec.production
    assert target is not None
    exclude = {"adopt_existing"}
    if target.server_mode == production_ops.DEFAULT_SERVER_MODE:
        # The default stays out of the hash, so manifests applied before
        # serverMode existed keep their appliedHash (no spurious recreate).
        exclude.add("server_mode")
    value = target.model_dump(mode="json", by_alias=True, exclude=exclude)
    value["extraRepositories"] = {
        name: repo.branch for name, repo in manifest.spec.extra_repositories.items()
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def desired_record(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    target = manifest.spec.production
    assert target is not None
    env = resolve_env_values(target.env, environ=environ)
    production_ops._production_env_vars(env)
    # Check references before any volume/database/service creation, without logging values.
    secret_store.resolve_env_secrets(team, env)
    return {
        "domain": target.domain,
        "repo_url": sanitize_repo_url(target.repo_url),
        "branch": target.branch,
        "odoo_image": target.odoo_image,
        "git_user": target.git_user,
        "extra_addons": {
            name: repo.branch for name, repo in manifest.spec.extra_repositories.items()
        },
        "env_vars": env,
        "auto_update": target.auto_update,
        "allow_copy_to_dev_mcp": target.allow_copy_to_dev_mcp,
        "server_mode": target.server_mode,
        "odoo_conf": target.odoo_conf,
    }


def runtime_drift(
    settings: Settings, team: TeamSettings, record: dict[str, Any]
) -> list[str]:
    """Read effective container identity/configuration; never return secret values."""
    container = production_ops._get_container(
        get_client(), settings, team, record["name"]
    )
    if container is None:
        return ["container missing"]
    attrs = container.attrs
    config = attrs["Config"]
    labels = config.get("Labels") or {}
    if any(
        labels.get(key) != value
        for key, value in {
            settings.managed_label: "true",
            settings.team_label: team.team_id,
            "oduflow.prod": "true",
            "oduflow.prod_name": record["name"],
        }.items()
    ):
        return ["container ownership"]
    drift = []
    if container.status != "running":
        drift.append("container stopped")
    if config.get("Image") != record["odoo_image"]:
        drift.append("container image")
    for label, field in [
        ("oduflow.domain", "domain"),
        ("oduflow.git_branch", "branch"),
        (settings.repo_label, "repo_url"),
    ]:
        if labels.get(label) != record[field]:
            drift.append("container " + field)
    declared = json.loads(labels.get("oduflow.env_vars", "{}"))
    if json.loads(labels.get("oduflow.extra_addons", "{}")) != record.get(
        "extra_addons", {}
    ):
        drift.append("container extra_addons")
    if labels.get("oduflow.git_user", "") != record.get("git_user", ""):
        drift.append("container git_user")
    if production_ops.routing_drift(settings, team, record["name"], record, labels):
        drift.append(ROUTE_DRIFT)
    if declared != record.get("env_vars", {}):
        drift.append("container env references")
    effective = dict(value.split("=", 1) for value in config.get("Env", []))
    if effective.get("HOST") != settings.prod_db_container:
        drift.append("container database host")
    resolved = secret_store.resolve_env_secrets(team, record.get("env_vars", {})) or {}
    if any(effective.get(key) != value for key, value in resolved.items()):
        drift.append("container env")
    return drift


def _field_drift(record: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    """Desired record fields the registry record does not match. Records
    created before server_mode existed carry no key; the effective mode is
    compared so they show no perpetual drift."""
    current = {**record, "server_mode": production_ops.server_mode_of(record)}
    return [
        key
        for key, value in desired.items()
        if current.get(key, {} if isinstance(value, dict) else None) != value
    ]


def plan_production(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    environ: Mapping[str, str] | None,
) -> list[tuple[str, str]]:
    target = manifest.spec.production
    assert target is not None
    if not settings.prod_enabled:
        return [("conflict", "production.enabled must be true")]
    if settings.routing_mode != "traefik":
        return [("conflict", "production requires Traefik routing")]
    desired = desired_record(settings, team, manifest, environ)
    production_ops._assert_domain_free(
        settings, target.domain, own_team=team.team_id, own_name=target.name
    )
    try:
        record = production_registry.get_production(team, target.name)
    except NotFoundError:
        if target.adopt_existing:
            return [("conflict", "adoptExisting requires an existing production")]
        if target.template is not None:
            templates = system_ops.list_templates(settings, team)
            if not any(
                item["template_name"] == target.template and item.get("db_loaded")
                for item in templates
            ):
                return [("conflict", "production template is not available")]
        return [("create", target.name)]
    ownership = record.get("meta", {}).get("stack")
    if ownership and (
        ownership.get("name") != manifest.metadata.name
        or ownership.get("resource") != "production"
    ):
        return [("conflict", "production is owned by another stack")]
    if not ownership and not target.adopt_existing:
        return [
            (
                "conflict",
                "existing production is not owned by this stack; use adoptExisting explicitly",
            )
        ]
    if record.get("deploy_in_progress"):
        return [("conflict", "production deploy is in progress")]
    fields = _field_drift(record, desired)
    runtime = runtime_drift(settings, team, record)
    if "container ownership" in runtime:
        return [("conflict", "existing container is not this team's production")]
    if not ownership:
        # Outdated Traefik routing alone (a container created before the
        # current routing) is no reason to refuse: apply recreates it.
        blocking = [item for item in runtime if item != ROUTE_DRIFT]
        if fields or blocking or target.template is not None or record.get("unhealthy"):
            return [
                (
                    "conflict",
                    "adoption requires matching configuration and a running production; drift: "
                    + ", ".join(fields + runtime),
                )
            ]
        if runtime:
            return [("adopt", f"{target.name} (recreates container: {ROUTE_DRIFT})")]
        return [("adopt", target.name)]
    immutable = [
        key
        for key in fields
        if key in {"repo_url", "branch", "git_user", "extra_addons"}
    ]
    if ownership.get("template") != target.template:
        immutable.append("template")
    if immutable:
        return [
            (
                "conflict",
                "production source/seed changes require explicit production operations: "
                + ", ".join(immutable),
            )
        ]
    if record.get("unhealthy"):
        return [
            (
                "conflict",
                "production is marked unhealthy; inspect it before reconciliation",
            )
        ]
    if fields or runtime or ownership.get("appliedHash") != spec_hash(manifest):
        return [
            ("update", ", ".join(fields + runtime) or "incomplete configuration apply")
        ]
    return []


def apply_production(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    operation: str,
    environ: Mapping[str, str] | None,
) -> None:
    target = manifest.spec.production
    assert target is not None
    desired = desired_record(settings, team, manifest, environ)
    metadata = {
        "name": manifest.metadata.name,
        "resource": "production",
        "template": target.template,
        "appliedHash": "",
    }
    if operation == "create":
        kwargs = {k: v for k, v in desired.items() if k != "odoo_conf"}
        production_ops.create_production(
            settings,
            team,
            target.name,
            template_name=target.template,
            stack_metadata=metadata,
            **kwargs,
        )
    record = production_registry.get_production(team, target.name)
    if operation != "adopt":
        # Mark pending before mutation: registry intent can get ahead of a failed
        # container replacement. Reapply must retry even if the intent now matches.
        production_registry.set_nested(team, target.name, "meta", {"stack": metadata})
        conf_changed = record.get("odoo_conf", {}) != desired["odoo_conf"]
        if conf_changed:
            production_ops.set_production_odoo_conf(
                settings,
                team,
                target.name,
                set_options=desired["odoo_conf"],
                replace=True,
                restart=False,
            )
        infrastructure = {
            key: desired[key]
            for key in ("domain", "odoo_image", "env_vars", "server_mode")
        }
        must_reconfigure = operation == "update" and (
            conf_changed
            or bool(set(_field_drift(record, desired)) & set(infrastructure))
            or bool(runtime_drift(settings, team, record))
            or not record.get("meta", {}).get("stack", {}).get("appliedHash")
        )
        if must_reconfigure:
            result = production_ops.reconfigure_production(
                settings, team, target.name, force_recreate=True, **infrastructure
            )
            if not result.get("healthy"):
                raise ConflictError(
                    "Production configuration did not become healthy; Stack apply remains incomplete"
                )
        elif conf_changed:
            production_ops.restart_production(settings, team, target.name)
        production_registry.update_production(
            team,
            target.name,
            {
                "auto_update": desired["auto_update"],
                "allow_copy_to_dev_mcp": desired["allow_copy_to_dev_mcp"],
            },
        )
        if operation == "create" or conf_changed:
            if not production_ops.wait_production_healthy(
                get_client(), settings, team, target.name
            ):
                raise ConflictError(
                    "Production did not become healthy; Stack apply remains incomplete"
                )
    elif runtime_drift(settings, team, record):
        # plan_production lets adoption through only with outdated routing;
        # reconfigure recreates the container from the (matching) record.
        result = production_ops.reconfigure_production(settings, team, target.name)
        if not result.get("healthy"):
            raise ConflictError(
                "Adopted production did not become healthy; Stack apply remains incomplete"
            )
    metadata["appliedHash"] = spec_hash(manifest)
    production_registry.set_nested(team, target.name, "meta", {"stack": metadata})
