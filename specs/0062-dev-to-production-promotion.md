# 0062 — Dev → production promotion (`create_production from_environment`)

**Status:** Adopted (still in force)
**Type:** Architecture / Data flow
**First introduced:** `investigate-prod-domain-traefik` branch (2026-09-16)
**Key code today:** `docker_ops/production_ops.py` (`from_environment` in `create_production`, `_source_env_info`, `_copy_env_data_into_production`, `_copy_db_into_prod_cluster`), `server.py`/`web_ui.py` (parameter + locks), dashboard "Promote to Production" env action

## Context

[[0059-production-to-dev-data-flow]] made prod → dev a first-class, gated
flow. The opposite direction — turning a working dev environment into a new
production — had no direct mechanism. The workaround was
`save_as_template` + `create_production(template_name=...)`, which leaves an
intermediate template behind (a second full copy of DB + filestore),
**resets the source environment** (its data becomes the template baseline —
a surprising side effect when the goal is promotion), and forces the user to
re-enter repo/branch/image the environment already knows.

## Decision

`create_production(from_environment=...)` promotes a dev environment
directly, as the mirror of `create_environment(from_production=...)`:

- The environment's database is dumped from the dev cluster and restored
  into the production cluster via the same cross-cluster pg_dump/pg_restore
  path templates use (generalized into one `_copy_db_into_prod_cluster`).
- The filestore is copied from the environment's *merged* mount, which
  transparently covers both plain-copy and fuse-overlay filestores.
- Both copies happen with the environment's Odoo briefly stopped (then
  restarted) so the DB/filestore pair is consistent. The environment is
  **left as it was** — nothing is reset, no intermediate template exists.
- Omitted `repo_url`/`branch`/`odoo_image`/`git_user`/`extra_addons` default
  to the environment's own (from its container labels); explicit arguments
  win. `from_environment` and `template_name` are mutually exclusive.
- No sanitization — the data flows *into* production, so the
  `allow_copy_to_dev_mcp` gate does not apply. If the environment itself
  descends from a production template (sanitized data), the result carries
  an explicit warning.

Both the source environment's branch lock and the new production's lock are
held for the duration.

## How it works (macro)

Promotion is the normal production-creation pipeline with a different seed.
`create_production(from_environment=...)` first reads the source
environment's container labels to fill in whatever the caller omitted —
repo, branch, Odoo image, git user, extra addons — so the form/call defaults
to exactly what the environment runs. It then stops the environment's Odoo
just long enough to take the database (cross-cluster dump/restore into the
production cluster) and the filestore (copied from the merged mount) as one
consistent pair, and restarts it — the environment is a read-only source,
never reset, and no intermediate template is created. From there provisioning
is identical to any other new production: full workspace clone, production
PG cluster, Traefik routing, registry record. Sanitization is skipped by
design, since the data flows *into* production rather than out of it.

## Consequences

- Promotion becomes one command / one pre-filled dashboard form ("More →
  Promote to Production"), with no storage double-spend and no side effects
  on the dev environment.
- A brief dev-environment downtime is accepted as the price of a consistent
  copy — cheaper and simpler than snapshot-based consistency for a dev-tier
  source.
- The promotion inherits the environment's *current* extra-addons set and
  branch, so what was tested is what ships; the production then evolves via
  the normal deploy engine ([[0061-mutable-production-configuration]] covers
  later changes).

## History

- Introduced together with the pre-filled create-production modal and docs
  (`docs/production.md` "Promoting a dev environment").
