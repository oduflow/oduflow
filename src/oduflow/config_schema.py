"""Declarative registry of the oduflow.toml keys the Server settings console edits.

``Settings.from_raw`` stays the one authoritative parser: the console never
decides on its own whether a document is valid, it dry-runs the candidate
through that parser (see :mod:`oduflow.config_store`). This registry only adds
what a parser cannot know — how to render a key, how to coerce browser input
into the right TOML type, which values are secrets, and what it takes for a
change to reach the running server:

``live``
    Read from the current Settings on every request or sweep; the console
    swaps the cached Settings right after saving.
``restart``
    Consumed at startup (auth provider, threads, Traefik/agent/quota
    reconciliation, route registration). Saved now, applied by a restart.
``recreate``
    Frozen into containers when they are created (Traefik arguments, routing
    labels). A restart reconciles the shared infrastructure, but existing
    environments, services and productions keep the old value until each is
    recreated.
``locked``
    Changing the key in the file alone breaks the deployment (the PostgreSQL
    role password lives inside the cluster, data moves with data_dir). The
    console shows it read-only and says what to do instead.

See specs/0077-server-settings-console.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from oduflow.settings import (
    DEFAULT_AGENT_IMAGE,
    DEFAULT_OPENCODE_API_KEY_ENV,
    DEFAULT_OPENCODE_BASE_URL_ENV,
    DEFAULT_POSTGRES_IMAGE,
    ENV_VAR_NAME_RE,
    Settings,
)

LIVE = "live"
RESTART = "restart"
RECREATE = "recreate"
LOCKED = "locked"

# Severity order: a save is classified by its most demanding change.
APPLY_ORDER = (LIVE, RESTART, RECREATE, LOCKED)

# Value kinds understood by coerce() and the console's form renderer.
KINDS = frozenset(
    {
        "str",
        "secret",
        "int",
        "float",
        "bool",
        "enum",
        "port_range",
        "str_list",
        "tls",
        "env_map",
    }
)

# Collection member names ([team.<id>], [route.<name>]) end up in data paths,
# Docker names and Traefik router ids, so keep them to a conservative alphabet.
COLLECTION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


@dataclass(frozen=True)
class Field:
    """One TOML key."""

    key: str
    kind: str
    label: str
    help: str = ""
    default: Any = None
    choices: tuple[str, ...] = ()
    apply: str = RESTART
    # Consequence or caveat shown next to the apply badge.
    note: str = ""
    minimum: float | None = None
    required: bool = False
    advanced: bool = False
    # Legacy spellings of the same key; removed when this one is written so the
    # parser never sees both.
    aliases: tuple[str, ...] = ()
    # A live key that still needs a restart in some boot states (e.g. the
    # reaper thread only exists when lifecycle was enabled at startup).
    restart_if: Callable[[Settings], bool] | None = None
    placeholder: str = ""

    @property
    def secret(self) -> bool:
        return self.kind == "secret" or self.kind == "env_map"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "help": self.help,
            "default": self.default,
            "apply": self.apply,
            "note": self.note,
            "required": self.required,
            "advanced": self.advanced,
            "placeholder": self.placeholder,
        }
        if self.choices:
            out["choices"] = list(self.choices)
        if self.minimum is not None:
            out["minimum"] = self.minimum
        return out


@dataclass(frozen=True)
class Group:
    """One TOML table (``("server",)``) or collection (``("team", "*")``)."""

    id: str
    title: str
    table: tuple[str, ...]
    fields: tuple[Field, ...]
    description: str = ""
    # Presence of the table enables a feature ([backup], image_registry).
    optional: bool = False
    # Apply class of adding/removing the table (or a collection member).
    presence_apply: str = RESTART
    presence_note: str = ""

    @property
    def collection(self) -> bool:
        return "*" in self.table

    def field(self, key: str) -> Field | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "table": list(self.table),
            "description": self.description,
            "optional": self.optional,
            "collection": self.collection,
            "presence_apply": self.presence_apply,
            "presence_note": self.presence_note,
            "fields": [f.to_json() for f in self.fields],
        }


def _lifecycle_off_at_boot(s: Settings) -> bool:
    return (
        s.auto_stop_hours <= 0 and s.auto_delete_hours <= 0 and s.prod_purge_hours <= 0
    )


def _ui_auth_off_at_boot(s: Settings) -> bool:
    return not any(t.ui_password for t in s.teams.values())


_PUBLIC_SCHEME_HELP = (
    "Scheme of every URL Oduflow hands out. Empty derives it from the routing "
    "mode (https with Traefik, http in port mode). Set http only when nothing "
    "terminates TLS in front of a tls = false deployment."
)

GROUPS: tuple[Group, ...] = (
    Group(
        id="server",
        title="Server",
        table=("server",),
        description="The Oduflow process itself: listener and safety switches.",
        fields=(
            Field(
                "bind",
                "str",
                "Listen address",
                "Interface the HTTP server binds to. Ignored in traefik mode, "
                "which always listens on 0.0.0.0 behind the proxy.",
                default="0.0.0.0",
                aliases=("host",),
            ),
            Field("port", "int", "Port", "HTTP port.", default=8000, minimum=1),
            Field(
                "allow_local_path",
                "bool",
                "Allow local_path",
                "Let environments live-mount a host directory. Meant for trusted "
                "single-user local development; disable on hosted servers.",
                default=True,
                apply=LIVE,
            ),
            Field(
                "allow_insecure_http",
                "bool",
                "Allow unauthenticated HTTP",
                "Serve /mcp and the dashboard with NO authentication. Only "
                "behind your own auth proxy.",
                default=False,
            ),
            Field(
                "trace",
                "bool",
                "Trace logging",
                "Verbose tracing for git analysis and environment operations.",
                default=False,
                apply=LIVE,
            ),
            Field(
                "disable_telemetry",
                "bool",
                "Disable telemetry",
                "Stop anonymous usage telemetry.",
                default=False,
                apply=LIVE,
            ),
            Field(
                "agent_feedback",
                "bool",
                "Agent feedback tool",
                "Let coding agents report friction with the Oduflow MCP tools.",
                default=False,
                advanced=True,
            ),
        ),
    ),
    Group(
        id="routing",
        title="Routing",
        table=("routing",),
        description="How environments, services and dashboards are reached.",
        fields=(
            Field(
                "mode",
                "enum",
                "Mode",
                "port: every environment on its own host port. traefik: "
                "hostnames behind the managed Traefik proxy.",
                default="port",
                choices=("port", "traefik"),
                apply=RECREATE,
                note="Existing environments, services and productions keep "
                "their old routing until each is recreated.",
            ),
            Field(
                "tls",
                "tls",
                "TLS",
                "Traefik only. true: Traefik terminates TLS with Let's Encrypt "
                "on every managed route. {}: same listener and redirect with "
                "Traefik's default certificate (local/test). false: plain HTTP "
                "on :80 behind a TLS-terminating upstream.",
                default="true",
                choices=("true", "{}", "false"),
                apply=RECREATE,
                note="Routing labels are fixed at creation: recreate existing "
                "environments and services after the restart.",
            ),
            Field(
                "acme_email",
                "str",
                "ACME e-mail",
                "Let's Encrypt account e-mail. Required with tls = true.",
                default="",
                placeholder="admin@example.com",
                note="Turning ACME on or off recreates Traefik on restart; "
                "changing only the address needs a manual Traefik recreate.",
            ),
            Field(
                "public_scheme",
                "enum",
                "Public URL scheme",
                _PUBLIC_SCHEME_HELP,
                default="",
                choices=("", "http", "https"),
            ),
        ),
    ),
    Group(
        id="routes",
        title="Extra routes",
        table=("route", "*"),
        description="Traefik only. Forward an external hostname to an upstream "
        "URL for a service Oduflow does not manage. http://127.0.0.1 means "
        "the Docker host.",
        presence_apply=RESTART,
        presence_note="Traefik's dynamic config is rewritten on restart.",
        fields=(
            Field(
                "host",
                "str",
                "Hostname",
                "Incoming Host header to match.",
                required=True,
                placeholder="api.example.com",
            ),
            Field(
                "url",
                "str",
                "Upstream URL",
                "Where matching requests are forwarded.",
                required=True,
                placeholder="http://127.0.0.1:3000",
            ),
        ),
    ),
    Group(
        id="database",
        title="Database",
        table=("database",),
        description="The shared PostgreSQL clusters. These values are baked "
        "into the running clusters, so the console shows them read-only.",
        fields=(
            Field(
                "user",
                "str",
                "Superuser",
                default="odoo",
                apply=LOCKED,
                note="Baked into the PostgreSQL containers; changing it here "
                "alone breaks every connection.",
            ),
            Field(
                "password",
                "secret",
                "Superuser password",
                default="odoo",
                apply=LOCKED,
                note="The password lives inside the cluster. Rotate it with "
                "ALTER ROLE first, then update the file in Raw TOML.",
            ),
            Field(
                "image",
                "str",
                "PostgreSQL image",
                default=DEFAULT_POSTGRES_IMAGE,
                apply=LOCKED,
                note="Existing clusters are never recreated from a new image; "
                "a major version change needs the documented migration.",
            ),
        ),
    ),
    Group(
        id="storage",
        title="Storage",
        table=("storage",),
        fields=(
            Field(
                "data_dir",
                "str",
                "Data directory",
                "Root of every team's workspaces, templates and registries.",
                default="",
                apply=LOCKED,
                note="Moving it needs the data to move too; edit it in Raw TOML "
                "only after relocating the directory.",
            ),
            Field(
                "overlay_threshold_mb",
                "int",
                "Overlay threshold (MB)",
                "Template filestores above this size are mounted with "
                "fuse-overlayfs instead of copied.",
                default=50,
                minimum=0,
                apply=LIVE,
                note="Applies to templates saved from now on.",
            ),
        ),
    ),
    Group(
        id="lifecycle",
        title="Lifecycle",
        table=("lifecycle",),
        description="Automatic management of idle and stopped environments. "
        "0 disables a behaviour; protected environments are always exempt.",
        fields=(
            Field(
                "auto_stop_hours",
                "int",
                "Auto-stop after (hours idle)",
                "Stop idle environments. Non-destructive.",
                default=48,
                minimum=0,
                apply=LIVE,
                restart_if=_lifecycle_off_at_boot,
            ),
            Field(
                "auto_delete_hours",
                "int",
                "Auto-delete after (hours stopped)",
                "DESTRUCTIVE: deletes the database and workspace of environments "
                "stopped this long.",
                default=0,
                minimum=0,
                apply=LIVE,
                restart_if=_lifecycle_off_at_boot,
            ),
            Field(
                "prod_purge_hours",
                "int",
                "Purge deleted productions after (hours)",
                "DESTRUCTIVE: removes the database and files a production "
                "deletion kept.",
                default=0,
                minimum=0,
                apply=LIVE,
                restart_if=_lifecycle_off_at_boot,
            ),
        ),
    ),
    Group(
        id="agent",
        title="Coding agent",
        table=("agent",),
        description="Deployment-wide bits of the per-team coding agent. "
        "Enable the agent and set its credentials per team.",
        fields=(
            Field(
                "image",
                "str",
                "Agent image",
                default=DEFAULT_AGENT_IMAGE,
                note="Agent containers are recreated on restart.",
            ),
            Field(
                "claude_model",
                "str",
                "Claude model",
                "Empty = the CLI default.",
                default="",
                apply=LIVE,
            ),
            Field(
                "codex_model",
                "str",
                "Codex model",
                "Empty = the CLI default.",
                default="",
                apply=LIVE,
            ),
            Field(
                "opencode_model",
                "str",
                "OpenCode model",
                "provider/model; empty = the OpenCode default.",
                default="",
                apply=LIVE,
            ),
        ),
    ),
    Group(
        id="agent_opencode_provider",
        title="OpenCode provider",
        table=("agent", "opencode_provider"),
        optional=True,
        description="A custom OpenAI-compatible provider (Chat Completions) "
        "for OpenCode, e.g. a LiteLLM gateway. The base URL and the key stay "
        "in each team's agent environment; only their variable names are "
        "set here. Select its models as <id>/<model> in OpenCode model.",
        presence_apply=LIVE,
        fields=(
            Field(
                "id",
                "str",
                "Provider id",
                "Lowercase letters, digits, - and _; not anthropic, google, "
                "openai, opencode or openrouter.",
                required=True,
                apply=LIVE,
                placeholder="litellm",
            ),
            Field(
                "models",
                "str_list",
                "Models",
                "One model id per line, as the gateway names them.",
                required=True,
                apply=LIVE,
            ),
            Field(
                "name",
                "str",
                "Display name",
                "Empty = the provider id.",
                default="",
                apply=LIVE,
            ),
            Field(
                "base_url_env",
                "str",
                "Base URL variable",
                "Agent environment variable with the base URL, including /v1.",
                default=DEFAULT_OPENCODE_BASE_URL_ENV,
                apply=LIVE,
            ),
            Field(
                "api_key_env",
                "str",
                "API key variable",
                "Agent environment variable with the API key.",
                default=DEFAULT_OPENCODE_API_KEY_ENV,
                apply=LIVE,
            ),
        ),
    ),
    Group(
        id="production",
        title="Production",
        table=("production",),
        description="Production hosting: long-lived customer environments on a "
        "dedicated PostgreSQL cluster. Productions themselves are created from "
        "the dashboard or MCP.",
        fields=(
            Field(
                "enabled",
                "bool",
                "Production hosting",
                default=False,
            ),
            Field(
                "workers_cap",
                "int",
                "Workers cap",
                "Upper bound for auto-tuned Odoo workers.",
                default=8,
                minimum=1,
                apply=LIVE,
                note="Existing productions pick it up on reconfigure.",
            ),
            Field(
                "odumcp_repo_url",
                "str",
                "Production MCP addon repo",
                default="https://github.com/oduflow/oduflow-client-addons.git",
                apply=LIVE,
                advanced=True,
                note="Used for new checkouts only.",
            ),
            Field(
                "odumcp_ref",
                "str",
                "Production MCP addon ref",
                default="19.0",
                apply=LIVE,
                advanced=True,
                note="Used for new checkouts only.",
            ),
            Field(
                "walg_version",
                "str",
                "WAL-G version",
                "Empty = the version Oduflow pins.",
                default="",
                advanced=True,
            ),
        ),
    ),
    Group(
        id="production_wal",
        title="WAL safety",
        table=("production", "wal"),
        description="Cluster-wide disk protection for the production cluster, "
        "active while production hosting is enabled.",
        fields=(
            Field(
                "upload_timeout",
                "int",
                "Upload timeout (s)",
                "Seconds per wal-push before it is terminated.",
                default=120,
                minimum=1,
                note="Built into archive_command when the cluster is reconciled.",
            ),
            Field(
                "warn_after",
                "int",
                "Warn after (s)",
                "Seconds without archive progress while WAL is queued.",
                default=120,
                minimum=1,
                apply=LIVE,
            ),
            Field(
                "stall_after",
                "int",
                "Stall after (s)",
                "Must be at least warn_after.",
                default=300,
                minimum=1,
                apply=LIVE,
            ),
            Field(
                "stop_free_gb",
                "float",
                "Stop below free (GiB)",
                "Stop productions when less space than this is available.",
                default=2.0,
                apply=LIVE,
            ),
            Field(
                "resume_free_gb",
                "float",
                "Resume above free (GiB)",
                "Must exceed stop_free_gb.",
                default=4.0,
                apply=LIVE,
            ),
            Field(
                "stop_within",
                "int",
                "Stop within (s)",
                "Stop when the estimated time to the reserve is this short.",
                default=300,
                minimum=1,
                apply=LIVE,
            ),
            Field(
                "warn_queue_gb",
                "float",
                "Warn queue (GiB)",
                "Warn on this much queued unarchived WAL.",
                default=2.0,
                apply=LIVE,
            ),
            Field(
                "stop_queue_gb",
                "float",
                "Stop queue (GiB)",
                "Stop productions at this much queued WAL; must exceed the "
                "warning level.",
                default=8.0,
                apply=LIVE,
            ),
        ),
    ),
    Group(
        id="backup",
        title="Backups",
        table=("backup",),
        optional=True,
        description="S3 backups for productions: continuous WAL archiving, "
        "scheduled snapshots and retention. Requires bucket and keys.",
        presence_apply=RESTART,
        presence_note="The backup scheduler and WAL-G are configured on restart.",
        fields=(
            Field("bucket", "str", "Bucket", required=True),
            Field("access_key", "secret", "Access key", required=True),
            Field("secret_key", "secret", "Secret key", required=True),
            Field(
                "endpoint",
                "str",
                "Endpoint",
                "Empty = AWS. Set for MinIO / R2 / other S3-compatible stores.",
                default="",
                placeholder="https://s3.example.com",
            ),
            Field("region", "str", "Region", default=""),
            Field(
                "prefix",
                "str",
                "Prefix",
                "Key prefix inside the bucket.",
                default="oduflow",
                note="Changing it starts a new backup history.",
            ),
            Field(
                "snapshot_time",
                "str",
                "Snapshot time",
                "Daily per-production snapshots, server-local HH:MM.",
                default="02:00",
                apply=LIVE,
            ),
            Field(
                "basebackup_time",
                "str",
                "Base backup time",
                "Daily WAL-G base backup, server-local HH:MM.",
                default="03:30",
                apply=LIVE,
            ),
            Field(
                "keep",
                "str_list",
                "Snapshot retention",
                'One "interval_days:age_days" pair per line.',
                default=["30:180", "7:30", "1:7"],
                apply=LIVE,
            ),
            Field(
                "walg_keep_full",
                "int",
                "WAL-G base backups kept",
                default=7,
                minimum=1,
                apply=LIVE,
            ),
            Field(
                "upload_threads",
                "int",
                "Upload threads",
                "Parallel filestore chunk uploads; each costs ~4 MiB of RAM.",
                default=16,
                minimum=1,
                apply=LIVE,
            ),
        ),
    ),
    Group(
        id="team",
        title="Teams",
        table=("team", "*"),
        description="Each team gets isolated workspaces, templates, "
        "credentials, services and dashboard.",
        presence_apply=RESTART,
        presence_note="A new team's directories, network, Traefik route and "
        "OAuth client are created on restart. Removing a team leaves its data "
        "directory and containers behind.",
        fields=(
            Field(
                "hostname",
                "str",
                "Hostname",
                "Dashboard and OAuth host of the team; unique.",
                required=True,
                placeholder="dev.example.com",
                note="Existing environment and service hostnames keep the old "
                "name until recreated.",
            ),
            Field(
                "base_domain",
                "str",
                "Base domain",
                "Traefik only. The team's DNS zone: environments, services and "
                "productions live directly under it.",
                default="",
                placeholder="demo.example.com",
            ),
            Field(
                "ui_password",
                "secret",
                "Dashboard password",
                "Team members sign in to the dashboard with it.",
                required=True,
                apply=LIVE,
                restart_if=_ui_auth_off_at_boot,
                note="Signs every team member out at once.",
            ),
            Field(
                "auth_token",
                "secret",
                "MCP token",
                "Bearer token / OAuth client secret for /mcp.",
                note="Already-issued OAuth tokens stay valid until they expire.",
            ),
            Field(
                "production_token",
                "secret",
                "Production MCP token",
                "Separate 32+ character Bearer key for /production.",
                default="",
                note="Productions keep the old key until sync_production_mcp runs.",
            ),
            Field(
                "port_range",
                "port_range",
                "Port range",
                "Host ports for Odoo containers [start, end); must not overlap "
                "another team.",
                default=[50000, 50100],
                apply=LIVE,
                note="Ports already published stay until the environment is recreated.",
            ),
            Field(
                "environment_slots",
                "int",
                "Environment slots",
                "Maximum concurrent environments; 0 = unlimited.",
                default=20,
                minimum=0,
                apply=LIVE,
            ),
            Field(
                "environment_hostname_mode",
                "enum",
                "Environment hostnames",
                "branch: feature.dev.example.com. slots: dev1..devN (traefik "
                "only, needs environment slots).",
                default="branch",
                choices=("branch", "slots"),
                apply=LIVE,
                note="Existing environments keep their hostnames.",
            ),
            Field(
                "service_slots",
                "int",
                "Service slots",
                "Maximum managed auxiliary services; 0 = unlimited.",
                default=10,
                minimum=0,
                apply=LIVE,
            ),
            Field(
                "db_quota_gb",
                "int",
                "Database quota (GB)",
                "Combined size cap of the team's databases; 0 = off.",
                default=50,
                minimum=0,
                apply=LIVE,
            ),
            Field(
                "disk_quota_gb",
                "int",
                "Disk quota (GB)",
                "Cap on team files + databases via XFS project quotas; 0 = off.",
                default=0,
                minimum=0,
                note="Applied on restart; setting 0 does not lift an existing "
                "kernel limit.",
            ),
            Field(
                "public_scheme",
                "enum",
                "Public URL scheme",
                "Per-team override of the routing public scheme; empty = global.",
                default="",
                choices=("", "http", "https"),
            ),
            Field(
                "agent_enabled",
                "bool",
                "Coding agent",
                "Per-team agent container (dashboard Agent Chat / Agent CLI).",
                default=False,
                note="The agent container is created or removed on restart.",
            ),
            Field(
                "agent_default",
                "enum",
                "Default agent",
                default="claude",
                choices=("claude", "codex", "opencode"),
                apply=LIVE,
            ),
            Field(
                "agent_env",
                "env_map",
                "Agent environment",
                "Variables injected into the team's agent container: provider "
                "credentials (CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, "
                "OPENAI_API_KEY, OPENCODE_API_KEY) and anything custom.",
                default={},
                note="The agent container is recreated on restart.",
            ),
        ),
    ),
    Group(
        id="image_registry",
        title="Image registry",
        table=("team", "*", "image_registry"),
        optional=True,
        description="Enables the image build/publish MCP tools for the team. "
        "Agents can publish only below the repository prefix.",
        presence_apply=LIVE,
        fields=(
            Field(
                "repository_prefix",
                "str",
                "Repository prefix",
                required=True,
                apply=LIVE,
                placeholder="acme",
            ),
            Field("host", "str", "Registry host", default="docker.io", apply=LIVE),
            Field(
                "username",
                "str",
                "Username",
                "Set together with the token, or leave both empty to use the "
                "host's docker login.",
                default="",
                apply=LIVE,
            ),
            Field("token", "secret", "Token", default="", apply=LIVE),
            Field(
                "build_timeout_seconds",
                "int",
                "Build timeout (s)",
                default=1800,
                minimum=1,
                apply=LIVE,
            ),
            Field(
                "max_context_mb",
                "int",
                "Max context (MB)",
                default=512,
                minimum=1,
                apply=LIVE,
            ),
            Field(
                "max_log_mb", "int", "Max log (MB)", default=16, minimum=1, apply=LIVE
            ),
            Field(
                "max_concurrent_builds",
                "int",
                "Concurrent builds",
                default=2,
                minimum=1,
                apply=LIVE,
            ),
            Field(
                "keep_images",
                "int",
                "Staging images kept",
                "0 disables pruning.",
                default=10,
                minimum=0,
                apply=LIVE,
            ),
        ),
    ),
    Group(
        id="admin",
        title="Console access",
        table=("admin",),
        description="The credential of this console. It unlocks every team's "
        "secrets, so keep it separate from all team passwords.",
        fields=(
            Field(
                "password",
                "secret",
                "Console password",
                "At least 12 characters. Empty disables the console.",
                required=True,
                apply=LIVE,
                note="Signs you out of this console; sign in with the new one.",
            ),
        ),
    ),
)


def group_by_id(group_id: str) -> Group | None:
    for group in GROUPS:
        if group.id == group_id:
            return group
    return None


def _matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    return len(pattern) == len(path) and all(
        p == "*" or p == part for p, part in zip(pattern, path)
    )


def find_group(table_path: tuple[str, ...]) -> Group | None:
    """The group whose table is exactly ``table_path`` (wildcards allowed)."""
    for group in GROUPS:
        if _matches(group.table, table_path):
            return group
    return None


def find_field(path: tuple[str, ...]) -> tuple[Group, Field] | None:
    """Schema entry for a key path such as ``("team", "2", "hostname")``."""
    if len(path) < 2:
        return None
    group = find_group(path[:-1])
    if group is None:
        return None
    f = group.field(path[-1])
    return (group, f) if f is not None else None


def is_secret_path(path: tuple[str, ...]) -> bool:
    """Whether a key path holds a credential (masked in diffs and payloads).

    Keys outside the registry are judged by name, so a credential the console
    does not know about (a future key, a typo) is still never echoed.
    """
    hit = find_field(path)
    if hit is not None:
        return hit[1].secret
    if len(path) >= 3 and path[-2] == "agent_env":
        return True
    return bool(re.search(r"(password|token|secret|key)", path[-1], re.I))


def schema_json() -> list[dict[str, Any]]:
    return [group.to_json() for group in GROUPS]


class CoercionError(ValueError):
    """Browser input that cannot become the key's TOML type."""


def coerce(f: Field, value: Any) -> Any:
    """Turn a JSON value from the console into the Python value written to TOML.

    Returns ``None`` for "remove the key" (an empty optional string, so the
    parser's own default applies — the file only ever carries deliberate
    values). Range and cross-key rules stay with the parser.
    """
    kind = f.kind
    label = f.label
    if kind in ("str", "secret"):
        if value is None:
            return None
        if not isinstance(value, str):
            raise CoercionError(f"{label}: expected text")
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise CoercionError(f"{label}: must be a single line")
        return text or None
    if kind == "enum":
        text = "" if value is None else str(value).strip()
        if text not in f.choices:
            raise CoercionError(f"{label}: must be one of {', '.join(f.choices)}")
        return text or None
    if kind == "bool":
        if not isinstance(value, bool):
            raise CoercionError(f"{label}: expected true or false")
        return value
    if kind == "int":
        number = _as_int(value, label)
        if f.minimum is not None and number < f.minimum:
            raise CoercionError(f"{label}: must be >= {int(f.minimum)}")
        return number
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise CoercionError(f"{label}: expected a number")
        try:
            result = float(value)
        except ValueError:
            raise CoercionError(f"{label}: expected a number") from None
        if result != result or result in (float("inf"), float("-inf")):
            raise CoercionError(f"{label}: expected a finite number")
        return result
    if kind == "port_range":
        if not isinstance(value, list) or len(value) != 2:
            raise CoercionError(f"{label}: expected [start, end]")
        start, end = (_as_int(v, label) for v in value)
        if not (1 <= start < end <= 65535):
            raise CoercionError(f"{label}: expected 1 <= start < end <= 65535")
        return [start, end]
    if kind == "str_list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise CoercionError(f"{label}: expected a list of text values")
        items = [v.strip() for v in value if v.strip()]
        return items or None
    if kind == "tls":
        text = str(value).strip()
        if text == "true":
            return True
        if text == "false":
            return False
        if text == "{}":
            return {}
        raise CoercionError(f"{label}: must be true, false or {{}}")
    if kind == "env_map":
        if not isinstance(value, dict):
            raise CoercionError(f"{label}: expected NAME = value pairs")
        out: dict[str, str] = {}
        for name, item in value.items():
            name = str(name).strip()
            if not ENV_VAR_NAME_RE.match(name):
                raise CoercionError(f"{label}: invalid variable name {name!r}")
            if not isinstance(item, str):
                raise CoercionError(f"{label}: {name} must be text")
            if "\n" in item or "\r" in item:
                raise CoercionError(f"{label}: {name} must be a single line")
            out[name] = item
        return out or None
    raise CoercionError(f"{label}: unsupported kind {kind}")


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise CoercionError(f"{label}: expected a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    raise CoercionError(f"{label}: expected a whole number")


def display_value(f: Field, value: Any) -> Any:
    """A raw TOML value in the shape the console form edits."""
    if value is None:
        return None
    if f.kind == "tls":
        if value is True:
            return "true"
        if value is False:
            return "false"
        return "{}"
    return value
