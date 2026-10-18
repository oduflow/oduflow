# 0023 — Import a template from Odoo.sh via a push-based, resumable shell client

**Status:** Adopted (still in force)
**Type:** Architecture / Capability
**First introduced:** `import-from-odoosh` branch (2026-07-02)
**Key code today:** `import_tokens.py` (short-lived upload tokens), `web_ui.py` (`/import-odoo.sh` + `/api/templates/import/*` ingest endpoints, `import-token` mint), `docker_ops/system_ops.py` (`finalize_imported_template`, `extract_filestore_tar`, reusing `reload_template`), `templates/import-odoo.sh` (the client), dashboard "Import from Odoo.sh" button

## Context

Oduflow could already seed a template from a *running* Odoo via
[[0003-database-templates-and-filestore-isolation]]: `import_from_odoo` pulls a
`.zip` from `/web/database/backup` (SQL dump + filestore + manifest). That path
does not work against **Odoo.sh**, which is where many real customer databases
live. Investigation of a live Odoo.sh instance established the constraints:

- The SSH tenant role is deliberately restricted — no `pg_roles`, `pg_settings`,
  or `pg_authid` — so `pg_dump` fails at startup on **every** client version. A
  shell cannot produce its own logical dump.
- The Odoo web workers are not reachable from the SSH container (no local
  `:8069`), and `/web/database/manager` is disabled on the public URL, so the
  existing pull-based importer has nothing to talk to.
- However, on **production** builds the platform writes a ready-made daily
  backup under `~/backup.daily/<db>_daily.{sql.gz,json,/}` — a plain-SQL dump
  (already `psql`-restorable), a manifest, and the filestore tree — created by
  privileged infrastructure. This is the only complete, restorable artifact a
  shell can reach.
- The filestore is ~10 GB while free disk is often less, so no full intermediate
  archive can be staged locally: the transfer must stream.

So the direction of transport has to invert. Oduflow cannot reach *in*; the
Odoo.sh shell must push *out*.

## Decision

Add a **push-based import**: a one-line client script, fetched from the Oduflow
server and run inside the Odoo.sh shell, locates the latest daily backup and
streams it to Oduflow, which lays it out as a template and restores it through
the **existing** template machinery. Authorization is a **short-lived,
single-purpose token** minted by a dashboard button, not the UI password.

## How it works (macro)

- **Token.** The dashboard "Import from Odoo.sh" button calls a UI-authed
  endpoint that mints a random token (15 min TTL) bound to the target template
  name and returns a ready-to-paste command. The token is one JSON file per
  team carrying only auth + the target template; it is deleted on finalize or
  expiry. A stale token left in terminal scrollback is inert.
- **Staging directory + atomic swap.** Uploads land in
  `<team>/import_staging/<template>/`, never in the live template. Each
  filestore chunk is extracted into a temp sibling and renamed into staging
  only once fully unpacked, so a truncated upload can't masquerade as a
  complete chunk. `finalize` promotes the whole staged set into the template
  directory inside the overlay remount guard — re-importing over an existing
  template therefore replaces it with fresh data instead of silently
  "resuming" from the old files, and live envs never see a half-written lower
  layer.
- **Resume is disk-derived, not token-derived.** `status` reports what sits in
  the staging directory (metadata written → manifest done; `dump.sql.gz`
  present → dump done; each hash-dir present → that chunk done). Keeping
  resume state on disk rather than in the token means a fresh token minted for
  the same template — e.g. after the first expired mid-upload — still
  continues from what already landed.
- **Client (`import-odoo.sh`).** Served unauthenticated from `/import-odoo.sh`.
  It reads `$PGDATABASE`, finds `~/backup.daily/<db>_daily.*` (erroring cleanly
  if absent — daily backups exist only on production builds), resolves any
  http→https redirect up front (POST bodies must not rely on
  redirect-following), asks the server what it already has, and uploads only
  the missing pieces. Nothing large hits disk **or RAM**: the filestore is
  `tar`-streamed **per top-level hash directory** (256 dirs + `checklist`)
  via `curl -T` (chunked transfer), not `--data-binary` (which buffers the
  whole payload in memory). It prints an aggregate percentage computed from
  per-chunk sizes.
- **Ingest endpoints.** `/api/templates/import/{status,manifest,dump,filestore,
  finalize}` authenticate by token (a `Bearer` header), so they bypass Basic
  auth — as EXACT public paths, never a prefix, so sibling routes like
  `/api/templates/{name}/delete` (with `name="import"`) stay behind auth. The
  mint endpoint stays behind the UI login. `manifest` writes `metadata.json`
  (Odoo image from `odoo_branch`, module list); `dump` streams `dump.sql.gz`;
  `filestore?chunk=<hash>` unpacks one tar chunk (zip-slip guarded);
  `finalize` swaps staging into the template and runs the restore.
- **Reuse, not reinvention.** By the time `finalize` runs, the template
  directory already holds exactly what `import_from_odoo` produces
  (`dump.sql.gz` + `filestore/` + `metadata.json`), so finalize is the shared
  tail: chown the filestore, remount live overlay envs non-destructively, and
  call the unchanged `reload_template` (which already handles gzip'd plain-SQL
  dumps via `gunzip | psql`).

## Consequences

- **Resumable by construction.** Progress is read from the staged files at
  manifest / dump / per-chunk granularity, so a dropped 10 GB transfer resumes
  where it stopped instead of restarting; re-running the same command — even
  with a freshly minted token — is safe and idempotent.
- **No new dump technology.** The design rides the platform's own backup and
  Oduflow's own restore path; the only new server logic is streaming ingest +
  token auth. The restore tolerates the dump's `\restrict`/`OWNER TO` lines the
  same way it already tolerates external dumps (psql without `ON_ERROR_STOP`).
- **Production-only source.** Daily backups are kept on production builds; on
  staging/dev the client reports "backup not found" rather than inventing one.
  There is no fallback to `_manual`/`_update` backups by design (keep it
  unambiguous).
- **New public surface.** Two path families become reachable without the UI
  password (`/import-odoo.sh`, `/api/templates/import/`); they are gated instead
  by the expiring token, and the mint endpoint stays authed.

## History

- `import-from-odoosh` (2026-07-02) — push-based Odoo.sh import: `import_tokens`
  module, `/import-odoo.sh` client + `/api/templates/import/*` ingest endpoints,
  `finalize_imported_template`/`extract_filestore_tar` reusing `reload_template`,
  and the dashboard "Import from Odoo.sh" button.
- `add-without-filestore-odoo-sh-import` (2026-10-17) — database-only mode:
  `--without-filestore` skips the filestore upload; the dashboard choice is
  also stored on the token, and either one makes finalize leave staged chunks
  unpromoted and keep the live filestore. Resume counts the live filestore only
  when finalize actually promoted one (a separate marker beside `.promoted`), so
  a retry with the full upload never mistakes a kept filestore for its own.
