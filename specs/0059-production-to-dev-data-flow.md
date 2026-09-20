# 0059 — Production → dev data flow, gated for agents only

**Status:** Adopted (v1)
**Type:** Architecture — new capability
**First introduced:** this change (2026-09-13), branch `create-template-environment-production`
**Key code today:** `docker_ops/system_ops.py` (`publish_production_as_template`, `template_is_ready`, the shared publish helpers); `server.py` (`save_production_as_template`, `create_environment(from_production=…)`, `ensure_production_template`, `_production_record_for_dev_copy`); `docker_ops/production_ops.py` + `production_registry.py` (`allow_copy_to_dev_mcp`); the Production tab toggle in `web_ui.py` / `templates/dashboard.html`

## Context

[[0035-production-hosting]] made the road from dev to production: a template
seeds a new production's database and filestore, and deploys carry code
forward. The road back did not exist. Yet the everyday reason to want it is the
strongest one there is — *the bug only happens on the customer's data*. In
practice that gap was filled by hand: an operator dumped the production
database over SSH, copied a filestore tarball, imported it as a template
([[0003-database-templates-and-filestore-isolation]]), and remembered to
sanitize. Slow, undocumented, and exactly the kind of privileged data movement
that should not depend on remembering a step.

Three forces shaped the answer. **Production must not pause** — any mechanism
that stops Odoo to get a consistent copy is unusable for the one thing it is
for. **The data is real** — the copy is unsanitized by definition, and the
neutralization machinery that already exists for dev environments must be the
thing that runs, not a second half-implementation. And **the caller is often an
agent**: [[0009-agent-guidance-system]] tools are invoked with far less
deliberation than a dashboard button, so "an agent can pull production into dev
whenever it judges it useful" is a policy decision an owner may reasonably
refuse.

## Decision

**Publish a production as an ordinary dev template, and make "environment from
production" a thin resolver on top of that** — plus an
**`allow_copy_to_dev_mcp`** flag on the production record that gates the *MCP
tool layer only*.

The alternative — dumping the production per environment — was rejected: it
would have duplicated the filestore for every developer, re-read the production
cluster each time, and produced environments with no lineage, outside the
overlay/sanitize/provenance machinery. Routing through a template means
production data enters dev through exactly one door, and everything downstream
already knows what to do with it.

## How it works (macro)

- **One publish primitive.** `publish_production_as_template` dumps the
  production database from the production cluster with a consistent `pg_dump`
  (no downtime — the same trade-off `snapshot_production` already accepts),
  stages it, restores it into the *dev* cluster via the existing template reload
  path, and only then installs it as the template's dump: a failed restore
  leaves no half-published template. The production filestore — always a plain
  directory — is snapshotted into the template's baseline under the standard
  overlay remount, so live environments on that template are swapped safely and
  keep their `upper` deltas by default.
- **Provenance from the registry, not from labels.** A production carries no
  `oduflow.*` code labels, so the counterpart of
  [[0043-template-code-provenance-and-lineage]] reads repo, image, git user and
  extra addons from the authoritative `productions.json` record and the commit
  from the production checkout. Environments created from the template therefore
  get the same code/database drift verdict as any other template.
- **`from_production` is a resolver, not a second pipeline.** It resolves to a
  managed template named `prod-<name>`, published on first use and reused after
  that; the output says which happened and how old the snapshot is, and points
  at `save_production_as_template(..., overwrite=True)` to refresh it. So the
  second environment from the same production is an instant `CREATE DATABASE …
  TEMPLATE` plus an overlay mount. "Published" means metadata *and* template
  database present — never a bare directory, which a failed publish must not
  leave behind either. Publishing happens *after* the adopt-existing fast path,
  and under the production's own lock rather than the team lock, which
  `create_environment` cannot take. The dump is streamed out of the production
  cluster into the dev cluster's exchange dir: one full-size write, nothing in
  either container's writable layer.
- **Sanitization stays where it already is.** The template is unsanitized by
  construction and documented as production-confidential; the neutralization and
  repository sanitize scripts run at environment creation, unchanged.
- **The gate is deliberately asymmetric.** `allow_copy_to_dev_mcp` is checked in
  one place in `server.py`, so the dashboard — which calls `docker_ops`
  directly — is never gated: an administrator can always copy production to dev
  from the UI. A missing key reads as `True`, so productions predating the flag
  keep working. **No MCP tool can change the flag**, only the dashboard can; an
  agent that hits the refusal cannot lift it, which is the entire point.
- **The gate governs new copies, not existing ones.** Its purpose is to stop
  *unsanctioned copying* of production. A template already published from the
  production is a sanctioned copy and stays usable like any other template:
  environments made from it are neutralized on creation. The template records
  `source_production`, and the only thing that stays refused for a gated
  production is `sanitize=False` on such a template — the raw data. Withdrawing
  the copy itself is `delete_template`.

## Consequences

- The "reproduce it on real data" workflow is one call, and it is the same
  template object the rest of Oduflow already understands — lineage, overlays,
  quotas, `list_templates`, deletion.
- Copying production data becomes a *reviewable* act: the flag is a per-production
  answer to "may agents do this unattended", recorded next to the production and
  visible in `list_productions` / `get_production_info`.
- Enforcing the policy at the tool layer rather than in `docker_ops` is a
  conscious choice, with a cost: a future non-MCP caller must re-apply the check
  itself. In exchange the primitive stays honest about what it does, and the
  admin path can never be locked out by a flag an admin set.
- `prod-<name>` is a namespace Oduflow owns and republishes with `overwrite`, so
  a stale template database without its directory cannot wedge the flow.
- The copy is only as fresh as its last publish. Refresh is explicit rather than
  automatic: re-dumping a production on every environment creation is precisely
  the cost this design avoids.

## History

- 2026-09-13 — `publish_production_as_template`, the
  `save_production_as_template` tool, `create_environment(from_production=…)`
  and the `allow_copy_to_dev_mcp` record flag with its dashboard-only toggle
  (branch `create-template-environment-production`).
