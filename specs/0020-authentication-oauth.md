# 0020 — Authentication for MCP HTTP transport: GitHub OAuth → self-hosted OAuth Authorization Server

**Status:** Adopted (self-hosted AS is current; every routing mode always uses a per-team, host-relative issuer; the OAuth flow mints independent expiring/rotating/revocable tokens with a persistent store; static Bearer tokens retained)
**Type:** Architecture
**First introduced:** `97c3fc8` "add GitHub OAuth support for MCP HTTP transport" (2026-03-13)
**Key code today:** `server.py` (`_build_auth`, transport/auth wiring), `oauth_provider.py` (`OduflowOAuthProvider`, host-relative `get_routes`), `oauth_token_store.py` (persistent minted-token store), `settings.py` (`oauth_enabled`, per-team `auth_token`/`hostname`)

## Context

Remote, multi-user access ([[0002-remote-multi-user-mcp-access]]) established two
invariants: the server can run over HTTP for callers that aren't the local
process, and **identity is carried out-of-band in the transport, never as a tool
parameter**. The first authenticated form of that identity was a raw **static
Bearer token** per team (introduced with team-based multi-tenancy,
[[0014-team-based-multi-tenancy]]): the caller sets an `Authorization` header,
the server maps the token to a team and scopes every resource accordingly.

A static header token is fine for `curl`, CLI clients, and IDEs that let you
paste a header. But the most important MCP clients — claude.ai, MCP Inspector —
expect to authenticate via **OAuth**, not a hand-pasted Bearer token. To be a
first-class, hostable MCP service, Oduflow had to speak the auth protocol those
clients actually use, while keeping the "no token in tool args" rule intact.

The first answer leaned on GitHub as the identity provider. That worked but
created a hard dependency: every deployment needed a registered GitHub OAuth app
(`oauth_client_id`/`secret`), access control was a per-team `github_users`
whitelist, and the trust root was a third party. For a tool whose whole point is
self-contained, single-binary deployment, depending on an external IdP to log in
to your *own* environments was the wrong trade.

## Decision

Harden MCP HTTP auth in two steps, ending at a **self-hosted OAuth 2.1
Authorization Server** that issues and validates tokens itself, with static
Bearer tokens retained as the simple fallback.

- **Step 1 — GitHub OAuth (proxy).** Add OAuth 2.1 via GitHub alongside static
  tokens, auto-detected from config: setting `oauth_client_id`/`secret`/
  `base_url` enables the GitHub flow; `auth_token` keeps static tokens; both can
  coexist. Team resolution and access control move to GitHub login
  (`github_users` whitelist). This made claude.ai/Inspector connect with a real
  OAuth dance — but tied Oduflow to GitHub.
- **Step 2 — self-hosted Authorization Server.** Replace the GitHub proxy with a
  built-in OAuth 2.1 AS so MCP clients authenticate **without any external
  identity provider**. Oduflow exposes `/authorize`, `/token`, and
  `/.well-known/oauth-authorization-server`. Each team's existing `auth_token`
  remains its single configured secret and the direct Bearer path stays intact.
  OAuth is enabled whenever a team has that token, and the team's required
  hostname is always the issuer identity. There is no separate OAuth
  configuration. The GitHub-specific config and `github_users` are removed.

The interface invariant from [[0002-remote-multi-user-mcp-access]] is preserved
throughout: auth never appears in a tool signature; identity is resolved from the
request context and threaded into per-team scoping.

## How it works (macro)

- **One provider, two client paths.** Whenever a team has `auth_token`, Oduflow
  serves the self-hosted Authorization Server and also accepts that same secret
  directly from the `Authorization` header. Discovery derives the issuer from
  the request's validated team hostname.
- **One secret, one public id.** A team is preregistered as an OAuth client whose
  `client_id` is the **non-secret** `team_<id>` (e.g. `team_1`) and whose
  `client_secret` is the team's `auth_token`. The OAuth flow mints an independent,
  opaque, expiring access token (see Evolution) that carries the team's numeric id,
  so after the OAuth dance the team resolves exactly as it does for a direct Bearer
  `auth_token` call — no second identity system. Keeping the secret out of the
  `client_id` matters because `client_id` travels in the `/authorize` query string;
  the secret is sent only in the POST `/token` body.
- **Scoping unchanged.** Once a request is authenticated to a team, the existing
  per-team resource scoping and locking apply; OAuth only changes *how the team
  is proven*, not what it can see.

## Consequences

- Oduflow can be **hosted as a real MCP service** that claude.ai and Inspector
  connect to via standard OAuth, with no tokens in tool arguments and no external
  IdP in the trust path — the operator controls token issuance and revocation
  (rotate `auth_token`).
- Keeping one configured team secret avoids a separate client registry, while
  the static Bearer path (curl/CLI/IDEs) continues to work alongside independently
  minted OAuth access and refresh tokens.
- Dropping GitHub was a **breaking config change** (`oauth_client_id`,
  `oauth_client_secret`, per-team `github_users` removed; `oauth_base_url`
  added), traded for self-containment and operator control.
- Per-user **git** credentials for repo operations are a separate concern from
  MCP auth and are covered in [[0011-per-user-git-credentials]]; this record is
  only about authenticating the MCP transport.

## Evolution

- **Per-team, host-relative issuer (traefik).** The first self-hosted design used
  a single global issuer (`oauth_base_url`) for every team. Behind Traefik that
  forced a separate central hostname whose only job was to be the issuer — but
  Traefik requests a certificate only for hostnames it actually routes (each
  **team's** hostname, see [[0027-hard-tenant-isolation]]), so the central issuer
  host had no cert and the OAuth discovery it advertised was unreachable. Since a
  team is identified in the flow by its `auth_token`, not by the host, the issuer
  never needed to be global. Oduflow now **derives the issuer per request from the
  incoming host** in traefik mode: `OduflowOAuthProvider.get_routes` swaps the
  SDK's static discovery-metadata endpoints
  (`/.well-known/oauth-authorization-server`,
  `/.well-known/oauth-protected-resource`) for handlers that build `issuer`,
  `authorization_endpoint`, `token_endpoint`, and `resource` from the request's
  forwarded proto/host — validated against the registered team hostnames, so a
  forged `X-Forwarded-Host` cannot advertise a foreign issuer (and the 401
  `WWW-Authenticate` challenge is rewritten under the same check). Each team's
  OAuth flow therefore runs entirely on its own
  already-certificated hostname ([[0014-team-based-multi-tenancy]]), and
  `_build_auth` enables the Authorization Server automatically whenever routing is
  traefik. `oauth_base_url` becomes **optional** — needed only to pin a fixed
  issuer or in **port** mode (which has no per-team TLS host). The `/authorize`
  and `/token` operational routes are path-only and unchanged, and static Bearer
  tokens still work. In the same change, `[routing].hostname` is restricted to
  port mode (in traefik every team must set its own hostname, so a shared default
  is dead and would collide two teams on one host).

- **Non-secret `client_id` + OAuth sub-path routing (claude.ai custom
  connector).** The self-hosted AS originally set `client_id == client_secret ==
  auth_token`. But a claude.ai custom connector (manual client_id/secret) puts the
  `client_id` in the `/authorize` **query string** and derives the OAuth endpoints
  **path-relative to the connector URL** — it requests `https://<host>/mcp/authorize`
  and `/mcp/token`, ignoring the (correct, root) endpoints from discovery. Two
  problems followed: the secret leaked into URLs/logs, and the outer
  `ScopedEnvASGI` shim (scoped `/mcp/<env>` access, [[0028-scoped-environment-mcp-access]])
  treated `authorize`/`token` as an *environment name*, rewriting `/mcp/authorize`
  onto the auth-protected `/mcp` route → `401 invalid_token`, so the flow never
  started. Fixed by (a) **splitting the credential**: `client_id` becomes the
  public `team_<id>`, `client_secret` stays the `auth_token`, and the issued access
  token stays the `auth_token` (Bearer path unchanged; `team_<id>` alone is not a
  valid token); and (b) teaching `ScopedEnvASGI` to **alias the reserved OAuth
  sub-paths** under `/mcp/` (`authorize`, `token`, `register`, and the two
  `.well-known/*` discovery docs) back to the real root routes instead of scoping
  them as an env — mirroring the existing scoped-PRM alias. Scoped `/mcp/<env>`
  connectors remain Bearer-only (ephemeral environments; no per-env OAuth).
  Breaking: any already-configured claude.ai connector must be re-entered as
  `client_id = team_<id>`, `client_secret = auth_token`.

- **Host-relative issuer in port mode behind an upstream tunnel.** Port mode was
  originally excluded from automatic OAuth because it was assumed to have no
  per-team TLS hostname. A TLS-terminating upstream such as Cloudflare Tunnel
  invalidates that assumption: it exposes the team's configured hostname over
  HTTPS while forwarding to Oduflow's plain HTTP listener. The existing
  host-relative provider is therefore enabled whenever a team has `auth_token`,
  independently of routing mode. The incoming host is still checked against the
  configured, now-required and unique team hostnames, so arbitrary Host headers
  cannot become issuers. This also keeps hosted agents local in port mode: with
  no global issuer setting, their scoped MCP URL remains the Docker host gateway
  instead of hairpinning through the public tunnel.

- **One issuer source: the team hostname.** Once host-relative OAuth worked in
  both routing modes, the remaining fixed `oauth_base_url` override only created
  a second public identity that could disagree with the team selected by the
  request. The `[oauth]` section and `oauth_base_url` setting are therefore
  removed. Browser-facing URLs follow the validated request origin, Traefik uses
  the team's hostname, and local port-mode agent containers keep using the Docker
  host gateway.

- **Independent, expiring, revocable minted tokens (#83).** The credential split
  (above) kept the *issued* access token equal to the `auth_token`, so the OAuth
  client (claude.ai/IDE) still stored the team's long-lived master secret, the
  token never expired, and `revoke_token` was a no-op. The code/refresh exchange
  now **mints an independent, opaque access token** (`secrets.token_urlsafe`) with
  a rotating refresh token: the client never receives `auth_token`; a leaked minted
  token expires (default 1h — a fixed sensible default, not a config knob); using a
  refresh token rotates the pair (the old one is invalidated); and the enabled
  `/revoke` endpoint really deletes a token. A minted token stores the **numeric
  `team_id` as its `client_id`** with empty scope, so `_resolve_team` routes it
  identically to the `auth_token` (full team access). Minted tokens are **persisted**
  (`oauth_token_store.py`, mirroring `port_registry`'s per-path thread lock +
  cross-process flock + atomic `os.replace`, mode `0o600`) and served from an
  in-memory cache on the per-request verify path; persistence means a restart
  (upgrade, config reload) does not drop live claude.ai/IDE OAuth sessions. The
  **direct Bearer path is unchanged**: the `auth_token` remains a preseeded,
  non-expiring, non-revocable credential (curl/CLI), and per-environment tokens
  ([[0028-scoped-environment-mcp-access]]) stay Bearer-only. The secret still never
  rides in the front channel — the MCP SDK validates `client_secret` at `/token`
  (`client_secret_post`) — so this is a token-lifecycle change layered on the
  credential split. Not covered by unit tests: validate against a live claude.ai
  connect + at least one IDE before shipping.

## History

- `97c3fc8` (2026-03-13) — GitHub OAuth 2.1 for MCP HTTP transport alongside
  static Bearer tokens; auto-detected mode; per-team `github_users` whitelist;
  GitHub login in MCP context (#9).
- `5f32f58` (2026-05-25) — replace the GitHub proxy with a **self-hosted OAuth
  Authorization Server**; `auth_token` doubles as client_id/secret/access token;
  remove `oauth_client_id`/`secret`/`github_users`; `oauth_base_url` toggles
  OAuth vs static tokens (breaking) (#19).
- `73aa9b9` (2026-06-10) — document self-hosted OAuth in the config reference,
  quick start, and llms docs (#33).
- `2026-07-04` — derive the OAuth issuer per-request from the team's own hostname
  in traefik mode (host-relative discovery metadata); enable the Authorization
  Server automatically in traefik and make `oauth_base_url` optional there;
  restrict `[routing].hostname` to port mode.
- `2026-07-15` — split the OAuth credential so the secret no longer travels in the
  `/authorize` URL: `client_id = team_<id>` (public), `client_secret = auth_token`,
  issued access token still `= auth_token`; route the reserved OAuth sub-paths a
  path-relative client (claude.ai custom connector) requests under `/mcp/`
  (`/mcp/authorize`, `/mcp/token`, `/mcp/register`, `/mcp/.well-known/*`) to the
  real root routes so the flow reaches the AS instead of 401-ing on the scoped
  `/mcp/<env>` shim (breaking: reconfigure existing connectors).
- `2026-07-19` — finish #83: the OAuth code/refresh exchange mints independent,
  opaque, **expiring** access tokens with rotating refresh tokens and a real
  `/revoke`, persisted across restarts (`oauth_token_store.py`); the `auth_token`
  stays a non-expiring direct Bearer credential and per-env tokens stay
  Bearer-only.
- `2026-08-29` — enable the existing per-team host-relative issuer in port mode
  for TLS-terminating upstreams such as Cloudflare Tunnel; make every team
  hostname explicit and unique; retain `oauth_base_url` only as a fixed-issuer
  override.
- `2026-09-12` — remove `[oauth]` and `oauth_base_url`; the required team
  hostname becomes the only OAuth issuer source in every routing mode.
