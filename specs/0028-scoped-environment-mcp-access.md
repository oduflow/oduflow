# 0028 — Scoped single-environment MCP access (`/mcp/<env>` + per-environment tokens)

**Status:** Adopted
**Type:** Architecture / MCP capability
**First introduced:** scoped single-environment MCP access (this change)
**Key code today:** `scoped_access.py` (`ScopedEnvASGI`, `ScopedAccessMiddleware`, `OduflowTokenVerifier`, allowlist), `env_tokens.py` (per-env token generation + resolution), `oauth_provider.py` (team OAuth clients plus env-token Bearer verification), `server.py` (`_build_auth`, `_start_http` wiring), `docker_ops/env_ops.py` (`oduflow.mcp_token` label, `get_env_token`), `web_ui.py` + `templates/dashboard.html` (MCP Access modal)

## Context

The MCP HTTP server exposes one endpoint per team (`/mcp`) and, through it, the
*entire* tool surface: create/delete/stop environments, manage templates,
services, volumes, repos — plus the per-environment dev-loop tools. Identity is a
single team `auth_token` ([[0020-authentication-oauth]]) that unlocks everything
that team owns ([[0014-team-based-multi-tenancy]]).

That is the right granularity for an operator, but the wrong one for handing a
*coding agent* a single environment to work in. There was no way to give an agent
a handle that is confined to one environment — able to push code, install/upgrade
modules, run tests, open a shell, query the DB — yet structurally unable to
create, delete, or stop environments or touch any other tenant resource. Sharing
the team token over-grants; the blast radius of a confused or compromised agent is
the whole team.

## Decision

Add a **scoped single-environment endpoint** addressed by URL, `/mcp/<env>`,
backed by a **per-environment access token** generated at creation time. On that
endpoint only an allowlisted, single-environment toolset is visible and callable,
and the environment is implied by the URL — never passed as a tool argument,
preserving the "no identity/target in tool signatures" invariant of
[[0002-remote-multi-user-mcp-access]].

- **Per-environment token.** `create_environment` mints a random token and stores
  it in the container label `oduflow.mcp_token`. The token is a Bearer credential
  for the scoped endpoint. It is not accepted as an OAuth client_id because OAuth
  authorization URLs expose client_id values in logs and browser history.
- **Default-deny toolset.** The scoped endpoint exposes a curated allowlist (full
  dev loop + `restart`, plus read-only/diagnostic tools). Everything else —
  lifecycle, templates, services, volumes, repos, listing other environments — is
  denied by omission, enforced on *both* `tools/list` and `tools/call`.
- **One server, two faces.** The same FastMCP instance serves `/mcp` (full, team
  token) and `/mcp/<env>` (scoped). No second server, no duplicated tools.

The choice of a per-environment credential over "team token + env in URL" was
deliberate: it makes the credential itself the boundary, so an agent given only
the scoped URL + Secret Key is *cryptographically* confined to its environment,
not merely pointed at a narrower endpoint.

## How it works (macro)

- **URL → scope.** An outermost ASGI shim recognises `/mcp/<env>`, stashes the
  env in the request scope, and rewrites the path to the canonical `/mcp` route so
  the existing streamable transport, auth, and OAuth resource-metadata serve it
  unchanged. (The OAuth discovery path `/.well-known/.../mcp/<env>` is rewritten
  the same way.)
- **Token → identity.** Token verification resolves a presented token to
  `(team_id, env_name)`: a team `auth_token` → full access; an `oduflow.mcp_token`
  label match → scoped, carrying the env in an `oduflow_env:<env>` token scope.
  Resolution is shared by the Bearer verifier and the OAuth provider, with a small
  in-memory cache over a Docker label scan.
- **Policy, default-deny.** A FastMCP middleware combines the two signals — env
  from the URL and env from the token — into one decision: a team token at `/mcp`
  is full access (no-op); at `/mcp/<env>` it is scoped to that env; a per-env token
  is valid *only* at its own `/mcp/<env>` and denied anywhere else. In scoped mode
  the middleware filters the tool list to the allowlist, strips `env_name` from
  advertised schemas, and on every call re-checks the allowlist and injects the
  resolved `env_name` — so a tool can never target another environment, and a
  leaked listing can never enable a forbidden call.
- **Operator UX.** Each environment card surfaces an **MCP Access** action showing
  its `/mcp/<env>` URL and Bearer Secret Key.

## Consequences

- An agent can be given a **confined handle to exactly one environment** — full
  in-environment dev loop, zero ability to create/delete/stop environments or
  reach other tenant resources — without ever exposing the team token.
- The scoped endpoint is **additive and free**: same server, same tools, no new
  config knobs. Behaviour for the existing `/mcp` endpoint is unchanged.
- **Defense in depth:** policy is enforced on both list and call, default-deny;
  the env is injected server-side, so cross-environment access is structurally
  impossible even if the tool surface were mis-listed.
- Tokens live in **Docker labels** (the model of [[0021-code-delivery-modes]]'s
  label-carried env metadata). They survive container recreation but cannot be
  added to a live container: environments created before this feature carry no
  token until recreated — acceptable given environments are ephemeral and
  per-branch. Operators rotate a token by recreating the environment.
- The scoped URL is Bearer-only. OAuth remains available for the team-wide
  `/mcp` endpoint via the public `team_<id>` client_id and team `auth_token`
  client_secret.

## Evolution

- 2026-10-18 — the per-environment token moved from the `oduflow.mcp_token`
  container label into the per-team `mcp_tokens.json` registry, keyed by
  environment name, so it survives recreate and rename; resolution reads the
  registries instead of scanning Docker ([[0080-stable-environment-identity-across-recreate]]).

## History

- (this change) — scoped single-environment MCP access: `/mcp/<env>` endpoint,
  per-environment `oduflow.mcp_token` Bearer credential, default-deny allowlist
  middleware, and the dashboard MCP Access modal.
