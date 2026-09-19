# CLI Reference

## Global Options

```bash
# Show version
oduflow --version
```

## Running the Server

```bash
# Single-user / stdio mode (default — for local MCP clients)
oduflow
uvx oduflow

# Server / HTTP mode (for remote and multi-user deployments)
oduflow --transport http
oduflow -t http
uvx oduflow --transport http
uvx oduflow -t http
```

Shared infrastructure (Docker network, PostgreSQL, team directories) is initialized automatically on startup.

**stdio mode** — the server communicates over stdin/stdout. The MCP client starts the process directly; no network port is needed. Ideal for local clients like Claude Desktop, Windsurf, etc.

**HTTP mode** — starts a persistent HTTP server on `http://0.0.0.0:8000` by default. Exposes the MCP endpoint at `/mcp`, a Web Dashboard at `/`, and a REST API at `/api/`. MCP uses Bearer tokens; the dashboard uses a form/session cookie, while API clients may also use HTTP Basic auth.

Configuration is loaded from `oduflow.toml` (see [Installation](installation.md#configuration-reference)).
See [Quick Start](quick-start.md) for MCP client configuration examples for both modes.

To reconcile a declarative Stack before starting the server:

```bash
oduflow --stack /path/to/oduflow.yaml --stack-team 1 --transport http
```

Startup stops with a non-zero exit if Stack validation, preflight, or apply
fails. See [Declarative Stacks](stacks.md).

## Declarative Stack Commands

```bash
# Local syntax and schema validation (does not require Docker)
oduflow stack validate oduflow.yaml

# Read-only comparison with live resources
oduflow stack plan oduflow.yaml --team 1

# Reconcile under the team's lock
oduflow stack apply oduflow.yaml --team 1

# JSON status: drift plan plus the last successful apply record
oduflow stack status oduflow.yaml --team 1
```

Stack apply is additive and non-destructive in V1. Existing resources owned by
someone else and environment changes that require replacement are reported as
conflicts; no automatic deletion or pruning is performed.

## System Commands

```bash
# Destroy all shared infrastructure (requires no active environments)
oduflow destroy

# Three-way merge deployed files with the installed bundled versions
oduflow upgrade

# Skip the confirmation prompt and overwrite conflicts with the new bundle
oduflow upgrade --force

# Preview the unified host resource plan and managed config diffs
oduflow retune-postgres

# Back up and write configs; stage production Odoo configs in containers
oduflow retune-postgres --apply
```

`retune-postgres` accounts for `[production].enabled` and does not restart
containers. For existing productions, `--apply` also regenerates `odoo.conf`
with the planned worker count and copies it into the container; the command
then lists every PostgreSQL and Odoo container that should be restarted. It
refuses to replace a custom PostgreSQL config unless `--apply --force` is
given. See [PostgreSQL resource planning](installation.md#configuration-file-overrides).

`oduflow upgrade` reconciles each team's `odoo.conf`, agent guides, and bundled
sanitize script against a stored pristine baseline. It compares complete file
contents, updates an untouched file, preserves local-only changes, and uses
`git merge-file` when both the local and bundled versions changed. Before a live
update, the previous file is saved under
`<team-data>/.bundled_upgrade/backups/`.

On a clean merge the live file and baseline advance together. On conflict the
live file and accepted baseline stay untouched; the merge result is written to
`*.oduflow-merge`. Existing customized installations with no baseline receive
`*.oduflow-new` for a one-time manual reconciliation. Resolve/install the
sidecar and remove it; until then the command exits with status 1.

`--force` makes the command fully non-interactive: it skips the confirmation
prompt and resolves conflicts, legacy files, and merge failures in favour of
the new bundle. The replaced live file is saved under
`<team-data>/.bundled_upgrade/backups/`, the baseline advances, and any stale
sidecar is removed, so a forced run leaves nothing to review and exits 0.
Clean merges still merge, local-only changes are still left untouched, and a
first-line `# KEEP` remains an unconditional opt-out.

This command is separate from upgrading the Python package (for example,
`uv tool upgrade oduflow`). It does not manage `postgresql.conf`; use
`oduflow retune-postgres` for PostgreSQL planning and updates.

## Template Commands

All template commands accept `--team` to specify the team ID (default: `1`).

```bash
# Generate a clean template from a Docker image
oduflow init-template --odoo-image odoo:19.0 --template-name myproject [--modules base,web,sale] [--force] [--team 1]

# Save a branch environment as the new template.
# Other environments on this template keep their filestore changes by default;
# pass --reset-env-changes to discard them and reset to the new baseline.
oduflow template-from-env <branch> --template-name myproject [--reset-env-changes] [--team 1]

# Re-apply a template's current filestore to live overlay environments
# (non-destructive by default; --reset-env-changes discards env deltas)
oduflow refresh-template <template_name> [--reset-env-changes] [--team 1]

# Attach or replace a template filestore from a local dir, archive, rsync://, or SSH rsync source
oduflow attach-filestore <template_name> <source> [--strip-prefix auto|none|PREFIX] [--reset-env-changes] [--team 1]

# List all template profiles
oduflow list-templates [--team 1]

# Delete a template profile
oduflow delete-template <template_name> [--team 1]

# Import a template from a running Odoo instance
oduflow import-template <odoo_url> <master_pwd> --template-name myproject [--db-name <db>] [--without-filestore] [--team 1]

# Import (or incrementally re-sync with --overwrite) from an S3 prefix or a
# local path holding dump.* + filestore/; a single dump file imports DB-only
oduflow import-template s3://bucket/backups/mydb/ --template-name myproject [--overwrite] [--team 1]
oduflow import-template /backups/mydb/ --template-name myproject [--overwrite] [--team 1]

# Reload the template DB from files already placed in the template directory
# (an external rsync/scp drop) and refresh its metadata
oduflow import-template --template-name myproject --refresh [--team 1]
```

`template-from-env`, `refresh-template`, `attach-filestore`, and `import-template` with `--overwrite`/`--refresh` are **non-destructive** for live overlay environments: each is unmounted and remounted against the new template filestore while keeping its `upper` changes. Use `--reset-env-changes` (on `template-from-env`/`refresh-template`/`attach-filestore`) to reset environments to the clean baseline instead. Without `--overwrite`, `import-template` creates a new template and refuses an existing template name.

## Service Commands

```bash
# List all managed services
oduflow list-services [--team 1]

# List persistent PostgreSQL databases for auxiliary services (no passwords)
oduflow list-service-databases [--team 1]
```

Create, inspect, rotate, and delete databases through the matching MCP tools
with `oduflow call`, for example:

```bash
oduflow call create_service_database '{"name":"events"}'
# Or on the dedicated production PostgreSQL cluster (requires production hosting)
oduflow call create_service_database '{"name":"events","cluster":"prod"}'
oduflow call get_service_database '{"name":"events"}'
oduflow call rotate_service_database_password '{"name":"events"}'
oduflow call delete_service_database '{"name":"events"}'
```

## Maintenance Commands

```bash
# Show orphaned databases, workspaces, and port entries (dry-run by default)
oduflow cleanup [--team 1]

# Same as above — only show what would be removed
oduflow cleanup --dry-run [--team 1]

# Actually remove orphaned resources
oduflow cleanup --force [--team 1]
```

The `cleanup` command detects and removes resources that no longer have a corresponding running or stopped container:

- **Orphan databases** — PostgreSQL databases with the `oduflow_` prefix that have no matching environment container
- **Orphan workspaces** — workspace directories on disk that have no matching environment container
- **Orphan port entries** — entries in `ports.json` that have no matching environment container

By default, `cleanup` runs in **dry-run mode** and only reports what would be removed. Use `--force` to actually delete the orphaned resources.

## Systemd Service

```bash
# Install and enable systemd service
oduflow systemd-install

# Remove the systemd service
oduflow systemd-uninstall
```

The `systemd-install` command generates a unit file at `/etc/systemd/system/oduflow.service`, runs `daemon-reload`, and enables the service.

See [Auto-start with systemd](installation.md#auto-start-with-systemd) for the full setup guide.

## Tool Introspection

```bash
# List all registered MCP tools with parameters
oduflow list [--verbose]
```

## Direct Tool Invocation

You can invoke any registered MCP tool directly from the terminal using `oduflow call`, without running the server or connecting an MCP client. This is useful for scripting, debugging, and manual operations.

```bash
# List all available tools with their parameters
oduflow call

# Call a tool with positional arguments (mapped to parameters in order)
oduflow call create_environment dev "" "" https://github.com/owner/repo.git odoo:19.0
oduflow call delete_environment dev
oduflow call list_environments
oduflow call get_environment_logs main 50
oduflow call run_odoo_command dev "ls /mnt/extra-addons"
oduflow call create_service redis redis:7 6379

# Call a tool with JSON-encoded arguments
oduflow call create_environment '{"branch":"dev","repo_url":"https://github.com/owner/repo.git","odoo_image":"odoo:19.0","template_name":"myproject"}'

# Service with NET_ADMIN capability (VPN / tun / iptables)
oduflow call create_service '{"name":"vpn","image":"linuxserver/wireguard","port":51820,"net_admin":true}'

# Type coercion is automatic: int, bool, and float parameters are cast from strings
oduflow call get_environment_logs dev 500
```

## Remote Tool Invocation

`oduflow client` calls the same registered tools on a running Oduflow HTTP
server through FastMCP. Unlike `oduflow call`, it does not load the local
`oduflow.toml`, access local Docker, or execute server functions in the client
process.

Configure the exact full or scoped MCP endpoint and its Bearer credential:

```bash
export ODUFLOW_MCP_URL="https://oduflow.example.com/mcp"
export ODUFLOW_MCP_TOKEN="<team-auth-token>"

# Tools advertised by this endpoint
oduflow client list
oduflow client list --verbose

# Read live help generated from a tool's input schema
oduflow client create_environment --help

# Call team-wide tools
oduflow client list_environments
oduflow client create_environment \
  --repo-url https://github.com/owner/addons.git \
  --odoo-image odoo:19.0 \
  --template-name myproject
```

The client reads the live `tools/list` response, converts kebab-case flags to
MCP parameter names, validates basic scalar/JSON types, and then calls the tool.
A single JSON object is also accepted for complex arguments:

```bash
oduflow client create_service '{
  "name": "redis",
  "image": "redis:7",
  "port": 6379
}'
```

Client options must appear before the tool name:

```bash
oduflow client --timeout 1200 --json list_environments
oduflow client --env demo pull_and_apply --upgrade sale_custom --strict
```

When a remote schema requires `env_name`, the client uses `--env`, then
`ODUFLOW_ENV_NAME`, then the current Git branch. When `create_environment`
requires `branch`, it uses the current Git branch unless `--branch` is supplied;
other tools that take a required branch, such as `create_production`, always
need it spelled out.
An explicit environment override is useful when an environment name differs
from its source branch.

For confined development access, use the environment's scoped endpoint and
Secret Key from **More → MCP Access**:

```bash
export ODUFLOW_MCP_URL="https://oduflow.example.com/mcp/feature-x"
export ODUFLOW_MCP_TOKEN="<environment-secret-key>"

# env_name is absent from the scoped schema and injected by the server
oduflow client get_environment_info
oduflow client pull_and_apply --upgrade sale_custom --strict
oduflow client run_odoo_tests --modules sale_custom
```

The scoped server advertises only its allowlisted single-environment tools, so
team-wide commands such as `list_environments` and `create_environment` are not
available. The client does not maintain a second authorization list; the live
server schema remains authoritative.

For scripts and CI, `--json` emits the complete MCP result and tool failures
return a non-zero exit code. To avoid storing a token in the environment, pass
it on standard input:

```bash
printf '%s\n' "$TOKEN_FROM_SECRET_STORE" | \
  oduflow client --token-stdin --json list_environments
```

In summary, `oduflow call <tool>` is local in-process execution, while
`oduflow client <tool>` is remote authenticated MCP execution.
