# 0067 — Unified template import (S3 prefix, local path, in-place refresh)

**Status:** Adopted (v2 — unified)
**Type:** Architecture — new capability + consolidation
**First introduced:** branch `slim-stingray` (2026-09-19)
**Key code today:** `template_import.py` (source clients, listing, diff/plan, parallel fetch, staged promote, refresh); `docker_ops/system_ops.py` (`import_template` dispatcher, HTTP db-manager path); `server.py` (`import_template` tool + `import-template` CLI); `web_ui.py` (`/api/templates/import-from-odoo` passthrough)

## Context

Every existing door into a template assumed the source could package itself: a
running Odoo builds a `.zip` via the database manager
([[0003-database-templates-and-filestore-isolation]]), Odoo.sh streams tars
through a push client ([[0023-import-from-odoo-sh]]), a production is dumped
live ([[0059-production-to-dev-data-flow]]). For the remaining case — *a big
database whose owner can only hand us files* — packaging is exactly the
problem: zipping a multi-hundred-GB filestore takes hours and double disk on
the source, the archive cannot be resumed, and a periodic refresh re-ships
everything. Meanwhile the artifacts already exist in their natural form: a
`pg_dump` and a directory tree, and every hosting shop can `aws s3 sync` (or
plain rsync) them somewhere reachable.

Two half-answers had accreted for that case and overlapped ambiguously:
`import_template_from_odoo` (HTTP pull only) and a CLI-only `reload-template
--source` that shelled out to `aws s3 sync`/`rsync` **into the live template
directory** — environments stayed unmounted for the whole download, a crash
left the template half-updated, metadata (sizes, version, overlay mode) went
stale, and it always re-restored the DB.

## Decision

**One import door.** A single `import_template` tool/CLI/REST endpoint,
dispatched on the shape of `source`: http(s) → the db-manager pull;
`s3://bucket/prefix` → raw-layout sync; a local directory or single dump file
→ the same engine via hardlinks; no source + `refresh=true` → reload the
template from files already dropped into its directory. The raw source format
is defined as exactly one `dump.pgdump`/`dump.sql[.gz]` at the root plus an
optional one-to-one `filestore/` tree. The old tool name and `reload-template`
were **removed outright** (small user base, docs over compatibility shims), the
`aws s3 sync` shell-out (`sync.py`) deleted.

Rejected alternatives: a ZIP in S3 (inherits every packaging cost the feature
exists to remove); a separate tool per source (two doors for one act); keeping
`reload-template` as an alias (ambiguity was the disease being treated).

## How it works (macro)

- **Staging first, promote by rename.** Sources materialize into the Odoo.sh
  import's staging area — S3 objects in parallel, local files as hardlinks —
  never into the live template. The promote is a handful of renames under
  `remount_template_overlays`, so live environments keep their upper-layer
  deltas and a crashed import never masquerades as a template. Resume is
  free: a staged file with the right size is skipped.
- **`overwrite=true` is an incremental re-sync, not a re-import.** The live
  template filestore is hardlinked into staging for unchanged files (an
  `rsync --link-dest` analogue — atomic swap *and* incrementality), files
  deleted at the source drop out of the swapped tree, and a dump whose
  identity token (S3 ETag, or local size+mtime) matches the last sync skips
  both the fetch and the template-DB reload. This makes "pull last night's
  production backup into dev" a cheap cron-able operation.
- **`refresh` blesses the drop-point workflow.** An external process may
  rsync straight into the template directory; `refresh` reloads the DB from
  the dump found there, re-reads version/modules from the restored database,
  fixes filestore ownership, recomputes sizes/overlay mode, and remounts live
  overlays — everything the old `reload-template` silently left stale.
- **S3 credential resolution, most explicit wins:** `s3_*` tool parameters →
  the `[backup]` settings when the bucket matches → anonymous (explicitly
  UNSIGNED, so ambient host credentials are never silently used). A custom
  endpoint passes the same SSRF gate as HTTP imports.
- **Metadata from the restored database.** There is no manifest in the raw
  layout; version, image and module list are read from the restored template
  DB (the fallback the DB-only HTTP import already used, now general).
  `snapshot_at` records the dump's upload/modification time — freshness
  describes the data, not the import.

## Consequences

- Template data now enters through exactly one reviewable door across MCP,
  CLI and the dashboard API; the AWS-CLI/rsync host dependencies are gone.
- Breaking rename (`import_template_from_odoo` → `import_template`, first
  argument `odoo_url` → `source`) and removal of `reload-template` — accepted
  deliberately: few users, documented migration, no alias left to keep the
  ambiguity alive. The REST route keeps its `/import-from-odoo` path and the
  legacy `odoo_url` body field so the dashboard form needed no change.
- The size-only file comparison leans on Odoo filestore paths being content
  hashes; a non-filestore tree with same-size edits would not re-fetch.
  Accepted for the register this feature serves.
- `overwrite`/`refresh` replace template data by design and skip the
  fresh-import quota gate (like refresh/reload before them); the tool
  docstring demands explicit user permission before using either.

## History

- 2026-09-19 — v1: S3-prefix source added to `import_template_from_odoo`
  (branch `slim-stingray`).
- 2026-09-19 — v2: unified into `import_template` (local path + refresh
  sources); `reload-template` and `sync.py` removed (same branch).
