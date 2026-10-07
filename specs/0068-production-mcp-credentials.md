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
The initial addon implementation supports Odoo 19 and requires the corresponding
client-addon release before rollout.

Related: [[0035-production-hosting]], [[0028-scoped-environment-mcp-access]],
[[0059-production-to-dev-data-flow]].

## History

- 2026-09-19: agreed separate endpoints, a shared configured production key,
  direct OduMCP API access and automatic installation on production creation.
