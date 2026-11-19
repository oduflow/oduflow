# 0085 — One-time browser sign-in links over MCP (`connect_link`)

**Status:** Adopted
**Type:** MCP capability / Routing / Security
**First introduced:** `feat/mcp-connect-link-v2` (2026-11-19)
**Key code:** `server.py` (`connect_link`, `handle_errors`), `connect_tokens.py` (`LinkGrant`, `issue_link` / `peek_link` / `discard`), `web_ui.py` (`api_connect_land`, `_connect_redeem`), `templates/connect.html`, `docker_ops/odoo_ops.py` (`resolve_env_user`), `log_redaction.py`, `scoped_access.py`

## Context

[[0031-connect-as-user-impersonation]] gives an MCP client a raw `session_id`
cookie. That serves browser automation, which owns the browser and can inject
it. It does not serve a person: an application that drives Oduflow over MCP and
wants an "Open as admin" button cannot set a cookie for the environment's host
from its own origin.

[[0036-cross-subdomain-connect-as-landing]] already solved the cookie problem
for the dashboard's own Open button — a one-time, host-bound token redeemed at
`/oduflow-connect` on the environment's host. But its entry point needs a
dashboard login, which the external application's user does not have, and its
shape assumes an immediate redirect by an authenticated browser: the session is
minted first and the first GET of the landing consumes the token.

A link handed out over MCP lives differently. It passes through another program
and often through a chat window before a person clicks it; it may never be
opened; and link previewers and mail scanners fetch URLs before people do.

## Decision

Add an MCP tool `connect_link(env_name, user="")` that returns a one-time URL on
the environment's host, reusing the 0036 landing and token store, with three
changes in how a token for a *relayed* link behaves:

1. **The session is minted at redemption, not at issue.** The token stores a
   grant — team, environment, the user selector — instead of a `session_id`.
   An unopened link expires as a dictionary entry and leaves no live Odoo
   session behind.
2. **A GET changes nothing.** Opening the URL renders a small page; the sign-in
   happens in the POST that page makes. A previewer or scanner that fetches the
   URL neither spends the link nor creates a session. The page continues by
   itself in a person's browser and waits for a click in an automated one
   (`navigator.webdriver`).
3. **The token is spent only by a successful sign-in.** A busy or stopped
   environment, or a failed mint, leaves the link valid.

The dashboard's Open flow keeps its eager, one-hop form: there the URL is never
seen by anything but the redirecting browser, and a second page would be pure
friction. The two token kinds share the store, the TTL machinery and the host
binding, and do not cross (a session token cannot be POSTed, a link token is
not consumed by the GET).

## How it works (macro)

- **Issue (tool call).** Same decorators as the neighbouring environment tools
  (error handling, environment lock, `prod-*` rejected, activity recorded). It
  refuses port routing (no per-environment host serves the landing) and any
  process without the web UI mounted — stdio and `oduflow call` — since the
  token store is in-process and nothing would redeem it. It wakes a stopped
  environment and waits for Odoo, validates the user, then issues the token
  (300 s; the dashboard's stays 120 s, a relayed link needs a person to notice
  it).
- **User validation without a session.** `resolve_env_user` answers "would the
  mint find this user?" with one `psql` query on the shared database container
  — no Odoo registry boot, no session. It mirrors the mint script's rule, so an
  unknown user fails at the tool call rather than in the person's browser. The
  default selector is empty, meaning the `base.user_admin` record: a literal
  `login = 'admin'` lookup breaks as soon as the admin's login becomes an email
  address. The grant keeps the *selector*, so redemption resolves the user by
  the same rule again.
- **Redeem (POST on the environment host).** Take the environment lock; if it
  is held, show a "busy" page that retries by itself and keeps the link. Under
  the lock re-read the token (this is what makes the link single-use — every
  redemption of a token holds the same lock), start the environment if it is
  stopped, mint through the existing `connect_as_user`, discard the token the
  moment a session exists, wait for Odoo if it had to be started, and set the
  host-only cookie as in 0036.
  "Environment or user no longer exists" retires the link; any other failure
  keeps it and offers a retry.
- **The result is structured.** The tool returns an object (`url`, `login`,
  `uid`, `env_name`, `expires_in`, `note`), so a program reads one field to get
  its redirect target instead of parsing prose.
- **Scoped access.** `connect_link` joins the `/mcp/<env>` allow-list on the
  reasoning that put `connect_as_user` there: a scoped token already reaches a
  session as any user of its own environment (`run_odoo_shell`), and the link
  is bound to that environment's host.
- **Credentials out of the log.** The link is a bearer credential until used,
  and two writers would have recorded it: the tool-result preview in
  `handle_errors` and uvicorn's access log (query string included). Auditing
  the first showed it already leaked for existing tools — the head of
  `connect_as_user`'s session id, `reset_admin_password`'s password, the
  service-database password. So the fix is general (`log_redaction`): a tool
  wraps each credential it returns in `_secret()`, and everything
  `handle_errors` logs is scrubbed of the values marked during that call
  (before truncation, so a credential cut by the preview limit cannot leak its
  head). An access-log filter masks credential query parameters (`token`,
  `key`) on every path, which also covers artifact download links and share
  links. The POST carries the token in its body.

## Consequences

- An MCP-driven application can offer one-click sign-in to an environment with
  no knowledge of cookies, hosts or Odoo sessions.
- A link is a five-minute, single-use, host-bound bearer credential for one
  user. Leaking it within that window signs the finder in; nothing narrower is
  possible without authenticating the person, which is exactly what the caller
  cannot do.
- The landing gained a page and a POST. The page is served on the environment's
  host, where only `/oduflow-connect` reaches Oduflow, so it is self-contained
  (no fonts, logo or script files).
- A scanner that executes JavaScript *and* hides `navigator.webdriver` would
  still redeem a link. Requiring a click from everyone would close that at the
  cost of a second click on every legitimate open; the automatic continue was
  judged the better default for a redirect target.
- Links do not survive a server restart (in-process store, as in 0036), and the
  feature exists only under Traefik routing with the HTTP transport.
- New credential-returning tools must mark their secrets with `_secret()`; the
  marking is per value, so the rest of the log line stays useful.

## History

- `feat/mcp-connect-link-v2` (2026-11-19) — `connect_link` tool, deferred
  grants in `connect_tokens`, GET-page / POST-redeem landing with busy and
  stopped handling, `resolve_env_user`, `log_redaction` (tool-result scrubbing
  and access-log filter). Extends [[0036-cross-subdomain-connect-as-landing]]
  to callers without a dashboard session.
