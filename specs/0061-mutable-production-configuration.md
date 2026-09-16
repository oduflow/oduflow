# 0061 — Mutable production configuration

**Status:** Adopted (still in force)
**Type:** Architecture / Production lifecycle
**First introduced:** `investigate-prod-domain-traefik` branch (2026-09-16)
**Key code today:** `docker_ops/production_ops.py` (`reconfigure_production`, `set_production_odoo_conf`, `_container_spec`, `RESERVED_ODOO_CONF_KEYS`), `production_registry.py` (`odoo_conf` field), `server.py` (both MCP tools), `web_ui.py` (`/reconfigure`, `/odoo-conf`), dashboard Settings panel

## Context

A production's configuration was write-once: domain, Odoo image, branch,
repository, git user and extra addon repos were fixed at `create_production`
and stored in the registry, but no tool could change them afterwards. Every
real-world evolution — an Odoo image update, a domain move (or Odoo
multi-website needing routing changes), a branch or repo migration, adding an
extra addons repo — required delete + recreate or hand-editing
`productions.json` plus manual container surgery. The generated `odoo.conf`
was equally closed: the base conf chain plus auto-tuned worker settings, with
no supported way to pin or add a single option (e.g. `limit_time_real`) for
one production.

## Decision

Make the registry record the *mutable intent* and give it two convergence
tools, mirrored in the dashboard:

- **`reconfigure_production`** changes any subset of
  domain / odoo_image / branch / repo_url / git_user / extra_addons in the
  registry first, then converges the workspace (re-clone or fetch+checkout,
  worktree diff) and **recreates the container** from the record. Container
  env/volumes/labels are assembled by one shared `_container_spec` helper so
  a recreated container is provably identical to a created one. The database
  and filestore live outside the container and survive; convergence is
  idempotent so a mid-way failure is retried by re-running the call.
- **`set_production_odoo_conf`** stores per-production `[options]` overrides
  in a new registry field `odoo_conf`. They are merged into every conf
  rebuild *after* the auto-tuned worker settings (explicit user value wins)
  and therefore survive deploys, retunes and reconfigures. Keys Oduflow must
  own (`addons_path`, `data_dir`, `db_*`) are reserved and refused.

The dashboard gains a per-production **Settings** panel (More → Settings)
exposing both, plus the `allow_copy_to_dev_mcp` gate — which deliberately
remains dashboard-only, so an agent cannot lift its own restriction.

## How it works (macro)

The registry record is the single source of intent, and convergence flows one
way from it. A reconfigure writes the changed fields to the record, then
brings the workspace up to the recorded code state (re-clone on a repo change,
fetch + checkout otherwise; extra addon worktrees diffed against the recorded
list) and recreates the container from the record — the only downtime is the
container swap itself, because database and filestore live outside it. Conf
overrides never touch a file directly: they sit in the record's `odoo_conf`
field, and every conf rebuild layers merged base conf → auto-tuned worker
settings → user overrides, so an override set once survives every later
deploy, retune, and reconfigure. The MCP tools and the dashboard Settings
panel are thin frontends over the same `production_ops` functions, each run
under the per-production lock, so both paths converge identically and never
race a deploy.

## Consequences

- Productions stop being immutable pets: domain moves, image bumps, branch
  and repo migrations and extra-addons changes are first-class, lock-guarded
  operations with a brief, explicit downtime.
- Reconfigure changes *infrastructure*, never database state: an image bump
  does not migrate the DB, and a branch switch deploys code without module
  install/upgrade — both return explicit notes pointing at
  `update_production` / an upgrade plan. Code-affecting reconfigures land in
  the deploy history (`action: "reconfigure"`).
- The registry-first ordering means a failed convergence leaves record and
  runtime briefly divergent — accepted, because the record is authoritative
  intent and the convergence is retryable.
- Odoo multi-website still needs multi-domain routing (one Host rule per
  production today); reconfigure makes the single domain changeable, a
  future `extra_domains` list would extend the same label mechanism.

## History

- Introduced together with the dashboard Settings panel and docs
  (`docs/production.md` "Reconfiguring a production").
