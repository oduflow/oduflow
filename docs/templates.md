# Template Management

![Templates Dashboard](img/templates.png)

Templates are the foundation of Oduflow's instant environment creation. A template consists of a PostgreSQL dump file and an optional filestore directory.

Create templates from production dumps, staging snapshots, or from scratch. Maintain **multiple named templates** side-by-side (e.g. per Odoo version, per client, per project phase) and spin up any combination of branch + database in seconds.

## Starting from Scratch (No Production Dump)

If you don't have a production database dump — for example, you're starting a new Odoo project or just want to try Oduflow — you can generate a clean template automatically.

### Generate a clean template

```bash
oduflow init-template --odoo-image odoo:19.0 --template-name default
```

If a `dump.sql` or filestore already exists, the command will refuse to run. Use `--force` to overwrite:

```bash
oduflow init-template --odoo-image odoo:19.0 --template-name default --force
```

This will:

1. Start a PostgreSQL container (if not already running)
2. Run a temporary Odoo container that initializes a fresh database with the `base` module
3. Dump the database to `{data_dir}/team_{ID}/templates/{name}/dump.pgdump`
4. Extract the filestore to `{data_dir}/team_{ID}/templates/{name}/filestore/`
5. Load the dump into the template database automatically

### Install additional modules during generation

```bash
oduflow init-template --odoo-image odoo:19.0 --template-name default --modules base,web,contacts,sale
```

### Named templates for different projects

```bash
oduflow init-template --odoo-image odoo:19.0 --template-name myproject-v19
oduflow init-template --odoo-image odoo:15.0 --template-name legacy-v15
```

## From a Production Dump

Place your dump file at `{data_dir}/team_{ID}/templates/default/dump.sql` (plain SQL) or `dump.pgdump` (PostgreSQL custom format) and optionally copy the filestore:

```bash
mkdir -p /srv/oduflow/team_1/templates/default/
cp /path/to/production.sql /srv/oduflow/team_1/templates/default/dump.sql
cp -r /path/to/filestore/ /srv/oduflow/team_1/templates/default/filestore/
oduflow import-template --template-name default --refresh
```

## Saving a Branch as Template

When you've made significant changes in a branch environment (installed modules, created configurations), you can save it as the new template:

```bash
oduflow template-from-env my-branch --template-name default
oduflow template-from-env my-branch --template-name myproject  # save to a named template
```

This operation:

1. Dumps the branch database to a new template dump file
2. Reloads the template database from the new dump
3. Snapshots the branch's merged filestore
4. Unmounts the overlay filesystems of other active environments on this template (keeping their `upper` deltas)
5. Replaces the template filestore with the snapshot
6. Remounts those overlays against the new baseline, **preserving each environment's filestore changes** by default
7. Restarts the affected containers

The **source environment** is always reset to the new baseline (its data just became the template). **Other environments keep their filestore changes** (the overlay `upper` layer) — this is non-destructive by default. Their existing files shadow the new template (env-local edits win); files they deleted stay deleted; new template files show through.

To instead discard other environments' changes and reset them to the clean new baseline:

```bash
oduflow template-from-env my-branch --template-name default --reset-env-changes
```

!!! note
    `--reset-env-changes` is **destructive**: other environments lose their filestore deltas. Without it, their changes are preserved.

!!! info "Copy-mode templates"
    Environments created from a small (copy-mode, `use_overlay=false`) template have an independent filestore copy, not an overlay. They are not affected by template filestore updates and are left untouched.

## Create a Template from Production

A [production](production.md) can be published as a dev template — the reverse of seeding a production from a template. The production database is dumped out of the production cluster and restored into the dev cluster as the template database, and the production filestore becomes the template's baseline:

```bash
oduflow call save_production_as_template '{"prod_name":"erp","template_name":"erp-2026-09"}'
```

The production **keeps serving throughout**: the dump is a consistent `pg_dump` snapshot, nothing is stopped or changed on the production side (the same trade-off as `snapshot_production`).

The template records the production's `repo_url`, `odoo_image`, `git_user` and extra addons, plus [provenance](#template-metadata) — the production's branch, the commit its checkout is on, and the snapshot time — so environments created from it report code/database drift like any other template.

!!! danger "The template holds unsanitized production data"
    Real customer records, real email addresses, real API credentials. Sanitization happens **later**, when an environment is created from the template: `create_environment` runs Odoo's neutralization plus the repository's [sanitize scripts](environments.md#database-sanitization) by default. Treat the template itself — and its dump on disk — as production-confidential.

Like `template-from-env`, this refuses to overwrite an existing template. Pass `overwrite=true` to deliberately re-baseline one from the current production data:

```bash
oduflow call save_production_as_template '{"prod_name":"erp","template_name":"erp-2026-09","overwrite":true}'
```

Environments on that template keep their filestore changes (the overlay `upper` layer) unless you pass `reset_env_changes=true`, which is destructive.

!!! info "MCP copies can be switched off per production"
    A production whose administrator disabled copies to dev refuses this tool (and the first `create_environment(from_production=...)`, which would publish). A template that was already published stays usable — sanitized only — and the metadata records its `source_production`. The flag gates MCP/CLI agents only; the dashboard is never gated. See [Copying production data to dev](production.md#copying-production-data-to-dev).

## Refreshing Template Overlays

Re-apply a template's current on-disk filestore to all live overlay environments without re-importing or re-saving — non-destructive by default (each environment keeps its `upper` deltas):

```bash
oduflow refresh-template default
oduflow refresh-template default --reset-env-changes   # discard env deltas (destructive)
```

Use this after changing the template filestore on disk, or to re-sync an environment that was busy/skipped during an import or save.

## Database Dump and Separate Filestore

Production backups are often delivered as two artifacts: a database dump first and a filestore archive or directory later. Use this flow when you imported a template with `--without-filestore`, or when a manual backup gives you the database and filestore separately.

```bash
# 1. Import the database only from a running Odoo instance
oduflow import-template https://my-odoo.example.com master_password \
  --db-name odoo19-mirage \
  --template-name prod \
  --without-filestore

# 2. Attach the filestore when it is available
oduflow attach-filestore prod /backups/odoo19-mirage-filestore.zip

# 3. Create environments from the complete template
oduflow call create_environment '{"branch":"dev","template_name":"prod"}'
```

`attach-filestore` replaces the template's filestore and updates `metadata.json` (`includes_filestore`, `filestore_size_mb`, and `use_overlay`). It does not reload the database.

Supported sources:

```bash
# Local archive; entries like odoo19-mirage/60/<sha1> are normalized to 60/<sha1>
oduflow attach-filestore prod /backups/odoo19-mirage-filestore.zip

# Local directory
oduflow attach-filestore prod /backups/odoo19-mirage/filestore

# Remote rsync over SSH
oduflow attach-filestore prod odoo@example.com:/srv/odoo/.local/share/Odoo/filestore/odoo19-mirage

# rsync daemon URL
oduflow attach-filestore prod rsync://backup.example.com/odoo/filestore/odoo19-mirage
```

Archives may be `.zip`, `.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`, or `.txz`. Local directories and remote sources are copied with `rsync -a --delete`, so `rsync` must be installed on the Oduflow host and reachable over SSH for `user@host:/path` sources.

Oduflow detects the wrapper prefix automatically by looking for Odoo filestore paths shaped like `XX/<40-character sha1>`. For example, an archive containing `odoo19-mirage/60/609e7ca59cc05bf0de7233c6781a381b742a2931` is installed as `filestore/60/609e7ca59cc05bf0de7233c6781a381b742a2931`. If a source has multiple possible wrappers, pass the prefix explicitly:

```bash
oduflow attach-filestore default /backups/filestore.zip --strip-prefix odoo19-mirage
oduflow attach-filestore default /backups/filestore.zip --strip-prefix none
```

Like `template-from-env`, this is non-destructive for live overlay environments by default: Oduflow remounts them against the new template filestore while preserving their `upper` changes. Pass `--reset-env-changes` only when you intentionally want those environments reset to the new baseline. Copy-mode environments are independent copies and are not changed by attaching a new template filestore.

## Importing from an S3 Prefix

For very large databases and filestores, skip archives entirely: upload the raw
dump and a one-to-one filestore copy to any S3-compatible bucket and import
straight from it. No Odoo master password, no ZIP packing on the source, no
double disk space.

Expected layout under the prefix — a dump file (`dump.pgdump`, `dump.sql`,
`dump.sql.gz`, `dump.pgdump.gz`; the hand-made `db.dump` / `db.dump.gz` names
are accepted too and installed under the canonical name) plus an optional
`filestore/` tree. If several dump candidates are present, the same
precedence as the template directory applies: canonical names win over
`db.dump`:

```
s3://mybucket/backups/mydb/
├── dump.pgdump
└── filestore/
    ├── 60/609e7ca59cc05bf0de7233c6781a381b742a2931
    └── ...
```

```bash
# On the source host
pg_dump -Fc mydb > dump.pgdump
aws s3 cp dump.pgdump s3://mybucket/backups/mydb/
aws s3 sync ~/.local/share/Odoo/filestore/mydb/ s3://mybucket/backups/mydb/filestore/

# On the Oduflow host (CLI, MCP tool import_template, or the dashboard)
oduflow import-template s3://mybucket/backups/mydb/ --template-name prod
```

### In the dashboard

**Templates → Import from S3** fills the same request in a form. Enter the
connection (endpoint — empty for AWS — region, access key ID, secret key),
type a bucket or leave it empty, and **Browse**: the dialog walks the bucket
one folder at a time, newest files first, and marks the folder that holds a
recognized dump. **Use this folder** fills in the source and suggests a
template name. You can also type the prefix directly.

A template imported this way is tagged **S3** in the list and gains a **Sync**
button — the same incremental re-sync as `--overwrite`, with the connection
and the stored secret already filled in.

How it behaves, in the order it matters at scale:

- **Parallel + resumable.** Filestore files download on several connections;
  an interrupted import re-run skips everything already staged. Nothing
  touches the live template until the staged copy is complete — the final
  swap happens under the standard overlay remount, so live environments keep
  their filestore changes.
- **Incremental re-sync with `--overwrite`.** Re-running against an existing
  template downloads only new/changed filestore files (unchanged ones are
  hardlinked from the current template), removes files deleted on S3, and —
  if the dump's S3 ETag is unchanged since the last sync — skips the dump
  download *and* the template DB reload. This makes a periodic
  "pull the latest production backup into dev" job cheap.
- **Credentials, remembered.** `--s3-access-key`/`--s3-secret-key` (with
  `--s3-endpoint` for MinIO or other S3-compatible stores, and `--s3-region`
  when required) win; otherwise the secret remembered for this template is
  reused; otherwise the `[backup]` settings when the bucket matches;
  otherwise the bucket is read anonymously (public bucket).

    **The secret key is never written into template metadata.** A raw value is
    stored as the team secret `s3-template-<template>` (visible under
    **Credentials → Secrets** in the dashboard, where it can be rotated) and
    the metadata records only a `secret:<name>` reference, so no template
    listing, metadata editor or MCP tool can echo it back. Pass
    `secret:<name>` yourself to sign with an existing team secret instead —
    Oduflow then leaves that secret alone.

    So a re-sync of a private bucket needs **no key at all**: omit
    `--s3-secret-key` and the stored one signs the request. It is required
    again when the endpoint or the access key changes, so a stored secret is
    never sent to a different endpoint or key pair. To keep the key out of
    `~/.bash_history`, `--s3-secret-key` defaults to `$ODUFLOW_S3_SECRET_KEY`.

    A secret Oduflow created is deleted together with the template, and
    released when the template is re-published from an environment or a
    production (its data no longer comes from the bucket).
- **Metadata.** The Odoo version, image, and installed modules are read from
  the restored database; `snapshot_at` records the dump's upload time, so
  template freshness reflects the data, not the import.

`--without-filestore` skips the `filestore/` tree for a database-only import.

### An Odoo backup `.zip` already in the bucket

If what you have is a backup `.zip` — written by Odoo's own database manager,
or downloaded from Odoo.sh — point the import straight at that object instead
of a folder:

```bash
oduflow import-template s3://mybucket/backups/acme-2026-10-13.zip --template-name prod
```

A key ending in `.zip` is unpacked as an Odoo backup (`dump.sql` plus
`filestore/`); anything else is read as a prefix holding the raw layout. A
re-sync with `--overwrite` still costs nothing while the object's ETag is
unchanged.

Prefer the raw layout for a large database when you can choose. A zip has to
be fetched whole every time it changes, its filestore replaces the template's
wholesale instead of syncing file by file, and an interrupted download starts
over — all of which the raw layout avoids. The `.zip` shape exists for the
archive you already have, not as the recommended way to make one.

## Importing from a Local Path

The same engine and layout work for a directory on the Oduflow host — useful
when a backup job or rsync already delivers the artifacts locally:

```bash
# Directory with dump.* + filestore/ — files are hardlinked into the
# template (near-instant on the same filesystem)
oduflow import-template /backups/mydb/ --template-name prod

# Periodic re-sync of the same template (only changed files, unchanged dump
# skips the DB reload)
oduflow import-template /backups/mydb/ --template-name prod --overwrite

# A single dump file imports database-only (format sniffed automatically)
oduflow import-template /backups/nightly.dump --template-name prod --overwrite
```

For a local source the dump-unchanged check uses size + mtime instead of an
S3 ETag; everything else (staging, atomic promote, overlay remount, metadata
refresh) behaves exactly like the S3 import.

## Refreshing from the Template's Own Files

If an external process writes `dump.*` and `filestore/` **straight into the
template directory** (an rsync drop point, scp from a backup host), apply
them with `--refresh` — no source argument:

```bash
rsync -a --delete backup-host:/srv/backups/mydb/ {data_dir}/team_{ID}/templates/prod/
oduflow import-template --template-name prod --refresh
```

This reloads the template DB from the dump found in the directory, re-reads
the Odoo version and module list from the restored database, fixes filestore
ownership, recomputes sizes/overlay mode in `metadata.json`, and remounts
live overlay environments against the updated filestore (keeping their
changes). Environment databases are **not** reloaded. Copy-mode environments
are unchanged. Retained generations created by scheduled sync are unchanged.
The dashboard exposes this operation as **Templates → More → Refresh**.
`refresh-template` is a separate command that only remounts the filestore.
For an already-used template, prefer staging via a local source and
`--overwrite`: direct writes happen before Oduflow can stop live overlays.

## Scheduled SSH sync (system cron)

Open **Templates → More → Source / schedule / log**. Set the SSH host, user,
port, absolute backup directory and five-field cron schedule; enable the
schedule and save. Times use the **host cron timezone**. **Sync now** queues a
request for cron's next minute tick using the saved settings. It works even
with the schedule disabled. The web server never starts the sync worker.

Prerequisites:

- A running system cron daemon and write access as root to `/etc/cron.d`.
  Oduflow manages only its own `oduflow-template-*` files; existing jobs remain
  untouched. Remove the configuration in the dialog before renaming or
  deleting the template; removing it also deletes the downloaded backup copy
  (the inbox) but keeps status and logs, and deleting the template then
  removes those too. Disabling its schedule keeps the manual-request job.
- `rsync` and OpenSSH on the Oduflow host, and rsync on the source host.
- Add the team's public SSH key (dashboard **Credentials**) to the source
  account's `authorized_keys`. Verify the remote SSH host key independently
  and put it in `{team.data_dir}/ssh/known_hosts`. Sync uses strict host-key
  verification, the team's key,
  and noninteractive authentication. For a non-default port the known_hosts
  entry uses `[host]:port`. Key rotation requires updating the source account.

The source must be a **completed, immutable backup**, not a live PostgreSQL
storage directory:

```text
/backups/completed/
├── dump.pgdump          # or dump.sql / dump.sql.gz / dump.pgdump.gz / db.dump
├── filestore/           # matching Odoo attachments; required, may be empty
└── READY                # unique snapshot ID, e.g. 2026-10-07T030000Z
```

The client is responsible for producing a consistent dump and filestore.
Publish `READY` last, containing 1–256 ASCII letters/digits or `_ . : -`.
Do not change the published directory while it is being fetched. Publish a
new unique ID for every backup. The worker verifies the ID before and after
rsync and in the downloaded copy, then imports from its local inbox. Failed
or changing transfers never reach the live template. Symlinks are not copied.
No remote dump commands are executed by Oduflow.

The worker runs `rsync → import → optional environment refresh`, writes
`{team.data_dir}/template_sync/{template}/status.json` and `run.log`, and
rotates the log at 2 MiB with three retained files. The dialog reads the last
64 KiB and shows timestamps, queued/running/success/partial/failed/interrupted
state, and errors. **Refresh status / log** reads current files. If no worker
has ever started, check the host cron service/log for configuration or CLI
startup errors. Repeated execution of an active job is rejected; scheduled
failures retry on the next scheduled run or a manual request.

The minute tick that picks up manual requests first checks for the queued
request file (`test -e`, through `docker exec` under Docker), so an idle
template costs no Python start. A run is never dropped for being busy: a
scheduled run that finds the template in use (another sync, a dashboard save,
an MCP template operation) is queued for the next minute instead, and a run
whose environments stay busy for five minutes after the transfer is reported
as **deferred** and queued again.

Deployment settings in `oduflow.toml`:

```toml
[template_sync]
cron_dir = "/etc/cron.d"
cron_user = ""          # empty: the user running Oduflow (natively the only one allowed)
cron_container = ""     # native host install; see Docker setup below
```

Natively the worker must run as the same user as the server: everything it
writes (locks, status, pins, imported template files) has to stay readable and
replaceable by the server, so a different `cron_user` is rejected.

The generated command pins the current Python executable and `ODUFLOW_TOML`
path. Re-save schedules after moving the installation/configuration. The
worker can also be run directly with the same saved configuration:

```bash
oduflow sync-template prod --team 1             # enabled schedule
oduflow sync-template prod --team 1 --pending   # consume a queued UI request only
```

### Optional environment data refresh

Use **Environment → More → Auto-refresh data: Off** to opt in. The confirmation
covers subsequent resets: local database and filestore changes are discarded.
Code, container settings, credentials, URL and ports stay in place. Protected
environments are skipped and reported. Stopped environments stay stopped.

Before updating a shared lower filestore, cron pins existing overlay
workspaces to a retained generation. This initial pin briefly stops/remounts
those containers, including ones whose database auto-refresh is off. Subsequent
syncs leave them on their old generation. Copy-mode environments already have
an independent filestore. This keeps old databases and attachments together.

Template refreshes, filestore attaches and imports leave pinned environments
on their retained generation and name them in their result, instead of
remounting them silently. To move a pinned environment onto the current
template, enable its data auto-refresh (the next sync resets it) or recreate
it. Saving an environment as its template unpins that environment: what it
published is now the template's filestore.

An environment cloned from a template that a sync last imported remembers the
snapshot it came from, so a later sync of the same snapshot leaves it alone
even if it opted in after it was created. Any other import into the template
clears that record.

An opted-in environment receives a staged database clone and a fresh
filestore. A separate maintenance container uses its code/configuration to
install configured modules, neutralize Odoo and run team/project sanitization
before the serving container can access the new data. Preparation or
sanitization errors leave the old environment intact. The worker stops the
serving container for the switch, verifies readiness if it was running, and
rolls back both database and filestore on a switch/startup error. Odoo major
versions must match; environments created without a managed filestore mount
must first be recreated from a template that includes one.

Workspace pins survive container updates and restarts. Retained generations
use hardlinks for Odoo's content-addressed attachments; unused generations are
removed after a sync, and when the last environment pinned to one is deleted.
A sync only hardlinks a new generation when an opted-in environment actually
moves to the new snapshot. **Never edit attachment files in place** or use rsync's
`--inplace` against template/inbox/generation trees. Use staged imports. Old
generations consume space for attachments no longer present in the current
snapshot; preparation also needs space for an additional database and any
copy-mode filestore. Busy operations are excluded through shared file locks
under `{data_dir}/.locks`; never delete these files to "unlock" a job.

An OS/process crash during refresh can leave a workspace
`.template-refresh.json` recovery journal. Oduflow blocks start/restart/update/delete
and overlay recovery for that workspace until an operator reconciles the
recorded database names, old/new filestore paths and pin, and restores the
recorded restart policy. In this case it retains recovery files/generations;
do not simply delete the journal. Ordinary caught failures roll back
automatically. A new sync will report the affected environment as failed.

### Cron when Oduflow runs in Docker

Run cron **on the host**. Bind-mount its cron directory into the Oduflow
container, adding `-v /etc/cron.d:/etc/cron.d` to the existing deployment, and
set:

```toml
[template_sync]
cron_dir = "/etc/cron.d"
cron_user = ""              # host user running `docker exec`; empty means root
cron_container = "oduflow"  # actual container name
```

Generated host jobs use `/usr/bin/docker exec` to run the worker in the
existing Oduflow container, sharing its configuration, filesystem, Docker
socket and mount namespace. The container must run as root to write
root-owned cron files. The host needs cron and the Docker CLI at that path;
no cron daemon is started inside the Oduflow server/container. **Do not run a
separate worker container**: overlay mounts must be created in the same mount
namespace as the server. The native installation uses the configured Python
executable directly.

## Listing and Dropping Templates

```bash
# List all template profiles with their status
oduflow list-templates

# Delete a template profile (removes DB + files from disk)
oduflow delete-template myproject
```

## Template Metadata

Each template profile can contain a `metadata.json` file that stores defaults and configuration:

```json
{
  "odoo_image": "odoo:19.0",
  "repo_url": "https://github.com/company/addons.git",
  "extra_addons": {"enterprise": "19.0"},
  "env_vars": {"WORKERS": "2", "LIMIT_TIME_CPU": "600"},
  "use_overlay": true,
  "source_url": "https://my-odoo.example.com",
  "source_db": "production",
  "odoo_version": "19.0+e",
  "pg_version": "15.0"
}
```

When `create_environment` is called with a template name, `repo_url`, `odoo_image`, and `extra_addons` are automatically loaded from metadata if not explicitly provided. This means after importing or configuring a template, you can create environments with just `branch` and `template_name` — all other parameters are inherited.

### Environment variables

`env_vars` holds `{"NAME": "value"}` pairs injected into the Odoo container of every environment created from the template — the same mechanism as the `env_vars` argument of `create_environment`, but recorded once on the template instead of being repeated at each call. Saving a template from a live environment (`save_as_template`) carries that environment's variables over automatically.

The two sets are **merged per key**, with the values passed at creation time winning:

```bash
# Template records WORKERS=2 and LIMIT_TIME_CPU=600
oduflow call create_environment '{
  "branch": "19.0",
  "template_name": "default",
  "env_vars": "WORKERS=8"
}'
# Container gets WORKERS=8 (overridden) and LIMIT_TIME_CPU=600 (inherited)
```

Edit them in the dashboard under **Templates → Settings → Environment variables** (one `KEY=VALUE` per line), or directly in `metadata.json`. Names must be valid shell identifiers (`[A-Za-z_][A-Za-z0-9_]*`); the dashboard rejects anything else on save, and a hand-edited file with an invalid entry is ignored with a warning rather than blocking environment creation.

Variables set here are applied on top of the database connection variables (`HOST`, `USER`, `PASSWORD`) that Oduflow injects itself, so reusing those names overrides the connection settings.

The `use_overlay` flag determines whether new environments use fuse-overlayfs (for large filestores) or a simple copy (for small ones). It is set automatically based on `overlay_threshold_mb` (in `[storage]`) when the template is created.

## Template Decision Matrix

| Scenario | Command |
|---|---|
| New project, no existing database | `oduflow init-template --odoo-image odoo:19.0 --template-name default` |
| Regenerate template from scratch | `oduflow init-template --odoo-image odoo:19.0 --template-name default --force` |
| Named template for a specific project | `oduflow init-template --odoo-image odoo:19.0 --template-name myproject` |
| Have a production dump file | `oduflow import-template /path/to/dump.pgdump --template-name default` (or place it in the template dir and `--refresh`) |
| Need to install modules or configure the template | Create an env, configure it, then `oduflow template-from-env my-branch --template-name default` |
| Update the template from a newer production dump | `oduflow import-template /path/to/new.dump --template-name default --overwrite` |
| Sync template from S3 or a local dir | `oduflow import-template s3://bucket/prod/ --template-name default --overwrite` |
| Save a branch environment as template (keep other envs' changes) | `oduflow template-from-env my-branch --template-name default` |
| Save a branch as template and reset all other envs | `oduflow template-from-env my-branch --template-name default --reset-env-changes` |
| Re-apply a template's filestore to live envs | `oduflow refresh-template default` |
| Attach a separately delivered filestore | `oduflow attach-filestore default /backups/filestore.zip` |
| List all templates | `oduflow list-templates` |
| Delete a template | `oduflow delete-template myproject` |
