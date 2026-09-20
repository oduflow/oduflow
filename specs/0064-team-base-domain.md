# 0064 — Team base domain and multi-domain productions

**Status:** Adopted
**Type:** Routing / Tenancy
**First introduced:** 2026-09-16
**Key code today:** `domains.py`, `settings.py` (`TeamSettings.base_domain`), `naming.py` (`get_env_hostname`), `docker_ops/env_ops.py`, `docker_ops/service_ops.py`, `docker_ops/production_ops.py` (`prod_host_rule`, `_normalize_extra_domains`)

## Context

A team's single `hostname` served two roles: the dashboard/OAuth identity and
the parent of every environment and service hostname
([[0004-stable-addressing-port-registry-and-traefik]]). Once production
hosting put the dashboard itself on a subdomain (`oduflow.demo.example.com`),
branch environments landed two levels deep
(`feature.oduflow.demo.example.com`) while the production sat at the apex
(`demo.example.com`) — an inconsistent, hard-to-certify layout. The slots
mode ([[0048-reusable-environment-hostname-slots]]) already implied a
"parent domain" by splitting the team hostname, but only for numbered or
explicit short hostnames, never for the default branch-derived names.
Productions also had exactly one domain each, though hosted clients routinely
need a team-zone name *and* their own public domain on the same instance.

## Decision

Make the team's DNS zone an explicit, first-class setting: `base_domain`
(e.g. `demo.example.com`). When set (Traefik mode only):

- environments and services are named directly under the zone
  (`feature.demo.example.com`), siblings of the dashboard;
- the dashboard `hostname` defaults to `oduflow.<base_domain>`;
- production primary domains must be the zone apex or a subdomain; the
  team's first production defaults to the apex, later ones to
  `<name>.<base_domain>`;
- productions gain `extra_domains` — additional arbitrary FQDNs (client-owned
  domains) routed to the same container via one multi-`Host()` Traefik rule.

The zone is exclusive: no other team's hostname, base domain or production
domain may live inside it. A new `domains.py` module is the single authority
for the global `Host()` namespace — every name Oduflow hands out
(environment, service, production primary or extra domain) is checked against
team hostnames, other teams' zones, all teams' production domains, static
`[route.*]` hosts and the names live containers serve before it is claimed.
Environments and services hold their claim only as a Traefik label, so that
last check reads the rules back off the containers; it is best-effort, and an
unreachable Docker daemon degrades to the configured-names check rather than
blocking every create.

`domains.py` also owns the question of *which* domain a short name hangs off
(`service_hostname`, `env_hostname`). That rule was originally restated at
each call site and the copies drifted, which is the class of bug this
consolidation exists to prevent.

Unset `base_domain` preserves the legacy nested layout exactly.

## How it works (macro)

- `get_env_hostname` gains a `base_domain` parameter that short-circuits the
  legacy prefix-splitting; slot allocation and usage scanning derive the
  parent domain from the zone when present.
- There is no migration: existing environments keep their nested hostname
  until their next `update_environment`, which recomputes the route from
  current settings (the established repair path for routing changes) and
  moves them into the zone. Because of that, anything reporting a live URL
  reads the container's own `Host()` rule rather than recomputing the name
  from current settings — otherwise every pre-existing environment would be
  advertised at a name Traefik does not route.
- The production Traefik rule is produced by one shared helper
  (`prod_host_rule`), also used by the Stack drift check
  ([[0063-production-stack-targets]]) so intent and container always compare
  the same expression. Let's Encrypt issues certificates covering every
  domain in the rule.

## Consequences

- One wildcard DNS record (`*.demo.example.com`, plus the apex) covers the
  dashboard, every environment, every service and default production names.
- Environment/service names now share one level with production domains and
  the dashboard prefix; the global namespace check turns silent Traefik
  routing ambiguity into an explicit `ConflictError` at claim time.
- Multi-team deployments get a hard tenancy boundary on names: a team cannot
  squat a FQDN inside another team's zone, including via production
  `extra_domains`.
- Client-owned domains no longer require operator-level `[route.*]` entries
  ([[0034-external-traefik-routes]]) to reach a production.

## History

- `cheerful-hedgehog` branch (2026-09-16) — base_domain setting, flattened
  environment/service hostnames, production domain zone policy with apex
  default, `extra_domains`, global hostname collision checks.
