"""Plan, apply, and report declarative Oduflow Stack state."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
from dataclasses import dataclass, field
from typing import Any, Mapping

from pydantic import BaseModel

from oduflow import git_ops, image_builds, stack_production
from oduflow.docker_ops import (
    env_ops,
    odoo_ops,
    service_database_ops,
    service_ops,
    system_ops,
    volume_file_ops,
    volume_ops,
)
from oduflow.domains import service_hostname
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    FlowError,
    NotFoundError,
)
from oduflow.locking import prod_lock_key
from oduflow.naming import sanitize_repo_url, validate_env_name
from oduflow.service_runtime import normalize_runtime
from oduflow.settings import Settings, TeamSettings
from oduflow.stack_loader import (
    manifest_hash,
    read_stack_file,
    resolve_env_values,
)
from oduflow.stack_models import Service, ServiceDatabase, StackManifest
from oduflow.stack_state import load_state, save_state

STACK_LABEL = "oduflow.stack"
STACK_RESOURCE_LABEL = "oduflow.stack-resource"
STACK_SPEC_HASH_LABEL = "oduflow.stack-spec-hash"
STACK_SANITIZE_LABEL = "oduflow.stack-sanitize"
_STACK_FILE_LIMIT = 1_000_000


@dataclass(frozen=True)
class PlanAction:
    operation: str
    resource: str
    detail: str = ""
    modules: tuple[str, ...] = ()

    @property
    def conflict(self) -> bool:
        return self.operation == "conflict"


@dataclass(frozen=True)
class PlannedImage:
    """The image a service will run, as resolved while planning.

    For a service that declares ``build``, ``commit`` is what its ref pointed
    at and ``image`` is the staging tag of a finished build of that commit —
    empty while that build is still to be run by apply.
    """

    image: str
    commit: str = ""


@dataclass(frozen=True)
class StackPlan:
    stack: str
    actions: tuple[PlanAction, ...]
    images: Mapping[str, PlannedImage] = field(default_factory=dict)

    @property
    def has_conflicts(self) -> bool:
        return any(action.conflict for action in self.actions)

    @property
    def has_changes(self) -> bool:
        return any(action.operation != "conflict" for action in self.actions)


def _resource_hash(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    # Default service databases retain the hash of the original empty spec.
    raw = (
        value.model_dump(
            mode="json",
            by_alias=True,
            exclude_defaults=isinstance(value, ServiceDatabase),
        )
        if isinstance(value, BaseModel)
        else value
    )

    # Adding productionField or a service's build must not invalidate the
    # ownership hashes of resources created before those keys existed.
    def compatible(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: compatible(v)
                for k, v in value.items()
                if not (k in ("productionField", "build") and v is None)
            }
        if isinstance(value, list):
            return [compatible(item) for item in value]
        return value

    encoded = json.dumps(compatible(raw), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _stack_labels(
    stack: str, resource: str, value: BaseModel | dict[str, Any]
) -> dict[str, str]:
    return {
        STACK_LABEL: stack,
        STACK_RESOURCE_LABEL: resource,
        STACK_SPEC_HASH_LABEL: _resource_hash(value),
    }


def _environment_labels(stack: str, manifest: StackManifest) -> dict[str, str]:
    """Label only the environment-owned part of the manifest.

    Module installation is reconciled independently and must not leave the
    environment container carrying a stale hash when only that list changes.
    """
    assert manifest.spec.environment is not None
    value = manifest.spec.environment.model_dump(mode="json", by_alias=True)
    value.pop("modules", None)
    labels = _stack_labels(stack, "environment", value)
    labels[STACK_SANITIZE_LABEL] = str(manifest.spec.environment.sanitize).lower()
    return labels


def _environment_hash_with_sanitize(manifest: StackManifest, sanitize: bool) -> str:
    assert manifest.spec.environment is not None
    value = manifest.spec.environment.model_dump(mode="json", by_alias=True)
    value.pop("modules", None)
    value["sanitize"] = sanitize
    return _resource_hash(value)


def _owned_by(info: Mapping[str, Any], stack: str, resource: str) -> bool:
    return info.get("stack") == stack and info.get("stack_resource") == resource


def _normalized_template(value: Any) -> str:
    return "none" if value in (None, "", "none") else str(value)


def _plan_service_image(
    team: TeamSettings, service: Service, current_image: str
) -> PlannedImage:
    """Resolve the image a service should run without building anything.

    A declared ``build`` costs one ``git ls-remote``: the ref's commit decides
    whether an earlier build can be reused or apply has to build.
    """
    from oduflow.docker_ops import build_ops

    build = service.build
    if build is None:
        return PlannedImage(image=str(service.image))
    build_ops.build_settings(team)  # refuses when the operator disabled builds
    git_ops.validate_repo_url(build.repo_url)
    commit, _label = build_ops.resolve_remote_ref(
        team, git_ops.inject_credential_user(build.repo_url, build.git_user), build.ref
    )
    key = image_builds.build_key(
        sanitize_repo_url(build.repo_url),
        commit,
        build.dockerfile,
        build.context,
        build.target,
        build.args,
    )
    return PlannedImage(
        image=build_ops.find_built_image(team, key, prefer=current_image),
        commit=commit,
    )


def _build_service_image(
    team: TeamSettings,
    name: str,
    service: Service,
    planned: PlannedImage,
    keep_tags: set[str],
) -> str:
    """Run the build a plan found missing; returns the staging tag to run.

    *keep_tags* holds the images this apply is going to run. No container
    references them until the services are created, so each build hands the
    set to retention, which would otherwise be free to prune them.
    """
    from oduflow.docker_ops import build_ops

    build = service.build
    if build is None or planned.image:
        return planned.image
    job = build_ops.build_git_image(
        team,
        build.repo_url,
        build.ref,
        build.dockerfile,
        build.context,
        build.target,
        dict(build.args),
        git_user=build.git_user,
        commit=planned.commit,
        keep_tags=frozenset(keep_tags),
    )
    if job.status != image_builds.STATUS_SUCCEEDED:
        tail = image_builds.store.read_log_tail(team, job.build_id, 40)
        raise ExternalCommandError(
            "image build",
            1,
            f"Stack service '{name}': build {job.build_id} of "
            f"{job.repo_url} @ {job.commit[:12]} is '{job.status}' — {job.error}"
            + (f"\n{tail}" if tail else ""),
        )
    keep_tags.add(job.local_tag)
    return job.local_tag


def _refuse_conflicts(plan: StackPlan) -> None:
    if plan.has_conflicts:
        conflicts = "; ".join(
            f"{item.resource}: {item.detail}" for item in plan.actions if item.conflict
        )
        raise ConflictError(f"Stack apply refused: {conflicts}")


def _planned_tags(plan: StackPlan) -> set[str]:
    return {planned.image for planned in plan.images.values() if planned.image}


def _service_changes(plan: StackPlan, name: str) -> bool:
    resource = f"services.{name}"
    return any(
        item.resource == resource and item.operation in ("create", "update")
        for item in plan.actions
    )


def _prebuild_service_images(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    manifest_path: str,
    environ: Mapping[str, str] | None,
    keep_tags: set[str],
) -> dict[str, PlannedImage]:
    """Run the builds an apply needs before it takes any lock.

    A build can take as long as ``build_timeout_seconds``, and the team lock
    refuses every environment operation of the team while it is held. Planning
    reads only, so the missing images are built off an unlocked plan; the
    locked plan that follows is the authority and picks the results up by
    commit. A ref that moves in between is simply built under the lock.
    """
    services = manifest.spec.services
    if not any(service.build is not None for service in services.values()):
        return {}
    plan = build_plan(settings, team, manifest, manifest_path, environ=environ)
    _refuse_conflicts(plan)
    keep_tags.update(_planned_tags(plan))
    built: dict[str, PlannedImage] = {}
    for name, service in services.items():
        if service.build is None or not _service_changes(plan, name):
            continue
        planned = plan.images[name]
        built[name] = PlannedImage(
            image=_build_service_image(team, name, service, planned, keep_tags),
            commit=planned.commit,
        )
    return built


def _desired_mounts(service: Service) -> list[dict[str, str]]:
    return sorted(
        [
            {"volume": mount.source, "mount_path": mount.target, "mode": mount.mode}
            for mount in service.volumes
        ],
        key=lambda item: (item["volume"], item["mount_path"], item["mode"]),
    )


def _desired_routes(service: Service) -> list[dict[str, object]]:
    return [route.model_dump() for route in service.routes]


def _actual_routes(raw: Any) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for route in raw or []:
        result.append(
            {
                "path": route.get("path"),
                "port": route.get("port"),
                "strip_prefix": bool(route.get("strip_prefix", False)),
            }
        )
    return result


def _actual_mounts(raw: Any) -> list[dict[str, str]]:
    return sorted(
        [
            {
                "volume": str(item.get("volume", "")),
                "mount_path": str(item.get("mount_path", "")),
                "mode": str(item.get("mode", "rw")),
            }
            for item in raw or []
            if item.get("mount_path") != "/etc/traefik"
        ],
        key=lambda item: (item["volume"], item["mount_path"], item["mode"]),
    )


def _desired_hostname(
    settings: Settings, team: TeamSettings, name: str, hostname: str | None
) -> str | None:
    """What ``create_service`` would assign, so drift compares like with like.

    Must stay delegated to ``domains.service_hostname``: when this spelled the
    rule out itself it kept nesting under the team hostname after base_domain
    landed, so every plan reported drift and every apply recreated the
    container.
    """
    if settings.routing_mode != "traefik":
        return hostname
    return service_hostname(team, name, hostname)


def _installed_modules(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    requested: list[str],
) -> set[str]:
    if not requested:
        return set()
    quoted = ",".join("'" + name.replace("'", "''") + "'" for name in requested)
    result = odoo_ops.run_db_query(
        settings,
        team,
        env_name,
        "SELECT name FROM ir_module_module "
        f"WHERE state = 'installed' AND name IN ({quoted}) ORDER BY name",
    )
    rows = csv.DictReader(io.StringIO(str(result.get("output", ""))))
    return {str(row.get("name", "")) for row in rows if row.get("name")}


def _file_matches(
    settings: Settings,
    team: TeamSettings,
    volume: str,
    path: str,
    expected: str,
) -> bool:
    try:
        result = volume_file_ops.read_file_in_volume(
            settings, team, volume, path, max_bytes=_STACK_FILE_LIMIT
        )
    except NotFoundError:
        return False
    return "error" not in result and result.get("output") == expected


def _service_env_matches(actual: Mapping[str, Any], desired: Mapping[str, str]) -> bool:
    """Compare declared values while ignoring untouched image defaults."""
    effective = actual.get("env_vars", {})
    image_defaults = actual.get("image_env_vars", {})
    if not isinstance(effective, dict) or not isinstance(image_defaults, dict):
        return False
    if any(effective.get(key) != value for key, value in desired.items()):
        return False
    return not any(
        key not in desired and image_defaults.get(key) != value
        for key, value in effective.items()
    )


def build_plan(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    manifest_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> StackPlan:
    """Compare the manifest with live resources without changing anything."""
    stack = manifest.metadata.name
    spec = manifest.spec
    desired_env = spec.environment
    target = desired_env or spec.production
    if desired_env is not None:
        validate_env_name(desired_env.name)
    if target is not None:
        git_ops.validate_repo_url(target.repo_url)
    for desired_repo in spec.extra_repositories.values():
        git_ops.validate_repo_url(desired_repo.repo_url)
    actions: list[PlanAction] = []

    from oduflow import extra_addons

    repos = (
        {item["name"]: item for item in extra_addons.list_extra_repos(team)}
        if spec.extra_repositories
        else {}
    )
    for name, desired_repo in spec.extra_repositories.items():
        actual = repos.get(name)
        if actual is None:
            actions.append(
                PlanAction("create", f"extraRepositories.{name}", desired_repo.repo_url)
            )
        elif sanitize_repo_url(str(actual.get("repo_url", ""))) != sanitize_repo_url(
            desired_repo.repo_url
        ):
            actions.append(
                PlanAction(
                    "conflict",
                    f"extraRepositories.{name}",
                    "an extra repository with this name has a different URL",
                )
            )

    volumes = (
        {item["name"]: item for item in volume_ops.list_volumes(settings, team)}
        if spec.volumes
        else {}
    )
    for name, desired_volume in spec.volumes.items():
        actual = volumes.get(name)
        resource = f"volumes.{name}"
        if actual is None:
            actions.append(PlanAction("create", resource, desired_volume.description))
        elif not _owned_by(actual, stack, resource):
            actions.append(
                PlanAction(
                    "conflict", resource, "existing volume is not owned by this stack"
                )
            )
        elif actual.get("description", "") != desired_volume.description:
            actions.append(
                PlanAction(
                    "conflict",
                    resource,
                    "volume descriptions are immutable in V1; replacement required",
                )
            )

    # Only the declared databases, with credentials: each inspection probes
    # PostgreSQL, and the records also feed service env resolution below.
    service_databases = {
        item["name"]: item
        for item in service_database_ops.list_databases(
            settings, team, spec.databases, reveal_password=True
        )
    }
    pending_databases: set[str] = set()
    blocked_databases: set[str] = set()
    for name, desired_database in spec.databases.items():
        resource = f"databases.{name}"
        actual = service_databases.get(name)
        if desired_database.cluster == "prod" and not settings.prod_enabled:
            conflict = "production.enabled must be true for cluster prod"
        elif actual is None:
            pending_databases.add(name)
            actions.append(
                PlanAction("create", resource, f"cluster {desired_database.cluster}")
            )
            continue
        elif actual.get("status") == "credentials-error":
            conflict = "stored credentials for this database cannot be read"
        elif not _owned_by(actual, stack, resource):
            conflict = "existing database is not owned by this stack"
        elif service_database_ops.record_cluster(actual) != desired_database.cluster:
            conflict = "changing the database cluster requires an explicit migration"
        elif actual.get("status") == "unavailable":
            conflict = "the database cluster is unavailable"
        elif actual.get("status") != "ready":
            conflict = "managed PostgreSQL database or role is missing"
        elif actual.get("stack_spec_hash") != _resource_hash(desired_database):
            conflict = "database metadata is inconsistent with the manifest"
        else:
            continue
        blocked_databases.add(name)
        actions.append(PlanAction("conflict", resource, conflict))

    target_needs_create = False
    if desired_env is not None:
        desired_env_vars = resolve_env_values(
            desired_env.env,
            settings=settings,
            team=team,
            env_name=desired_env.name,
            environ=environ,
        )

        environments = {
            item["env_name"]: item for item in env_ops.list_environments(settings, team)
        }
        actual_env = environments.get(desired_env.name)
        env_resource = "environment"
        target_needs_create = actual_env is None
        if actual_env is None:
            actions.append(PlanAction("create", env_resource, desired_env.name))
            if desired_env.template is not None:
                templates = {
                    item["template_name"]: item
                    for item in system_ops.list_templates(settings, team)
                }
                template = templates.get(desired_env.template)
                if template is None or not template.get("db_loaded", False):
                    actions.append(
                        PlanAction(
                            "conflict",
                            env_resource,
                            f"template '{desired_env.template}' is not available",
                        )
                    )
        elif not _owned_by(actual_env, stack, env_resource):
            actions.append(
                PlanAction(
                    "conflict",
                    env_resource,
                    "existing environment is not owned by this stack",
                )
            )
        else:
            immutable_drift: list[str] = []
            if sanitize_repo_url(
                str(actual_env.get("repo_url", ""))
            ) != sanitize_repo_url(desired_env.repo_url):
                immutable_drift.append("repoUrl")
            if actual_env.get("git_branch") != desired_env.branch:
                immutable_drift.append("branch")
            if (
                desired_env.hostname
                and actual_env.get("hostname") != desired_env.hostname
            ):
                immutable_drift.append("hostname")
            if _normalized_template(
                actual_env.get("template_name")
            ) != _normalized_template(desired_env.template):
                immutable_drift.append("template")
            desired_extras = {
                name: item.branch for name, item in spec.extra_repositories.items()
            }
            if actual_env.get("extra_addons", {}) != desired_extras:
                immutable_drift.append("extraRepositories")
            actual_sanitize = actual_env.get("stack_sanitize", "")
            if actual_sanitize in ("true", "false"):
                sanitize_changed = (actual_sanitize == "true") != desired_env.sanitize
            else:
                # Older Stack environments predate the explicit policy label. Their
                # spec hash can prove the current manifest has the same policy, but
                # any other mismatch is ambiguous: sanitization happens only during
                # creation, so never stamp a policy through an ordinary update.
                sanitize_changed = actual_env.get(
                    "stack_spec_hash"
                ) != _environment_hash_with_sanitize(manifest, desired_env.sanitize)
            if sanitize_changed:
                immutable_drift.append("sanitize")
            if immutable_drift:
                actions.append(
                    PlanAction(
                        "conflict",
                        env_resource,
                        "replacement required for: " + ", ".join(immutable_drift),
                    )
                )
            mutable_drift: list[str] = []
            if actual_env.get("odoo_image") != desired_env.odoo_image:
                mutable_drift.append("odooImage")
            current_info = env_ops.get_environment_info(
                settings, team, desired_env.name
            )
            if current_info.get("env_vars", {}) != desired_env_vars:
                mutable_drift.append("env")
            if mutable_drift:
                actions.append(
                    PlanAction("update", env_resource, ", ".join(mutable_drift))
                )

    elif spec.production is not None:
        production_actions = stack_production.plan_production(
            settings, team, manifest, environ
        )
        actions.extend(
            PlanAction(operation, "production", detail)
            for operation, detail in production_actions
        )
        target_needs_create = any(
            operation == "create" for operation, _ in production_actions
        )

    for item in spec.files:
        content = read_stack_file(manifest_path, item.source)
        resource = f"files.{item.volume}:{item.path}"
        if item.volume not in volumes or not _file_matches(
            settings, team, item.volume, item.path, content
        ):
            actions.append(PlanAction("write", resource, item.source))

    services = (
        {item["name"]: item for item in service_ops.list_services(settings, team)}
        if spec.services
        else {}
    )
    images: dict[str, PlannedImage] = {}
    for name, desired_service in spec.services.items():
        resource = f"services.{name}"
        if desired_service.routes and settings.routing_mode != "traefik":
            actions.append(
                PlanAction(
                    "conflict",
                    resource,
                    "HTTP routes require routing.mode = 'traefik'",
                )
            )
            continue
        if settings.acme_enabled:
            reserved_mount = next(
                (
                    mount.target
                    for mount in desired_service.volumes
                    if posixpath.normpath(mount.target) == "/etc/traefik"
                    or posixpath.normpath(mount.target).startswith("/etc/traefik/")
                ),
                None,
            )
            if reserved_mount is not None:
                actions.append(
                    PlanAction(
                        "conflict",
                        resource,
                        f"mount path '{reserved_mount}' is reserved for Traefik TLS",
                    )
                )
                continue
        actual = services.get(name)
        try:
            planned_image = _plan_service_image(
                team, desired_service, str((actual or {}).get("image") or "")
            )
        except FlowError as exc:
            actions.append(PlanAction("conflict", resource, f"image build: {exc}"))
            continue
        images[name] = planned_image
        if actual is None:
            # Validate host-only values now; environment values are safe to
            # defer until after the environment create action.
            for key, value in desired_service.env.items():
                if getattr(value, "from_env", None) is not None:
                    resolve_env_values({key: value}, environ=environ)
            build = desired_service.build
            pending = (
                f"build {build.repo_url} @ {planned_image.commit[:12]}"
                if build is not None
                else ""
            )
            actions.append(
                PlanAction("create", resource, planned_image.image or pending)
            )
            continue
        if not _owned_by(actual, stack, resource):
            actions.append(
                PlanAction(
                    "conflict", resource, "existing service is not owned by this stack"
                )
            )
            continue
        has_environment_value = any(
            (
                getattr(value, "environment_field", None) is not None
                or getattr(value, "production_field", None) is not None
            )
            for value in desired_service.env.values()
        )
        has_pending_database_value = any(
            getattr(value, "database", None) in pending_databases
            for value in desired_service.env.values()
        )
        has_blocked_database_value = any(
            getattr(value, "database", None) in blocked_databases
            for value in desired_service.env.values()
        )
        deferred_env = (
            target_needs_create and has_environment_value
        ) or has_pending_database_value
        if deferred_env or has_blocked_database_value:
            # A generated input this service consumes does not exist yet (or
            # is in conflict), so its value cannot be compared against the
            # running container. Still validate every host-env source now;
            # apply resolves the generated values after creating the Odoo
            # environment and the databases.
            for key, value in desired_service.env.items():
                if getattr(value, "from_env", None) is not None:
                    resolve_env_values({key: value}, environ=environ)
            desired_service_env: dict[str, str] | None = None
        else:
            desired_service_env = resolve_env_values(
                desired_service.env,
                settings=settings,
                team=team,
                env_name=desired_env.name if desired_env is not None else "",
                production_name=spec.production.name
                if spec.production is not None
                else "",
                production_domain=spec.production.domain if spec.production else "",
                database_records=service_databases,
                environ=environ,
            )
        desired_caps = (
            ["NET_ADMIN"]
            if desired_service.net_admin and not desired_service.privileged
            else []
        )
        drift: list[str] = []
        if desired_service_env is not None and not _service_env_matches(
            actual, desired_service_env
        ):
            drift.append("env")
        comparisons = (
            (
                "runtime",
                actual.get("runtime") or {},
                normalize_runtime(
                    desired_service.runtime.model_dump(exclude_none=True)
                ),
            ),
            # Empty while a build is pending, which reads as image drift.
            ("image", actual.get("image"), planned_image.image),
            ("port", actual.get("port"), desired_service.port),
            (
                "hostname",
                actual.get("hostname"),
                _desired_hostname(settings, team, name, desired_service.hostname),
            ),
            ("hostMode", bool(actual.get("host_mode")), desired_service.host_mode),
            (
                "volumes",
                _actual_mounts(actual.get("volumes")),
                _desired_mounts(desired_service),
            ),
            ("capabilities", sorted(actual.get("cap_add", [])), desired_caps),
            (
                "command",
                list(
                    actual.get("command")
                    or (actual.get("image_command") if desired_service.command else [])
                    or []
                ),
                desired_service.command,
            ),
            (
                "privileged",
                bool(actual.get("privileged")),
                desired_service.privileged,
            ),
            (
                "routes",
                _actual_routes(actual.get("routes")),
                _desired_routes(desired_service),
            ),
        )
        drift.extend(
            field for field, current, wanted in comparisons if current != wanted
        )
        if deferred_env:
            # The generated value is about to change (a database is being
            # recreated, or the environment is being created), and the plan is
            # what drives apply. Without an explicit action apply would create
            # the input and leave this container holding the superseded
            # credentials, while still reporting success.
            drift.append("env")
        if drift or actual.get("stack_spec_hash") != _resource_hash(desired_service):
            actions.append(
                PlanAction("update", resource, ", ".join(drift) or "metadata")
            )
        elif actual.get("status") != "running":
            # Services run under restart_policy unless-stopped, so a container
            # that is not running was stopped by hand. The manifest declares it
            # running; converge like production does for a stopped container.
            actions.append(PlanAction("start", resource, str(actual.get("status"))))

    modules = list(desired_env.modules.install) if desired_env is not None else []
    if modules and desired_env is not None:
        installed = (
            set()
            if target_needs_create
            else _installed_modules(settings, team, desired_env.name, modules)
        )
        missing = [module for module in modules if module not in installed]
        if missing:
            actions.append(
                PlanAction(
                    "install", "modules", ", ".join(missing), modules=tuple(missing)
                )
            )

    return StackPlan(stack=stack, actions=tuple(actions), images=images)


def format_plan(plan: StackPlan) -> str:
    icons = {
        "create": "+",
        "update": "~",
        "write": ">",
        "install": "+",
        "conflict": "!",
        "adopt": "=",
        "start": "^",
    }
    lines = [f"Stack {plan.stack}:"]
    if not plan.actions:
        lines.append("  No changes.")
        return "\n".join(lines)
    for action in plan.actions:
        detail = f" — {action.detail}" if action.detail else ""
        lines.append(
            f"  {icons.get(action.operation, '?')} {action.operation} "
            f"{action.resource}{detail}"
        )
    return "\n".join(lines)


def _service_kwargs(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    name: str,
    desired: Service,
    environ: Mapping[str, str] | None,
    image: str,
) -> dict[str, Any]:
    env_vars = resolve_env_values(
        desired.env,
        settings=settings,
        team=team,
        env_name=manifest.spec.environment.name if manifest.spec.environment else "",
        production_name=manifest.spec.production.name
        if manifest.spec.production
        else "",
        production_domain=manifest.spec.production.domain
        if manifest.spec.production
        else "",
        environ=environ,
    )
    return {
        "runtime": normalize_runtime(desired.runtime.model_dump(exclude_none=True)),
        "image": image,
        "port": desired.port,
        "hostname": desired.hostname,
        "env_vars": env_vars or None,
        "host_mode": desired.host_mode,
        "volumes": _desired_mounts(desired) or None,
        "cap_add": ["NET_ADMIN"]
        if desired.net_admin and not desired.privileged
        else None,
        "privileged": desired.privileged,
        "routes": _desired_routes(desired) or None,
        "command": list(desired.command) or None,
        "stack_labels": _stack_labels(
            manifest.metadata.name, f"services.{name}", desired
        ),
    }


def apply_stack(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    manifest_path: str,
    *,
    environ: Mapping[str, str] | None = None,
    lock_manager: Any = None,
) -> StackPlan:
    """Converge live resources to a preflighted, non-destructive V1 plan."""
    # Builds run before the first mutation and before any lock: a Dockerfile
    # is the likeliest step to fail, and failing here leaves nothing
    # half-applied and nobody locked out.
    keep_tags: set[str] = set()
    prebuilt = _prebuild_service_images(
        settings, team, manifest, manifest_path, environ, keep_tags
    )
    acquired = False
    production_key = None
    if lock_manager is not None:
        lock_manager.acquire_team(team.team_id, operation="stack_apply")
        acquired = True
    try:
        if lock_manager is not None and manifest.spec.production is not None:
            key = prod_lock_key(team.team_id, manifest.spec.production.name)
            lock_manager.acquire_env(key, operation="stack_apply")
            production_key = key
        plan = build_plan(settings, team, manifest, manifest_path, environ=environ)
        _refuse_conflicts(plan)
        operations = {(item.operation, item.resource): item for item in plan.actions}
        stack = manifest.metadata.name
        spec = manifest.spec

        # Normally every image is there by now. What is still missing — a ref
        # that moved since the builds above — is built here, still ahead of
        # the first mutation.
        keep_tags.update(_planned_tags(plan))
        service_images: dict[str, str] = {}
        for name, desired_service in spec.services.items():
            if not _service_changes(plan, name):
                continue
            # Every service the plan creates or updates has a planned image.
            planned = plan.images[name]
            early = prebuilt.get(name)
            if not planned.image and early and early.commit == planned.commit:
                planned = early
            service_images[name] = _build_service_image(
                team, name, desired_service, planned, keep_tags
            )

        from oduflow import extra_addons

        for name, desired_repo in spec.extra_repositories.items():
            if ("create", f"extraRepositories.{name}") in operations:
                extra_addons.register_extra_repo(
                    team,
                    name,
                    desired_repo.repo_url,
                    git_user=desired_repo.git_user,
                )

        for name, desired_volume in spec.volumes.items():
            resource = f"volumes.{name}"
            if ("create", resource) in operations:
                volume_ops.create_volume(
                    settings,
                    team,
                    name,
                    description=desired_volume.description,
                    stack_labels=_stack_labels(stack, resource, desired_volume),
                )

        for name, desired_database in spec.databases.items():
            resource = f"databases.{name}"
            if ("create", resource) in operations:
                service_database_ops.create_database(
                    settings,
                    team,
                    name,
                    cluster=desired_database.cluster,
                    stack_labels=_stack_labels(stack, resource, desired_database),
                )

        desired_env = spec.environment
        if desired_env is not None:
            env_vars = resolve_env_values(
                desired_env.env,
                settings=settings,
                team=team,
                env_name=desired_env.name,
                environ=environ,
            )
            env_labels = _environment_labels(stack, manifest)
            extras = {
                name: item.branch for name, item in spec.extra_repositories.items()
            }
            if ("create", "environment") in operations:
                env_ops.create_environment(
                    settings,
                    team,
                    desired_env.branch,
                    desired_env.repo_url,
                    desired_env.odoo_image,
                    env_name=desired_env.name,
                    template_name=desired_env.template,
                    extra_addons=extras or None,
                    sanitize=desired_env.sanitize,
                    env_vars=env_vars or None,
                    stack_labels=env_labels,
                    hostname=desired_env.hostname or "",
                )
            elif ("update", "environment") in operations:
                updated = env_ops.update_environment(
                    settings,
                    team,
                    desired_env.name,
                    env_override=env_vars,
                    image_override=desired_env.odoo_image,
                    label_overrides=env_labels,
                )
                if updated.get("exit_code"):
                    raise ExternalCommandError(
                        "dependency install",
                        int(updated["exit_code"]),
                        f"Stack environment '{desired_env.name}' was recreated, "
                        "but its dependencies failed to install. Remaining stack "
                        "operations were not applied. Fix the dependency error, "
                        "run update_environment for this environment, then rerun "
                        "stack apply.\n" + "\n".join(updated.get("setup_logs", [])),
                    )

        elif spec.production is not None:
            for operation in ("create", "adopt", "update"):
                if (operation, "production") in operations:
                    stack_production.apply_production(
                        settings, team, manifest, operation, environ
                    )

        for item in spec.files:
            resource = f"files.{item.volume}:{item.path}"
            if ("write", resource) in operations:
                volume_file_ops.write_file_in_volume(
                    settings,
                    team,
                    item.volume,
                    item.path,
                    read_stack_file(manifest_path, item.source),
                )

        for name, desired_service in spec.services.items():
            resource = f"services.{name}"
            create_service = ("create", resource) in operations
            update_service = ("update", resource) in operations
            if ("start", resource) in operations:
                service_ops.restart_service(settings, team, name)
                continue
            if not create_service and not update_service:
                continue
            kwargs = _service_kwargs(
                settings,
                team,
                manifest,
                name,
                desired_service,
                environ,
                service_images[name],
            )
            if create_service:
                service_ops.create_service(settings, team, name, **kwargs)
            elif update_service:
                service_ops.update_service(
                    settings,
                    team,
                    name,
                    env_override=kwargs["env_vars"] or {},
                    image_override=kwargs["image"],
                    port_override=kwargs["port"],
                    hostname_override=kwargs["hostname"] or "",
                    host_mode_override=kwargs["host_mode"],
                    volume_override=kwargs["volumes"] or [],
                    cap_add_override=kwargs["cap_add"] or [],
                    privileged_override=kwargs["privileged"],
                    routes_override=kwargs["routes"] or [],
                    command_override=kwargs["command"] or [],
                    runtime_override=kwargs["runtime"],
                    stack_labels=kwargs["stack_labels"],
                )

        module_action = operations.get(("install", "modules"))
        if module_action and desired_env is not None:
            result = odoo_ops.install_odoo_modules(
                settings, team, desired_env.name, *module_action.modules
            )
            if int(result.get("exit_code", 1)) != 0:
                raise ConflictError(
                    "Stack module installation failed: "
                    + str(result.get("output", "unknown error"))
                )
            env_ops.restart_environment(settings, desired_env.name, team)

        target_resource = {}
        if desired_env is not None:
            target_resource["environment"] = desired_env.name
        elif spec.production is not None:
            target_resource["production"] = spec.production.name
        save_state(
            team,
            stack,
            manifest_hash(manifest),
            {
                **target_resource,
                "extraRepositories": sorted(spec.extra_repositories),
                "volumes": sorted(spec.volumes),
                "databases": sorted(spec.databases),
                "services": sorted(spec.services),
                "modules": list(desired_env.modules.install)
                if desired_env is not None
                else [],
            },
        )
        return plan
    finally:
        if production_key is not None:
            lock_manager.release_env(production_key)
        if acquired:
            lock_manager.release_team(team.team_id)


def stack_status(
    settings: Settings,
    team: TeamSettings,
    manifest: StackManifest,
    manifest_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    plan = build_plan(settings, team, manifest, manifest_path, environ=environ)
    return {
        "stack": manifest.metadata.name,
        "inSync": not plan.actions,
        "hasConflicts": plan.has_conflicts,
        "manifestHash": manifest_hash(manifest),
        "state": load_state(team, manifest.metadata.name),
        "plan": [
            {
                "operation": item.operation,
                "resource": item.resource,
                "detail": item.detail,
            }
            for item in plan.actions
        ],
    }
