# 0066 — Production WAL monitoring and disk protection

**Status:** Adopted
**Type:** Architecture / Operations
**First introduced:** 2026-09-19
**Key code:** `wal_monitor.py`, `walg.py`, production lifecycle, dashboard

## Context

A production cluster accumulated 1,986 unarchived WAL segments while WAL-G
could not verify the storage service's TLS certificate. PostgreSQL exhausted
its filesystem and repeatedly restarted. The main databases were small; WAL
dominated usage. Neither `max_wal_size` nor team development tablespace quotas
limit unarchived production WAL. A successful S3 probe from Oduflow also did
not validate the trust store inside PostgreSQL.

The operator requested detection, visible diagnostics, archive controls, and
automatic protection against disk exhaustion. The chosen trade-off preserves
WAL and recovery continuity, accepting production downtime instead of silently
acknowledging or deleting unsaved segments. This extends production hosting
from [[0035-production-hosting]].

## Decision

Run a dedicated local monitor independently of backup scheduling and HTTP.
Detect archive stalls from a pending queue and absence of progress, and disk
risk from non-root available space and currently observed consumption
confirmed across consecutive samples. Protect the
whole production cluster because WAL and its filesystem are shared.

Protection is a persistent latch, not a transient health warning. It disables
Docker restarts and stops applications before PostgreSQL. Ordinary lifecycle
operations cannot clear it. Recovery starts PostgreSQL alone with network
database connections fenced, verifies actual new WAL archiving, and requires
more headroom than the stop threshold before releasing protection. Applications
are restarted explicitly by the operator.

## How it works

A mounted CA bundle supplies WAL-G trust consistently for archives, backups,
and restore helpers. An archive wrapper bounds attempts and records their
outcomes without reporting success on cancellation or timeout. Pause preserves
the queue; retry targets only an active WAL upload.

Production admission requires an in-container WAL-G storage read/write
preflight and confirmation that a newly generated WAL segment was archived.
Failure blocks the start/deploy without acknowledging retained WAL. For a
production that is already admitted and running, a current sample proving
healthy archiving is accepted in place of that forced segment: keeping a hung
production restartable outweighs re-proving delivery on every restart, and the
evidence is live, never a cached verdict. A dedicated
PostgreSQL 15 image includes CA certificates and pins its Debian base digest;
it is published under immutable multi-architecture tags from main before the
application release. Existing containers receive trust through the persistent
config mount, avoiding a risky container replacement during disk exhaustion.

The monitor samples the actual WAL filesystem, including through a read-only
volume helper while PostgreSQL is down. Cached diagnostics drive MCP, REST,
the Production panel, and health checks. Stale observations are errors. The
local safety decision precedes any remote storage probe.
An absolute queue-size limit protects even large disks; fenced recovery may
exceed that limit while draining, but release requires a smaller queue.

The latch reserves file space before an incident and fails closed if unreadable.
Emergency shutdown does not wait for long-running production operation locks.
Recovery temporarily fences PostgreSQL through its managed HBA file, keeping
local administration and S3 access working while blocking other writers.
Cluster controls use the existing full-team authentication and explicit
cluster confirmation model of PITR, excluding environment-scoped access.

## Consequences

Disk pressure can intentionally take every production offline. Operators must
size reserve/headroom for peak writes and shutdown latency, and monitor Oduflow
externally: an unresponsive daemon or abrupt exhaustion can still defeat a
periodic guard. In-flight deployments/backups may be interrupted. Other host
writers and replication-slot retention remain separate operational concerns.
Custom PostgreSQL HBA paths need operator intervention during recovery.

## History

- 2026-09-19: Adopted after the WAL-G CA failure and explicit approval of
  automatic protective shutdown in the production incident discussion.
