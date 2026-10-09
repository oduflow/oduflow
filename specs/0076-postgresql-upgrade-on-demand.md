# PostgreSQL major upgrade on operator request

**Status:** Accepted | **Type:** Upgrade policy | **First introduced:** 2026-10-09

**Key code:** `postgres_migration.py`, `settings.py` (`postgres_image_explicit`),
`server.py` (startup, `oduflow upgrade` preflight), `docker_ops/system_ops.py`
(`require_postgres_for_odoo`), `backup_scheduler.py` (`request_base_backup`)

Supersedes [[0074-postgresql16-cluster-replacement]].

## Context

v1.85.0 introduced PostgreSQL 16 for Odoo 20 as a one-time startup migration
([[0074-postgresql16-cluster-replacement]]). It made the PG16 release itself
the trigger: a PG15 `[database].image` refused to start, and the first start
on PG16 replaced the clusters. It also refused to run while any production or
service database existed. That was not the intended product behaviour. The
operator should decide when a cluster changes major, and production and
service data must survive the change. Development environments are disposable
and do not need to be carried over.

## Decision

Oduflow supports PostgreSQL 15 to 17 and leaves each cluster on its major
until the operator raises `[database].image`. Upgrading is a startup step that
runs on every start (not a one-time registry migration), so it applies
whenever the configured major is newer than a cluster's data. Only an
explicitly configured image triggers it. If `oduflow.toml` has no `image` key
while a cluster holds older data, startup refuses and asks for an explicit
choice, so a new default never upgrades a cluster. Downgrades are refused.

The upgrade carries over service databases and production databases (including
soft-deleted productions whose data is kept), together with every PostgreSQL
role and its password hash. Templates are restored from their dumps on disk,
as in 0074. Environments must still be deleted first, and any database Oduflow
cannot account for refuses the upgrade. The old production WAL-G archive is
still deleted: its base backups cannot restore into a newer major.

## How it works (macro)

Planning is read-only. It checks environments, classifies every database, and
verifies that the target image provides the extensions in use and that there
is room for the dumps. Services and production Odoo containers are then
stopped, and dumps are taken with the target image's client tools, run in the
old cluster's network namespace. Every dump is verified before the first old
cluster is removed. A failure up to that point restores the previous state, and
a restart re-plans from scratch, so setting the image back cancels the upgrade.

After removal, a journal drives creation of the new clusters and the
restoration of roles, then databases, then templates. Restarts resume this
without resetting a new cluster. At the end, production is re-admitted through
the normal provisioning path (networks, pg_hba, verified archiving) before its
Odoo containers start. The backup scheduler is asked for an immediate base
backup, and the dumps are deleted. `oduflow upgrade` runs the same checks
before `self-update` restarts the service. Creating or switching to an Odoo 20
environment or production on a PG15 cluster is refused, with a pointer to the
upgrade.

## Consequences

Installations upgraded from v1.84 stay on PG15 and keep working until the
operator opts in. The upgrade is a planned maintenance window: productions and
services are down while their databases are dumped and restored, development
environments are lost, and PITR history before the upgrade is gone (logical
snapshots remain). The same mechanism covers later majors (16→17). A
half-finished v1.85.0 replacement is taken over, a finished one is ignored, and
migration `0009-postgresql16` remains in the registry as a no-op.

## History

- 2026-10-09: owner rejected the forced replacement of 0074. Requirements:
  manual trigger by changing the image; carry over service and production
  databases; environments deleted beforehand; refuse unknown databases;
  delete dumps after success; restore templates from their on-disk dumps.
