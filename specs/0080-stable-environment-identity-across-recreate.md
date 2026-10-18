# 0080 — Stable environment identity across recreate (token registry + `recreate_environment`)

**Status:** Adopted
**Type:** Architecture / MCP capability
**First introduced:** 2026-10-18
**Key code today:** `env_tokens.py` (per-team `mcp_tokens.json` registry, stat-cached resolution), `docker_ops/env_ops.py` (`recreate_environment`, `delete_environment(preserve_access=)`, token mint/rename/revoke), `server.py` (`recreate_environment` MCP tool), `web_ui.py` (`api_recreate` thin wrapper), `migrations.py` (`0012-env-mcp-token-registry`)

## Context

An environment handed to a client carries two name-addressed credentials: the
dashboard share link (`/env/<name>?key=…`, [[0055-scoped-environment-ui-sharing]])
and the scoped MCP Bearer token (`/mcp/<name>`,
[[0028-scoped-environment-mcp-access]]). Both were supposed to feel like *the
environment's* identity, but only the share secret actually was: it lives in a
per-team registry keyed by environment name. The MCP token lived in the
`oduflow.mcp_token` Docker label, frozen at container creation — every recreate
minted a new one, silently disconnecting every configured MCP client.

Worse, the common "rebuild it" path for agents was `delete_environment` +
`create_environment` over MCP, and a *delete* rightly revokes the share secret
(a future unrelated environment under the same name must not be reachable with
credentials handed out for the old one). So an agent-driven rebuild also killed
the share link the operator had already sent to a client, who then had to be
re-sent a new link. The dashboard's Recreate button preserved the share
(`preserve_share=True`) but still rotated the MCP token — identity survived one
surface and not the other, with no way for an agent to do it right at all.

## Decision

Make **"an environment of this name" the identity unit** for both credentials,
and make "replace" a first-class operation distinct from "retire":

- **MCP tokens move from the container label to a per-team registry**
  (`mcp_tokens.json` next to `shares.json`, 0600), keyed by environment name —
  the exact model 0055 chose for share secrets, for the same reason: labels are
  frozen at container creation, names are not. Minted at create, carried across
  rename and recreate, revoked on delete. Startup migration
  `0012-env-mcp-token-registry` adopts existing label tokens so already
  configured clients keep working; the label is no longer written (a read-only
  fallback adopts tokens from pre-registry backups).
- **Token resolution drops the Docker scan.** `resolve_token` reads the
  registries through an in-memory reverse map revalidated by one `os.stat` per
  team, replacing the rate-limited full container scan and its negative-cache
  window.
- **A new `recreate_environment` MCP tool** (and the same engine behind the
  dashboard's Recreate, now shared in `env_ops.recreate_environment`) replays
  the recorded configuration and deletes with `preserve_access=True`, keeping
  both the share secret and the MCP token. Plain `delete_environment` still
  revokes both — that boundary is deliberate.

## How it works (macro)

- `delete_environment(preserve_access=)` is the single switch: recreate paths
  preserve the name-keyed credentials, retirement revokes them.
- `recreate_environment` re-reads the container's labels (repo, image,
  template, extra addons, env vars, live-mount, hostname policy), runs the
  refuse-before-destroy checks (disk, secrets, system readiness), then
  delete-preserving + create. Scoped MCP sessions cannot call it (allowlist is
  default-deny), same as every lifecycle tool.
- Image-build ownership (`environment_owner_id`) hashes the token, so build
  jobs now survive recreate too and still detach on true delete.

## Consequences

- A share link or MCP client configured against an environment name keeps
  working through any number of recreates; only an explicit delete (or share
  revoke/rotate) cuts access. Operators stop re-sending links to clients.
- Agents get an honest tool for "rebuild this": no more delete+create cargo
  cult, no silent credential churn.
- The 0055 caveat "the MCP token rotates only by recreating the environment"
  is gone — by design there is now **no** rotation path for the MCP token short
  of delete; an operator-facing rotate (like the share's Regenerate) is a
  possible follow-up.
- Pre-registry containers are untouched (labels are immutable and now inert);
  the registry is the single source of truth after migration.

## History

- 2026-10-18 — token registry, stat-cached resolution, migration 0012,
  `preserve_access` delete switch, shared `env_ops.recreate_environment`,
  `recreate_environment` MCP tool.
