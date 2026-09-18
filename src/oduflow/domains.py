"""Team base-domain policy and global public-hostname collision checks.

A public FQDN maps to exactly one Traefik ``Host()`` rule, so every name
Oduflow hands out — team dashboard hostnames, static ``[route.*]`` hosts,
production domains and extra_domains, environment and service hostnames —
lives in one global namespace. This module is the single place that answers
"is this name free, and is the caller allowed to claim it?".

A team's ``base_domain`` is its exclusive zone: environments and services are
named directly under it and another team may never claim a name inside it.
Production primary domains must stay inside the owning team's zone (the apex
itself is allowed — and is the default for the team's first production);
``extra_domains`` may be arbitrary client-owned FQDNs outside every other
team's zone.

This module is also the single definition of *which* domain a short name
hangs off: :func:`service_hostname` and :func:`env_hostname` exist so the
rule is not re-derived (and re-derived differently) at each call site.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from oduflow.errors import ConflictError
from oduflow.naming import get_env_hostname, validate_domain
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger(__name__)

# Traefik router rules are the authoritative record of what a live container
# actually answers on: labels are frozen at create time, so a container may
# still serve a name that current settings would no longer compute.
_HOST_RULE_RE = re.compile(r"Host\(`([^`]+)`\)")


def in_zone(fqdn: str, zone: str) -> bool:
    """Whether ``fqdn`` is the zone apex or any name under the zone."""
    return bool(zone) and (fqdn == zone or fqdn.endswith(f".{zone}"))


def service_parent_domain(team: TeamSettings) -> str:
    """The domain short service names hang off.

    The team zone when configured, else the team hostname itself (the legacy
    nested layout, where ``redis`` on team ``dev.example.com`` becomes
    ``redis.dev.example.com``).
    """
    return team.base_domain or team.hostname


def service_hostname(team: TeamSettings, name: str, hostname: str | None) -> str:
    """Resolve a service's public FQDN.

    A dotted ``hostname`` is a full FQDN used as-is; a bare label — or no
    hostname at all, in which case the service name is used — is attached to
    :func:`service_parent_domain`. Every caller that needs to know where a
    service answers must go through here: ``create_service``, the no-recreate
    branch of ``update_service``, the Stack drift check and the preset writer
    previously each spelled this rule out, and they drifted apart.
    """
    result = (hostname or name).strip().lower()
    if "." not in result:
        result = f"{result}.{service_parent_domain(team)}"
    return result


def env_hostname(team: TeamSettings, env_name: str, route_hostname: str = "") -> str:
    """Resolve an environment's public FQDN from the team's routing layout.

    Thin team-aware wrapper over :func:`oduflow.naming.get_env_hostname`, which
    stays a pure function of its arguments. Note this is the name current
    settings *would* assign: a container created before ``base_domain`` was set
    still routes on its old nested name until its next ``update_environment``,
    so anything reporting a live URL must read the container's Traefik rule
    instead (see ``env_ops.container_route_host``).
    """
    return get_env_hostname(env_name, team.hostname, route_hostname, team.base_domain)


@dataclass(frozen=True)
class NameIndex:
    """One snapshot of every claimed public FQDN.

    Built once per claim (or once per batch of claims) so that checking a
    production's primary domain plus three extra domains does not re-read every
    team's ``productions.json`` four times over, nor list Docker containers
    four times.
    """

    # (team_id, production_name, domains) — primary first, then extra_domains.
    productions: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    # fqdn -> (team_id, kind, resource_name); kind is "environment"/"service".
    live: dict[str, tuple[str, str, str]] = field(default_factory=dict)


def _live_host_rules(settings: Settings) -> dict[str, tuple[str, str, str]]:
    """Map every FQDN a managed container currently serves to its owner.

    Environments and services are not recorded in any registry — their claim on
    a name exists only as a Traefik label — so the namespace check has to read
    it back off the containers. Best-effort by design: if the Docker daemon is
    unreachable we fall back to the configured-names-only check rather than
    blocking every create behind a healthy daemon.
    """
    if settings.routing_mode != "traefik":
        return {}
    try:
        from oduflow.docker_ops.client import get_client

        client = get_client()
        containers = client.containers.list(
            all=True, filters={"label": [f"{settings.managed_label}=true"]}
        )
    except Exception:
        logger.debug(
            "Hostname index: Docker unavailable, skipping live rules", exc_info=True
        )
        return {}

    rules: dict[str, tuple[str, str, str]] = {}
    for container in containers:
        labels = container.labels or {}
        team_id = labels.get(settings.team_label, "")
        service_name = labels.get("oduflow.service", "")
        env_name = labels.get(settings.branch_label, "")
        if service_name:
            kind, resource = "service", service_name
        elif env_name:
            kind, resource = "environment", env_name
        else:
            # Production containers carry no branch label; their domains are
            # already authoritative in productions.json.
            continue
        for key, value in labels.items():
            if not key.startswith("traefik.http.routers.") or not key.endswith(".rule"):
                continue
            for fqdn in _HOST_RULE_RE.findall(value):
                rules.setdefault(fqdn.lower(), (team_id, kind, resource))
    return rules


def build_name_index(settings: Settings) -> NameIndex:
    """Snapshot production records and live container routes in one pass."""
    from oduflow import production_registry

    productions: list[tuple[str, str, tuple[str, ...]]] = []
    for team in settings.teams.values():
        for prod_name, record in production_registry.list_productions(team).items():
            domains = [str(record.get("domain", ""))]
            domains.extend(str(d) for d in (record.get("extra_domains") or []))
            productions.append(
                (team.team_id, prod_name, tuple(d for d in domains if d))
            )
    return NameIndex(productions=tuple(productions), live=_live_host_rules(settings))


def find_owner(
    settings: Settings,
    fqdn: str,
    *,
    own_team: str,
    exclude_production: str = "",
    exclude_env: str = "",
    exclude_service: str = "",
    allow_own_team_host: bool = False,
    index: NameIndex | None = None,
) -> str:
    """Return a human-readable current owner of ``fqdn``, or '' if free.

    Checks team hostnames, other teams' base-domain zones, static extra routes,
    every team's production domains (primary + extra) and the names live
    environment and service containers currently serve.

    ``allow_own_team_host`` permits the caller's own team dashboard hostname:
    a service with ``routes`` publishes only ``Host() && PathPrefix()`` routers
    and no catch-all, so sharing the dashboard host under a URL prefix is a
    supported layout rather than a collision.
    """
    fqdn = fqdn.lower()
    if index is None:
        index = build_name_index(settings)

    for team in settings.teams.values():
        if fqdn == team.hostname.lower():
            if allow_own_team_host and team.team_id == own_team:
                continue
            return f"team '{team.team_id}' dashboard hostname"
        if team.team_id != own_team and in_zone(fqdn, team.base_domain):
            return f"team '{team.team_id}' zone '{team.base_domain}'"
    for route in settings.extra_routes:
        if fqdn == route.host.lower():
            return f"static route '{route.name}'"
    for team_id, prod_name, domains in index.productions:
        if team_id == own_team and prod_name == exclude_production:
            continue
        if fqdn in domains:
            return f"production '{prod_name}' (team '{team_id}')"
    owner = index.live.get(fqdn)
    if owner:
        team_id, kind, resource = owner
        excluded = exclude_env if kind == "environment" else exclude_service
        if not (team_id == own_team and resource == excluded):
            return f"{kind} '{resource}' (team '{team_id}')"
    return ""


def assert_public_hostname_free(
    settings: Settings,
    fqdn: str,
    *,
    own_team: str,
    exclude_production: str = "",
    exclude_env: str = "",
    exclude_service: str = "",
    allow_own_team_host: bool = False,
    index: NameIndex | None = None,
    purpose: str = "hostname",
) -> None:
    owner = find_owner(
        settings,
        fqdn,
        own_team=own_team,
        exclude_production=exclude_production,
        exclude_env=exclude_env,
        exclude_service=exclude_service,
        allow_own_team_host=allow_own_team_host,
        index=index,
    )
    if owner:
        raise ConflictError(
            f"Cannot use '{fqdn}' as {purpose}: it is already used by {owner}."
        )


def default_production_domain(settings: Settings, team: TeamSettings, name: str) -> str:
    """Default domain for a new production in a base-domain team.

    The team's first production gets the zone apex; later ones get
    ``<name>.<base_domain>``. Teams without a base_domain have no default —
    the domain must be passed explicitly.
    """
    from oduflow import production_registry

    if not team.base_domain:
        return ""
    if not production_registry.list_productions(team):
        return team.base_domain
    return f"{name}.{team.base_domain}"


def validate_production_domain(
    settings: Settings,
    team: TeamSettings,
    name: str,
    domain: str,
    *,
    is_primary: bool,
) -> str:
    """Validate one production domain against the team's zone policy.

    Primary domains in a base-domain team must be the apex or a subdomain of
    the zone; extra domains may be any FQDN as long as it does not fall in
    another team's zone (checked by the caller's free-name assertion).
    """
    domain = validate_domain(domain)
    if is_primary and team.base_domain and not in_zone(domain, team.base_domain):
        raise ValueError(
            f"Production domain '{domain}' is outside the team zone "
            f"'{team.base_domain}'. Use the zone apex or a subdomain such as "
            f"'{name}.{team.base_domain}'; arbitrary client domains go in "
            "extra_domains."
        )
    return domain
