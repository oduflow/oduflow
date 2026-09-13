# Production Hosting

Oduflow can host **production** Odoo environments alongside the dev
environments it was built for. Productions get special treatment:

- a **dedicated PostgreSQL cluster** (`oduflow-prod-db`) — physically
  separate from the dev one, auto-tuned for production workloads;
- a **custom domain** per production (`erp.customer.com`), routed by Traefik
  with a Let's Encrypt certificate;
- an **auto-tuned production `odoo.conf`** (workers from host CPU/RAM, cron
  enabled, proxy mode) — never the dev profile;
- **no sanitization/neutralization**, no idle reaper, no `--dev=xml`;
- deploys with **automatic code rollback** on failure;
- **S3 backups**: continuous WAL archiving (WAL-G), scheduled snapshots
  (database dump + deduplicated filestore), disaster-recovery PITR.

Productions are managed by their own MCP tool stack (`create_production`,
`update_production`, …) and a dedicated **Production** tab in the dashboard —
they never mix with dev environment tooling.

## Requirements

- `routing_mode = "traefik"` (custom domains are Traefik `Host()` rules).
- The production's DNS record must point at the server.
- For backups: an S3-compatible bucket (AWS, MinIO, Cloudflare R2, …).
- Debian-based `postgres:*` images (the default; `-alpine` images do not run
  the WAL-G binary).

## Configuration

Production hosting is disabled by default. Enable it globally in TOML and
restart Oduflow; productions themselves are then created at runtime:

```toml
[production]
enabled = true          # required; restart Oduflow after changing
postgres_image = ""     # default: [database].image
workers_cap = 8         # upper bound for auto-tuned Odoo workers

[backup]                # configures backups; production must also be enabled
bucket = "acme-backups"
access_key = "AKIA..."
secret_key = "..."
endpoint = ""           # empty = AWS; set for MinIO/R2 (path-style implied)
region = "eu-central-1"
# defaults you normally leave alone:
# prefix = "oduflow"
# snapshot_time = "02:00"      (daily per-production snapshots)
# basebackup_time = "03:30"    (daily WAL-G base backup)
# keep = ["30:180", "7:30", "1:7"]  (snapshot retention: interval:age days)
# walg_keep_full = 7           (base backups retained)
```

While disabled, the dashboard tab and production HTTP/webhook routes are not
registered, production MCP tools return an enablement error, and scheduled
backup work does not run.

The production PostgreSQL cluster is provisioned lazily and idempotently. If
production hosting is disabled, Oduflow stops every managed production Odoo
container and then its dedicated PostgreSQL container without deleting any
container, volume, database, filestore, or registry data. Re-enabling starts
PostgreSQL first and then starts all managed production Odoo containers.

Enabling production also changes the unified host resource plan. New configs
coordinate dev PostgreSQL, production PostgreSQL, and production Odoo workers
instead of letting each profile size itself against the whole host. Existing
configs are not silently replaced: after changing `enabled`, run
`oduflow retune-postgres` to inspect the new plan, then
`oduflow retune-postgres --apply`. The apply step also stages regenerated
worker settings in every existing production Odoo container; restart the
PostgreSQL and Odoo containers it lists.

## Creating a production

```text
create_production(
    name="erp",
    repo_url="https://github.com/acme/odoo-erp.git",
    branch="production",
    domain="erp.acme.com",
    odoo_image="odoo:18.0",
    template_name="acme-prod",   # optional: seed DB+filestore from a template
    auto_update=False,
)
```

`template_name` is the migration path for an existing production: import it
first (e.g. [from Odoo.sh](templates.md)), then create the production from
that template — the database is copied into the production cluster and the
filestore into the production's plain (non-overlay) directory.

The clone is **full** (not shallow): the branch's commit history is the
production's deploy history and the source of rollback targets.

## Deploys and rollback

`update_production(name)` pulls the branch (and extra-addon worktrees),
classifies the changes (or takes explicit `install=` / `upgrade=` /
`restart=true`), applies them, and **verifies** the deploy — module exit
codes plus an in-container health check. In production a "refresh"-class
change (XML/JS only) still restarts the container: there is no `--dev=xml`.

If verification fails, the checkout is reset to the pre-deploy commit, the
config re-applied and the container restarted — **the code rolls back
automatically**. The database is *never* rolled back automatically: if a
module upgrade left it inconsistent, restore a snapshot explicitly
(`restore_production`). A snapshot is taken automatically before every deploy
when backups are configured.

Every deploy lands in the production's history (`production_deploys`):
commits, action, modules, status (`success` / `rolled_back` /
`rollback_failed`), trigger (mcp / ui / webhook / schedule).

Manual code rollback to any commit: `rollback_production(name, to_commit)`.

## GitHub webhooks (auto-deploy)

Point a GitHub webhook at `POST https://<server>/api/webhooks/github`
(content type `application/json`) with the team's webhook secret — shown in
the dashboard's Production tab, auto-generated with the first production.
Requests are authenticated by their `X-Hub-Signature-256` HMAC.

A push deploys only productions that match the repo + branch **and** have
`auto_update` enabled (`set_production_auto_update`). Dev environments are
never touched by webhooks. Failed webhook deploys roll back like any other.

## Backups

Two complementary layers (both to S3, enabled by `[backup]`):

**Snapshots — per-production restore.** A snapshot is a consistent triple:
`pg_dump` of the production database (streamed to S3, no temp disk), a
deduplicated filestore revision, and a manifest recording the deployed commit
sha. Taken daily (`snapshot_time`, per-production override via
`set_production_backup_schedule`), before every deploy, and on demand
(`snapshot_production`). Restore with:

```text
restore_production(name="erp", snapshot_id="20260711T020000Z", confirm="erp")
```

Restore is swap-based: the dump is restored into a scratch database and
swapped in by rename; the filestore is rebuilt beside the live one and
swapped in. A failed restore leaves the previous state untouched. If the
snapshot's commit differs from the checkout, the result warns you to
`rollback_production` to the matching commit.

The filestore engine (a clean-room, duplicacy-inspired content-defined
chunking store) deduplicates across daily revisions *and* across a team's
productions; retention (`keep`) is applied weekly with safe two-step fossil
collection.

**WAL-G — cluster disaster recovery.** Continuous WAL archiving plus daily
base backups of the whole production cluster. This is the "server burned
down" path:

```text
restore_cluster_pitr(target_time="", confirm="RESTORE-CLUSTER")
```

restores the **entire cluster** (every production database at once) from the
latest base backup + WAL replay — optionally to a point in time
(`target_time="2026-07-10 12:00:00+00"`). The displaced data directory is
kept inside the Docker volume for manual cleanup. Because the state lives in
S3, a **fresh Oduflow server** with the same `[backup]` section can resurrect
the cluster the same way.

`production_backup_status()` shows per-production snapshot state, WAL
archiver health (`pg_stat_archiver`), base backup inventory, and S3
reachability.

## Health

`GET /healthz` (public, no auth, no secrets) returns 200 when healthy and
503 when degraded — point your uptime monitor at it. Checks: dev PostgreSQL,
production PostgreSQL, Traefik, S3 (HeadBucket), disk usage (warn at 85%),
and productions flagged unhealthy by a failed rollback. The dashboard's
status bar shows the same checks as chips.

## Deleting a production

`delete_production` (or **Delete** in the dashboard) removes the container and
the registry record, but **keeps the database and the workspace** (filestore,
repo, deploy history) on disk — productions are precious, deleting bytes is
opt-in. Pass `drop_database=true` over MCP/CLI to remove everything at once.

Kept leftovers are *tombstoned* (a `deleted.json` marker in the workspace) so
they can be reclaimed later:

- **Deferred purge** — set `[lifecycle] prod_purge_hours = N` in
  `oduflow.toml` and the background sweep permanently purges the leftovers
  (database, PostgreSQL role, workspace) N hours after the deletion. `0`
  (default) keeps them forever. Re-creating a production with the same name
  clears the tombstone, so a revived production is never purged.
- **Immediate purge** — `oduflow cleanup --purge-deleted-productions`
  lists tombstoned leftovers; add `--force` to purge them now, regardless of
  age.

Only tombstoned leftovers are ever purged: a workspace without the marker is
presumed alive and is never touched (`oduflow cleanup` skips the whole
`prod-*` namespace for the same reason).

## MCP tool reference

| Tool | Purpose |
|---|---|
| `create_production` | Provision a production (optionally from a template) |
| `list_productions` / `get_production_info` | Status, deployed commit, history, backups |
| `update_production` | Deploy latest commits with auto code rollback |
| `rollback_production` | Manual code rollback to a commit |
| `production_deploys` | Deploy history |
| `production_logs` | Container logs |
| `start_production` / `stop_production` / `restart_production` | Lifecycle |
| `set_production_auto_update` | Toggle webhook auto-deploy |
| `snapshot_production` / `list_production_snapshots` | Snapshots to S3 |
| `restore_production` | Restore DB + filestore from a snapshot |
| `set_production_backup_schedule` | Per-production snapshot time / off |
| `production_backup_status` | Backup posture (snapshots + WAL-G + S3) |
| `prune_production_backups` | Apply retention now |
| `restore_cluster_pitr` | Cluster-wide disaster recovery / PITR |
| `delete_production` | Remove (database/files kept unless `drop_database`) |
