# 0050 — Resource-scoped locks: the team lock stops being the catch-all

**Status:** Adopted
**Type:** Architecture
**First introduced:** 2026-08-17
**Key code today:** `locking.py` (resource key builders, `keyed_mutex`, `acquire_system`, `team_lock`), `server.py` (`with_key_lock`), `web_ui.py` (`_template_locks`), `docker_ops/service_ops.py`, `docker_ops/volume_ops.py`

## Context

[[0015-granular-locking]] replaced a global mutex with per-branch, per-team and
system locks — and per-team became the default for everything that was not
obviously one environment: services, volumes, extra-addon repos, credentials,
service presets, backup pruning, even *listing* templates.

That scope is far wider than what those operations touch. Because the lock
manager also enforces team↔environment mutual exclusion (a team-wide operation
must not run while any of the team's environments is busy, and vice versa), a
ten-minute `create_environment` made `delete_service`, `create_volume`,
`add_extra_repo` and `list_templates` all fail with `BusyError` — and a service
tweak could reject an environment build. Agents read those rejections as stuck
state and reach for restarts. Meanwhile the coarse lock was not even buying
correctness where it mattered: `restart_service` and `run_service_command` took
no lock at all, and backup pruning could run in the middle of a snapshot,
because productions lock in their own `prod:` keyspace that the team lock never
touched. The one genuine team-wide invariant — publishing a template remounts
other environments' overlay filestores — was hidden among a dozen operations
that had nothing team-wide about them.

## Decision

**Lock the resource, not the tenant.** Every operation takes the narrowest key
that names what it actually mutates, using the lock manager's existing generic
keyed lock:

- `svc:{team}:{name}`, `vol:{team}:{name}`, `preset:{team}`, `creds:{team}`,
  `prod-backups:{team}` — acquired *without* a team id, so they sit outside the
  team↔environment mutex entirely.
- The **team lock is reserved for the one real team×environments invariant**:
  template mutations that remount live environments' filestores.
- Operations that another mechanism already serialises take **no** lock:
  extra-addon repos (per-repo RLock plus a container-based dependency guard in
  `extra_addons.py`), pure reads, and the `odoo_*` XML-RPC tools — PostgreSQL,
  not Oduflow, arbitrates concurrent ORM calls, exactly as it already did for
  the neighbouring lock-free `http_request_to_odoo`.
- The **system lock**, dead since [[0015-granular-locking]], is revived for
  `restore_cluster_pitr` and made mutually exclusive with the whole `prod:`
  keyspace in both directions — the honest expression of "this rewrites the
  cluster every team's productions live in".

## How it works (macro)

- One module builds every key string, because the MCP tools and the REST
  dashboard must lock the same resource under the same key or they stop seeing
  each other. A `with_key_lock(key_builder)` decorator applies it to tools; the
  dashboard calls the same builders.
- Below the tool layer, two invariants span calls the tool layer cannot see: the
  service-slot count, and "no service mounts this volume". These use a short,
  blocking, re-entrant `keyed_mutex` inside `docker_ops` rather than a
  user-visible lock — it must be held across `update_service`'s remove-and-
  recreate window, and a caller has nothing useful to do but wait for
  milliseconds. Ordering is fixed (tool key first, then the mutex; nothing under
  the mutex takes a tool key), so the two schemes cannot deadlock.
- Lock-free tools that wake a stopped environment made an existing
  check-then-start race reachable; the wake is now atomic per environment
  through the same keyed mutex.
- Contention still surfaces as `BusyError` naming the holding operation and its
  age — the diagnostic contract agents depend on is unchanged, only its blast
  radius shrank.

## Consequences

- Work on unrelated resources genuinely runs in parallel: a long environment
  build no longer bounces service, volume, credential or repo operations, and
  vice versa. Locks now fail only when two callers really do want the same
  thing.
- Races that the coarse lock never actually covered are closed: service restart
  and exec now exclude delete/update, prune excludes snapshot and restore, and
  cluster PITR excludes every production operation including the blocking
  webhook-deploy path.
- **Accepted window:** `create_environment` materialises shared extra-addon
  checkouts *before* its container exists, so the delete guard cannot see it. A
  `delete_extra_repo` landing exactly there makes the in-flight create fail with
  a clear error instead of every repo operation queueing behind a team lock for
  minutes. Documented at the guard.
- The team lock now means something specific, so the "which scope?" question for
  a new tool has a sharper answer — but the answer is no longer "team by
  default", and mis-scoping is still the standing risk of granular locking.
- Locks remain in-process; cross-process safety on shared files stays with the
  registries' own flocks.

## Evolution

**2026-09-20 — the template family gets its own key.** "Team lock = template
mutations" turned out to be one notch too coarse in one direction and one notch
too vague in the other.

Too coarse: `import_template_from_odoo` refuses to touch an existing template,
so the template it builds is always brand new and no environment can reference
it — its remount pass is structurally a no-op. It nonetheless held the team lock
across an HTTP download of a full Odoo backup (a ten-minute timeout) plus the
restore, bouncing every environment operation in the team for the duration. The
dashboard's metadata editor had the same shape for a much smaller write: one
`metadata.json`, already guarded by its own revision check. Both now take a
**template-scoped key** (`tpl:{team}:{name}`) and no team lock. `attach_filestore`
keeps the team lock but is handed it as a context manager and enters it only for
the remount-and-swap window, so staging its source — an rsync of a
multi-gigabyte filestore — runs outside it. That deferral is also the one place
where a team acquire *blocks* (with a timeout): by the time it asks, the call
has already paid for the staging, and refusing instantly would throw hours of
transfer away over an environment operation that will be gone in seconds.

Too vague: `delete_template` and `rename_template` remount nothing; they *refuse*
while any environment uses the template. Their real reason for the team lock is
that this dependent-environment scan is a check-then-act, and only
team↔environment mutual exclusion keeps a concurrent `create_environment` — which
clones the template database before its container exists, and is therefore
invisible to the scan — out of the window. That is now written down at the guard
rather than inferred from the ADR.

The resulting rule: **every template mutation takes that template's key; the ones
that remount live environments' filestores (or that need the create-environment
exclusion) take the team lock as well.** Holding both is what keeps the narrowed
operations from racing the wide ones — without the key, a team-locked publish and
a key-locked import could have collided on the same template name. Order between
the two varies by caller and cannot deadlock: every acquire here is
non-blocking — except `attach_filestore`'s post-staging team acquire, which
waits on a bounded timeout and holds no other lock that a team operation could
want — so an inversion surfaces as `BusyError`. `rename_template` takes both
names' keys, since its "is the target free?" check is a check-then-act too —
unless the two names are equal, where the second acquire would be the same key
and the caller would be told to wait for itself. Two
consequences of narrowing had to be paid for directly: the import's download path
was a fixed per-team filename (safe only because the team lock serialised
imports) and is now unique per call, with explicit cleanup — a partial download
is no longer self-healing.

## History

- `3a26c70` (2026-02-06) — global mutex.
- `ad3b382` (2026-03-01) — `LockManager` with per-branch/per-team/system locks
  ([[0015-granular-locking]]).
- 2026-08-17 — resource-scoped keys, template-only team lock, lock-free `odoo_*`
  and extra-repo tools, revived system lock for `restore_cluster_pitr`.
- 2026-09-20 — `template_lock_key`; import and metadata editing drop the team
  lock, `attach_filestore` defers it past staging (and waits, bounded, for it),
  and the delete/rename team lock is documented as the create-environment
  exclusion it actually is.
