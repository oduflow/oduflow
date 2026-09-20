# 0063 — Production targets in declarative Stacks

**Status:** Adopted
**Type:** Architecture / Deployment
**First introduced:** `feature/stack-production` (2026-09-16)
**Key code:** `stack_models.py`, `stack_production.py`, `stack_ops.py`, `production_registry.py`

## Context

The platform control plane moved from a Stack-managed development environment to
production. Reapplying the previous Stack would revive the retired development
container and its obsolete upstreams. A manual deployment inventory documented
the new layout but could not converge it. The operator requested a real production
Stack target, using the production lifecycle introduced in [[0061-mutable-production-configuration]].

## Decision

A Stack declares exactly one `environment` or `production`. Production creation
and reconfiguration reuse the normal production operations and dedicated database
cluster. Registry metadata owns the production on behalf of a Stack, independently
of the disposable Odoo container. Existing dev manifests retain their behavior.

Existing productions are adopted only with an explicit flag and a matching
configuration and running container. Adoption updates registry metadata alone.
It cannot create a missing production or take one from another Stack. This lets
a promoted production join its former infrastructure Stack without another copy
of its database or an Odoo restart.

## How it works

Preflight validates configuration, ownership, secret references, effective runtime
identity and the host's production setting. Apply holds the team's lock plus the
same production lock as native production tools. A pending fingerprint is persisted
before changing configuration; success records the applied fingerprint. A failed
container replacement can therefore be retried after registry intent has changed.

Production service references expose the URL, container name and database name.
There is no production scoped dev MCP token. Application commits and module
migrations stay with the production deploy engine; Stack does not silently fetch
code. Production source and seed changes require an explicit production workflow.

## Consequences

The same infrastructure Stack can manage a production control plane. Applying an
unchanged manifest converges without restarting it. Data and existing resources
are never pruned by Stack. Services and volumes retain their existing ownership
rules; adopting production does not claim independently managed infrastructure.
Host backup settings and production data remain outside the manifest.

## History

- Implemented after the demo platform's dev-to-production migration on 2026-09-16.
