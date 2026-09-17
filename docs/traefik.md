# Traefik Routing (Auto-HTTPS)

By default Oduflow uses **port mode**: each environment gets a dedicated host port (e.g. `http://server:50001`). This is simple and works well for local or single-developer setups.

For production-like access with HTTPS, Oduflow can deploy a **Traefik** reverse proxy that gives every environment its own subdomain with an automatically issued Let's Encrypt certificate.

## Setup

1. **Configure a wildcard DNS record.** Point `*.dev.example.com` to your server's IP address:

   ```
   *.dev.example.com  →  A  →  203.0.113.10
   ```

   Every environment will get a subdomain: `feature-login.dev.example.com`, `fix-invoice.dev.example.com`, etc.

2. **Set the configuration** in `oduflow.toml`:

   ```toml
   [routing]
   mode = "traefik"
   acme_email = "admin@example.com"

   [team.1]
   hostname = "dev.example.com"
   environment_slots = 20
   environment_hostname_mode = "branch"
   ```

3. **Start (or restart) Oduflow.** On startup, Oduflow will create a Traefik v3 container that:
   - Listens on ports 80 and 443
   - Automatically redirects HTTP to HTTPS
   - Obtains a TLS certificate from Let's Encrypt for each routed hostname via HTTP-01 challenge
   - Routes requests to the correct Odoo container based on the subdomain
   - Also routes the Oduflow server itself via the team `hostname`

## Hostname and certificate strategies

The default `environment_hostname_mode = "branch"` keeps descriptive and
backward-compatible routes: `feature-login.dev.example.com`,
`fix-invoice.dev.example.com`, and so on. `environment_slots = 20` limits how
many environments may exist but does not change those names. This mode matches
the `*.dev.example.com` DNS record shown above and works with a Cloudflare or
other wildcard certificate for `*.dev.example.com`.

For Traefik HTTP-01 installations that need to bound Let's Encrypt issuance,
opt into a reusable pool:

```toml
[team.1]
hostname = "dev.example.com"
environment_slots = 20
environment_hostname_mode = "slots"
```

Oduflow then allocates `dev1.example.com` through `dev20.example.com` and
returns names to the pool on deletion. These names are one DNS level higher
than branch-derived routes: configure individual `dev1`…`dev20` records or a
wildcard record for `*.example.com`. A `*.dev.example.com` record or certificate
does not cover `dev1.example.com`.

`create_environment(hostname="qa")` requests `qa.example.com` in either mode.
The configured team hostname must include a distinct prefix
(`dev.example.com`, not bare `example.com`) for pooled or explicit short names.

## Team base domain

Setting `base_domain` gives the team one flat DNS zone instead of nesting
everything under the dashboard hostname:

```toml
[team.1]
base_domain = "demo.example.com"
# hostname defaults to "oduflow.demo.example.com" (the dashboard)
```

With a base domain, environments and services live **directly under the
zone** — `feature-login.demo.example.com`, `meilisearch.demo.example.com` —
instead of `feature-login.oduflow.demo.example.com`, and production domains
default into the zone too (the apex `demo.example.com` for the team's first
production, `<name>.demo.example.com` afterwards; see
[Production Hosting](production.md#domains)). One `*.demo.example.com` DNS
record (plus the apex, if a production uses it) covers everything.

The zone is exclusive to the team, and every name handed out is checked
against the whole routed namespace — the dashboard hostname, production
domains of all teams, static `[route.*]` hosts, other teams' zones and the
names live environment and service containers currently serve — so two
resources can never claim the same FQDN. Existing environments keep their old
nested hostname until their next `update_environment`, which moves them into
the zone; until then that is the name they are reported at and checked
against, because a container's Traefik rule is fixed when it is created.

A service with `routes` is the one deliberate exception: it publishes only
`Host() && PathPrefix()` routers and no catch-all, so it may share the team's
dashboard hostname to expose a URL prefix beside the dashboard.

## OAuth on each team's hostname

The self-hosted [OAuth Authorization Server](security.md#self-hosted-oauth-for-claudeai-and-other-mcp-clients) is enabled automatically whenever a team has an `auth_token` and runs on **each team's own hostname** in every routing mode. With Traefik, the incoming host already has a Let's Encrypt certificate; with `tls = false`, the upstream tunnel provides it. There is no separate OAuth section: point Claude.ai at `https://<team-hostname>/mcp` and complete the OAuth flow there.

## Service routing with Traefik

Auxiliary services also get Traefik routing. A service named `meilisearch` under team hostname `dev.example.com` becomes accessible at `https://meilisearch.dev.example.com`; with a team `base_domain` it attaches to the zone instead (`meilisearch.demo.example.com`). Custom hostnames are also supported.

## Routing extra domains to external services

Traefik in Oduflow can also forward a domain to a service that Oduflow does
**not** manage — another Docker container, a process on the host, or a machine
elsewhere. There are two ways, from simplest to most flexible.

### 1. Declarative routes in `oduflow.toml`

For the common "this hostname → that URL" case, add a `[route.<name>]` section:

```toml
[routing]
mode = "traefik"
acme_email = "admin@example.com"

[team.1]
hostname = "dev.example.com"

[route.legacy-api]
host = "api.example.com"
url  = "http://127.0.0.1:3000"
```

On the next start Oduflow generates a Traefik router for `api.example.com` and
forwards it to `http://127.0.0.1:3000`. In TLS mode the route gets its own
Let's Encrypt certificate (point the domain's DNS at this server first), exactly
like a team hostname; behind a `tls = false` upstream it is served over plain
HTTP on port 80.

Notes:

- **`127.0.0.1` / `localhost` mean "on the Docker host".** Traefik runs in a
  container, so Oduflow rewrites an `http://` loopback upstream to
  `host.docker.internal` (mapped to the host gateway). So `http://127.0.0.1:3000`
  reaches a service listening on port 3000 of the host. Use the real IP/hostname
  for anything off the host. An `https://localhost` upstream is **not** rewritten
  (that would break backend TLS certificate verification) — for a TLS backend on
  the host, use its real hostname or a drop-in dynamic file with a
  `serversTransport`.
- `url` must be `http://…` or `https://…`; `host` must be a plain hostname (no
  path) and unique across all routes and team hostnames.
- These routes are declared once in config; the generated router set is
  rewritten on every restart, so hand-editing the generated file is pointless
  (use option 2 for custom Traefik config).

### 2. Drop-in Traefik dynamic files

For anything the simple `host → url` form can't express — middleware, header
rewrites, custom TLS options, sticky sessions, multiple services — Oduflow
mounts a **dynamic-config directory** that Traefik watches:

- On the host it is `<config-dir>/traefik-dynamic/` — `/etc/oduflow/traefik-dynamic/`
  when writable, otherwise `~/.oduflow/conf/traefik-dynamic/`.
- Oduflow writes and overwrites only `oduflow.yml` there (its own routers). Any
  **other** `*.yml`/`*.yaml`/`*.toml` file you place in that directory is loaded
  by Traefik and **never touched by Oduflow** — it survives restarts and
  upgrades.

For example, `<config-dir>/traefik-dynamic/custom.yml`:

```yaml
http:
  routers:
    my-app:
      rule: "Host(`app.example.com`)"
      entryPoints: ["websecure"]
      tls:
        certResolver: letsencrypt
      service: my-app
      middlewares: ["my-headers"]
  middlewares:
    my-headers:
      headers:
        customRequestHeaders:
          X-Forwarded-Proto: "https"
  services:
    my-app:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:9000"
```

Traefik picks it up within a second (no restart needed). This is the full
Traefik [file-provider dynamic configuration](https://doc.traefik.io/traefik/providers/file/),
so use it when you outgrow the declarative routes above.

## Behind a Cloudflare tunnel (or other TLS-terminating upstream)

If HTTPS is terminated upstream — for example by a **Cloudflare tunnel** (`cloudflared`) that already serves a valid certificate — Traefik should not obtain its own certificates or redirect to HTTPS. Set `tls = false`:

```toml
[routing]
mode = "traefik"
tls = false          # Traefik listens on plain HTTP :80 only

[team.1]
hostname = "dev.example.com"
```

With `tls = false` Traefik:

- Listens on **port 80 only** (443 is not published), serving plain HTTP.
- Does **not** redirect HTTP→HTTPS and does **not** request Let's Encrypt certificates (`acme_email` is not required).
- Routes by the same `Host` rules as before, so each environment keeps its own subdomain.

Point the tunnel at the server's port 80 and route the wildcard hostname to it (e.g. `*.dev.example.com → http://localhost:80`). Cloudflare provides the certificate and forwards requests over HTTP; the tunnel sets `X-Forwarded-Proto: https`. On the `web` entrypoint Oduflow enables `forwardedHeaders.insecure` so Traefik passes those headers through (by default Traefik would overwrite `X-Forwarded-Proto` with the plain-HTTP connection scheme), letting Oduflow see the request as secure — the dashboard's session cookie stays `Secure` and every environment/service URL Oduflow reports is still `https://…`. Because this entrypoint trusts all forwarded headers, expose port 80 **only** to the tunnel, not to the public internet.

> **Changing `tls` on a running deployment recreates Traefik but not your environments.** Each environment and service bakes its Traefik routing labels in at creation time — `entrypoints=websecure` (with Let's Encrypt) when `tls = true`, `entrypoints=web` when `tls = false`. Restarting Oduflow recreates the Traefik container in the new mode, but pre-existing environments and services keep their old labels: after the switch their routers point at an entrypoint that no longer matches, so they become unreachable until **recreated** (or, going `false → true`, get caught by the HTTP→HTTPS redirect). Treat `tls` as a deploy-time choice; if you must flip it on a live server, recreate the existing environments and services afterwards. The default is `tls = true` (Traefik terminates TLS with Let's Encrypt, as described above).

## Plain HTTP, with nothing terminating TLS

`tls = false` on its own means "**someone else** terminates TLS", so the links
Oduflow hands out — environment and service URLs, dashboard share links, MCP
endpoints — stay `https://`. If there is no such upstream (a LAN box, an
internal staging server, a local demo), those links point at an endpoint nobody
serves. Say so explicitly with `public_scheme`:

```toml
[routing]
mode = "traefik"
tls = false
public_scheme = "http"   # no TLS anywhere: hand out http:// links

[team.1]
hostname = "dev.example.com"
```

`public_scheme` sets the scheme of every URL Oduflow reports; it defaults to
`https` in traefik mode and `http` in port mode, and is independent of `tls`
(which only decides whether *Traefik* terminates TLS). It also switches off
`forwardedHeaders.insecure` on the `web` entrypoint: with no trusted terminator
in front, that entrypoint is directly reachable and must not believe a
client-supplied `X-Forwarded-Proto`.

Changing `public_scheme` recreates the Traefik container (the forwarded-headers
argument changes) but not your environments — only the reported URLs change, so
no environment or service needs recreating.

!!! warning "Plain HTTP is unencrypted"
    Odoo logins, session cookies, the dashboard password and MCP bearer tokens
    all travel in cleartext. Use this only on a trusted network, never on a
    public-facing server.

## Mixing HTTP and HTTPS teams in one deployment

`public_scheme` can be overridden per team. The typical shape: one team is
reached directly over the LAN in plain HTTP, another is published through a
**Cloudflare tunnel** that terminates TLS — same server, same Traefik on plain
`:80`:

```toml
[routing]
mode = "traefik"
tls = false              # Traefik listens on plain HTTP :80 only
public_scheme = "http"   # default for teams without an override (the LAN team)

[team.1]
hostname = "dev.internal.example.com"   # LAN, http:// links

[team.2]
hostname = "dev.example.com"            # via Cloudflare tunnel
public_scheme = "https"                 # its links are https://
```

Point the tunnel at the server's port 80 for the second team's hostnames
(e.g. `dev.example.com` and `*.dev.example.com → http://localhost:80`); the
first team's clients resolve its hostname to the server directly. Every URL
Oduflow hands out — dashboard links, MCP endpoints, environment and service
URLs — uses each team's resolved scheme. No `acme_email` is involved: with
`tls = false` Traefik never talks to Let's Encrypt, and the tunnel's
certificate comes from Cloudflare.

!!! warning "Forwarded-header trust is deployment-wide"
    Because at least one team resolves to `https`, the `web` entrypoint trusts
    inbound `X-Forwarded-*` headers (as in the tunnel setup above) — so the
    tunnel's `X-Forwarded-Proto: https` survives. That trust is
    **entrypoint-wide**, not per team: any client that can reach port 80
    directly can forge `X-Forwarded-Host`, `X-Forwarded-Proto` and
    `X-Forwarded-For` on requests to *any* hostname this Traefik serves —
    including the other team's environments and productions. Production Odoo
    runs in proxy mode and uses those headers to rebuild absolute URLs
    (`web.base.url`, password-reset links) and for IP logging and login
    throttling. Only mix schemes when every network that can reach port 80 is
    trusted for **all** teams on the deployment; otherwise split the teams
    onto separate deployments.

The per-team value obeys the same rules as the global one: `https` is invalid
in port mode, and `http` is invalid while `tls = true` (the :80→:443 redirect
would break the links).

Production URLs are reported with the **owning team's** scheme. A production's
domain is free-form (it need not live under the team's hostname), so make sure
each production domain is fronted the same way as the rest of its team — a
plain-HTTP team's production published through the other team's tunnel would be
reported as `http://` even though only `https://` answers.
