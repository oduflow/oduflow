# 0055 — Shared single-environment dashboard (`/env/<name>` + share links)

**Status:** Adopted
**Type:** Architecture / Web capability
**First introduced:** 2026-09-02
**Key code today:** `ui_scope.py` (default-deny allowlist, scoped cookie name), `env_share.py` (per-team `shares.json` secrets), `web_ui.py` (`scoped_env_page`, share cookie mint/verify, `UIAuthMiddleware` scoped principal, `api_share_*`), `templates/dashboard.html` (`data-scoped-env`, Share modal, scoped card rendering)

## Context

[[0028-scoped-environment-mcp-access]] solved this problem for *agents*: an
operator can hand out a `/mcp/<env>` URL plus a per-environment Bearer token and
know the holder cannot reach anything else the team owns. The dashboard had no
counterpart. Its authentication is per team and all-or-nothing: the
`ui_password` opens every environment plus templates, services, volumes, extra
repos, credentials, productions and host statistics.

So a hosting customer who wanted to show a client their staging environment —
or let them click around, watch the logs, or drive the coding agent in
chat — had to give away the team password. In practice that meant either not
sharing at all, or sharing far more than intended. The people this matters for
are not agents and often not developers: they want a browser tab, not an MCP
client.

## Decision

Serve the **same dashboard, scoped by URL**, at `/env/<name>`, and give each
environment a **share secret** the operator can mint, regenerate and revoke from
the environment card. Opening `https://<team-host>/env/<name>?key=<secret>` exchanges the
key for a signed, host-only cookie and lands on the clean `/env/<name>`.

- **One page, two faces.** The scoped view is `dashboard.html` rendered with a
  `data-scoped-env` attribute, not a second template. Agent Chat, the consoles,
  the logs viewer and the module dialogs are the ones operators already use, so
  they cannot drift apart.
- **The server is the boundary, not the rendering.** A default-deny allowlist
  (`ui_scope.is_allowed`) runs in the auth middleware for every request a scoped
  session makes — HTTP and WebSocket — and any env-addressed path must address
  the cookie's own environment. Hiding buttons is presentation; this is the
  policy.
- **Agent Chat yes, Agent CLI no.** Chat is the point of sharing (a client grows
  their Odoo by talking to the agent). The CLI is a PTY in the *per-team* agent
  container, whose `/workspace` holds a checkout of every environment of the
  team ([[0029-agent-console-and-chat]]), so it is not a single-environment
  surface and is denied.
- **Secrets in a registry, not a container label.** Unlike the MCP token, the
  share secret lives in the team's `shares.json`. Labels cannot be added to a
  live container, which would have made every pre-existing environment
  unshareable — unacceptable for a feature whose whole purpose is to share the
  environment you are looking at right now.

The dev-loop actions (start/stop/restart/sync, install/upgrade modules,
consoles, Connect As, and the `/mcp/<env>` credentials) are exposed
deliberately: a share link is for someone who is meant to *work in* the
environment, and the scoped MCP endpoint already grants equivalent in-environment
power to anyone holding its token.

## How it works (macro)

- **Link → session.** `/env/<name>` is outside the team login and authenticates
  itself. With `?key=`, the secret is verified against each team's share
  registry (host-matched team first), traded for a cookie signed with the
  server secret under its own salt, and dropped from the URL by a redirect, so
  it does not persist in history or a `Referer`. The cookie embeds a
  fingerprint of the secret, so regenerating or revoking the link invalidates
  live sessions at once — the same mechanism that makes a password change
  invalidate operator cookies.
- **Cookie → principal.** The auth middleware, having found no team credential,
  resolves the scoped cookie to *(team, environment)* and stores both on the
  request. Handlers keep acting as the team; the environment is the restriction.
- **Policy, default-deny.** Every scoped request is matched against the
  allowlist before reaching a handler; a mismatch is a 403 (or a 1008 WebSocket
  close). Page *navigation* out of scope is recovered rather than refused: a
  non-API `GET`/`HEAD` is redirected back to `/env/<name>`, so a stray click or
  an old bookmark returns the visitor to their workspace instead of showing
  them a bare error. The environment list endpoint is allowed but filtered
  server-side to the one environment, mirroring how the scoped MCP endpoint
  filters `tools/list`.
- **Operator UX.** The card's **Share UI** action shows the link masked, with
  Copy, Regenerate and Revoke. The share routes are themselves outside the
  allowlist, so a shared session cannot read, reissue or revoke the link it
  arrived on.

## Consequences

- An environment can be handed to a **non-technical client in a browser** —
  card, logs, consoles, Connect As, Agent Chat — with no team password and no
  reach into any other environment or team-wide resource.
- **Revocation is real and immediate**, per environment, and does not require
  recreating anything (contrast the MCP token, which rotates only by recreating
  the environment).
- **Not a cryptographic confinement.** Agent Chat runs in the per-team agent
  container, so the agent a shared visitor drives can read other environments'
  checkouts and the team's git credentials. The link bounds the dashboard
  surface; it does not sandbox the agent. Making it a hard boundary would need a
  per-share agent container — deliberately out of scope here, and documented as
  a caveat in `docs/security.md`.
- **One scoped session per browser and host.** The cookie is single-valued, so
  opening a second share link on the same host replaces the first. Reopening
  either link restores it.
- Sharing is **operator-controlled and audit-visible**: `shares.json` records
  which environments are shared and since when.

## History

- 2026-09-02 — shared single-environment dashboard: `/env/<name>` page, per-env
  share secrets with rotate/revoke, default-deny scoped allowlist in the auth
  middleware, Share modal on the environment card.
- 2026-09-24 — dead ends made recoverable: out-of-scope page navigation
  redirects back to `/env/<name>` instead of a 403, and leaving the shared view
  lands on a page that names both ways back in. A revoked share cookie no
  longer suppresses an operator's own sign-out.
