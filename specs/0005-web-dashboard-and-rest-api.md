# 0005 — Web dashboard + REST API + interactive consoles

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `83054eb` "Web UI and small fixes" (2026-02-07)
**Key code today:** `web_ui.py` (Starlette app, REST API, auth middleware, WebSocket terminals), `templates/dashboard.html` (single-page UI), `templates/login.html`

## Context

The founding design ([[0001-mcp-orchestrated-ephemeral-per-branch-environments]])
exposed everything through MCP tools — a machine-first surface aimed at agents.
But the environments those agents create are real Docker containers with real
state, and a **human** needs a window into them: is my branch's environment up?
why did the install fail? what is the host load? which templates and services
exist? Driving all of that through an agent's tool-use loop is the wrong
ergonomics for a quick operational glance.

Forces at play:
- Agents *create* environments; humans mostly *observe* and occasionally
  *intervene* (stop, delete, rebuild, read logs). The two audiences want
  different affordances over the same underlying state.
- The dashboard must reflect **real Docker state**, not a parallel bookkeeping
  store that can drift from reality.
- Sometimes the human needs to drop into the running container — an Odoo shell
  or a `psql` prompt — without leaving the browser or holding SSH access to the
  host.

## Decision

Ship a **Starlette web application alongside the MCP server**, served by the
same process and fronted by the same Traefik routing
([[0004-stable-addressing-port-registry-and-traefik]]). It provides three things:

- **A single-page dashboard** (`dashboard.html`) that visualizes environments,
  templates, auxiliary services, volumes, extra-addon repos, and git
  credentials, with the small set of manual lifecycle actions a human needs.
- **A REST API** under `/api/*` that mirrors the MCP tool operations
  (list/create/start/stop/restart/recreate/delete environments,
  service/volume/preset/repo/credential management, stats, logs). The browser
  talks to this API; it is the same orchestration layer the tools call, so the
  UI never invents its own view of state.
- **In-browser interactive consoles** — an Odoo shell and a `psql` prompt —
  exposed as WebSocket-backed terminals that attach to the running container.

The dashboard is deliberately **visualization-first**: nobody is expected to
provision environments through it (agents do that over MCP). Creation-flow
polish, deep linking, and keyboard accelerators are explicit non-goals; the UI
exists to *show the machine* and allow occasional intervention.

## How it works (macro)

- **One process, two surfaces.** The `/mcp` route serves agents; the dashboard,
  `/api/*`, and the terminals serve humans. Both ultimately call into the same
  `docker_ops` orchestration, so a change an agent makes shows up in the UI and
  vice versa.
- **Polling, not a parallel store.** The dashboard polls `/api/*` for live
  status and stats and renders straight from Docker reality (a single polling
  interval that pauses while the browser tab is hidden).
- **Interactive consoles over WebSockets.** Opening a Console or SQL terminal
  upgrades to a WebSocket that bridges an `xterm.js` front-end to a shell
  (`odoo shell`) or `psql` session running inside the environment's container.
  This required the auth layer to authenticate WebSocket upgrades, not just
  ordinary HTTP requests.
- **Tenancy-aware.** Every dashboard/API call resolves to a team
  ([[0014-team-based-multi-tenancy]]) and is scoped to that team's resources,
  the same way tools are.

## Authentication evolution

Auth for the human surface changed shape as the consoles and hosting needs
matured:

1. **HTTP Basic auth.** The first dashboard gated `/` and `/api/*` behind a
   `BasicAuthMiddleware` checking the team's `ui_password`. Simple, but it pops
   the browser's native credential dialog and has no logout.
2. **WebSocket gap, then cookie fallback.** Browsers cannot send an
   `Authorization: Basic` header on a WebSocket handshake, so the new Console/SQL
   terminals were rejected with 403 while REST worked. The fix added a
   **signed cookie** the middleware validates for both HTTP and WebSocket scopes.
   An early version keyed the cookie HMAC off the password itself (deterministic,
   brute-forceable from one leaked cookie); it was promptly replaced with an
   `itsdangerous` token signed by a **persistent server-side secret** (stored
   `0600` in the data dir), expiring after 7 days, carrying a password
   fingerprint so changing `ui_password` revokes outstanding cookies.
3. **Session-cookie login form.** The Basic-auth dialog was finally replaced by
   a proper **`/login` page and `/logout`** (`#54`): unauthenticated page loads
   redirect to `/login`, `/api/*` returns `401 JSON`, WebSocket handshakes close
   with `1008`. **Basic auth is retained for API/CLI clients** — so scripts keep
   working while humans get a real login.
4. **Session-only UI with optional TOTP.** With the remote MCP CLI available,
   UI Basic authentication is removed. The operator login can require a local
   authenticator factor; shared environment links retain their own limited
   sessions. See [[0067-dashboard-totp]].

## Consequences

- Oduflow became a **two-audience tool**: an agent-facing MCP surface and a
  human-facing console over one shared orchestration core, neither owning a
  separate copy of state.
- Reusing the REST API as the MCP tools' sibling kept the UI honest — it shows
  what the tools would do, not a hand-maintained mirror.
- The interactive consoles made the host's Docker reality directly reachable
  from the browser, which in turn forced WebSocket-aware authentication and the
  move off Basic auth to signed session cookies.
- This human surface is where later work landed: the [[0007-auxiliary-services-and-volumes]]
  management tabs and the [[0022-engineers-console-design-system]] redesign both
  build on this dashboard + REST + WebSocket foundation.

## History

- `83054eb` (2026-02-07) — first Web UI (dashboard + REST API) alongside the MCP
  server; `1a50bfa` (2026-02-07) — follow-up UI fixes.
- `894f1ea` (2026-02-25) — interactive web console (Odoo shell) via `xterm.js` +
  WebSocket; authenticate WebSocket upgrades in the Basic-auth middleware.
- `15d0e59` (2026-02-25) — interactive SQL console (`psql`) on the same pattern.
- `9e24ab2` (2026-06-12, `#54`) — session-cookie auth with a `/login` form and
  logout; squashes the WebSocket cookie fallback (`a009824`) and the
  server-secret-signed cookie fix (`7b484f2`), keeping Basic auth for API
  clients.
- `b152559` (2026-06-12) — align the login page with the Engineer's Console
  system (see [[0022-engineers-console-design-system]]).
