# 0036 — Cross-subdomain "Connect as user" landing (token → env-host cookie)

**Status:** Adopted
**Type:** Routing / Web dashboard
**First introduced:** `litnimax/connect-as-user-selects` branch (2026-07-19)
**Key code today:** `connect_tokens.py` (one-time token store), `web_ui.py` (`api_connect_open` traefik branch, `api_connect_land`, `_PUBLIC_PATHS`), `docker_ops/system_ops.py` (`_write_traefik_dynamic_config` `oduflow-connect` router), `templates/dashboard.html` (Connect As modal)

## Context

[[0031-connect-as-user-impersonation]] mints a passwordless Odoo session
server-side and hands back the `session_id` cookie + URL — enough for Playwright
(which injects the cookie into its context) but not for a human clicking a
dashboard button: a browser can't be handed a cookie for another host from
JavaScript (Odoo issues `session_id` HttpOnly), and the dashboard's "Open" only
worked when it shared a host with the env (port/local mode). 0031 explicitly
deferred the "token-URL" convenience form and assumed its only clean home was an
addon installed inside every Odoo env — the platform-layer approach exists
precisely to avoid that.

Under Traefik routing the env lives on its own subdomain (`<slug>.<team-host>`,
[[0004-stable-addressing-port-registry-and-traefik]]) while the dashboard is on
the parent host. Two "obvious" cross-domain cookie tricks both fail:

- A **host-only** cookie set from the dashboard response is scoped to the
  dashboard host; the browser never sends it to the env subdomain.
- A **parent-domain** cookie (`Domain=<team-host>`) does reach the subdomain, but
  per RFC 6265 it is a *distinct* cookie from any host-only `session_id` Odoo
  already set on the env host — it does not override it, both are sent, and the
  older (stale) one is sent first. So it is unreliable exactly when the user has
  already opened the env, and it shares one session across all of a team's env
  subdomains (one login at a time, sid broadcast to siblings).

## Decision

Set the env's session cookie **from the env's own host**, like odoo.sh — but
without an Odoo addon. Reserve one path on every env host, intercept it at the
Traefik layer, and route it to the Oduflow server, which sets the cookie
host-only there.

Flow:

1. The dashboard's Connect action calls `api_connect_open`. In traefik mode it
   mints the session (existing `connect_as_user`), stashes the `session_id`
   behind a **one-time, short-lived, host-bound token** (`connect_tokens`), and
   303-redirects the browser to `https://<env-host>/oduflow-connect?token=…`. No
   cookie is set on the dashboard response.
2. A file-provider Traefik router `PathPrefix(/oduflow-connect)` (high priority,
   beating the env's docker `Host(...)` router for that path only) sends that
   request to Oduflow — not Odoo.
3. `api_connect_land` (public, authenticated solely by the token) consumes the
   token and sets `session_id` **host-only** on the env host — which overrides
   any stale host-only cookie Odoo left there — then 303s to `/web`, landing
   authenticated.

Port/same-host mode is unchanged (the dashboard sets the cookie directly and
redirects). The `connect_as_user` cookie payload remains for Playwright/API
callers; the dashboard's own cookie-copy affordances are retired in favour of the
one-click open.

## How it works (macro)

- **One-time token store** (`connect_tokens`): in-process (single-process server,
  like `agent_sessions`). `issue(env_host, sid)` returns a random
  `token_urlsafe`; `consume(token, env_host)` returns the sid exactly once,
  rejecting unknown/used/expired/host-mismatched tokens (120 s TTL). The token,
  not a dashboard session, is the sole credential at the landing — so
  `/oduflow-connect` is in `_PUBLIC_PATHS`.
- **Traefik interception** reuses the dynamic-config machinery of
  [[0034-external-traefik-routes]]: one extra router in `oduflow.yml`, service
  `oduflow` (already `host.docker.internal:<port>`), `_route_entrypoint` for
  web/websecure. Because it matches only `/oduflow-connect`, every other path on
  the env host still reaches Odoo. A high explicit `priority` removes
  cross-provider ambiguity with the docker `Host()` router.
- **Host-only override is the crux.** The cookie is set on a response served *for
  the env host*, so it shares the (name, host-only, path) identity of Odoo's own
  `session_id` and replaces it — the reliability the parent-domain cookie can't
  give.
- **Dashboard UX**: Connect As becomes one action — pick a user, click Connect, a
  new tab opens already logged in. The browser flow no longer needs the
  `/connect-as` cookie round-trip, so the URL/cookie/Copy/Show fields and the
  explanatory notes are dropped; programmatic callers use the `connect_as_user`
  MCP tool instead.

## Consequences

- The human "Connect as user" loop works across env subdomains with a single
  click, closing the gap 0031 left — and without the per-env Odoo addon 0031
  assumed would be required.
- **Per-host cookies**: each env host gets its own `session_id`, so multiple envs
  can be open at once and no session token is broadcast to sibling subdomains —
  strictly better than the parent-domain alternative that was considered and
  rejected.
- The token is a full session behind a 120 s one-time handle; it can appear in the
  tab's URL/history but is single-use and host-bound, and only issued to an
  already-authorized dashboard user. This narrows 0031's "session id is a live
  credential" exposure for the browser path — the sid never rides in the URL,
  only the one-time token does.
- Adds one reserved path (`/oduflow-connect`) and one Traefik router; no new
  config knobs. Traefik mode only — port/local mode keeps the shared-host cookie.

## History

- `litnimax/connect-as-user-selects` (2026-07-19) — one-time token store, traefik
  branch in `api_connect_open`, public `api_connect_land` landing,
  `oduflow-connect` high-priority PathPrefix router, and the simplified
  single-click Connect As dialog. Realises the token-URL form deferred in
  [[0031-connect-as-user-impersonation]].
- `feat/mcp-connect-link` (2026-11-05) — the same landing exposed to MCP clients
  as `connect_link(env_name, user="admin")`, returning the bare one-time URL for
  "Open as admin" buttons in other apps; link building shared with the dashboard
  via `connect_tokens.login_link`. Requires Traefik routing and the HTTP
  transport (the token is redeemed in-process by the web UI).
  The tool's token carries a grant (team, environment, user), not a session:
  the landing mints the Odoo session on redemption, under the env lock, so an
  unused link leaves no live session behind. The user is checked up front via
  psql. Credential-bearing tool results are marked `@secret_result` and kept out
  of the server log, and `token=` query values are redacted from the access log.
