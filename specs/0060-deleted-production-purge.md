# 0060 — Tombstoned purge of deleted-production leftovers

**Status:** Adopted (still in force)
**Type:** Architecture / Data lifecycle
**First introduced:** `naive-dragon` branch (2026-09-13)
**Key code today:** `docker_ops/production_ops.py` (tombstone write/clear, `purge_deleted_productions`, `_destroy_leftovers`), `reaper.py` (`_purge_deleted_productions` in the sweep), `settings.py` (`[lifecycle] prod_purge_hours`), `server.py` (`cleanup --purge-deleted-productions`), `docker_ops/system_ops.py` (`cleanup_orphans` skips the `prod-*` namespace)

## Context

`delete_production` deliberately keeps the database and workspace on disk —
productions are precious, deleting bytes is opt-in ("delete" from the
dashboard is a deregistration, not destruction). But kept leftovers had no
lifecycle at all: nothing recorded *when* a production was deleted, so
abandoned leftovers accumulated forever, and the only way out was
`drop_database=true` at deletion time or manual surgery.

Worse, the generic `oduflow cleanup` was actively dangerous here: production
containers carry no branch label by design (to keep dev-side listings and the
reaper blind to them), so `cleanup_orphans` saw *every* production workspace —
including a live one's — as an orphan, and `--force` would destroy it.

## Decision

Soft-deleting a production writes a **tombstone** (`deleted.json` with the
deletion timestamp) into the kept workspace. Only tombstoned leftovers are
ever purged; anything without a tombstone is presumed alive and untouchable.
Two reclamation paths consume tombstones:

- **Deferred:** the existing reaper sweep purges leftovers
  `[lifecycle] prod_purge_hours` after deletion (opt-in, default `0` = keep
  forever — same posture as `auto_delete_hours`).
- **Immediate:** `oduflow cleanup --purge-deleted-productions --force`.

Re-creating a same-named production clears the tombstone (revival), and a
tombstone found on a *registered* production is treated as stale and removed,
never acted on. `cleanup_orphans` now skips the whole `prod-*` workspace
namespace, closing the live-production destruction hole.

## How it works (macro)

The tombstone is the sole authority: presence marks "these bytes belong to a
deleted production", its timestamp starts the purge clock (an unreadable
tombstone restarts the clock rather than guessing an age). A purge drops the
database in the production cluster, the PG role, and the workspace — but if
the `DROP DATABASE` fails the workspace (and tombstone) is kept so a later
sweep retries instead of silently orphaning the database. The sweep takes the
same per-production lock as MCP/REST operations, so it never races a deploy
or restore. `delete_production(drop_database=True)` and the purge share one
destruction routine.

## Consequences

- Abandoned leftovers are reclaimable, automatically (opt-in) or on demand;
  the "productions are precious" default is unchanged.
- The delete → keep → purge window gives operators an undo horizon they can
  size per deployment (`prod_purge_hours = 168` ≈ a week).
- A live production's workspace can no longer be destroyed by
  `oduflow cleanup --force`.
- Purge correctness depends on the tombstone file surviving in the workspace;
  manual workspace surgery that deletes it simply reverts to "keep forever" —
  the failure mode is conservative.

Related: [[0035-production-hosting]] (the production tier this manages).

## History

- 2026-09-13 — Introduced (tombstones, `prod_purge_hours`, cleanup flag, prod
  namespace exclusion in `cleanup_orphans`).
