"""Typed schema for declarative Oduflow Stack manifests."""

from __future__ import annotations

import posixpath
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from oduflow.naming import (
    parse_service_command,
    validate_env_hostname,
    validate_env_name,
    validate_template_name,
)
from oduflow.service_runtime import ServiceRuntime

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


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class StackModel(BaseModel):
    """Strict base model with a YAML-friendly camelCase public shape."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
        strict=True,
    )


class Metadata(StackModel):
    name: ResourceName


class ValueFrom(StackModel):
    """A value loaded at apply time rather than persisted in the manifest."""

    from_env: str | None = None
    environment_field: Literal["url", "token"] | None = None
    database: ServiceDatabaseName | None = None
    database_field: (
        Literal["url", "host", "port", "database", "username", "password"] | None
    ) = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> ValueFrom:
        sources = sum(
            value is not None
            for value in (self.from_env, self.environment_field, self.database)
        )
        if sources != 1:
            raise ValueError(
                "set exactly one of fromEnv, environmentField, or database"
            )
        if (self.database is None) != (self.database_field is None):
            raise ValueError("database and databaseField must be set together")
        if self.from_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.from_env
        ):
            raise ValueError("fromEnv must be a valid environment-variable name")
        return self


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


class Volume(StackModel):
    description: str = ""


class ServiceDatabase(StackModel):
    """An empty managed PostgreSQL database; credentials are generated at apply."""

    pass


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


class Service(StackModel):
    runtime: ServiceRuntime = Field(default_factory=ServiceRuntime)
    image: NonEmptyString
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


class StackSpec(StackModel):
    environment: Environment
    extra_repositories: dict[ExtraRepositoryName, ExtraRepository] = Field(
        default_factory=dict
    )
    volumes: dict[ResourceName, Volume] = Field(default_factory=dict)
    databases: dict[ServiceDatabaseName, ServiceDatabase] = Field(default_factory=dict)
    files: list[VolumeFile] = Field(default_factory=list)
    services: dict[ResourceName, Service] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references_exist(self) -> StackSpec:
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
        for key, value in self.environment.env.items():
            if isinstance(value, ValueFrom) and value.database is not None:
                raise ValueError(
                    f"environment env '{key}' cannot reference a managed database; "
                    "database value sources are only available to auxiliary services"
                )
        return self


class StackManifest(StackModel):
    api_version: Literal["oduflow.dev/v1alpha1"] = "oduflow.dev/v1alpha1"
    kind: Literal["Stack"] = "Stack"
    metadata: Metadata
    spec: StackSpec
