# Operator-only cluster disaster recovery

**Status:** Accepted | **Type:** Security / tenancy / operator surface | **First introduced:** 2026-10-18

**Key code:** `backup_ops.py` (`restore_cluster_pitr` orchestration),
`admin_ui.py` (`/admin/api/wal`, `/admin/api/wal/control`, `/admin/api/pitr`),
`templates/admin.html` (Cluster DR section), `server.py`
(`oduflow restore-cluster`, `oduflow wal`), `production_access.py`

## Context

Two production capabilities act on the shared PostgreSQL cluster — that is, on
**every team's productions at once**: whole-cluster PITR (stop all
productions, rewrite the cluster from WAL-G, restart) and WAL archiving
control (pause/resume/retry, and the recover/release steps of disk-protection
recovery). Both were exposed as MCP tools (`restore_cluster_pitr`,
`control_production_wal`) and WAL control also as a team-dashboard REST
endpoint, gated only by a typed confirmation string (`RESTORE-CLUSTER` /
`ALL-PRODUCTIONS`) plus ordinary team credentials.

Under hard tenant isolation ([[0027-hard-tenant-isolation]]) that is a scope
violation: any one team's agent or dashboard session could rewind or pause
every other team's production. A partial upstream mitigation rejected the two
MCP tools on multi-team servers, but the asymmetry remained — the string
confirmation protects against *accidents*, not against the wrong *principal* —
and coding agents have no business driving disaster recovery at all.

## Decision

Cluster-scoped mutations are **operator actions, not team actions**. They move
to the two surfaces the operator already owns:

- the **server settings console** (`/admin`, [[0077-server-settings-console]])
  gets a *Cluster DR* section: WAL monitor state, base-backup inventory,
  WAL control actions and a cluster PITR form, each behind the console
  session plus the existing typed confirmation;
- the **owner CLI** gets `oduflow wal <action>` and
  `oduflow restore-cluster`, preserving the headless path — in particular
  "resurrect the cluster on a fresh install from S3 alone", which must not
  depend on a configured console password.

The MCP tools and the team-dashboard WAL-control endpoint are **removed**
(not deprecated): read-only cluster status stays on both team surfaces
(`production_wal_status`, `GET /api/productions/wal-status`), mutation does
not. The PITR orchestration (lock, pre-flight, stop loop,
restart-on-failure — see the P-H11 hardening) moved from the MCP layer into
`backup_ops.restore_cluster_pitr(settings, locks, …)` so the console and the
CLI share one implementation.

## How it works (macro)

Both actions take the system lock, which excludes the whole `prod:` keyspace.
`/admin` endpoints are guarded by the console session (separate password and
cookie, same-origin POSTs only); a busy cluster maps to HTTP 409. The CLI
calls the same shared functions directly with the process-local lock manager —
the same semantics the stdio MCP path always had. Per-team backup tools
(snapshot/restore/schedule/prune/status) are unaffected and stay on MCP.

## Consequences

- A team credential — MCP token, production token, or dashboard session — can
  no longer affect another team's productions; the cross-tenant surface is
  gone rather than confirmation-gated.
- Agents lose direct DR access by design; runbooks point at `/admin` or the
  CLI instead of tool calls.
- The console, previously a pure config editor, now owns its first
  infrastructure actions; anything else cluster-scoped should land there too.

## History

- Introduced as the fix for review finding P-M2 (code-review/oduflow branch).
