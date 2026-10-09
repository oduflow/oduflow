# PostgreSQL 16 cluster replacement

**Status:** Superseded by [[0076-postgresql-upgrade-on-demand]] | **Type:** Upgrade policy | **First introduced:** 2026-09-29

**Key code:** `postgres_migration.py`, `migrations.py`, `settings.py`,
`docker_ops/system_ops.py`, `docker_ops/env_ops.py`, `walg.py`, `s3_client.py`

## Context

Odoo 20 requires PostgreSQL 16 or newer, while existing Oduflow installations
use PostgreSQL 15. Customers currently use development environments; there are
no production customers whose databases must be migrated. The owner accepted
discarding development environments as an explicit preparation step. Templates
are reusable assets: their dumps, filestores and metadata already exist on disk.

The shared development cluster also hosts independent service databases, and
production can retain databases after an environment is deleted. Neither kind
of data may disappear merely because the environment list looks empty.

## Decision

Make PostgreSQL 16 the standard image for new installations and add a one-time
replacement of standard PG15 clusters through [[0025-startup-data-migrations]].
This is not an in-place PostgreSQL upgrade or a general database migration tool.

Operators delete development environments through the normal lifecycle and
manually update existing image selections in `oduflow.toml` before upgrading.
An old image selection refuses startup with a corrective message. Remaining
environments, productions, service databases or unaccounted database contents
prevent the destructive step; `oduflow upgrade` reports them before a restart,
so the old server keeps running while they are removed. Custom old clusters
need operator handling; clusters already on PostgreSQL 16 or 17 are retained
and must keep their major version. PostgreSQL 18 images are unsupported: they
refuse the data-volume layout Oduflow mounts.

Restore template databases from the existing on-disk dumps while retaining
their filestores and metadata. A selected template without a restored database
must fail environment creation; it must never silently produce an empty Odoo.

## How it works

Before removing either old cluster, verify the retained template files and
download and verify all replacement images. Both clusters default to the
official PostgreSQL image and share one image setting, `[database].image`.
The removed production-specific setting raises a configuration error. WAL-G uses the existing
mounted CA bundle; a separate PostgreSQL image build is unnecessary.
A durable phase journal separates removal of PG15 from
creation of PG16 and records each completed template restore. Retrying after a
crash resumes the unfinished work without resetting the replacement cluster.

Remove the old container, its named data volume, and the PG15 development
tablespace files ([[0026-per-team-pg-tablespaces]]). With backups configured,
also delete the old production cluster's WAL-G archive: its PG15 base backups
cannot restore into PG16, and the new cluster's timeline-1 segments would
otherwise mix with the old ones. Keep team configuration, templates, networks
and unrelated services. The startup migration completes only after PostgreSQL
is ready and template restoration succeeds. A template deleted by the operator
after a failed restore is skipped on retry. Restores log progress from live
PostgreSQL activity, so the startup watchdog still detects a wedged Docker
daemon. Old image cleanup never forces deletion of an image still used by
another container.

## Consequences

Upgrades require a planned interruption: back up, stop everything, upgrade and
start again. Customer code must be saved before environments are deleted, and
the previous point-in-time recovery history is discarded. Development data is
intentionally not transferred.
Template dump failures prevent startup and can be repaired and retried without
another cluster reset. Future populated production upgrades require a different,
data-preserving migration; this one must refuse them. Package releases no longer
depend on building or publishing a separate production PostgreSQL image.

## History

- 2026-09-29: owner approved disposable development clusters, empty-production
  replacement, manual configuration changes and restoration of saved templates;
  required an explicit error when a selected template database is missing.
- 2026-09-29: owner selected the official PostgreSQL image for both clusters;
  the existing CA mount makes the custom image and its publication workflow redundant.
- 2026-09-29: owner removed the separate production image option; both clusters
  and recovery operations use `[database].image`.
- 2026-09-30: owner chose to delete the old WAL-G archive during the replacement
  instead of preserving PG15 recovery history; operators back up beforehand.
- 2026-09-30: owner approved PostgreSQL 16–17 as the supported range, a
  preflight in `oduflow upgrade`, and skipping templates deleted after a failed
  restore.
