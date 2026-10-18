# 0035 — Production hosting: dedicated PG cluster, WAL-G/S3 backups, auto-rollback deploys

**Status:** Adopted (v1)
**Type:** Architecture — new pillar
**First introduced:** this change (2026-07-11), branch `litnimax/production-hosting`
**Key code today:** `docker_ops/production_ops.py` (lifecycle + deploy engine); `production_registry.py` (per-team `productions.json`); `prod_tune.py` (PG + Odoo worker profiles); `walg.py` (WAL archiving, base backups, cluster PITR); `chunkstore/` (dedup filestore backup engine); `s3_client.py`; `backup_ops.py` (snapshots/restore/prune); `backup_scheduler.py`; `webhooks.py`; `health.py`; `ensure_prod_infra` in `docker_ops/system_ops.py`; the `*_production` MCP tool stack in `server.py`; `/api/productions*` + `/healthz` + `/api/webhooks/github` in `web_ui.py`; the Production tab in `templates/dashboard.html`

## Context

Oduflow provisioned only ephemeral dev environments: one shared lean-tuned
PostgreSQL, sanitized databases, branch-derived hostnames, an idle reaper.
Hosting a customer's *production* Odoo is the opposite regime — long-lived,
rarely created, mostly *updated*, with real data that must never be
neutralized, a customer-facing domain, production-grade worker settings, and
recoverable backups. Bolting "don't sanitize, don't reap, bigger workers"
flags onto the dev path would have scattered production concerns through
every dev code path; a fully separate stack would have duplicated the naming,
credentials, networking, git-classification and container machinery that
already works.

Key forces: physical separation of production data from dev churn (one
`DROP DATABASE` bug away otherwise); S3 as the durability boundary (WAL
replication, snapshots, scheduled backups); filestore backups needed
dedup across daily revisions and across a team's productions — duplicacy's
lock-free content-defined-chunking design fits, but its license is not free,
so a clean-room re-implementation was required; failed deploys must revert
themselves without an operator watching.

## Decision

**A production is a namespaced environment with its own metadata plane and a
dedicated database cluster.** Internal env name `prod-{name}` flows through
the shared naming chain (containers, PG roles, workspace paths, module
install/upgrade, logs are reused untouched), but productions carry **no
`oduflow.branch` label** — dev listings, the reaper and dev quotas stay blind
to them *by construction* — and are recorded in a per-team registry
(`productions.json`), which is the authoritative record of existence and
intent (domain, repo, branch, auto_update, health flags). A second PostgreSQL
container (`oduflow-prod-db`, own volume, own auto-generated resource profile)
holds all production databases; the dev cluster keeps its lean sub-1GB
profile, so both are co-designed for one host. Production infra is provisioned
lazily and idempotently on startup (`ensure_prod_infra`) — dev-only installs
never grow a second PostgreSQL.

Team attachment is a **hybrid**: physically a production belongs to a team
(credentials, isolated network, auth, data paths are reused; invisible in the
typical single-team install), conceptually it is a separate tier — its own
MCP tool stack (`*_production`), its own dashboard tab, no interaction with
dev quotas/reaper. Multi-production hosting on one instance works but is
deliberately not optimized for ("90% of production installs host one
production").

Production hosting is a **strict global opt-in**: `[production].enabled =
true` is required. When disabled, Oduflow removes the dashboard and HTTP
entry points, rejects production MCP tools, suspends webhooks and backups,
and stops managed production Odoo containers before the dedicated PostgreSQL
container without deleting data. Re-enabling starts PostgreSQL first and then
all managed production Odoo containers.

**Deploys reuse the pull→classify→apply engine with code-only auto-rollback.**
`update_production` records the pre-pull commits (full clone — history is the
point), runs the shared engine (a "refresh" outcome is promoted to restart:
no `--dev=xml` in production), then verifies via module exit codes plus an
in-container health probe. On failure the checkout and extra worktrees are
`git reset --hard` to the recorded commits, the conf re-applied, the container
restarted. The **database is never rolled back automatically** — restoring a
snapshot is an explicit operator action. Every outcome lands in `deploys.json`
(success / rolled_back / rollback_failed) and registry flags drive the
DEPLOYING/UNHEALTHY badges. GitHub push webhooks (HMAC-SHA256, per-team
auto-generated secret that doubles as the team resolver) trigger the same
engine in the background — only for productions with `auto_update`, never for
dev environments; rapid pushes coalesce.

**Backups are a hybrid of cluster-level WAL-G and per-production snapshots**
(one shared production cluster means WAL-G PITR is cluster-wide by nature):

- **WAL-G** = disaster recovery: continuous WAL archiving to S3 (the static
  binary is downloaded at bootstrap and bind-mounted as a *directory* into the
  official postgres image, so backups can be enabled/reconfigured without
  recreating the container; `archive_command` is managed via `ALTER SYSTEM`),
  daily base backups, and `restore_cluster_pitr` — which also serves the
  "resurrect production on a fresh server from S3" path.
- **Snapshots** = per-production restore granularity: pg_dump streamed
  container→S3 (no temp disk) + a filestore revision + a manifest binding
  them to the deployed commit sha; on demand, before every deploy (hook), and
  daily on schedule. Restore is swap-based (`{db}__restore` → rename;
  filestore rebuilt beside and renamed), so a failed restore leaves the
  previous state intact.

**The filestore engine (`chunkstore/`) is a clean-room, duplicacy-inspired
lock-free CDC store**: content-defined chunking (64-bit rolling hash over a
min-size window, per-storage secret byte table), split content addressing
(keyed hashes: chunk_hash for integrity, chunk_id for naming), zstd per chunk
(client-side encryption deliberately deferred — S3 SSE covers at-rest, no
key-loss risk; a format flag is reserved), incremental backups that splice
unchanged files' chunk ranges from the previous revision, and two-step fossil
collection for pruning. One chunkstore per team deduplicates across a team's
productions. Written from the published algorithm description; no duplicacy
code was translated (its license covers code, not algorithms). Retention is
decided once at the manifest level; chunkstore revisions are pruned in
lockstep (`keep_revisions`), so no dangling references.

A reaper-style scheduler thread fires snapshots/base-backups/prune on the
rule `last_success < fire_time <= now` (catch-up after downtime for free, no
double-fire after restarts). `GET /healthz` (public, 200/503) checks dev PG,
prod PG, Traefik, S3 HeadBucket, disk ≥85% and unhealthy productions, and
feeds the dashboard's status-bar chips.

## How it works (macro)

`create_production` → registry record (reserves the name, mints the team
webhook secret) → full clone → `CREATE DATABASE` in the prod cluster
(optionally seeded from a template via pg_dump/pg_restore — template DBs live
in the dev cluster and `CREATE DATABASE ... TEMPLATE` cannot cross clusters) →
plain filestore dir (no overlay) → container with a custom-domain Traefik
`Host()` rule, production odoo.conf chain (team > bundled; never the repo)
with auto-tuned workers, no `--dev=xml`, no sanitize. Failure rolls
everything back including the registry record. `[backup]` in `oduflow.toml`
(bucket + keys; everything else defaulted) configures the backup subsystem;
scheduled backup work runs only while `[production].enabled = true`.

## Consequences

- Production data is physically separated; dev tooling cannot see or touch
  productions (namespace guard in dev tools + no branch label).
- One prod cluster keeps RAM/tuning simple, at the cost of cluster-wide-only
  PITR; per-production point restores go through snapshots (accepted
  trade-off; per-production PG containers remain possible later since every
  DB primitive is parametrized by container name).
- Deploy failures self-heal at the code level; data-level recovery is always
  explicit — an operator decision, surfaced with a warning when the restored
  data's commit doesn't match the checkout.
- The chunkstore is pure Python (a few MB/s chunking) — fine because Odoo
  filestores are content-addressed (files only appear/disappear), so after
  the initial backup only the day's new files are chunked.
- New deps: boto3, zstandard. WAL-G pins v3.0.3 (last upstream release with
  PostgreSQL builds); Debian-based postgres images required (glibc binary).

## History

- 2026-10-14 — the repo `.oduflow/odoo.prod.conf` was dropped from the
  production conf chain: anyone who can push to the deployed branch could
  otherwise tune the production server. Operator-only per-production
  overrides and the team `odoo.prod.conf` remain.
- 2026-07-21 — production hosting became an explicit global opt-in; disabling
  it gates every entry point and stops the managed production workload stack
  without deleting state.
- 2026-07-11 — v1 landed as a staged series on `litnimax/production-hosting`:
  foundation (settings/naming/registry/tuning) → prod PG infra + WAL-G →
  lifecycle + MCP stack → update engine with rollback → chunkstore →
  snapshots/restore/PITR → scheduler → webhooks/health/dashboard.
