# 0056 — Public URL scheme (`[routing] public_scheme`)

**Status:** Adopted (still in force)
**Type:** Architecture / Routing / Security
**First introduced:** `litnimax/lethal-bullfrog` branch (2026-09-04)
**Key code today:** `settings.py` (`public_scheme_setting` field on `Settings` and `TeamSettings`, `public_scheme` / `public_scheme_for(team)` / `any_public_scheme_https`, `_validate_public_scheme`), `docker_ops/system_ops.py` (`_trusts_upstream_headers`, `_ensure_traefik` drift control), URL construction sites in `docker_ops/env_ops.py`, `docker_ops/service_ops.py`, `docker_ops/production_ops.py` (`prod_url(settings, team, record)`), `server.py`, `web_ui.py`

## Context

Every URL Oduflow hands out — environment/service/production URLs, dashboard
share links, MCP endpoints, artifact links — hardcoded its scheme: `https://`
in traefik mode, `http://` in port mode. That baked in an assumption: a
`tls = false` traefik deployment is always fronted by an upstream TLS
terminator (a Cloudflare tunnel), so links stay `https://` and Traefik's `web`
entrypoint unconditionally trusts inbound `X-Forwarded-*` headers
([[0034]] introduced that entrypoint shape).

Two deployment realities broke the assumption. First, plain-HTTP-end-to-end
setups exist (LAN boxes, internal staging, local demos): with nothing
terminating TLS, every handed-out `https://` link pointed at an endpoint
nobody served. Second, the unconditional `forwardedHeaders.insecure` trust is
a hole when :80 is directly exposed — any client could forge
`X-Forwarded-Proto: https` (flipping request-security detection) or
`X-Forwarded-For` (evading the login rate limiter), since uvicorn trusts
Traefik's address.

## Decision

Add one setting, `[routing] public_scheme` (`"http"` | `"https"`), that names
the scheme of every URL Oduflow hands out. Unset, it derives the historical
default: `https` in traefik mode, `http` in port mode — so existing
deployments (including `tls = false` behind a tunnel) are untouched.

The same setting drives Traefik's forwarded-header trust: the `web`
entrypoint gets `forwardedHeaders.insecure=true` only for the
"upstream terminates TLS" shape (`tls = false` **and** effective scheme
`https`). Declaring `public_scheme = "http"` therefore both fixes the links
and closes the header-forgery hole, because "plain HTTP end to end" and "no
trusted terminator in front" are the same statement about the topology.

`validate()` rejects the combinations that contradict what actually answers on
the wire: `public_scheme = "http"` with traefik `tls = true` (the :80→:443
redirect would bounce every link and leak tokens on the plaintext first hop)
and `public_scheme = "https"` in port mode (published per-environment ports
serve plain HTTP; internal probes built from the same base URL would fail the
TLS handshake). The only meaningful override is thus traefik + `tls = false` +
`http`.

## How it works (macro)

`Settings` keeps the raw TOML value in `public_scheme_setting` and resolves it
through the `public_scheme` property (the raw field stays empty by default so
the derived default can depend on `routing_mode`). All URL construction sites
interpolate `settings.public_scheme`; `prod_url` gained a `settings`
parameter for the same reason.

`_ensure_traefik`'s drift control ([[0034]]) compares the running container's
`forwardedHeaders.insecure` arg against `_trusts_upstream_headers(settings)`
and recreates the container on mismatch — so flipping `public_scheme` takes
effect on the next server start. Environments and services need no
recreation: their routing labels do not encode the scheme.

## Consequences

- Plain-HTTP deployments hand out reachable links, and their :80 entrypoint
  no longer believes client-supplied forwarded headers.
- Cookie `Secure` flags and request-security checks stay derived from the live
  request (`X-Forwarded-Proto`), never from `public_scheme`; with trust off,
  Traefik sanitizes those headers, keeping the two views consistent.
- `public_scheme = "http"` implies remote OAuth-based MCP clients (which
  require https issuers) cannot connect — inherent to plain HTTP and
  documented in `docs/traefik.md`, along with the cleartext warning.
- The default `tls = false` shape still trusts forwarded headers (it must, for
  tunnels); an exposed-:80-without-terminator misconfiguration only becomes
  safe once the operator declares `public_scheme = "http"`.

## Evolution

**Per-team override (2026-09-13).** `public_scheme` became overridable per
team (`[team.X] public_scheme`), resolved through
`Settings.public_scheme_for(team)`; every team-scoped URL site interpolates
the resolved value instead of the global one. The motivating shape: one
deployment where a LAN team hands out `http://` links while another team is
published through a Cloudflare tunnel with `https://` links — same Traefik on
plain `:80`.

This **changes the trust rule stated in the Decision above**: Traefik's
forwarded-header setting is entrypoint-scoped, so the `web` entrypoint now
gets `forwardedHeaders.insecure=true` whenever `tls = false` and *any* team
resolves to https (`Settings.any_public_scheme_https`, resolved per-team
values only). Consequently `[routing] public_scheme = "http"` alone no longer
guarantees the header-forgery hole is closed — a single team's `https`
override re-opens entrypoint-wide trust, for every router on `:80` including
proxy-mode production Odoo. "Plain HTTP end to end" and "no trusted
terminator" are still the same statement, but now only when it holds for
**all** teams. The blast radius and the resulting operational rule (port 80
must be reachable only from networks trusted for all teams; otherwise split
deployments) are documented in `docs/traefik.md` and in the
`_trusts_upstream_headers` docstring. A meaningful override is now also
per-team `https` on an otherwise-http deployment, not just the global
traefik + `tls = false` + `http`.

Production URLs (`prod_url`, now `(settings, team, record)`) use the owning
team's scheme; a production domain is free-form, so it is assumed to be
fronted the same way as its team's other hosts.

## History

- Introduced on the `litnimax/lethal-bullfrog` branch (2026-09-04).
- Per-team override, `per-team-acme-public-scheme` branch (2026-09-13).
