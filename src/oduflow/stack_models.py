"""Typed schema for declarative Oduflow Stack manifests."""

from __future__ import annotations

import posixpath
import re
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from oduflow.image_builds import validate_git_ref, validate_relative_path
from oduflow.naming import (
    parse_service_command,
    validate_domain,
    validate_env_hostname,
    validate_env_name,
    validate_extra_addon_spec,
    validate_prod_name,
    validate_template_name,
)
from oduflow.service_runtime import ServiceRuntime, to_camel

STACK_API_VERSION = "oduflow.dev/v1alpha1"
STACK_KIND = "Stack"

ResourceName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"),
]
ServiceDatabaseName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,30}$"),
]
ExtraRepositoryName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,63}$"),
]
ModuleName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"),
]
EnvironmentVariableName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
CommandArgument = Annotated[
    str,
    StringConstraints(strip_whitespace=False, min_length=1),
]


class StackModel(BaseModel):
    """Strict base model with a YAML-friendly camelCase public shape."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
        strict=True,
    )


class Metadata(StackModel):
    name: ResourceName


SERVICE_VALUE_PLACEHOLDER = "{value}"

# The docs promise the template is substituted literally ("that single
# placeholder and nothing else"), so declared whitespace must survive the
# model-wide strip_whitespace.
ServiceFormat = Annotated[str, StringConstraints(strip_whitespace=False)]


def service_format_error(template: str) -> str | None:
    """Why a service-reference ``format`` is unusable, or None when it is valid.

    Exactly one literal ``{value}`` and no other braces: the substitution is a
    plain one-shot replace, so anything resembling a second placeholder or a
    template expression is refused rather than passed through verbatim.
    """
    if template.count(SERVICE_VALUE_PLACEHOLDER) != 1:
        return f"must contain exactly one '{SERVICE_VALUE_PLACEHOLDER}'"
    rest = template.replace(SERVICE_VALUE_PLACEHOLDER, "", 1)
    if "{" in rest or "}" in rest:
        return f"must not contain braces other than '{SERVICE_VALUE_PLACEHOLDER}'"
    if "\x00" in template:
        return "must not contain NUL bytes"
    return None


class ValueFrom(StackModel):
    """A value loaded at apply time rather than persisted in the manifest."""

    from_env: str | None = None
    environment_field: Literal["url", "token"] | None = None
    production_field: Literal["url", "containerName", "database"] | None = None
    database: ServiceDatabaseName | None = None
    database_field: (
        Literal["url", "host", "port", "database", "username", "password"] | None
    ) = None
    service: ResourceName | None = None
    service_field: Literal["containerName"] | None = None
    format: ServiceFormat | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> ValueFrom:
        sources = sum(
            value is not None
            for value in (
                self.from_env,
                self.environment_field,
                self.production_field,
                self.database,
                self.service,
            )
        )
        if sources != 1:
            raise ValueError(
                "set exactly one of fromEnv, environmentField, productionField, "
                "database, or service"
            )
        if (self.database is None) != (self.database_field is None):
            raise ValueError("database and databaseField must be set together")
        if (self.service is None) != (self.service_field is None):
            raise ValueError("service and serviceField must be set together")
        if self.format is not None and self.service is None:
            raise ValueError("format is only allowed with a service source")
        if self.format is not None:
            problem = service_format_error(self.format)
            if problem:
                raise ValueError(f"format {problem}")
        if self.from_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.from_env
        ):
            raise ValueError("fromEnv must be a valid environment-variable name")
        return self

    @model_serializer(mode="wrap")
    def omit_unset_service_source(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Keep manifests without service references hashing as before.

        Every Stack ownership hash (manifest, service, environment, production)
        is taken over a dump of these models, so emitting the later-added keys
        as nulls would report drift on every existing Stack.
        """
        data: dict[str, Any] = handler(self)
        for key in ("service", "serviceField", "service_field", "format"):
            if key in data and data[key] is None:
                del data[key]
        return data


EnvValue = str | ValueFrom


class ExtraRepository(StackModel):
    repo_url: NonEmptyString
    branch: NonEmptyString
    git_user: str = ""


class Modules(StackModel):
    install: list[ModuleName] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_modules(self) -> Modules:
        if len(set(self.install)) != len(self.install):
            raise ValueError("install must not contain duplicate module names")
        return self


class Environment(StackModel):
    name: str
    hostname: str | None = None
    branch: NonEmptyString
    repo_url: NonEmptyString
    odoo_image: NonEmptyString
    template: str | None = None
    sanitize: bool = True
    env: dict[EnvironmentVariableName, EnvValue] = Field(default_factory=dict)
    modules: Modules = Field(default_factory=Modules)

    @field_validator("name")
    @classmethod
    def valid_environment_name(cls, value: str) -> str:
        return validate_env_name(value)

    @field_validator("hostname")
    @classmethod
    def valid_environment_hostname(cls, value: str | None) -> str | None:
        return validate_env_hostname(value) if value else None

    @field_validator("template", mode="before")
    @classmethod
    def normalize_template(cls, value: object) -> object:
        if value in (None, "", "none"):
            return None
        if isinstance(value, str):
            return validate_template_name(value)
        return value

    @model_validator(mode="after")
    def no_self_references(self) -> Environment:
        if any(
            isinstance(value, ValueFrom) and value.environment_field is not None
            for value in self.env.values()
        ):
            raise ValueError(
                "environment.env cannot reference environmentField on itself"
            )
        return self


class Production(StackModel):
    """A long-lived production target, mutually exclusive with a dev environment."""

    name: str
    domain: NonEmptyString
    repo_url: NonEmptyString
    branch: NonEmptyString
    odoo_image: NonEmptyString
    git_user: str = ""
    env: dict[EnvironmentVariableName, EnvValue] = Field(default_factory=dict)
    auto_update: bool = False
    allow_copy_to_dev_mcp: bool = True
    server_mode: Literal["workers", "gevent"] = "workers"
    odoo_conf: dict[str, str] = Field(default_factory=dict)
    template: str | None = None
    adopt_existing: bool = False

    _name = field_validator("name")(validate_prod_name)
    _domain = field_validator("domain")(validate_domain)

    @field_validator("template", mode="before")
    @classmethod
    def valid_template(cls, value: object) -> object:
        if value in (None, "", "none"):
            return None
        return validate_template_name(value) if isinstance(value, str) else value

    @field_validator("odoo_conf")
    @classmethod
    def valid_odoo_conf(cls, value: dict[str, str]) -> dict[str, str]:
        from oduflow.docker_ops.production_ops import (
            RESERVED_ODOO_CONF_KEYS,
            parse_list_db_override,
            validate_admin_passwd_override,
            validate_workers_override,
        )

        normalized = {}
        for key, option in value.items():
            key = key.lower()
            if key in normalized:
                raise ValueError(f"Duplicate odoo.conf option: {key}")
            if key in RESERVED_ODOO_CONF_KEYS:
                raise ValueError(
                    f"odoo.conf option {key} is reserved: Oduflow manages it "
                    "(see set_production_odoo_conf for the managed keys)"
                )
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                raise ValueError(f"Invalid odoo.conf option: {key}")
            if "\n" in option or "\r" in option:
                raise ValueError(f"odoo.conf option must be a single line: {key}")
            if key == "workers":
                validate_workers_override(option)
            if key == "admin_passwd":
                validate_admin_passwd_override(option)
            if key == "list_db":
                parse_list_db_override(option)
            normalized[key] = option
        # The pair rule itself (an enabled database manager needs a master
        # password) is enforced at plan/apply time, where the team's base
        # conf chain is known: the password may legitimately live there.
        return normalized


class Volume(StackModel):
    description: str = ""


class ServiceDatabase(StackModel):
    """An empty managed PostgreSQL database; credentials are generated at apply."""

    cluster: Literal["dev", "prod"] = "dev"


class VolumeFile(StackModel):
    source: NonEmptyString
    volume: ResourceName
    path: str

    @model_validator(mode="after")
    def safe_target(self) -> VolumeFile:
        normalized = self.path.lstrip("/")
        if not normalized or any(
            part in ("", ".", "..") for part in normalized.split("/")
        ):
            raise ValueError(
                "path must name a file inside the volume without traversal"
            )
        self.path = normalized
        return self


class ServiceMount(StackModel):
    source: ResourceName
    target: NonEmptyString
    mode: Literal["ro", "rw"] = "rw"

    @model_validator(mode="after")
    def absolute_target(self) -> ServiceMount:
        if (
            not self.target.startswith("/")
            or "//" in self.target
            or any(part in (".", "..") for part in self.target.split("/"))
        ):
            raise ValueError(
                "target must be an absolute container path without dot segments"
            )
        self.target = posixpath.normpath(self.target)
        return self


class ServiceRoute(StackModel):
    path: str
    port: int = Field(ge=1, le=65535)
    strip_prefix: bool = False

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or "//" in value
            or any(part in (".", "..") for part in value.split("/"))
            or any(ch in value for ch in ("?", "#", "`"))
            or any(ord(ch) < 32 or ch.isspace() for ch in value)
        ):
            raise ValueError("route path must be a safe absolute URL path")
        return value.rstrip("/") or "/"


class ServiceBuild(StackModel):
    """Build the service image from a git repository instead of pulling one.

    The ref is resolved to a commit on every plan, so a branch follows its tip
    and a tag or commit SHA pins the image.
    """

    repo_url: NonEmptyString
    ref: str = ""
    git_user: str = ""
    dockerfile: NonEmptyString = "Dockerfile"
    context: NonEmptyString = "."
    target: str = ""
    args: dict[EnvironmentVariableName, str] = Field(default_factory=dict)

    @field_validator("ref")
    @classmethod
    def valid_ref(cls, value: str) -> str:
        return validate_git_ref(value)

    @field_validator("repo_url")
    @classmethod
    def no_inline_credentials(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme in ("http", "https") and (parsed.username or parsed.password):
            raise ValueError(
                "repoUrl must not embed credentials; name the account in "
                "gitUser and store its token with setup_repo_auth"
            )
        return value

    @model_validator(mode="after")
    def paths_inside_repository(self) -> ServiceBuild:
        self.context = validate_relative_path(self.context, "context")
        self.dockerfile = validate_relative_path(self.dockerfile, "dockerfile")
        if self.dockerfile == ".":
            raise ValueError("dockerfile must be a file path, not '.'")
        return self


class Service(StackModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["image"],
                    "properties": {
                        "image": {"type": "string"},
                        "build": {"type": "null"},
                    },
                },
                {
                    "required": ["build"],
                    "properties": {
                        "build": {"not": {"type": "null"}},
                        "image": {"type": "null"},
                    },
                },
            ]
        }
    )

    runtime: ServiceRuntime = Field(default_factory=ServiceRuntime)
    image: NonEmptyString | None = None
    build: ServiceBuild | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    hostname: NonEmptyString | None = None
    env: dict[EnvironmentVariableName, EnvValue] = Field(default_factory=dict)
    host_mode: bool = False
    volumes: list[ServiceMount] = Field(default_factory=list)
    privileged: bool = False
    net_admin: bool = False
    routes: list[ServiceRoute] = Field(default_factory=list)
    command: list[CommandArgument] = Field(default_factory=list)

    @field_validator(
        "command",
        mode="before",
        json_schema_input_type=str | list[CommandArgument],
    )
    @classmethod
    def split_command_string(cls, value: object) -> object:
        """Accept the shell-quoted string form as well as an explicit argv list."""
        return parse_service_command(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def one_exposure_mode(self) -> Service:
        if (self.image is None) == (self.build is None):
            raise ValueError("set exactly one of image or build")
        if self.privileged and self.net_admin:
            raise ValueError("privileged and netAdmin are mutually exclusive")
        if self.port is not None and self.routes:
            raise ValueError("port and routes are mutually exclusive")
        if self.port is None and not self.routes:
            raise ValueError("set port or at least one route")
        targets = [(mount.source, mount.target) for mount in self.volumes]
        if len(set(targets)) != len(targets):
            raise ValueError("volumes must not contain duplicate mounts")
        paths = [route.path for route in self.routes]
        if len(set(paths)) != len(paths):
            raise ValueError("routes must not contain duplicate paths")
        return self


def service_references(services: Mapping[str, Service]) -> dict[str, list[str]]:
    """The sibling services each service's env references, in first-use order."""
    graph: dict[str, list[str]] = {}
    for name, service in services.items():
        targets: list[str] = []
        for value in service.env.values():
            if (
                isinstance(value, ValueFrom)
                and value.service is not None
                and value.service not in targets
            ):
                targets.append(value.service)
        graph[name] = targets
    return graph


def service_reference_cycle(graph: Mapping[str, list[str]]) -> list[str] | None:
    """The first reference cycle in manifest order, closed on its start node.

    Iterative so a long (if absurd) reference chain cannot exhaust the
    interpreter stack during validation.
    """
    on_path, finished = 1, 2
    state: dict[str, int] = {}
    for root in graph:
        if root in state:
            continue
        state[root] = on_path
        path = [root]
        pending = [iter(graph[root])]
        while pending:
            target = next(pending[-1], None)
            if target is None:
                state[path.pop()] = finished
                pending.pop()
            elif state.get(target) == on_path:
                return path[path.index(target) :] + [target]
            elif target not in state and target in graph:
                state[target] = on_path
                path.append(target)
                pending.append(iter(graph[target]))
    return None


class StackSpec(StackModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "environment": {"type": "null"},
                        "production": {"type": "null"},
                        "extraRepositories": {"maxProperties": 0},
                    }
                },
                {
                    "required": ["environment"],
                    "properties": {
                        "environment": {"not": {"type": "null"}},
                        "production": {"type": "null"},
                    },
                },
                {
                    "required": ["production"],
                    "properties": {
                        "production": {"not": {"type": "null"}},
                        "environment": {"type": "null"},
                    },
                },
            ]
        }
    )

    environment: Environment | None = None
    production: Production | None = None
    extra_repositories: dict[ExtraRepositoryName, ExtraRepository] = Field(
        default_factory=dict
    )
    volumes: dict[ResourceName, Volume] = Field(default_factory=dict)
    databases: dict[ServiceDatabaseName, ServiceDatabase] = Field(default_factory=dict)
    files: list[VolumeFile] = Field(default_factory=list)
    services: dict[ResourceName, Service] = Field(default_factory=dict)

    @field_validator("extra_repositories")
    @classmethod
    def extra_repository_branches(
        cls, value: dict[str, ExtraRepository]
    ) -> dict[str, ExtraRepository]:
        # "branch" or "branch@<sha40>" (pinned commit), normalized.
        for name, repo in value.items():
            repo.branch = validate_extra_addon_spec(name, repo.branch)
        return value

    @model_validator(mode="after")
    def references_exist(self) -> StackSpec:
        if self.environment is not None and self.production is not None:
            raise ValueError("set at most one of environment or production")
        odoo_target = self.environment or self.production
        if odoo_target is None:
            if self.extra_repositories:
                raise ValueError(
                    "extraRepositories requires an environment or production"
                )
            if not (self.volumes or self.databases or self.files or self.services):
                raise ValueError("a Stack must declare at least one resource")
        target_env = odoo_target.env if odoo_target else {}
        for key, value in target_env.items():
            if isinstance(value, ValueFrom) and (
                value.environment_field is not None
                or value.production_field is not None
            ):
                raise ValueError(
                    "target env cannot reference environmentField or productionField"
                )
            if isinstance(value, ValueFrom) and value.database is not None:
                raise ValueError(
                    f"environment env '{key}' cannot reference a managed database; "
                    "database value sources are only available to auxiliary services"
                )
            if isinstance(value, ValueFrom) and value.service is not None:
                target_kind = "environment" if self.environment else "production"
                raise ValueError(
                    f"{target_kind} env '{key}' cannot reference a service; "
                    "service references are only available to auxiliary services"
                )
        for service in self.services.values():
            for value in service.env.values():
                if isinstance(value, ValueFrom):
                    if value.environment_field is not None and self.environment is None:
                        raise ValueError("environmentField requires spec.environment")
                    if value.production_field is not None and self.production is None:
                        raise ValueError("productionField requires spec.production")
        targets: set[tuple[str, str]] = set()
        for item in self.files:
            if item.volume not in self.volumes:
                raise ValueError(
                    f"file target references undeclared volume '{item.volume}'"
                )
            target = (item.volume, item.path)
            if target in targets:
                raise ValueError(
                    f"files contains duplicate target '{item.volume}:{item.path}'"
                )
            targets.add(target)
        for service_name, service in self.services.items():
            for mount in service.volumes:
                if mount.source not in self.volumes:
                    raise ValueError(
                        f"service '{service_name}' references undeclared volume "
                        f"'{mount.source}'"
                    )
            for key, value in service.env.items():
                if isinstance(value, ValueFrom) and value.database is not None:
                    if service.host_mode:
                        raise ValueError(
                            f"host-mode service '{service_name}' cannot reference "
                            "a managed database because PostgreSQL has no host port"
                        )
                    if value.database not in self.databases:
                        raise ValueError(
                            f"service '{service_name}' env '{key}' references "
                            f"undeclared database '{value.database}'"
                        )
                if isinstance(value, ValueFrom) and value.service is not None:
                    self._check_service_reference(service_name, service, key, value)
        cycle = service_reference_cycle(service_references(self.services))
        if cycle:
            raise ValueError(
                "service reference cycle: "
                + " -> ".join(f"services.{name}" for name in cycle)
            )
        return self

    def _check_service_reference(
        self, consumer: str, service: Service, key: str, value: ValueFrom
    ) -> None:
        assert value.service is not None
        where = f"service '{consumer}' env '{key}'"
        target = self.services.get(value.service)
        if target is None:
            raise ValueError(f"{where} references undeclared service '{value.service}'")
        if value.service == consumer:
            raise ValueError(f"{where} cannot reference itself")
        # Container names resolve only through the team network's Docker DNS,
        # which a host-network container neither queries nor joins.
        if service.host_mode:
            raise ValueError(
                f"host-mode {where} cannot reference service '{value.service}'; "
                "only team-network services can resolve sibling container names"
            )
        if target.host_mode:
            raise ValueError(
                f"{where} cannot reference host-mode service '{value.service}'"
            )


class StackManifest(StackModel):
    api_version: Literal["oduflow.dev/v1alpha1"] = "oduflow.dev/v1alpha1"
    kind: Literal["Stack"] = "Stack"
    metadata: Metadata
    spec: StackSpec
