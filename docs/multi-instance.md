# Multi-Team Support

Oduflow supports running **multiple isolated teams** within a single server instance. Each team has its own environments, templates, services, credentials, port registry, Docker network, and PostgreSQL tablespace; the PostgreSQL and Traefik containers are the only shared infrastructure.

## Configuration

Define teams in `oduflow.toml` using `[team.*]` sections:

```toml
[team.1]
hostname = "team-a.example.com"
auth_token = "token-team-a"
ui_password = "pass-a"
port_range = [50000, 50050]

[team.2]
hostname = "team-b.example.com"
auth_token = "token-team-b"
ui_password = "pass-b"
port_range = [50050, 50100]
```

Every team must declare a unique `hostname`. Besides routing requests, that
hostname is the team's OAuth issuer identity when reached through a
TLS-terminating proxy such as Traefik or Cloudflare Tunnel.

Each team gets a dedicated data directory under the base `data_dir`:

```
/srv/oduflow/
├── team_1/
│   ├── workspaces/
│   ├── templates/
│   ├── shared_repos/
│   ├── ports.json
│   ├── .git-credentials
│   └── agent_guides/
├── team_2/
│   ├── workspaces/
│   ├── templates/
│   ├── shared_repos/
│   ├── ports.json
│   ├── .git-credentials
│   └── agent_guides/
```

## Team Resolution

When an MCP tool is called, Oduflow resolves the team using the following priority:

1. **Auth token** — matches the Bearer token against `auth_token` values in team configs
2. **Host header** — matches the HTTP `Host` header against team `hostname` values
3. **Single team** — if only one team is configured, uses it automatically
4. **Default** — falls back to team `"1"`

Steps 3–4 apply to the stdio transport (implicit local single user) only. In
HTTP mode a request that matches no token and no hostname is rejected, so it
can never land in another team's context — unless `allow_insecure_http = true`
explicitly opts out (e.g. behind your own auth proxy). HTTP mode with multiple
teams also requires a non-empty `auth_token` for every team at startup.

## Quotas

Each team can carry resource quotas (`0` disables a quota):

```toml
[team.1]
db_quota_gb = 50      # default: 50
disk_quota_gb = 0     # default: 0 (off)
```

- `db_quota_gb` caps the combined size of the team's PostgreSQL databases —
  environments plus templates. It is checked before operations that create a
  *new* database (`create_environment`, `save_as_template` of a new template,
  `import_template`) with a single catalog query
  (`pg_database_size()`), so there is no per-file scanning in the hot path.
  Replacement operations (refresh/reload of an existing template) are not
  gated, so a team at its quota can still shrink or refresh what it has.
- `disk_quota_gb` caps the team's disk usage — its data dir (workspaces,
  filestores, template dumps) **plus** its PostgreSQL tablespace — enforced
  by the kernel via XFS project quotas. Requirements: Linux, `xfsprogs`
  installed, and the data dir on an XFS filesystem mounted with `prjquota`.
  Both directory trees get the same project ID, so one `bhard` limit covers
  files and databases together; writes beyond it fail with ENOSPC while the
  rest of the machine is unaffected. On filesystems without project-quota
  support the limit is not enforced (one warning at startup) and usage stays
  visible via the dashboard and `/api/usage`.

## Per-Team PostgreSQL Tablespaces

Each team's databases (environments and templates) live in a dedicated
PostgreSQL tablespace, `oduflow_team_{id}`, whose files sit under
`{data_dir}/pg_tablespaces/team_{id}/` on the host. Only that
`pg_tablespaces/` directory is mounted into the PostgreSQL container — never
the rest of the data dir.

This makes a team's disk consumption one visible number: assign
`team_{id}/` and `pg_tablespaces/team_{id}/` the same XFS project ID and a
single project quota covers the team's files *and* its databases. WAL stays
in the shared `PGDATA`, so a team hitting its quota gets aborted
transactions, not a server-wide outage.

Existing installs are converted automatically on server start (startup
migration `0002-team-pg-tablespaces`): the PostgreSQL container is recreated
once with the new mount (its data volume persists), then each team database
is physically moved with `ALTER DATABASE ... SET TABLESPACE`. Expect the
first start after the upgrade to take time proportional to the total
database size.

A second base-level directory, `{data_dir}/pg_exchange/`, is mounted the same
way (as `/exchange`). Database dumps are staged in `pg_exchange/team_{id}/`
so `pg_dump` writes them once, straight to their final filesystem, and a
restore reads them in place instead of having a full-size copy pushed into
the PostgreSQL container's writable layer. Give it the **same XFS project ID**
as the rest of the team: besides keeping the accounting right, XFS refuses to
rename a file into a project-inheriting directory with a different ID, which
would break moving a finished dump into the team's templates directory.

Unlike the tablespace change, this one is not migrated. The mount is attached
when the PostgreSQL container is created, and an existing container is left
alone; installs without it keep streaming dumps out through the Docker exec
API and pick up the faster path whenever that container is next recreated.

## Shared vs. Per-Team Resources

| Resource | Scope |
|---|---|
| Infra Docker network (`oduflow-net`) | Shared (PostgreSQL, Traefik) |
| Team Docker network (`oduflow-{team}-net`) | Per-team — env/service containers join only their team's network; shared infra is attached to every team network |
| PostgreSQL container (`oduflow-db`) | Shared |
| PostgreSQL tablespace (`oduflow_team_{id}`) | Per-team |
| Traefik container (`oduflow-traefik`) | Shared |
| Environments (workspaces, containers) | Per-team |
| Templates (DB snapshots, filestores) | Per-team |
| Extra addon repositories | Per-team |
| Auxiliary services | Per-team |
| Auxiliary-service PostgreSQL databases and roles | Per-team — stored in the team's tablespace |
| Port assignments | Per-team |
| Git credentials | Per-team |

## Resource Naming

Databases and containers are namespaced by team ID:

- Environment DB: `oduflow_{team_id}_{slugified_branch}` (e.g. `oduflow_1_feature-login`)
- Template DB: `oduflow_template_{team_id}_{template_name}` (e.g. `oduflow_template_1_default`)
- Service DB: `oduflow_service_{team_id}_{name}` (e.g. `oduflow_service_1_events`)
- Service DB role: `svc_{team_id}_{name}` (e.g. `svc_1_events`)

Service database identifiers carry a `.<digest>` suffix whenever the readable
form would be ambiguous — a team id containing `_` or uppercase, or one that
would overflow PostgreSQL's 63-byte identifier limit. The suffix is derived
from the exact team/database pair, so two teams can never end up sharing a
database or a role.
- Environment containers: `oduflow-{team_id}-{env}-{type}` (e.g. `oduflow-1-feature-login-odoo`)
- Service containers: `oduflow-{team_id}-svc-{name}` (e.g. `oduflow-1-svc-redis`)

Containers are additionally labeled with `oduflow.team={team_id}`; listing and
filtering are label-based, and container names are team-scoped so two teams
can use the same branch name without colliding. Containers created by older
versions are renamed to this scheme automatically on server start (startup
migration `0001-team-scoped-container-names`).

## CLI Team Selection

CLI template and service commands accept a `--team` flag:

```bash
oduflow init-template --odoo-image odoo:19.0 --template-name myproject --team 2
oduflow list-templates --team 2
oduflow list-service-databases --team 2
oduflow cleanup --team 2
```

The default is `--team 1`.
