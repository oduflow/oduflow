"""Configuration loading from TOML with per-team isolation."""

from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

logger = logging.getLogger("oduflow")

TRACE: bool = False

DEFAULT_AGENT_IMAGE = "oduist/oduflow-coder:0.3.0"
_LEGACY_AGENT_IMAGE = "oduist/oduflow-coder:latest"

# Active MCP transport for the running server ("stdio" | "http").
# Set in server.main() before the server starts. Mostly informational;
# local_path is gated by the allow_local_path setting.
TRANSPORT: str = "stdio"


def _normalize_agent_image(value: object) -> str:
    image = str(value or "").strip() or DEFAULT_AGENT_IMAGE
    if image == _LEGACY_AGENT_IMAGE:
        logger.warning(
            "[agent].image %s is deprecated; using pinned image %s",
            _LEGACY_AGENT_IMAGE,
            DEFAULT_AGENT_IMAGE,
        )
        return DEFAULT_AGENT_IMAGE
    return image


# One path component of an OCI repository name; components are joined by "/".
_OCI_REPO_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)
_REGISTRY_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?$")


@dataclass(frozen=True)
class ImageRegistrySettings:
    """Container registry configuration ([team.X.image_registry] TOML section).

    Presence of the section enables the image build/publish MCP tools for the
    team; teams without it are refused with a clear prerequisite error. The
    agent can publish only below ``repository_prefix`` on ``host`` — that prefix
    is the authorization boundary. Credentials are optional: when ``username``
    and ``token`` are set, pushes send request-scoped auth to the Docker API;
    otherwise the host Docker daemon's ambient credentials (``docker login``)
    are used.
    """

    repository_prefix: str  # e.g. "acme" — namespace all published repos live under
    host: str = "docker.io"
    username: str = ""
    # Registry token/password from the Oduflow config. It is sent only as
    # request-scoped Docker API auth and is never persisted in build jobs.
    token: str = field(default="", repr=False)
    build_timeout_seconds: int = 1800
    max_context_mb: int = 512
    max_log_mb: int = 16
    max_concurrent_builds: int = 2
    # Local staging images kept per team (oduflow-build/team-<id>:<build-id>);
    # older ones are untagged after each successful build. 0 disables pruning.
    keep_images: int = 10


@dataclass(frozen=True)
class TeamSettings:
    """Per-team settings (isolated workspaces, templates, credentials, ports)."""

    team_id: str
    hostname: str = "localhost"
    # The team's DNS zone (e.g. "demo.example.com"). When set, environments
    # and services live directly under it (feature.demo.example.com), the
    # dashboard hostname defaults to "oduflow.<base_domain>", and production
    # domains must be the base domain itself (apex) or a subdomain of it —
    # extra_domains cover arbitrary client-owned names. Empty = legacy layout:
    # environments/services nest under the team hostname.
    base_domain: str = ""
    auth_token: str = ""
    ui_password: str = ""
    port_range_start: int = 50000
    port_range_end: int = 50100
    data_dir: str = ""
    port_registry_path: str = ""
    hostname_registry_path: str = ""
    # Hard cap on managed development environments. 0 disables the cap.
    environment_slots: int = 20
    # Public hostname strategy: "branch" keeps feature.dev.example.com;
    # "slots" allocates dev1.example.com..devN.example.com in Traefik mode.
    environment_hostname_mode: str = "branch"
    # Hard cap on managed auxiliary service containers. 0 disables the cap.
    service_slots: int = 10
    # Quotas; 0 disables. db_quota_gb caps the combined size of the team's
    # PostgreSQL databases (environments + templates) and is checked before
    # operations that create a new database. disk_quota_gb caps the team's
    # data dir plus its PG tablespace via XFS project quotas (see quotas.py);
    # on filesystems without project-quota support it stays informational.
    db_quota_gb: int = 50
    disk_quota_gb: int = 0
    # Coding agent (dashboard "Agent Chat" / "Agent CLI"). Opt-in per team:
    # it is a hosting feature (clients driving their environments from the
    # browser); local developers use their own agents, so it defaults to off.
    # agent_env holds the variables injected into the team's agent container
    # (provider credentials such as CLAUDE_CODE_OAUTH_TOKEN / OPENAI_API_KEY),
    # from the [team.X.agent_env] TOML table. Config is the source of truth:
    # the container is recreated automatically when these change (see
    # env_ops._ensure_agent_container). See specs/0029-agent-console-and-chat.md.
    agent_enabled: bool = False
    agent_default: str = "claude"  # which agent consoles/chats open by default
    agent_env: dict[str, str] = field(default_factory=dict)
    # Container image building ([team.X.image_registry]); None = the image
    # build/publish MCP tools are unavailable for this team.
    image_registry: ImageRegistrySettings | None = None
    # Raw per-team ``public_scheme`` override; empty falls back to the global
    # [routing] value. Read the resolved scheme via
    # :meth:`Settings.public_scheme_for`, not this field. Lets one deployment
    # mix teams reached over plain HTTP (e.g. LAN) with teams fronted by an
    # upstream TLS terminator such as a Cloudflare tunnel (https).
    public_scheme_setting: str = ""

    @property
    def workspaces_dir(self) -> str:
        return os.path.join(self.data_dir, "workspaces")

    @property
    def shared_repos_dir(self) -> str:
        return os.path.join(self.data_dir, "shared_repos")

    @property
    def shared_extra_checkouts_dir(self) -> str:
        """Persistent immutable extra-addons checkouts, keyed by repo/SHA."""
        return os.path.join(self.data_dir, "shared_extra_checkouts")

    def get_template_dir(self, template_name: str) -> str:
        # Reject names that could escape the templates directory (path
        # traversal) before they reach rmtree / file writes.
        from oduflow.naming import validate_template_name

        validate_template_name(template_name)
        return os.path.join(self.data_dir, "templates", template_name)

    def get_template_sql_path(self, template_name: str) -> str:
        tpl_dir = self.get_template_dir(template_name)
        for name in ("dump.pgdump", "dump.sql", "dump.pgdump.gz", "dump.sql.gz"):
            path = os.path.join(tpl_dir, name)
            if os.path.isfile(path):
                return path
        return os.path.join(tpl_dir, "dump.pgdump")

    def get_template_filestore_path(self, template_name: str) -> str:
        return os.path.join(self.get_template_dir(template_name), "filestore")

    def get_template_metadata_path(self, template_name: str) -> str:
        return os.path.join(self.get_template_dir(template_name), "metadata.json")

    def get_import_staging_dir(self, template_name: str) -> str:
        """Where a push-based Odoo.sh import stages its upload before finalize.

        Kept outside ``templates/`` so a partial upload never masquerades as (or
        clobbers) a live template; finalize swaps it into place atomically.
        """
        from oduflow.naming import validate_template_name

        validate_template_name(template_name)
        return os.path.join(self.data_dir, "import_staging", template_name)

    def list_templates(self) -> list[str]:
        templates_dir = os.path.join(self.data_dir, "templates")
        if not os.path.isdir(templates_dir):
            return []
        return sorted(
            entry
            for entry in os.listdir(templates_dir)
            if os.path.isdir(os.path.join(templates_dir, entry))
        )

    def git_credentials_file(self) -> str:
        return os.path.join(self.data_dir, ".git-credentials")

    def ssh_dir(self) -> str:
        """Team SSH deploy key directory (keypair + known_hosts).

        git_ops derives this path from the credential file's directory, so it
        must stay a sibling of :meth:`git_credentials_file`.
        """
        return os.path.join(self.data_dir, "ssh")


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_KEEP_PAIR_RE = re.compile(r"^\d+:\d+$")


@dataclass(frozen=True)
class BackupSettings:
    """S3 backup configuration for production hosting ([backup] TOML section).

    Presence of the section (bucket + access_key + secret_key) enables the
    whole backup subsystem: WAL-G continuous archiving of the production
    PostgreSQL cluster, scheduled per-production snapshots (pg_dump +
    filestore chunkstore revision), and retention/prune. Everything beyond
    the three required keys has sensible defaults.
    """

    bucket: str
    access_key: str
    secret_key: str
    # Empty endpoint = AWS S3. Set for MinIO / Cloudflare R2 / other
    # S3-compatible stores (implies path-style addressing).
    endpoint: str = ""
    region: str = ""
    prefix: str = "oduflow"
    # Daily schedules, server-local time "HH:MM".
    snapshot_time: str = "02:00"
    basebackup_time: str = "03:30"
    # Snapshot retention: "interval_days:older_than_days" pairs, duplicacy
    # -keep style, largest age first. Default: keep everything younger than
    # 7 days, one per day up to 30 days, one per week up to a year, one per
    # month beyond that is not kept (0:365 would drop older ones entirely).
    keep: tuple[str, ...] = ("30:180", "7:30", "1:7")
    # Number of WAL-G base backups retained (wal-g delete retain FULL n).
    walg_keep_full: int = 7


@dataclass(frozen=True)
class ExtraRoute:
    """A static Traefik route: an external hostname forwarded to an arbitrary
    upstream URL for a service Oduflow does not manage (from a ``[route.*]``
    TOML section). Traefik mode only. See specs/0034-external-traefik-routes.md.
    """

    name: str  # from the [route.<name>] key; used as the router/service id
    host: str  # the incoming Host header to match (e.g. "api.example.com")
    url: str  # upstream URL (e.g. "http://127.0.0.1:3000")


@dataclass(frozen=True)
class Settings:
    """Global settings + per-team isolation."""

    # Server
    bind_host: str = "0.0.0.0"
    port: int = 8000
    trace: bool = False
    disable_telemetry: bool = False
    # Undocumented, off by default: lets the coding agents working against this
    # instance report friction with the Oduflow MCP tools themselves. While
    # off, the submit_agent_feedback tool is not even listed. See
    # oduflow.agent_feedback.
    agent_feedback: bool = False
    allow_local_path: bool = True
    # Allow starting the HTTP transport with no MCP authentication. Off by
    # default so /mcp is never served unauthenticated by accident (#37); set
    # true only when fronting Oduflow with your own auth proxy.
    allow_insecure_http: bool = False

    # Routing
    routing_mode: str = "port"
    acme_email: str = ""
    # Whether Traefik terminates TLS itself. True (default): Traefik listens on
    # :443, redirects HTTP->HTTPS and obtains Let's Encrypt certificates. False:
    # Traefik listens on plain HTTP :80 only, no redirect and no ACME — for
    # running behind a TLS-terminating upstream (e.g. a Cloudflare tunnel) that
    # already serves HTTPS. Public URLs stay https:// (the upstream provides the
    # certificate) unless ``public_scheme`` says otherwise. Ignored in port mode.
    routing_tls: bool = True
    # Raw ``[routing] public_scheme`` value; read the resolved
    # :attr:`public_scheme` property instead of this field. Empty (default)
    # derives the scheme from the routing mode: ``https`` in traefik mode
    # (Traefik terminates TLS itself, or an upstream does when ``tls = false``)
    # and ``http`` in port mode. Set it explicitly to ``"http"`` when nothing
    # terminates TLS in front of a ``tls = false`` deployment — otherwise every
    # URL Oduflow hands out points at an https:// endpoint nobody serves.
    public_scheme_setting: str = ""
    # Static extra routes (traefik mode): each forwards an external hostname to
    # an arbitrary upstream URL for a service Oduflow does not manage. Parsed
    # from ``[route.*]`` TOML sections. For anything more complex than
    # host→URL, drop your own dynamic-config file into the Traefik dynamic
    # directory instead (see docs/traefik.md).
    extra_routes: tuple[ExtraRoute, ...] = ()

    # Database
    db_user: str = "odoo"
    db_password: str = "odoo"
    postgres_image: str = "postgres:15"

    # Storage
    base_data_dir: str = ""
    overlay_threshold_mb: int = 50

    # Coding agent — deployment-wide bits only; enabling the agent and its
    # credentials are per team (TeamSettings.agent_*). When enabled for a team,
    # a single agent container (Claude Code + OpenAI Codex + OpenCode) serves all
    # of its environments: one git checkout per environment on a persistent volume,
    # driving environments only through the Oduflow MCP server. Container and
    # volume names are derived per team in naming.py.
    # See specs/0029-agent-console-and-chat.md.
    agent_image: str = DEFAULT_AGENT_IMAGE
    agent_claude_model: str = ""  # optional; empty = CLI default
    agent_codex_model: str = ""  # optional; empty = CLI default
    agent_opencode_model: str = ""  # optional provider/model; empty = CLI default

    # Lifecycle: automatic stop of idle environments and cleanup of stopped
    # ones (see oduflow.reaper). 0 disables either behavior. Protected
    # environments are always exempt. auto-delete is DESTRUCTIVE (drops the
    # database and workspace), so it is opt-in (defaults to 0); auto-stop is
    # non-destructive and enabled by default.
    auto_stop_hours: int = 48
    auto_delete_hours: int = 0
    # Purge the kept leftovers (database, workspace) of a soft-deleted
    # production N hours after its deletion, unless it was re-created in the
    # meantime. DESTRUCTIVE and opt-in (defaults to 0 = keep forever); an
    # immediate purge is always available via
    # `oduflow cleanup --purge-deleted-productions --force`.
    prod_purge_hours: int = 0

    # Shared Docker resource names
    shared_network: str = "oduflow-net"
    shared_db_container: str = "oduflow-db"
    shared_db_volume: str = "oduflow-db-data"
    traefik_container: str = "oduflow-traefik"
    traefik_acme_volume: str = "oduflow-traefik-acme"

    # Production hosting ([production] TOML section — strict opt-in).
    # Productions themselves are created at runtime via MCP/UI and live in
    # per-team registries (productions.json), not in TOML. The production
    # PostgreSQL cluster is physically separate from the dev one and is
    # provisioned lazily (first production or existing container).
    prod_enabled: bool = False
    prod_db_container: str = "oduflow-prod-db"
    prod_db_volume: str = "oduflow-prod-db-data"
    prod_postgres_image: str = ""  # empty = [database].image
    prod_walg_version: str = ""  # empty = version pinned in walg.py
    prod_workers_cap: int = 8  # upper bound for auto-tuned Odoo workers

    # Backup subsystem ([backup] TOML section); None = backups disabled.
    backup: BackupSettings | None = None

    # Docker labels
    prefix: str = "oduflow-"
    branch_label: str = "oduflow.branch"
    team_label: str = "oduflow.team"
    managed_label: str = "oduflow.managed"
    system_label: str = "oduflow.system"
    repo_label: str = "oduflow.repo"
    image_label: str = "oduflow.image"

    # Config location
    etc_dir: str = ""
    toml_path: str = ""

    # Teams
    teams: dict[str, TeamSettings] = field(default_factory=dict)

    def get_team(self, team_id: str) -> TeamSettings:
        if team_id not in self.teams:
            raise ValueError(f"Team '{team_id}' not found in configuration.")
        return self.teams[team_id]

    def get_team_by_token(self, token: str) -> TeamSettings | None:
        if not token:
            return None
        for team in self.teams.values():
            if team.auth_token and hmac.compare_digest(team.auth_token, token):
                return team
        return None

    def get_team_by_hostname(self, hostname: str) -> TeamSettings | None:
        if not hostname:
            return None
        try:
            authority = urlsplit(f"//{hostname.strip()}")
            # Reading .port validates malformed or out-of-range port values.
            _ = authority.port
        except ValueError:
            return None
        if (
            authority.username is not None
            or authority.password is not None
            or authority.path
            or authority.query
            or authority.fragment
            or authority.hostname is None
        ):
            return None
        hostname = authority.hostname.lower()
        for team in self.teams.values():
            if team.hostname.lower() == hostname:
                return team
        return None

    @property
    def public_scheme(self) -> str:
        """URL scheme ("http"/"https") for the public URLs Oduflow hands out.

        Every dashboard link, MCP endpoint, share link and reported environment
        URL is built with this scheme. It is deliberately independent of
        ``routing_tls``: that flag only says whether *Traefik* terminates TLS,
        and a ``tls = false`` deployment is normally fronted by an upstream
        terminator (e.g. a Cloudflare tunnel) that still serves https. An
        operator who runs plain HTTP end to end sets ``[routing]
        public_scheme = "http"`` to override the derived default. Team-scoped
        URLs go through :meth:`public_scheme_for`, which lets a ``[team.X]
        public_scheme`` override this deployment-wide value.
        """
        if self.public_scheme_setting:
            return self.public_scheme_setting
        return "https" if self.routing_mode == "traefik" else "http"

    def public_scheme_for(self, team: TeamSettings) -> str:
        """Resolved URL scheme for one team's public URLs.

        A ``[team.X] public_scheme`` overrides the global ``[routing]`` value,
        so one deployment can hand out http:// links for a LAN-only team and
        https:// links for a team fronted by an upstream TLS terminator (e.g. a
        Cloudflare tunnel) at the same time.
        """
        return team.public_scheme_setting or self.public_scheme

    @property
    def any_public_scheme_https(self) -> bool:
        """Whether any team's resolved public scheme is https.

        Derived from the resolved per-team values only (``validate`` guarantees
        at least one team, and a team without an override already resolves to
        the global value). The raw global default must not vote on its own:
        when every team overrides to http, no URL Oduflow hands out is https
        and forwarded-header trust must stay off.
        """
        return any(self.public_scheme_for(t) == "https" for t in self.teams.values())

    @property
    def oauth_enabled(self) -> bool:
        # Every authenticated HTTP deployment serves OAuth on the hostname of
        # the team reached by the request.
        return any(t.auth_token for t in self.teams.values())

    def get_team_by_ui_password(self, password: str) -> TeamSettings | None:
        if not password:
            return None
        for team in self.teams.values():
            if team.ui_password and hmac.compare_digest(team.ui_password, password):
                return team
        return None

    def _validate_public_scheme(self, value: str, prefix: str = "") -> None:
        """The wire-reality rules for a public_scheme value (global or team).

        The scheme must match what actually answers on the wire: with Traefik
        terminating TLS, :80 redirects to :443, so http:// links would bounce
        (POSTs drop body/Authorization) and leak tokens on the first plaintext
        hop; in port mode nothing can terminate TLS on the published
        per-environment ports, so https:// links (and the internal probes built
        from them) would fail the handshake outright.
        """
        if value not in ("", "http", "https"):
            raise ValueError(f"{prefix}public_scheme must be 'http' or 'https'")
        if value == "http" and self.routing_mode == "traefik" and self.routing_tls:
            raise ValueError(
                f"{prefix}public_scheme = 'http' requires tls = false: with "
                "tls = true Traefik redirects :80 to :443, so http:// links "
                "would not work"
            )
        if value == "https" and self.routing_mode == "port":
            raise ValueError(
                f"{prefix}public_scheme = 'https' is not supported in port "
                "mode: published environment ports serve plain HTTP"
            )

    def validate(self) -> None:
        if not self.teams:
            raise ValueError(
                "No [team.*] sections found in configuration. "
                "At least one team is required."
            )

        if self.routing_mode not in ("port", "traefik"):
            raise ValueError("routing_mode must be 'port' or 'traefik'")

        self._validate_public_scheme(self.public_scheme_setting)

        if self.routing_mode == "traefik" and self.routing_tls:
            if not self.acme_email:
                raise ValueError(
                    "acme_email must be set when routing_mode=traefik and "
                    "routing tls is enabled"
                )

        # A hostname is the stable routing and OAuth identity of a team in every
        # mode. Requiring it explicitly avoids a hidden global fallback and makes
        # host-relative OAuth safe to enable behind any TLS-terminating proxy.
        seen_team_hosts: dict[str, str] = {}
        for team in self.teams.values():
            if not team.hostname:
                raise ValueError(
                    f"Team '{team.team_id}': hostname must be set explicitly."
                )
            if team.base_domain:
                if self.routing_mode != "traefik":
                    raise ValueError(
                        f"Team '{team.team_id}': base_domain requires "
                        "routing_mode=traefik"
                    )
                from oduflow.naming import validate_domain

                try:
                    validate_domain(team.base_domain)
                except ValueError as exc:
                    raise ValueError(
                        f"Team '{team.team_id}': invalid base_domain: {exc}"
                    ) from exc
            normalized_host = team.hostname.lower()
            if normalized_host in seen_team_hosts:
                raise ValueError(
                    f"Teams '{seen_team_hosts[normalized_host]}' and "
                    f"'{team.team_id}' have duplicate hostname "
                    f"'{team.hostname}'."
                )
            seen_team_hosts[normalized_host] = team.team_id
            # Per-team public_scheme obeys the same wire-reality rules as the
            # global one: it changes only the URLs handed out, not what Traefik
            # serves, so it must still match what actually answers on that
            # hostname.
            self._validate_public_scheme(
                team.public_scheme_setting, prefix=f"Team '{team.team_id}': "
            )
            if team.port_range_start >= team.port_range_end:
                raise ValueError(
                    f"Team '{team.team_id}': invalid port range "
                    f"{team.port_range_start}-{team.port_range_end}"
                )
            if team.db_quota_gb < 0 or team.disk_quota_gb < 0:
                raise ValueError(
                    f"Team '{team.team_id}': quotas must be >= 0 (0 disables)"
                )
            if team.environment_slots < 0:
                raise ValueError(
                    f"Team '{team.team_id}': environment_slots must be >= 0 "
                    "(0 disables)"
                )
            if team.environment_hostname_mode not in ("branch", "slots"):
                raise ValueError(
                    f"Team '{team.team_id}': environment_hostname_mode must be "
                    "'branch' or 'slots'"
                )
            if team.service_slots < 0:
                raise ValueError(
                    f"Team '{team.team_id}': service_slots must be >= 0 (0 disables)"
                )
            if team.environment_hostname_mode == "slots":
                if self.routing_mode != "traefik":
                    raise ValueError(
                        f"Team '{team.team_id}': environment_hostname_mode='slots' "
                        "requires routing_mode=traefik"
                    )
                if team.environment_slots <= 0:
                    raise ValueError(
                        f"Team '{team.team_id}': environment_hostname_mode='slots' "
                        "requires environment_slots > 0"
                    )
                from oduflow.naming import split_team_hostname

                try:
                    split_team_hostname(team.hostname)
                except ValueError as exc:
                    raise ValueError(
                        f"Team '{team.team_id}': environment_hostname_mode='slots' "
                        "requires a hostname such as 'dev.example.com'"
                    ) from exc

        # Base domains are exclusive team zones: environment, service and
        # production names are allocated inside them, so no duplicates, and
        # no other team may place its hostname or base domain in the zone —
        # that is how one team is prevented from squatting names that would
        # route into another team's zone.
        # Local import: domains.py imports Settings, and the zone-containment
        # rule must have exactly one definition or config-time validation and
        # run-time claim checks can disagree.
        from oduflow.domains import in_zone

        zones: dict[str, str] = {}
        for team in self.teams.values():
            if not team.base_domain:
                continue
            if team.base_domain in zones:
                raise ValueError(
                    f"Teams '{zones[team.base_domain]}' and '{team.team_id}' "
                    f"have duplicate base_domain '{team.base_domain}'."
                )
            zones[team.base_domain] = team.team_id
        for team in self.teams.values():
            for zone, owner in zones.items():
                if owner == team.team_id:
                    continue
                for label, value in (
                    ("hostname", team.hostname.lower()),
                    ("base_domain", team.base_domain),
                ):
                    if value and in_zone(value, zone):
                        raise ValueError(
                            f"Team '{team.team_id}': {label} '{value}' lies "
                            f"inside team '{owner}' base_domain '{zone}'."
                        )

        # Validate static extra routes ([route.*]). They only make sense in
        # traefik mode (port mode has no shared reverse proxy) and must forward
        # a real hostname to an http(s) upstream. Hostnames must be unique
        # across routes and team hostnames so a Host rule never routes two ways.
        if self.extra_routes and self.routing_mode != "traefik":
            raise ValueError("[route.*] sections require routing.mode = 'traefik'.")
        seen_hosts = {t.hostname.lower() for t in self.teams.values()}
        for route in self.extra_routes:
            if not route.host:
                raise ValueError(f"Route '{route.name}': host must be set.")
            if "/" in route.host:
                # A path component would land in Traefik's Host() rule, which
                # matches on hostname only — the route would silently never
                # match. Fail loudly instead.
                raise ValueError(
                    f"Route '{route.name}': host must be a plain hostname, not a "
                    f"URL with a path (got {route.host!r})."
                )
            if not route.url:
                raise ValueError(f"Route '{route.name}': url must be set.")
            if not route.url.startswith(("http://", "https://")):
                raise ValueError(
                    f"Route '{route.name}': url must start with http:// or https:// "
                    f"(got {route.url!r})."
                )
            normalized_route_host = route.host.lower()
            if normalized_route_host in seen_hosts:
                raise ValueError(
                    f"Route '{route.name}': host '{route.host}' collides with "
                    "another route or team hostname."
                )
            seen_hosts.add(normalized_route_host)

        # Validate that team port ranges do not overlap. The default range is
        # identical for every team, so two teams that never set an explicit
        # port_range would draw host ports from the same pool and collide.
        ranges = sorted(
            (
                (t.port_range_start, t.port_range_end, t.team_id)
                for t in self.teams.values()
            ),
            key=lambda r: r[0],
        )
        for (a_start, a_end, a_id), (b_start, b_end, b_id) in zip(ranges, ranges[1:]):
            # Ranges are half-open [start, end); they overlap when the next
            # range starts before the previous one ends.
            if b_start < a_end:
                raise ValueError(
                    f"Teams '{a_id}' and '{b_id}' have overlapping port ranges "
                    f"({a_start}-{a_end} and {b_start}-{b_end}). "
                    "Set a distinct [team.*] port_range for each team."
                )

        # Validate uniqueness of auth tokens
        tokens = [t.auth_token for t in self.teams.values() if t.auth_token]
        if len(tokens) != len(set(tokens)):
            raise ValueError("Duplicate auth_token values across teams.")

        # Validate uniqueness of UI passwords
        passwords = [t.ui_password for t in self.teams.values() if t.ui_password]
        if len(passwords) != len(set(passwords)):
            raise ValueError("Duplicate ui_password values across teams.")

        if self.prod_workers_cap < 1:
            raise ValueError("[production] workers_cap must be >= 1")

        if self.backup is not None:
            b = self.backup
            for label, value in (
                ("snapshot_time", b.snapshot_time),
                ("basebackup_time", b.basebackup_time),
            ):
                if not _HHMM_RE.match(value):
                    raise ValueError(
                        f"[backup] {label} must be 'HH:MM' (24h), got {value!r}"
                    )
            for pair in b.keep:
                if not _KEEP_PAIR_RE.match(pair):
                    raise ValueError(
                        "[backup] keep entries must be 'interval_days:age_days' "
                        f"pairs, got {pair!r}"
                    )
            if b.walg_keep_full < 1:
                raise ValueError("[backup] walg_keep_full must be >= 1")

    @staticmethod
    def from_toml(path: str) -> Settings:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        server = raw.get("server", {})
        routing = raw.get("routing", {})
        database = raw.get("database", {})
        storage = raw.get("storage", {})
        lifecycle = raw.get("lifecycle", {})
        agent = raw.get("agent", {})
        production = raw.get("production", {})
        prod_enabled = production.get("enabled", False)
        if not isinstance(prod_enabled, bool):
            raise ValueError("[production] enabled must be true or false")
        if "oauth" in raw:
            raise ValueError(
                "[oauth] has been removed; OAuth is enabled automatically from "
                "each [team.*] hostname and auth_token."
            )
        if "oauth_base_url" in server:
            raise ValueError(
                "[server].oauth_base_url has been removed; OAuth is enabled "
                "automatically from each [team.*] hostname and auth_token."
            )
        routing_mode = str(routing.get("mode", "port")).strip().lower()
        if "hostname" in routing:
            logger.warning(
                "[routing].hostname is ignored; set hostname explicitly in "
                "every [team.*] section."
            )

        bind_value = server.get("bind")
        legacy_host = server.get("host")
        if bind_value is not None and legacy_host is not None:
            bind_text = str(bind_value).strip()
            legacy_text = str(legacy_host).strip()
            if bind_text != legacy_text:
                raise ValueError(
                    "[server].bind and legacy [server].host disagree; keep only "
                    "bind or set both to the same value."
                )
        if legacy_host is not None:
            logger.warning("[server].host is deprecated; use [server].bind instead.")
        bind_host = str(
            bind_value
            if bind_value is not None
            else legacy_host
            if legacy_host is not None
            else "0.0.0.0"
        ).strip()
        if not bind_host:
            raise ValueError("[server].bind must not be empty.")
        backup = _parse_backup_section(raw.get("backup", {}))

        etc_dir = _resolve_etc_dir()
        base_data_dir = _resolve_data_dir(storage.get("data_dir", ""))

        # Parse teams
        teams_raw = raw.get("team", {})
        if not teams_raw:
            raise ValueError(
                "No [team.*] sections found in TOML config. "
                "At least one team is required (e.g. [team.1])."
            )

        teams: dict[str, TeamSettings] = {}
        for team_id_raw, team_cfg in teams_raw.items():
            team_id = str(team_id_raw)
            team_data_dir = os.path.join(base_data_dir, f"team_{team_id}")

            port_range = team_cfg.get("port_range")
            if port_range is None:
                port_start, port_end = 50000, 50100
            elif isinstance(port_range, list) and len(port_range) == 2:
                try:
                    port_start, port_end = int(port_range[0]), int(port_range[1])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Team '{team_id}': port_range must be two integers, "
                        f"got {port_range!r}"
                    ) from exc
            else:
                raise ValueError(
                    f"Team '{team_id}': port_range must be [start, end], "
                    f"got {port_range!r}"
                )

            raw_base_domain = str(team_cfg.get("base_domain", ""))
            base_domain = (
                re.sub(r"^https?://", "", raw_base_domain).strip().lower().rstrip(".")
            )

            raw_hostname = str(team_cfg.get("hostname", ""))
            hostname = re.sub(r"^https?://", "", raw_hostname).strip()
            if not hostname and base_domain:
                # The dashboard's default home in a team zone. An explicit
                # hostname still wins.
                hostname = f"oduflow.{base_domain}"

            agent_env_raw = team_cfg.get("agent_env", {})
            if not isinstance(agent_env_raw, dict):
                raise ValueError(
                    f"Team '{team_id}': agent_env must be a table "
                    f"([team.{team_id}.agent_env]), got {agent_env_raw!r}"
                )

            image_registry_raw = team_cfg.get("image_registry", {})
            if not isinstance(image_registry_raw, dict):
                raise ValueError(
                    f"Team '{team_id}': image_registry must be a table "
                    f"([team.{team_id}.image_registry]), got {image_registry_raw!r}"
                )
            image_registry = _parse_image_registry_section(team_id, image_registry_raw)

            teams[team_id] = TeamSettings(
                team_id=team_id,
                hostname=hostname,
                base_domain=base_domain,
                auth_token=str(team_cfg.get("auth_token", "")),
                ui_password=str(team_cfg.get("ui_password", "")),
                port_range_start=port_start,
                port_range_end=port_end,
                data_dir=team_data_dir,
                port_registry_path=os.path.join(team_data_dir, "ports.json"),
                hostname_registry_path=os.path.join(team_data_dir, "hostnames.json"),
                environment_slots=int(team_cfg.get("environment_slots", 20)),
                environment_hostname_mode=str(
                    team_cfg.get("environment_hostname_mode", "branch")
                )
                .strip()
                .lower()
                or "branch",
                service_slots=int(team_cfg.get("service_slots", 10)),
                db_quota_gb=int(team_cfg.get("db_quota_gb", 50)),
                disk_quota_gb=int(team_cfg.get("disk_quota_gb", 0)),
                agent_enabled=bool(team_cfg.get("agent_enabled", False)),
                agent_default=str(team_cfg.get("agent_default", "claude"))
                .strip()
                .lower()
                or "claude",
                agent_env={str(k): str(v) for k, v in agent_env_raw.items()},
                image_registry=image_registry,
                public_scheme_setting=str(team_cfg.get("public_scheme", ""))
                .strip()
                .lower(),
            )

        # Parse static extra routes ([route.<name>] → host + upstream url).
        routes_raw = raw.get("route", {})
        if not isinstance(routes_raw, dict):
            raise ValueError(
                "[route.*] sections must be tables (e.g. [route.legacy-api])."
            )
        extra_routes: list[ExtraRoute] = []
        for route_name_raw, route_cfg in routes_raw.items():
            route_name = str(route_name_raw)
            if not isinstance(route_cfg, dict):
                raise ValueError(
                    f"Route '{route_name}': must be a table ([route.{route_name}])"
                )
            raw_host = str(route_cfg.get("host", "")).strip()
            host = re.sub(r"^https?://", "", raw_host).strip().rstrip("/")
            extra_routes.append(
                ExtraRoute(
                    name=route_name,
                    host=host,
                    url=str(route_cfg.get("url", "")).strip(),
                )
            )

        trace = bool(server.get("trace", False))

        global TRACE  # noqa: PLW0603
        TRACE = trace

        return Settings(
            bind_host=bind_host,
            port=int(server.get("port", 8000)),
            trace=trace,
            disable_telemetry=bool(server.get("disable_telemetry", False)),
            agent_feedback=bool(server.get("agent_feedback", False)),
            allow_local_path=bool(server.get("allow_local_path", True)),
            allow_insecure_http=bool(server.get("allow_insecure_http", False)),
            routing_mode=routing_mode,
            acme_email=str(routing.get("acme_email", "")).strip(),
            routing_tls=bool(routing.get("tls", True)),
            public_scheme_setting=str(routing.get("public_scheme", "")).strip().lower(),
            extra_routes=tuple(extra_routes),
            db_user=str(database.get("user", "odoo")),
            db_password=str(database.get("password", "odoo")),
            postgres_image=str(database.get("image", "postgres:15")),
            base_data_dir=base_data_dir,
            overlay_threshold_mb=int(storage.get("overlay_threshold_mb", 50)),
            agent_image=_normalize_agent_image(agent.get("image")),
            agent_claude_model=str(agent.get("claude_model", "")).strip(),
            agent_codex_model=str(agent.get("codex_model", "")).strip(),
            agent_opencode_model=str(agent.get("opencode_model", "")).strip(),
            auto_stop_hours=int(lifecycle.get("auto_stop_hours", 48)),
            auto_delete_hours=int(lifecycle.get("auto_delete_hours", 0)),
            prod_purge_hours=int(lifecycle.get("prod_purge_hours", 0)),
            prod_enabled=prod_enabled,
            prod_postgres_image=str(production.get("postgres_image", "")).strip(),
            prod_walg_version=str(production.get("walg_version", "")).strip(),
            prod_workers_cap=int(production.get("workers_cap", 8)),
            backup=backup,
            etc_dir=etc_dir,
            toml_path=path,
            teams=teams,
        )


def _parse_image_registry_section(
    team_id: str, raw: dict[str, object]
) -> ImageRegistrySettings | None:
    """Parse a [team.X.image_registry] TOML section.

    Absent/empty section → None (image building disabled for the team). A
    present section must be complete and valid — misconfigured image publishing
    should fail at startup, not at the first agent build.
    """
    if not raw:
        return None
    prefix = str(raw.get("repository_prefix", "")).strip().strip("/")
    if not prefix:
        raise ValueError(
            f"Team '{team_id}': [team.{team_id}.image_registry] requires "
            "repository_prefix (the registry namespace published images live "
            "under, e.g. 'acme'). Remove the section to disable image building."
        )
    if not _OCI_REPO_RE.match(prefix):
        raise ValueError(
            f"Team '{team_id}': image_registry repository_prefix {prefix!r} is "
            "not a valid lowercase OCI repository namespace."
        )
    host = str(raw.get("host", "docker.io")).strip().lower() or "docker.io"
    if not _REGISTRY_HOST_RE.match(host) or "/" in host:
        raise ValueError(
            f"Team '{team_id}': image_registry host {host!r} must be a plain "
            "registry hostname (optionally with :port), without scheme or path."
        )
    username = str(raw.get("username", "")).strip()
    token = str(raw.get("token", "")).strip()
    if bool(username) != bool(token):
        raise ValueError(
            f"Team '{team_id}': image_registry username and token must be "
            "set together (explicit credentials) or both omitted (use the host "
            "Docker daemon's own `docker login` credentials)."
        )
    timeout = int(str(raw.get("build_timeout_seconds", 1800)))
    max_context_mb = int(str(raw.get("max_context_mb", 512)))
    max_log_mb = int(str(raw.get("max_log_mb", 16)))
    max_concurrent_builds = int(str(raw.get("max_concurrent_builds", 2)))
    keep_images = int(str(raw.get("keep_images", 10)))
    if timeout <= 0:
        raise ValueError(
            f"Team '{team_id}': image_registry build_timeout_seconds must be > 0"
        )
    if max_context_mb <= 0:
        raise ValueError(f"Team '{team_id}': image_registry max_context_mb must be > 0")
    if max_log_mb <= 0:
        raise ValueError(f"Team '{team_id}': image_registry max_log_mb must be > 0")
    if max_concurrent_builds <= 0:
        raise ValueError(
            f"Team '{team_id}': image_registry max_concurrent_builds must be > 0"
        )
    if keep_images < 0:
        raise ValueError(
            f"Team '{team_id}': image_registry keep_images must be >= 0 "
            "(0 disables pruning)"
        )
    return ImageRegistrySettings(
        repository_prefix=prefix,
        host=host,
        username=username,
        token=token,
        build_timeout_seconds=timeout,
        max_context_mb=max_context_mb,
        max_log_mb=max_log_mb,
        max_concurrent_builds=max_concurrent_builds,
        keep_images=keep_images,
    )


def _parse_backup_section(backup_raw: dict[str, object]) -> BackupSettings | None:
    """Parse the [backup] TOML section into BackupSettings.

    Absent/empty section → None (backups disabled). A partially configured
    section (some of bucket/access_key/secret_key missing) is a hard error —
    silently disabled backups would be worse than a startup failure.
    """
    if not backup_raw:
        return None
    bucket = str(backup_raw.get("bucket", "")).strip()
    access_key = str(backup_raw.get("access_key", "")).strip()
    secret_key = str(backup_raw.get("secret_key", "")).strip()
    if not (bucket and access_key and secret_key):
        raise ValueError(
            "[backup] section requires all of: bucket, access_key, secret_key "
            "(remove the section entirely to disable backups)."
        )
    keep_raw = backup_raw.get("keep", ["30:180", "7:30", "1:7"])
    if not isinstance(keep_raw, list):
        raise ValueError(f"[backup] keep must be a list of strings, got {keep_raw!r}")
    return BackupSettings(
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        endpoint=str(backup_raw.get("endpoint", "")).strip(),
        region=str(backup_raw.get("region", "")).strip(),
        prefix=str(backup_raw.get("prefix", "oduflow")).strip().strip("/") or "oduflow",
        snapshot_time=str(backup_raw.get("snapshot_time", "02:00")).strip(),
        basebackup_time=str(backup_raw.get("basebackup_time", "03:30")).strip(),
        keep=tuple(str(p).strip() for p in keep_raw),
        walg_keep_full=int(str(backup_raw.get("walg_keep_full", 7))),
    )


def find_toml() -> str:
    """Locate oduflow.toml: ODUFLOW_TOML > /etc/oduflow > ~/.oduflow/conf."""
    explicit = os.getenv("ODUFLOW_TOML", "").strip()
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise FileNotFoundError(f"ODUFLOW_TOML={explicit} does not exist.")

    candidates = [
        "/etc/oduflow/oduflow.toml",
        os.path.join(os.path.expanduser("~"), ".oduflow", "conf", "oduflow.toml"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "oduflow.toml not found. Searched:\n"
        + "\n".join(f"  - {c}" for c in candidates)
        + "\nCreate one or set ODUFLOW_TOML environment variable."
    )


_cached_etc_dir: str | None = None


def _resolve_etc_dir() -> str:
    """Resolve etc directory: /etc/oduflow or ~/.oduflow/conf (cached)."""
    global _cached_etc_dir
    if _cached_etc_dir is not None:
        return _cached_etc_dir
    default = "/etc/oduflow"
    if os.access(default, os.W_OK) or os.access(os.path.dirname(default), os.W_OK):
        _cached_etc_dir = default
    else:
        _cached_etc_dir = os.path.join(os.path.expanduser("~"), ".oduflow", "conf")
        logger.debug(
            "Default %s is not writable, falling back to %s", default, _cached_etc_dir
        )
    return _cached_etc_dir


_cached_data_dir: str | None = None


def _resolve_data_dir(explicit: str) -> str:
    """Resolve base data directory: explicit > /srv/oduflow > ~/.oduflow/data (cached)."""
    global _cached_data_dir
    if explicit:
        return explicit
    if _cached_data_dir is not None:
        return _cached_data_dir
    default = "/srv/oduflow"
    parent = os.path.dirname(default)
    if os.access(parent, os.W_OK) or (
        os.path.isdir(default) and os.access(default, os.W_OK)
    ):
        _cached_data_dir = default
    else:
        _cached_data_dir = os.path.join(os.path.expanduser("~"), ".oduflow", "data")
        logger.debug(
            "Default %s is not writable, falling back to %s", default, _cached_data_dir
        )
    return _cached_data_dir
