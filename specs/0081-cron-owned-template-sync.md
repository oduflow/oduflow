# 0081 — Cron-owned template sync and opt-in environment data refresh

**Status:** Adopted
**Type:** Architecture — background data delivery and snapshot isolation
**First introduced:** branch `dull-sheep` (2026-10-07)
**Key code:** `template_sync.py`, `template_generations.py`, `locking.py`, `server.py`, `web_ui.py`

## Context

Clients already produce Odoo backups that can be copied directly into a
managed template and imported ([[0069-unified-template-import]]). Operators
want that workflow in the dashboard and want scheduled retrieval using the
team SSH identity ([[0065-team-ssh-deploy-keys]]). A template refresh currently
updates its database and remounts dependent overlay filestores, but leaves
environment databases unchanged. Deleting old attachments from a shared lower
layer can therefore invalidate an environment that retains its old database.

The operator explicitly chose system cron to own task execution and log
production. The long-running Oduflow process should manage configuration and
expose status, without a scheduler thread or spawning backup/sync workers.

## Decision

Use managed system cron entries to invoke a separate CLI worker. The dashboard
writes configuration, reads bounded logs/status, and queues manual requests
that cron consumes. SSH retrieves a completed backup; producing that backup
remains the client's responsibility. A READY snapshot ID published last is
checked around transfer so a changing source cannot be imported accidentally.

Environment data refresh is explicit opt-in and resets database and filestore
together. Retain immutable filestore generations for environments that do not
advance, using workspace pins independent of container lifetime. Ordinary
manual Refresh remains the existing own-files import operation.

## How it works (macro)

- Transfer into an inbox, then reuse the staged import engine. Cron entries
  are scoped to deployment/team/template; unrelated jobs are never rewritten.
  Under Docker, host cron uses `docker exec` in the existing container so
  workers and server share paths and the overlay mount namespace.
- Shared/exclusive file-lock gates extend the existing resource/team lock
  semantics ([[0015-granular-locking]], [[0050-granular-resource-locks]]) across
  processes. The template key excludes competing jobs; the team gate protects
  publication and dependent environments after transfer.
- Pin existing overlay lowers before publication. Prepare each opted-in
  environment's database and clean filestore separately; neutralize and
  sanitize using its code/configuration in a maintenance container before
  switching the serving container. Preserve code, credentials, configuration
  and running/stopped state. Protected environments are reported and skipped.
- A sync records the snapshot the template now holds; environments cloned
  from it remember it, so re-applying a snapshot never resets data that
  already came from it. Any other import clears the record. Pinned
  environments are reported, never silently skipped, by later template
  updates.
- Natively the cron worker runs as the server's own user; a busy template or
  team defers a run to the next minute tick rather than dropping it.
- Keep the old database and filestore until the switch succeeds. Caught
  failures roll back. A persistent recovery journal fences interrupted
  transitions; retained generations are collected only without recovery
  journals and without workspace references.

## Consequences

System cron and root-owned cron-file access become deployment prerequisites
for this optional feature. Native and Docker deployment require different
command wrappers, but neither adds scheduling work to the web process.

Retaining generations trades disk space for consistency. Content-addressed
attachment files share hardlinks and must never be edited in place; scheduled
rsync uses replacement writes. Refresh preparation needs transient database
space. Initial pinning briefly stops overlay environments, including ones
whose databases are not opted in; later syncs leave their pinned data alone.

Worker failure is observable independently of the web process. Partial runs
report per-environment failures and retry on a later invocation; repeated
successful application of the same snapshot does not reset developer changes.
OS/process crashes in the transition window require operator recovery from
the journal, rather than an unsafe automatic start.

## History

- 2026-09-21 — discussed UI Refresh, SSH retrieval and optional database refresh;
  selected system cron instead of an in-process scheduler.
- 2026-10-07 — implementation authorized; added retained filestores and
  cross-process locking to preserve old environments during automated sync.
