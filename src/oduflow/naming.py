import hashlib
import os
import re
import shlex
from typing import Any
from urllib.parse import urlparse, urlunparse

# A template name becomes both a filesystem path (templates/<name>, where "/"
# denotes a sub-directory) and, with "/" replaced by "-", part of a PostgreSQL
# database identifier. "/" is therefore allowed as a segment separator, but each
# segment must start with an alphanumeric (which rules out "." and ".." — i.e.
# path traversal) and contain only [a-zA-Z0-9_.-] (which rules out quotes,
# semicolons and whitespace — i.e. SQL-identifier break-out).
_TEMPLATE_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_SERVICE_DATABASE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


def validate_template_name(template_name: str) -> str:
    """Validate a template name and return it unchanged.

    Raises ValueError if the name could escape the templates directory or break
    out of a SQL identifier. Enforced at the two derivation chokepoints
    (get_template_db_name and TeamSettings.get_template_dir), so every entry
    point — MCP tools, web UI, CLI — is covered.
    """
    name = template_name or ""
    segments = name.split("/")
    if (
        not name
        or len(name) > 63
        or not all(_TEMPLATE_SEGMENT_RE.match(seg) for seg in segments)
    ):
        raise ValueError(
            f"Invalid template name '{template_name}': each '/'-separated "
            "segment must start with a letter or digit and contain only "
            "[a-zA-Z0-9_.-] (max 63 chars total)."
        )
    return name


def validate_env_name(env_name: str) -> str:
    """Validate an environment/branch name and return it unchanged.

    ``env_name`` becomes a filesystem path segment via
    ``get_workspace_path`` (``os.path.join(dir, env_name.replace('/', '-'))``).
    Slashes are folded to ``-`` there, so the only way the result can escape the
    workspaces dir is a name that IS a traversal component (``.``/``..``) or that
    smuggles a native path separator/NUL. Reject those (and empty/oversized
    names) at the creation chokepoint so every derived path stays contained.
    Legitimate git branch names (``19.0``, ``feature/x``, ``release/v1.2``) pass.
    Returns the name **unchanged** (it is not canonicalized): a padded name like
    ``"foo "`` is rejected rather than silently trimmed, because slugified
    container/DB names strip the space while the workspace path keeps it — so the
    trimmed and raw forms would collide on containers yet diverge on disk.
    """
    name = env_name or ""
    if name != name.strip():
        raise ValueError(
            f"Invalid environment name '{env_name}': leading or trailing "
            "whitespace is not allowed."
        )
    if not name or len(name) > 100:
        raise ValueError(
            f"Invalid environment name '{env_name}': must be 1-100 characters."
        )
    if "\\" in name or "\x00" in name:
        raise ValueError(
            f"Invalid environment name '{env_name}': backslashes and NUL bytes "
            "are not allowed."
        )
    if any(seg in ("", ".", "..") for seg in name.split("/")):
        raise ValueError(
            f"Invalid environment name '{env_name}': '/'-separated segments must "
            "be non-empty and not '.' or '..'."
        )
    # A name made up only of stripped characters (e.g. "@@@", non-ASCII) would
    # slugify to "", producing a degenerate DB name ("oduflow_<team>_") that
    # collides with every other such name. Require at least one slug char.
    if not slugify_branch(name):
        raise ValueError(
            f"Invalid environment name '{env_name}': must contain at least one "
            "alphanumeric, '_' or '-' character."
        )
    return name


def validate_service_name(name: str) -> str:
    """Validate an auxiliary-service name and return it unchanged.

    Service names become Docker container names directly, so silently
    normalizing them would make the identifier shown to the user differ from
    the resource Docker created. Reject unsupported names at the shared naming
    chokepoint instead.
    """
    if not name or not _SERVICE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid service name '{name}': must start with a letter or digit "
            "and contain only letters, digits, dots, hyphens, and underscores. "
            "Spaces are not allowed; use hyphens instead (for example, "
            "'odoo-mcp-server')."
        )
    return name


def validate_service_database_name(name: str) -> str:
    """Validate the stable logical name of a sidecar PostgreSQL database.

    Unlike Docker service names these names are deliberately lowercase and
    short: they become credential-file names and feed two PostgreSQL
    identifiers whose hard limit is 63 bytes.  Rejecting instead of silently
    normalising keeps every control surface referring to the same resource.
    """
    if not name or not _SERVICE_DATABASE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid service database name '{name}': must start with a "
            "lowercase letter or digit, contain only lowercase letters, digits, "
            "hyphens, and underscores, and be at most 31 characters."
        )
    return name


_SECRET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def validate_secret_name(name: str) -> str:
    """Validate a team-secret name (``secret_store``) and return it unchanged."""
    if not name or not _SECRET_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid secret name '{name}': must start with a lowercase letter "
            "or digit and contain only lowercase letters, digits, dots, hyphens "
            "and underscores (max 64 characters)."
        )
    return name


_PG_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]")
_PG_IDENTIFIER_MAX_BYTES = 63
_PG_DIGEST_LEN = 10
# Marks an identifier whose readable rendering is not a faithful encoding of
# its (team, name) pair. Service database names cannot contain a dot, so a dot
# after the final underscore appears only on identifiers carrying this digest.
_PG_DIGEST_SEPARATOR = "."


def _pg_digest(prefix: str, team_id: str, name: str) -> str:
    """Digest the exact component tuple, never its rendered form.

    NUL cannot occur in any component, so joining on it keeps the digest
    injective over the tuple: ``("a", "b_c")`` and ``("a_b", "c")`` hash
    differently even though they render identically.
    """
    raw = "\x00".join((prefix, team_id, name))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_PG_DIGEST_LEN]


def _renders_faithfully(team_id: str) -> bool:
    """Whether ``team_id`` can be recovered from the rendered identifier.

    Team ids are unvalidated TOML table keys, so they may carry underscores or
    uppercase. ``_`` separates the segments, which makes ``svc_a_b_c``
    ambiguous between team ``a``/database ``b_c`` and team ``a_b``/database
    ``c``; sanitising and lower-casing are lossy the same way (``A`` and ``a``
    render identically). Database names are validated to exclude both hazards,
    so only the team segment needs the test.
    """
    return "_" not in team_id and _PG_UNSAFE_RE.sub("_", team_id).lower() == team_id


def _bounded_pg_identifier(prefix: str, team_id: str, name: str) -> str:
    """Build a deterministic, collision-free PostgreSQL identifier.

    The readable ``<prefix>_<team>_<name>`` rendering is used only when it
    uniquely determines the pair it came from; otherwise a digest of the exact
    pair is appended, so two distinct pairs can never share an identifier. The
    identifier is always double-quoted at SQL sinks, so dots and hyphens are
    retained: collapsing them to underscores would make e.g. ``worker-data``
    collide with ``worker_data``.
    """
    team_segment = _PG_UNSAFE_RE.sub("_", team_id).lower()
    safe = f"{prefix}_{team_segment}_{name}"
    if not _renders_faithfully(team_id):
        safe = f"{safe}{_PG_DIGEST_SEPARATOR}{_pg_digest(prefix, team_id, name)}"
    if len(safe.encode("utf-8")) <= _PG_IDENTIFIER_MAX_BYTES:
        return safe
    # Every component is ASCII after sanitising, so slicing bytes and slicing
    # characters agree. The digest keeps truncated identifiers distinct.
    head = _PG_IDENTIFIER_MAX_BYTES - _PG_DIGEST_LEN - len(_PG_DIGEST_SEPARATOR)
    return f"{safe[:head]}{_PG_DIGEST_SEPARATOR}{_pg_digest(prefix, team_id, name)}"


def get_service_database_name(name: str, team_id: str) -> str:
    """Physical database name for a managed sidecar database."""
    validate_service_database_name(name)
    return _bounded_pg_identifier("oduflow_service", team_id, name)


def get_service_database_role(name: str, team_id: str) -> str:
    """Scoped login role owning a managed sidecar database."""
    validate_service_database_name(name)
    return _bounded_pg_identifier("svc", team_id, name)


# Production names are deliberately stricter than env names: they feed
# container names, database identifiers, filesystem paths, Traefik router
# names and S3 key prefixes without any slugification step, so only
# lowercase alphanumerics and dashes are allowed.
# A production name also becomes a DNS label: in a base_domain team the
# default domain is "<name>.<base_domain>", so a trailing hyphen would
# build an FQDN validate_domain then rejects — for a domain the user
# never typed.
_PROD_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$")

# Internal namespace prefix separating production environments from dev
# environments in every name-derived resource (containers, databases, PG
# roles, workspace dirs). Dev tools reject names in this namespace; prod
# containers additionally carry no branch label, so dev-side listings never
# see them.
PROD_ENV_PREFIX = "prod-"


def validate_prod_name(name: str) -> str:
    """Validate a production environment name and return it unchanged."""
    if not name or not _PROD_NAME_RE.match(name):
        raise ValueError(
            f"Invalid production name '{name}': must start and end with a "
            "lowercase letter or digit and contain only [a-z0-9-] "
            "(max 31 chars)."
        )
    return name


# One managed dev template per production, published on first use and reused
# after that. The prefix keeps the namespace recognisable and owned by Oduflow.
PRODUCTION_TEMPLATE_PREFIX = "prod-"


def production_template_name(name: str) -> str:
    """Name of the managed dev template holding a copy of production *name*."""
    validate_prod_name(name)
    return f"{PRODUCTION_TEMPLATE_PREFIX}{name}"


def prod_env_name(name: str) -> str:
    """Internal environment name for a production ("prod-{name}").

    The single chokepoint mapping a production name into the shared naming
    scheme: every name-derived helper (get_resource_name, get_db_name,
    get_workspace_path, PG role generation) receives this value, which keeps
    productions in a reserved namespace that dev environments cannot enter.
    """
    validate_prod_name(name)
    return f"{PROD_ENV_PREFIX}{name}"


_DOMAIN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$"
)


def validate_domain(domain: str) -> str:
    """Validate a production domain (FQDN) and return it lowercased.

    The value lands in a Traefik ``Host(...)`` rule and in URLs, so only a
    plain dotted hostname is accepted — no scheme, port, path or wildcard.
    """
    normalized = (domain or "").strip().lower().rstrip(".")
    if not normalized or len(normalized) > 253 or not _DOMAIN_RE.match(normalized):
        raise ValueError(
            f"Invalid domain '{domain}': expected a fully-qualified hostname "
            "like erp.example.com (no scheme, port, or wildcard)."
        )
    return normalized


def odoo_major_from_image(image: str) -> int | None:
    """Best-effort Odoo major version parsed from an image reference's tag.

    Handles ``odoo:19.0``, ``odoo:19``, ``registry:5000/acme/odoo:19.0`` and
    version-prefixed custom tags like ``acme/odoo:19.0-custom``. Returns None
    when the tag carries no leading version (e.g. ``:latest`` or no tag) — the
    port in ``registry:5000/odoo`` is never mistaken for a tag, and neither is
    the hex of a digest-pinned reference (``odoo@sha256:98f...``).
    """
    # name[:tag][@algo:digest] — drop the digest first so its hex is never
    # parsed as a tag; a digest carries no version information anyway.
    _, sep, tag = image.split("@", 1)[0].rpartition(":")
    if sep and "/" not in tag:
        match = re.match(r"(\d+)(?:\D|$)", tag)
        if match:
            return int(match.group(1))
    return None


def slugify_branch(env_name: str) -> str:
    slug = env_name.replace("/", "-")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    slug = slug.lower()
    return slug[:63]


def get_db_name(env_name: str, team_id: str = "1") -> str:
    return f"oduflow_{team_id}_{slugify_branch(env_name)}"


def get_resource_name(
    env_name: str, resource_type: str, prefix: str, team_id: str
) -> str:
    """Docker container name for an environment resource.

    Team-scoped (naming v2): two teams using the same branch name must never
    collide on — or worse, operate on — each other's containers. Existing
    containers are renamed to this scheme by startup migration
    0001-team-scoped-container-names.
    """
    return f"{prefix}{team_id}-{env_name.replace('/', '-')}-{resource_type}"


def get_service_container_name(name: str, prefix: str, team_id: str) -> str:
    """Docker container name for an auxiliary service (team-scoped, see
    get_resource_name)."""
    validate_service_name(name)
    return f"{prefix}{team_id}-svc-{name}"


def get_agent_container_name(team_id: str, prefix: str) -> str:
    """The team's coding-agent container (one container serves every
    environment of the team). Cannot collide with env resources
    (``{prefix}{team}-{env}-{type}``, always type-suffixed) or services
    (``{prefix}{team}-svc-{name}``)."""
    return f"{prefix}{team_id}-agent"


def get_agent_home_volume_name(team_id: str, prefix: str) -> str:
    """Named volume mounted as the agent container's HOME (auth + sessions)."""
    return f"{prefix}{team_id}-agent-home"


def get_agent_workspace_volume_name(team_id: str, prefix: str) -> str:
    """Named volume mounted at /workspace (one checkout per environment)."""
    return f"{prefix}{team_id}-agent-workspace"


def get_agent_checkout_dir(env_name: str) -> str:
    """Path of an environment's checkout inside the team's agent container.

    Used both by the create hook (clone-env.sh target) and the agent console /
    ACP chat (exec workdir), so they must agree on the same slug.
    See specs/0029-agent-console-and-chat.md.
    """
    return f"/workspace/{slugify_branch(env_name)}"


def get_agent_upload_dir(env_name: str) -> str:
    """Directory for Agent Chat attachments inside the team's workspace volume.

    Attachments deliberately live outside the environment's git checkout so
    they never appear in ``git status`` or get committed by the coding agent.
    """
    return f"/workspace/.oduflow-uploads/{slugify_branch(env_name)}"


def get_workspace_path(env_name: str, workspaces_dir: str) -> str:
    return os.path.join(workspaces_dir, env_name.replace("/", "-"))


def get_repo_path(env_name: str, workspaces_dir: str) -> str:
    return os.path.join(get_workspace_path(env_name, workspaces_dir), "repo")


def get_env_short_hostname(env_name: str, route_hostname: str = "") -> str:
    slug = slugify_branch(route_hostname or env_name).replace("_", "-")
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def split_team_hostname(hostname: str) -> tuple[str, str]:
    """Split ``dev.example.com`` into the reusable prefix and parent domain."""
    value = hostname.strip().lower().rstrip(".")
    prefix, separator, parent_domain = value.partition(".")
    if not separator or not prefix or not parent_domain:
        raise ValueError(
            f"Team hostname '{hostname}' must contain a host prefix and parent "
            "domain, for example 'dev.example.com'."
        )
    try:
        validate_env_hostname(prefix)
        validate_domain(parent_domain)
    except ValueError as exc:
        raise ValueError(
            f"Team hostname '{hostname}' must contain a host prefix and parent "
            "domain, for example 'dev.example.com'."
        ) from exc
    return prefix, parent_domain


def get_env_hostname(
    env_name: str, hostname: str, route_hostname: str = "", base_domain: str = ""
) -> str:
    short_hostname = get_env_short_hostname(env_name, route_hostname)
    if base_domain:
        # Team base-domain mode: every environment is a direct child of the
        # base domain (feature.example.com), a sibling of the dashboard host.
        return f"{short_hostname}.{base_domain}"
    if not route_hostname:
        return f"{short_hostname}.{hostname}"
    _prefix, parent_domain = split_team_hostname(hostname)
    return f"{short_hostname}.{parent_domain}"


def validate_env_hostname(hostname: str) -> str:
    """Validate a short environment hostname such as ``dev1`` or ``qa``."""
    value = hostname.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(
            f"Invalid environment hostname '{hostname}': expected one DNS label "
            "such as 'dev1' (letters, digits and internal hyphens only)."
        )
    return value


def get_team_network_name(team_id: str, prefix: str = "oduflow-") -> str:
    """The team's isolated Docker network. Environment and service containers
    join only this network; shared infrastructure (PostgreSQL, Traefik) is
    additionally attached to every team network, so tenants can reach the
    infra but never each other."""
    return f"{prefix}{team_id}-net"


def get_tablespace_name(team_id: str) -> str:
    """PostgreSQL tablespace holding all of the team's databases (its files
    live under base_data_dir/pg_tablespaces/team_{id} on the host)."""
    return f"oduflow_team_{team_id}"


def get_template_db_name(template_name: str, team_id: str = "1") -> str:
    # validate_template_name guarantees the (slugified) name cannot break out of
    # the double-quoted SQL identifier this is interpolated into.
    validate_template_name(template_name)
    slug = template_name.replace("/", "-")
    return f"oduflow_template_{team_id}_{slug}"


def get_filestore_paths(env_name: str, workspaces_dir: str) -> dict[str, str]:
    base = get_workspace_path(env_name, workspaces_dir)
    return {
        "upper": os.path.join(base, "filestore_upper"),
        "work": os.path.join(base, "filestore_work"),
        "merged": os.path.join(base, "filestore"),
    }


def parse_env_vars(raw: str) -> dict[str, str]:
    """Parse KEY=VALUE assignments without treating commas inside values as separators.

    Entries are separated by newlines, or by a comma that is immediately
    followed by another "KEY=". So "WORKERS=2,LIMIT_TIME_CPU=600" is two
    variables while "TOOL_GROUPS=write,documents" stays a single one — values
    holding commas (set through a stack file, for example) survive a
    read-edit-write round trip through the dashboard and the MCP tools.
    """
    items = re.split(r"\r?\n|,(?=\s*[A-Za-z_][A-Za-z0-9_]*=)", raw)
    return dict(item.strip().split("=", 1) for item in items if "=" in item)


def parse_service_command(raw: str) -> list[str]:
    """Split a start-command string into the argv list Docker expects.

    Shell quoting rules apply, so ``server /data --address ":9001"`` becomes
    four arguments. An empty (or whitespace-only) string means "no override" —
    the image's own CMD is used.
    """
    if not raw or not raw.strip():
        return []
    try:
        return shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid command {raw!r}: {exc}") from None


_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize_env_vars(raw: object) -> dict[str, str]:
    """Coerce a stored or user-supplied env-var set into {NAME: value} strings.

    Accepts the mapping shape (how templates and container labels store them)
    or the KEY=VALUE text shape (MCP arguments, REST clients), so both reach
    the container through one validated representation. Values are cast to str
    because JSON round-trips integers ("WORKERS": 2) that Docker rejects.

    Raises ValueError for a name that could not be exported anyway; a bad name
    is a typo the caller must see, not something to silently drop.
    """
    if raw is None:
        return {}
    parsed: dict[Any, Any]
    if isinstance(raw, str):
        parsed = parse_env_vars(raw)
    elif isinstance(raw, dict):
        parsed = raw
    else:
        raise TypeError("Environment variables must be a mapping or KEY=VALUE text.")

    result: dict[str, str] = {}
    for name, value in parsed.items():
        key = str(name).strip()
        if not key:
            continue
        if not _ENV_VAR_NAME_RE.match(key):
            raise ValueError(
                f"Invalid environment variable name '{key}': use letters, "
                "digits and underscores, not starting with a digit."
            )
        result[key] = "" if value is None else str(value)
    return result


def sanitize_repo_url(url: str) -> str:
    if not url:
        return url
    try:
        parsed = urlparse(url)
        # Only HTTP(S) userinfo is a credential. An SSH URL's user (git@) is
        # part of the protocol and must survive for later clones.
        if parsed.scheme in ("https", "http") and (parsed.username or parsed.password):
            # Strip only userinfo.  Keep the original host spelling, IPv6
            # brackets, and explicit port: this sanitized URL is also used for
            # subsequent clones, not just display.
            clean = parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1])
            return urlunparse(clean)
        if parsed.password:
            # Non-HTTP schemes (ssh://): keep the protocol user, but an
            # embedded password is a secret and must not reach container
            # labels, environment info, or the dashboard.
            host = parsed.netloc.rsplit("@", 1)[-1]
            user = f"{parsed.username}@" if parsed.username else ""
            return urlunparse(parsed._replace(netloc=user + host))
    except Exception:
        pass
    return url


# Matches inline credentials embedded in any URL anywhere inside a larger string
# (e.g. git's stderr, which frequently echoes the full remote incl. user:PAT@host).
# sanitize_repo_url only handles a whole-string URL; this scrubs free text.
_URL_CREDS_RE = re.compile(r"(\w+://)[^/\s@]+@")


def redact_url_credentials(text: str) -> str:
    """Redact ``scheme://user:pass@`` credentials embedded anywhere in text."""
    if not text:
        return text
    return _URL_CREDS_RE.sub(r"\1***@", text)
