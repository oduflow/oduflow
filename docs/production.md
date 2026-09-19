# Production Hosting

Oduflow can host **production** Odoo environments alongside the dev
environments it was built for. Productions get special treatment:

- a **dedicated PostgreSQL cluster** (`oduflow-prod-db`) — physically
  separate from the dev one, auto-tuned for production workloads;
- a **custom domain** per production (`erp.customer.com`), routed by Traefik
  with a Let's Encrypt certificate — plus optional **extra domains** routed to
  the same production (e.g. the client's own public domain);
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
    allow_copy_to_dev_mcp=True,  # may agents copy this production into dev?
)
```

### Domains

In a team with [`base_domain`](traefik.md#team-base-domain) configured, the
primary `domain` must lie in the team zone — the zone apex
(`demo.example.com`) or a subdomain (`erp.demo.example.com`) — and may be
omitted: the team's **first** production defaults to the apex, later ones to
`<name>.<base_domain>`. Without a base domain, `domain` is required and may be
any FQDN.

`extra_domains` adds further public FQDNs routed to the same production —
typically the client's own domain alongside the team-zone name:

```text
create_production(name="erp", domain="demo.example.com",
                  extra_domains=["myodoo.pl"], ...)
```

All domains land in one Traefik router (`Host(a) || Host(b)`), each with its
own Let's Encrypt certificate; every DNS record must point at this server.
Extra domains may be arbitrary FQDNs but must not fall inside another team's
zone, and every domain — primary or extra — must be unused anywhere else in
the deployment (other productions, team hostnames, static routes).

`template_name` is the migration path for an existing production: import it
first (e.g. [from Odoo.sh](templates.md)), then create the production from
that template — the database is copied into the production cluster and the
filestore into the production's plain (non-overlay) directory.

The clone is **full** (not shallow): the branch's commit history is the
production's deploy history and the source of rollback targets.

### Promoting a dev environment

`from_environment` turns an existing dev environment into the seed — the
promotion path from "it works on the branch" to "it serves customers":

```text
create_production(name="erp", domain="erp.acme.com", from_environment="feature-x")
```

The environment's database and filestore are copied (its Odoo container is
briefly stopped so the pair is consistent, then restarted), and omitted
`repo_url` / `branch` / `odoo_image` / `git_user` / `extra_addons` default to
the environment's own — explicit arguments still win. Unlike the
save-as-template detour, no intermediate template is created and the source
environment is **not reset** — it keeps living as a dev environment.
`from_environment` and `template_name` are mutually exclusive.

Promotion also inherits the source environment's user environment variables.
`secret:<name>` references stay in the production registry and container labels;
values are resolved from the team's secret store only for the container runtime.
They survive domain/image/branch reconfiguration. Missing secrets are rejected
before the source is stopped. Pass `env_vars={...}` to replace the inherited set,
or `{}` to inherit none. `reconfigure_production(env_vars={...})` replaces the
stored set; omitting it preserves the existing variables. The managed database
variables `HOST`, `PORT`, `USER`, and `PASSWORD` cannot be overridden.

No sanitization happens — the data goes *into* production. One caveat: if
the source environment was itself created from a production
(`from_production`), its data was sanitized on that copy, and the new
production starts with that sanitized data (the result warns about this).

The dashboard offers the same via **More → Promote to Production** on an
environment card, which opens the create-production form pre-filled.

To promote into a production that **already exists**, use
`restore_production(from_environment=…)` instead — see
[Backups](#backups) for the restore mechanics.

## Reconfiguring a production

A production's settings are not frozen at creation.
`reconfigure_production` changes any of the domain, extra domains, Odoo
image, deployed branch, repository URL, git user, or the extra addon repos,
then **recreates the container** to match — the database and filestore live outside the
container and are preserved; expect a brief downtime:

```text
reconfigure_production(name="erp", domain="erp.newcustomer.com")
reconfigure_production(name="erp", extra_domains=["myodoo.pl"])  # [] removes all
reconfigure_production(name="erp", branch="18.0-stable")
reconfigure_production(name="erp", extra_addons={"acme-addons": "production"})
```

Omitted arguments are left unchanged (`git_user=""` explicitly clears the
git user). The registry record is updated first and the workspace/container
are converged to it, so re-running the same call after a mid-way failure
repairs a missing container or checkout instead of reporting a no-op. Two
things reconfigure deliberately does **not** do:

- Changing `odoo_image` does not migrate the database. A minor image refresh
  is safe; a major Odoo version bump additionally needs an explicit module
  upgrade plan.
- Changing `branch`/`repo_url` deploys the new code as-is (restart only).
  Run `update_production(install=..., upgrade=...)` afterwards if the new
  code needs module changes.

The dashboard offers the same settings on each production card under
**More → Settings**, together with the *agent copy to dev* gate
(dashboard-only, see [Copying production data to dev](#copying-production-data-to-dev)).

### odoo.conf overrides

The generated production `odoo.conf` merges a base conf chain
(`.oduflow/odoo.prod.conf` in the repo > team `odoo.prod.conf` > bundled)
with [auto-tuned](#configuration) worker/limit settings. Per-production
overrides sit on top of both and survive deploys, retunes and reconfigures:

```text
set_production_odoo_conf(name="erp", options={"limit_time_real": "300"})
set_production_odoo_conf(name="erp", unset="limit_time_real")
```

An explicit override beats the auto-tuned value (e.g. pin `workers`). Keys
managed by Oduflow are refused: `addons_path` and `data_dir` are generated,
and the `db_*` connection keys come from container env vars (option names
are compared case-insensitively and stored lowercased, matching how Odoo
reads them). Current overrides are shown by `get_production_info`; the
dashboard edits them in the same **Settings** panel. Applying restarts the
container (brief downtime) unless `restart=false`; a call that leaves the
overrides unchanged skips the restart entirely.

## Deploys and rollback

Module preflight and post-install checks use the production PostgreSQL cluster
and the production's own database role. Development requests retain their
separate cluster. An exception after source synchronization, including a module
preflight SQL failure, triggers code rollback just like a failed module command.

`update_production(name)` pulls the branch (and extra-addon worktrees),
classifies the changes (or takes explicit `install=` / `upgrade=` /
`restart=true`), applies them, and **verifies** the deploy — module exit
codes plus an in-container health check. A changed `.oduflow/requirements.txt`
or `.oduflow/apt_packages.txt` — in the main repo or an extra-addon repo —
reinstalls the pip/apt dependencies into the container before the restart
(they are also installed from scratch whenever the container is recreated:
create, reconfigure). In production a "refresh"-class
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

**Restoring from a dev environment.** The same tool also promotes a dev
environment's data into an *existing* production — the counterpart of
`create_production(from_environment=…)` for productions that already live:

```text
restore_production(name="erp", from_environment="feature-x", confirm="erp")
```

The environment's database and filestore are copied while its Odoo is
briefly stopped (the environment is **not reset**), staged, and swapped in
with the same all-or-nothing mechanics as a snapshot restore. No
sanitization happens — the data goes *into* production — and no `[backup]`
configuration is required. The production's code checkout is not touched;
the result warns when the environment's commit differs from the deployed
one. Take a `snapshot_production` first if the current production data may
still be needed. `snapshot_id` and `from_environment` are mutually
exclusive.

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

restores the **entire cluster** (every production database at once — including
auxiliary service databases created with `cluster="prod"`) from the
latest base backup + WAL replay — optionally to a point in time
(`target_time="2026-07-10 12:00:00+00"`). The displaced data directory is
kept inside the Docker volume for manual cleanup. Because the state lives in
S3, a **fresh Oduflow server** with the same `[backup]` section can resurrect
the cluster the same way.

`production_backup_status()` shows per-production snapshot state, WAL
archiver health (`pg_stat_archiver`), base backup inventory, and S3
reachability.

### WAL-G certificates and storage health

Oduflow writes a CA bundle alongside `walg.json` in
`<base_data_dir>/walg/` and configures WAL-G to read it through the existing
read-only `/etc/walg` directory mount. This works even when the PostgreSQL
image has no system CA bundle, and covers WAL uploads, base backups, and PITR
helpers. Restarting Oduflow refreshes these files for existing containers;
PostgreSQL does not need to be recreated for this fix.

The CA source is `AWS_CA_BUNDLE`, then `SSL_CERT_FILE`, then the server's
default CA file, with the boto3/botocore bundle as a fallback when there is no
default file. Set an explicit override in the Oduflow service environment for
a private certificate authority. Invalid overrides fail rather than disabling
TLS verification or replacing a working bundle.

The background WAL monitor checks storage list access using WAL-G **inside
production PostgreSQL, as the postgres user**. The dashboard's **WAL-G** health
chip and `/healthz` use its cached result. The
diagnostic command has a five-second timeout, with forced termination after
another two seconds. A TLS/access error degrades `/healthz` even when the
separate S3 check from the Oduflow server succeeds. List access does not prove
upload permissions, successful archiving, or a complete PITR chain. The backup
status API retains local archiver statistics when the remote inventory query
fails; inventory commands have a 30-second timeout.

### Production readiness at deployment

New installations using the default PostgreSQL 15 use
`oduist/oduflow-postgres:15-bookworm-1`. This image includes `ca-certificates`
and pins the upstream Debian Bookworm image by digest. A custom
`[production].postgres_image` takes precedence; a non-default
`[database].image` is still inherited for compatibility with other PostgreSQL
majors. Existing containers are reused, never automatically replaced or
upgraded across majors. Their WAL-G trust is repaired through the persistent
mounted CA bundle described above.

With `[backup]` configured, provisioning and production start/restart/deploy
require a successful check **inside PostgreSQL as the postgres user**:

1. Check the actual disk and queue safety thresholds.
2. Check the mounted CA bundle, executable and credentials file, then use
   WAL-G to list storage, upload a unique probe, read it back, compare its
   content and delete the probe. The storage phase is limited to 30 seconds,
   with bounded cleanup/forced termination. The credentials need read, list,
   write and delete permissions for the probe under
   `<backup.prefix>/walg/oduflow-preflight/`; backup objects are not modified.
3. Enable the managed archive command and confirm PostgreSQL applied it.
4. Generate a restore point and switch WAL, then wait up to
   `upload_timeout + 75` seconds for that segment to be archived. Only then
   allow the production application to start. This confirms current archive
   delivery; a recoverable PITR chain additionally requires a base backup.

Step 4 is skipped for an already admitted production — `restart_production`,
a deploy or a rollback — when the sample from step 1 already proves archiving
is healthy: the managed archive command is active, archiving is not stalled,
and the storage listing in that same sample succeeded. A hung production stays
restartable during a brief storage outage instead of waiting minutes for a new
segment. Admitting a new production, or starting a stopped one, always runs
the full check.

An error blocks the operation and appears as **Last startup check** in the
WAL panel. It stops driving the overall status once a newer healthy sample
supersedes it; live sampling then reports the current state. It does not convert an existing archive command to `/bin/true` or
delete retained WAL. An old no-op command is changed to an empty, retaining
command before preflight; fresh generated configurations also retain WAL
until provisioning decides. Existing working archiving continues during a
preflight failure; the disk/queue guard remains responsible for emergency
shutdown. Development environments can still start if production is unready.

The PostgreSQL image is published for amd64 and arm64 by
`.github/workflows/publish-postgres.yml` only from `main`, with an immutable
version tag. Publish it successfully before releasing the Oduflow package;
both the PyPI and application Docker release workflows verify its availability.
For an image update, change the base digest, bump `POSTGRES_IMAGE_VERSION`
and `DEFAULT_PROD_POSTGRES_IMAGE` together, merge, and wait for publication.
No package installation is performed inside PostgreSQL during deployment or
emergency recovery.

### WAL monitoring and automatic disk protection

The **PostgreSQL WAL** panel in Production is shared by every production and
team. It shows the queue's segment count and bytes, age of the oldest waiting
segment, time without archive progress, current upload duration, last upload
exit status, PostgreSQL restart count, and free space on the actual WAL
filesystem. Space reserved for root is excluded. A dedicated daemon samples
locally every 15 seconds, independently of backup jobs and dashboard polling;
the storage probe runs only after the local protection decision. Missing or
older-than-60-second samples are errors, never healthy results.

An empty queue is idle, not stalled. With a queue, lack of successful archive
progress triggers warning/error thresholds. A draining but old backlog stays
visible. WAL upload attempts have a hard timeout and return failure on timeout
or termination; PostgreSQL retains the segment and retries. Failed downloads
of the WAL-G executable never switch configured archiving to a success no-op.

Defaults can be changed in TOML:

```toml
[production.wal]
upload_timeout = 120    # seconds; TERM, then KILL after another 5 seconds
warn_after = 120        # seconds without archive progress, with a queue
stall_after = 300
stop_free_gb = 2        # GiB available to postgres: safety reserve
resume_free_gb = 4      # recovery headroom; must exceed stop_free_gb
stop_within = 300       # estimated seconds until the reserve is reached
warn_queue_gb = 2       # GiB of unarchived WAL before warning
stop_queue_gb = 8       # GiB of unarchived WAL before protective stop
```

Protection is active whenever production hosting is enabled, even without
S3 backups or while archiving is paused. It trips as soon as free space
reaches the reserve. A prediction alone is not enough: consumption measured
between the two most recent samples must predict reaching the reserve within
`stop_within`, and that prediction must hold for two consecutive samples.
`df` covers the whole filesystem, so a finished burst from an unrelated
consumer never stops the cluster, while a continuing leak still does.
It also trips when the unarchived queue reaches
`stop_queue_gb`, even on a large disk. Protection saves a persistent latch,
disables Docker restart policies, stops managed production applications,
then stops the shared PostgreSQL container. Failed stops are reported and retried. The latch blocks production
starts, deploys, restores and new backup jobs, including after Oduflow or
Docker restarts. Protection can interrupt in-flight jobs; it does not wait for
their locks while the disk fills.

Set the reserve for peak write volume and shutdown time. The monitor requires
Oduflow and Docker to be responsive; it cannot guarantee protection against
arbitrarily fast disk exhaustion or other host processes filling the same
filesystem after PostgreSQL stops. Monitor `/healthz` externally as well.

The panel and MCP offer these cluster-wide actions (MCP mutations require
`confirm="ALL-PRODUCTIONS"`):

| Action | Effect |
| --- | --- |
| `pause` | Retains unarchived WAL and interrupts the active upload. The queue can grow; disk protection stays active. |
| `resume` | Refreshes WAL-G config/certificates and resumes archiving. Does not restart stopped PostgreSQL. |
| `retry` | Terminates only the current `wal-push`, including a legacy upload predating the timeout wrapper; PostgreSQL retries it. |
| `recover` | Requires recovery headroom; starts only PostgreSQL, keeping applications stopped and rejecting network database connections. Local maintenance and outbound S3 access remain available. Requests a WAL switch to verify real archiving. |
| `release` | Requires recovery headroom, a safe consumption rate, working storage access, and a successful archive after recovery began. Restores normal connections and restart policies; applications remain stopped until explicitly started. |

Use `production_wal_status()` for cached diagnostics and
`control_production_wal(action="recover", confirm="ALL-PRODUCTIONS")` for
control. REST equivalents are `GET /api/productions/wal-status` and
`POST /api/productions/wal-control`. Full team authentication follows the
existing cluster PITR access model; environment-scoped access is excluded.
Queue size warns at `warn_queue_gb` and protects at `stop_queue_gb`. During
fenced PostgreSQL-only recovery the queue may exceed the stop threshold so
it can drain; disk pressure still stops PostgreSQL. Releasing protection
requires the queue to fall below `warn_queue_gb` as well as free-space headroom
and confirmed archive progress.

The monitor's latch is stored in `<base_data_dir>/wal_guard.json`; do not
delete it to bypass recovery checks. Recovery temporarily replaces the managed
`PGDATA/pg_hba.conf`, preserving the original alongside it, and restores it on
release. Custom HBA paths require operator intervention.

### Recovering from a full WAL disk

Production tables and WAL live together in the `oduflow-prod-db-data` Docker
volume, unlike the per-team tablespaces used for development. Inspect free
space on the filesystem containing that volume, including the space available
to the non-root postgres user. `max_wal_size` is not a hard limit when WAL
cannot be archived.

If logs report `No space left on device` and WAL-G reports an unknown
certificate authority:

1. Stop application writers to reduce additional WAL generation. Free several
   GiB from known disposable files **outside PostgreSQL data**, or expand the
   volume's filesystem. Never manually delete files from `pg_wal`.
2. Deploy the fix and restart Oduflow to refresh the mounted CA bundle and
   WAL-G configuration. If protection is active, use **Resume archiving** if
   paused, then **Start PostgreSQL recovery** after sufficient space is free.
   No PostgreSQL recreation is required.
3. Check the WAL-G health result and PostgreSQL logs. Confirm actual progress:
   `pg_stat_archiver.archived_count` increases and the queue of `.ready` files
   in `pg_wal/archive_status` drains. A successful list probe alone is not
   enough. An already-running WAL-G attempt uses its old configuration until
   it exits; **Retry upload** interrupts it without discarding the segment.
4. Allow PostgreSQL to recycle eligible WAL itself and verify free space
   recovers before releasing protection and explicitly starting the desired
   applications. Other retention requirements, such as replication slots,
   can also keep WAL on disk.

Do not switch `archive_command` to `/bin/true` to drain the queue: it reports
success without saving the segments and can break PITR continuity.

## Copying production data to dev

Productions are seeded from templates; the same road runs backwards, so a
developer can reproduce a bug on real data:

```text
save_production_as_template(prod_name="erp", template_name="erp-2026-09")
create_environment(branch="bugfix-invoice", from_production="erp")
```

Both dump the production database out of the production cluster with a
consistent `pg_dump` and restore it into the **dev** cluster, and snapshot the
production filestore as the template's baseline. **The production keeps
serving** — nothing is stopped or modified on its side.

`create_environment(from_production=…)` routes through one managed template per
production, `prod-<name>`, published on first use and reused afterwards; refresh
it with `save_production_as_template(name, "prod-<name>", overwrite=True)`. See
[Create a Template from Production](templates.md#create-a-template-from-production)
and [Creating an Environment from Production](environments.md#creating-an-environment-from-production).

!!! danger "The copy is unsanitized until an environment is created"
    The template carries real customer data and credentials. Environments made
    from it are neutralized and run the repository's sanitize scripts by
    default; the template itself is production-confidential.

**`allow_copy_to_dev_mcp`** (default `true`, set at `create_production`) gates
**new copies**: when it is `false`, an MCP/CLI agent asking for either tool gets
a refusal — and no MCP tool can turn the flag back on. It is a gate on *agents*,
not on people: the dashboard's Production tab is never gated and is the only
place the flag can be toggled, so an agent cannot re-enable its own access.
Productions created before the flag existed behave as `true`.

The flag does **not** revoke a copy that already exists. A `prod-<name>` (or
any) template published from the production stays usable through
`create_environment(template_name=...)` like every other template — its data is
neutralized on the way into each environment. The one thing agents lose is the
raw form: `sanitize=false` on a template whose `source_production` has the flag
off is refused. To withdraw the data itself, `delete_template` the copy.

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
| `reconfigure_production` | Change domain/image/branch/repo/extra addons; recreates the container |
| `set_production_odoo_conf` | Per-production odoo.conf overrides on top of auto-tuning |
| `snapshot_production` / `list_production_snapshots` | Snapshots to S3 |
| `restore_production` | Restore DB + filestore from a snapshot or a dev environment |
| `set_production_backup_schedule` | Per-production snapshot time / off |
| `production_backup_status` | Backup posture (snapshots + WAL-G + S3) |
| `save_production_as_template` | Publish the production's DB + filestore as a dev template (unsanitized) |
| `prune_production_backups` | Apply retention now |
| `restore_cluster_pitr` | Cluster-wide disaster recovery / PITR |
| `delete_production` | Remove (database/files kept unless `drop_database`) |
