# 0067 — Local dashboard TOTP and session-only UI authentication

**Status:** Adopted
**Type:** Architecture — UI authentication model
**First introduced:** this change (2026-09-19)
**Key code:** `ui_totp.py`, `web_ui.py` (`UIAuthMiddleware`, login/session helpers), `server.py` (`ui-2fa` CLI)

## Context

Operators wanted an authenticator-app second factor for the Oduflow dashboard
without Cloudflare or another external identity service. The existing identity
is a team with a shared UI password, rather than individual user accounts.
The priority was a small, locally administered addition to that model.

Basic authentication had survived the move to browser sessions to support old
REST scripts. After [[0051-remote-mcp-cli-client]], automation has a dedicated
MCP client and credential. Leaving password-only Basic enabled would bypass any
second factor placed on the login form.

## Decision

Use optional per-team TOTP at operator login, administered through the local
server CLI. Remove UI Basic authentication for all teams. The browser's session
cookie authenticates UI HTTP and WebSocket requests. MCP authentication stays
independent; remove the obsolete REST script helpers.

Keep [[0055-scoped-environment-ui-sharing]] links exempt from OTP, explicitly
preserving their existing environment scope and server-side allowlist. A shared
session must never become an operator session.

## How it works

`ui-2fa setup` prints a locally generated QR code and manual key, and enables the
factor only after a correct confirmation code. `ui-2fa reset` is a local recovery
operation, asks for confirmation, and returns the UI to password-only login.
Neither action is exposed through the UI or MCP. Commands use the server's
configuration and persistent team data, without starting Docker or the server.

One login form verifies password and TOTP before issuing a full session. The
session carries the exact authentication generation checked at login; enrollment
or reset changes that generation, invalidating existing full cookies. Dashboard
loads no longer renew the seven-day lifetime. Generation changes are seen on
subsequent requests and new WebSocket handshakes, not existing terminal streams.

A protected team file holds the TOTP secret, revocation generation, last consumed
time step, and failed attempts. File locking and atomic replacement coordinate
CLI and server processes and prevent concurrent reuse of a code. Invalid state
fails closed. The team attempt limit persists through restarts, supplementing
the existing per-IP login throttle.

## Consequences

- No additional running service, external requests, or identity-provider setup.
- The factor is shared at team level; personal enrollment and per-person audit
  would require a separate user identity model.
- Recovery trusts server administration access. There are no backup codes in
  this version; between reset and re-enrollment, login needs only the password.
- The persistent factor file and its backups are credentials. Deleting the file
  is not a supported reset procedure; the CLI preserves revocation history.
- Basic-based external integrations must migrate to MCP. Existing operator
  cookies are invalidated once on upgrade; scoped share cookies are unchanged.

## History

- 2026-09-19 — agreed and implemented in this change; related UI evolution is
  recorded in [[0005-web-dashboard-and-rest-api]].
