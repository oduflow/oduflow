# Separate production MCP credentials and shared OduMCP integration

**Status:** Accepted · **Type:** Architecture · **First introduced:** 2026-09-19  
**Key code:** `production_access.py`, `production_mcp.py`, `docker_ops/production_ops.py`; client addon `odumcp`

## Context

Oduflow serves administrators and developers. A single team MCP credential used
to expose both development and production lifecycle operations. Business access
to production additionally needs the policies, human approvals and audit already
implemented by the Odoo addon `odumcp`. Requiring another MCP server would duplicate
an agent's connection without adding an enforcement boundary inside Odoo.

## Decision

Use separate per-team development and production credentials and endpoints:
`/mcp` and `/production`. Oduflow validates production credentials from its own
configuration, independent of Odoo availability. The same production credential
is installed as an MCP-scoped API key on the administrator in each team production.
Keep one general `odumcp` addon usable by Oduflow and the standalone MCP server.

## How it works

Production tools are a fixed separate surface, checked on every call. The shared
output reader checks production scope and team ownership for cached prod logs. They remain
listed when production hosting is disabled and report configuration errors. Dev
credentials cannot address the production namespace through generic dev tools.
Local CLI and dashboard administration retain owner authority. On multi-team
servers, shared-cluster PITR and WAL mutations are restricted to owner interfaces.

Creation obtains compatible addon code, installs the module when needed and uses
a fixed internal Odoo shell operation to register the key. This setup is optional:
a failure leaves production running with a warning. Business tools remain listed
but report unavailable until synchronization succeeds; infrastructure operations
remain usable. Rotation repeats that
operation and reports outcomes per production. Odoo stores only a hash and retains
personal keys and existing policies; an absent profile starts read-only.

Business operations use the addon's HTTP API. Exact change plans, human approval,
policy revalidation and audit remain in Odoo. Infrastructure lifecycle operations
remain in Oduflow and can operate while Odoo is unavailable.

## Consequences

One administrator connection reaches all productions of its team. Shared keys
identify the integration rather than a person. Rotations across databases are
explicit and not atomic; stopped or failed targets must be retried. Restores may
restore old keys and require resynchronization. Endpoint authorization does not
replace host/container isolation or control developers' code delivery permissions.
The initial addon implementation supported Odoo 19 and required the corresponding
client-addon release before rollout.

## Evolution

- The connector repository now publishes `odumcp` with managed keys for every
  Odoo version from 16 to 20, one branch per version. The addon branch is no
  longer configured: it follows each production's Odoo version, taken from the
  image tag or, for an unversioned custom image, from `odoo --version`. A
  managed checkout made for another version (an upgraded production) is
  replaced on reconfiguration and on synchronization, and provisioning refuses
  an addon of another version. Odoo 15 and older are recorded as `unsupported`
  rather than as a failed synchronization. The dashboard flags both states.
- **Ownership policy.** Oduflow ships `odumcp` as a managed component by
  default; a client takes it over by committing the module into their own
  repository, and from then on Oduflow does not touch it. The two sources were
  previously allowed to coexist (the repository copy won only through
  addons_path ordering, with the managed mount lingering). Now a synchronization
  that finds the module in the client repositories removes the managed checkout
  atomically and recreates the container so the mount disappears; nothing of
  the client's code is ever modified, upgraded or refreshed by Oduflow. The
  reverse holds too: a module removed from the repository is managed again on
  the next synchronization.
- **Explicit refresh.** The managed checkout is still never updated silently —
  not on creation, not on deployments. `sync_production_mcp(refresh=true)` is
  the single operator lever: it re-clones the same Odoo version's branch from
  `odumcp_repo_url`, recreates the container and upgrades the module. It is a
  documented no-op for a client-shipped module. Results now name the addon
  source, what happened to the checkout, whether the container was recreated,
  and the connector branch and commit, so the state of a production's connector
  is inspectable rather than inferred. A refresh names one production; the
  whole-team form of the tool refuses it.
- **Mount decided by repository state; deploy completes the takeover.** The
  container builders mount the managed copy only while the client repositories
  do not ship `odumcp`, so any recreate — reconfiguration, image change,
  synchronization — drops the mount the moment the module is client code,
  independently of the checkout's presence on disk. The deploy that brings
  `odumcp` into the repository (manual, webhook or auto-update) finishes the
  takeover itself after the new code is verified: it recreates the container
  without the mount, then retires the checkout, and records the fact in the
  deploy history. A failed recreate leaves the still-mounted checkout intact
  for `sync_production_mcp` to finish.

Related: [[0035-production-hosting]], [[0028-scoped-environment-mcp-access]],
[[0059-production-to-dev-data-flow]].

## History

- 2026-09-19: agreed separate endpoints, a shared configured production key,
  direct OduMCP API access and automatic installation on production creation.
- 2026-10-16: OduMCP for Odoo 16–20; `[production].odumcp_ref` retired in favour
  of the production's own Odoo version.
- 2026-10-18: ownership policy made explicit — managed by default, client
  takeover by committing the module removes the managed checkout; explicit
  `refresh` lever on `sync_production_mcp`; creation reports the cause of a
  failed provisioning.
