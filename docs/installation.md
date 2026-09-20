# Installation

## System Requirements

- **Docker** (Docker Engine or Docker Desktop)
- **Python 3.10+**
- **Git**
- **fuse-overlayfs** (Linux only, for filestore overlay mounting) — auto-installed on first launch; see below
- **rsync** (all platforms, for incremental filestore copies) — auto-installed on first launch; see below

!!! note "macOS support"
    On macOS, Docker Desktop runs containers inside a Linux VM and projects
    files via VirtioFS. **fuse-overlayfs is not needed** — filestore overlays
    are skipped and a plain directory is used instead.
    File ownership (`chown`) is handled automatically: Oduflow detects the
    `PermissionError` that VirtioFS raises and falls back to running `chown`
    inside a throwaway container. No extra configuration is required.

### Install fuse-overlayfs

On Linux, Oduflow **auto-installs `fuse-overlayfs` on first launch** if it is
missing — it runs `apt-get install -y fuse-overlayfs` when it starts as **root**
on a Debian/Ubuntu host (the Docker image already bundles it). This is
best-effort: if Oduflow is not running as root, `apt-get` is unavailable, or the
install fails, it logs a warning and you can install the package yourself:

```bash
sudo apt install fuse-overlayfs
```

The `/dev/fuse` device must be available (present by default on Ubuntu).

Oduflow mounts each environment's filestore with `fuse-overlayfs`'s `allow_other` option so the Odoo container's (non-root) user can read it. When Oduflow runs as **root** — the default and recommended setup — no further configuration is needed. Only if you run Oduflow as a **non-root user** must you uncomment `user_allow_other` in `/etc/fuse.conf`:

```bash
# Only needed when running Oduflow as a non-root user:
sudo sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
```

### Install rsync

`rsync` is auto-installed the same way on Linux (`apt-get install -y rsync` when
running as root on a Debian/Ubuntu host; the Docker image bundles it). Unlike
fuse-overlayfs it matters on **every** platform, including macOS, where it ships
with the system:

```bash
sudo apt install rsync
```

Oduflow uses it to copy only what changed. Saving an environment as a template
snapshots its filestore by hardlinking every file that already matches the
template baseline, so a multi-gigabyte filestore costs only the environment's
own deltas. Without `rsync`, publishing still works but re-copies the whole
filestore each time (logged as a warning), and syncing a template from a local
source fails outright.

## Install Oduflow

### Run without installing

With [uv](https://docs.astral.sh/uv/) you can run Oduflow directly — no installation step needed. `uvx` downloads the package into a temporary environment and runs it:

```bash
uvx oduflow                      # stdio mode (default)
uvx oduflow --transport http     # HTTP server mode
uvx oduflow -t http              # HTTP server mode (short form)
```

This is the quickest way to try Oduflow or use it in CI pipelines.

### Permanent installation

Install via [uv](https://docs.astral.sh/uv/) (recommended — manages an isolated environment automatically):

```bash
uv tool install oduflow
```

Alternative — install via pip:

```bash
pip install oduflow
```

After installation, the `oduflow` command is available globally.

### From source

```bash
git clone https://github.com/oduflow/oduflow.git
cd oduflow
uv sync          # or: python -m venv .venv && pip install -e .
```

### Upgrade

Click the version next to **Oduflow** in the dashboard header to see whether a
newer release exists. The dialog compares the installed version with the latest
published release, names it, and links to its release notes. The check runs only
on that click — Oduflow never polls GitHub on its own.

The one-command path chains everything below and restarts the service:

```bash
oduflow self-update
```

It detects how Oduflow was installed and uses that installer (`uv tool upgrade`
or `pip install --upgrade`), then reconciles bundled files and restarts the
systemd service. It exits with an error inside a Docker container — a package
upgraded inside the container would revert on the next recreate; pull the new
image and recreate the container instead (see [Docker](docker.md)). Source
checkouts, editable installs, and `uvx` runs are likewise refused with an
explanation. See [CLI reference](cli.md#system-commands) for details.

The manual steps, equivalent to what `self-update` runs:

```bash
uv tool upgrade oduflow
oduflow upgrade
# For unattended automation:
oduflow upgrade --force
```

The first command upgrades the Python package. The second is a separate,
interactive reconciliation of each team's deployed `odoo.conf`, agent guides,
and bundled sanitize script. Package upgrade alone does not update those
deployed copies. `postgresql.conf` is intentionally separate: preview and apply
resource-tuning changes with `oduflow retune-postgres [--apply]`.

Oduflow keeps the previous pristine bundle under
`<team-data>/.bundled_upgrade/baselines/` and performs a three-way merge. An
unmodified deployed file receives the new bundle directly; local-only changes
stay untouched; disjoint local and upstream changes are merged. The pre-update
live file is retained under `.bundled_upgrade/backups/`.

For an installation created before baselines existed, the first upgrade keeps
the live file and writes the new bundle beside it as `*.oduflow-new`. Merge that
file manually into the live file, then delete the sidecar. A true merge conflict
similarly leaves the live file untouched and writes `*.oduflow-merge`; resolve
that file, install the resolved content as the live file, and remove the
sidecar. Until the sidecar is resolved, `oduflow upgrade` exits non-zero.

For unattended upgrades, pass `--force`. It skips the stdin confirmation and
resolves legacy files, conflicts, and merge failures in favour of the new
bundle: the live file is copied to `.bundled_upgrade/backups/`, then
overwritten, the baseline advances, and any stale sidecar is removed. A forced
run therefore leaves no sidecar to review and exits 0. Clean merges are still
merged and local-only changes are still preserved.

Automatic merging is the default. To opt a file out of all bundled changes,
add `# KEEP` as the **very first line**:

```conf
# KEEP
[options]
# Keep this odoo.conf entirely operator-managed.
...
```

Files marked with `# KEEP` are skipped and listed as `(kept)` in the upgrade
output, including under `--force`.

## Configuration Reference

All settings are configured via a TOML file. Oduflow searches for `oduflow.toml` in the following order:

1. `ODUFLOW_TOML` environment variable (explicit path)
2. `/etc/oduflow/oduflow.toml`
3. `~/.oduflow/conf/oduflow.toml`

If no config file exists when Oduflow starts, the bundled default is copied to
`/etc/oduflow/oduflow.toml` when that directory is writable, otherwise to
`~/.oduflow/conf/oduflow.toml`. The copied file is populated with generated
values for `[database].password`, `[team.1].auth_token`, and
`[team.1].ui_password`; the generated MCP token and Web Dashboard password are
also printed in the startup log.

### Minimal configuration

```toml
[team.1]
hostname = "localhost"
```

### Full configuration reference

```toml
# ── Server ────────────────────────────────────────────
[server]
bind = "0.0.0.0"           # HTTP listener address; legacy "host" is accepted
port = 8000                 # HTTP server port
allow_local_path = true     # trusted single-user local development; disable on hosted/multi-user servers
# allow_insecure_http = false  # serve /mcp over HTTP with NO auth (only behind your own proxy)
# trace = false             # verbose tracing for git analysis & env ops
# disable_telemetry = false # disable anonymous first_run/env_created events

# ── Routing ───────────────────────────────────────────
[routing]
mode = "port"               # "port" (direct host port) | "traefik" (reverse proxy with auto-HTTPS)
# acme_email = "admin@example.com"  # required when mode = "traefik" and tls = true
# tls = true                # traefik only. {} = HTTPS without ACME (self-signed by default); false = plain HTTP on :80, no ACME (behind a Cloudflare tunnel / TLS proxy)
# public_scheme = "https"   # scheme of the URLs Oduflow hands out. Default: https (traefik) / http (port).
                            # Set "http" with tls = false when nothing terminates TLS in front.
                            # Overridable per team ([team.X] public_scheme) for mixed deployments

# ── Extra routes (Traefik only) ───────────────────────
# [route.legacy-api]
# host = "api.example.com"
# url = "http://127.0.0.1:3000"

# ── Database ──────────────────────────────────────────
[database]
user = "odoo"               # PostgreSQL user for the shared database container
# password = "..."          # auto-generated on first launch; set explicitly to override
image = "postgres:15"       # PostgreSQL Docker image

# ── Storage ───────────────────────────────────────────
[storage]
# data_dir = "/srv/oduflow"         # base directory for all data (default: /srv/oduflow or ~/.oduflow/data)
overlay_threshold_mb = 50            # template filestore size threshold (MB) — larger uses fuse-overlayfs, smaller uses copy

# ── Lifecycle ─────────────────────────────────────────
[lifecycle]
auto_stop_hours = 48        # auto-stop environments idle for N hours (no MCP/dashboard work); 0 disables
auto_delete_hours = 0       # auto-delete environments stopped for N hours; 0 disables (opt-in; DESTRUCTIVE, protected envs exempt)
prod_purge_hours = 0        # purge DB/files kept by a production deletion after N hours; 0 disables (opt-in; DESTRUCTIVE)

# ── Coding agent (optional) ───────────────────────────
# One agent container per team (Claude Code + OpenAI Codex + OpenCode), driven
# from the dashboard (Agent Chat / Agent CLI). Opt-in per team via
# agent_enabled below.
# [agent]
# image = "oduist/oduflow-coder:0.3.1"
# claude_model = ""         # optional Claude model override; empty = CLI default
# codex_model = ""          # optional Codex model override; empty = CLI default
# opencode_model = ""       # optional provider/model override; empty = OpenCode default

# ── Production hosting (optional) ─────────────────────
# [production]
# enabled = true            # opt in; requires routing.mode = "traefik"
# postgres_image = ""       # managed PG15 with CA; inherits custom [database].image
# walg_version = ""         # empty = Oduflow's pinned WAL-G version
# workers_cap = 8           # upper bound for auto-tuned Odoo workers

# [backup]                  # optional; requires all three credentials below
# bucket = ""
# access_key = ""
# secret_key = ""
# endpoint = ""             # empty = AWS; set for MinIO/R2
# region = ""
# prefix = "oduflow"
# snapshot_time = "02:00"
# basebackup_time = "03:30"
# keep = ["30:180", "7:30", "1:7"]
# walg_keep_full = 7
# upload_threads = 16

# ── Teams ─────────────────────────────────────────────
# Each team gets isolated workspaces, templates, credentials, and services.
# At least one [team.*] section is required.

[team.1]
hostname = "localhost"               # required and unique; OAuth issuer host for this team
                                     # port mode: http://{hostname}:{port}, traefik: https://{slug}.{hostname}
# base_domain = "demo.example.com"   # team DNS zone (traefik mode): envs/services live at {name}.{base_domain},
                                     # hostname defaults to oduflow.{base_domain}, productions default into the zone
environment_slots = 20               # maximum concurrent environments; 0 = unlimited
environment_hostname_mode = "branch" # "branch": feature.dev.example.com; "slots": dev1.example.com..devN.example.com
service_slots = 10                   # maximum managed auxiliary services; 0 = unlimited
auth_token = ""                      # auto-filled in fresh configs; HTTP MCP Bearer token
ui_password = ""                     # auto-filled in fresh configs; Web UI password for admin
port_range = [50000, 50100]          # port range for Odoo containers [start, end)
# agent_enabled = false              # enable the per-team coding agent (Agent Chat / Agent CLI)
# agent_default = "claude"           # "claude" | "codex" | "opencode" — default agent
# db_quota_gb = 50                   # combined PostgreSQL database cap; 0 disables
# disk_quota_gb = 0                  # XFS project quota for team files + databases; 0 disables
# [team.1.agent_env]                 # provider credentials injected into the agent container
# CLAUDE_CODE_OAUTH_TOKEN = ""
# ANTHROPIC_API_KEY = ""
# OPENAI_API_KEY = ""
# OPENCODE_API_KEY = ""              # OpenCode Zen; arbitrary provider vars also work
# [team.1.image_registry]            # enables the image build/publish MCP tools for this team
# repository_prefix = "acme"         # registry namespace all published images live under (required)
# host = "docker.io"                 # plain registry hostname, optionally with :port
# username = "acme-ci"               # optional; with token, request-scoped push credentials
# token = "<registry-token>"         # registry token/password stored in this config
# build_timeout_seconds = 1800       # per-build wall clock limit
# max_context_mb = 512               # sealed build context size cap
# max_log_mb = 16                    # persisted log cap per build
# max_concurrent_builds = 2          # active build workers allowed for this team
# keep_images = 10                   # local staging images kept per team; 0 disables pruning
```

### Server settings

| Key | Default | Description |
|---|---|---|
| `[server].bind` | `0.0.0.0` | HTTP server listener address. The legacy key `[server].host` remains accepted with a deprecation warning; if both are present they must have the same value |
| `[server].host` | *(legacy)* | Deprecated alias for `[server].bind` |
| `[server].port` | `8000` | HTTP server port |
| `[server].allow_local_path` | `true` | Allow trusted local-development live-mounts that bind a host checkout read/write. Set `false` on hosted, remote, or multi-user servers, or whenever only git-clone delivery is required |
| `[server].allow_insecure_http` | `false` | Serve the `/mcp` endpoint over plain HTTP with **no** authentication. Only enable behind your own authenticating proxy |
| `[server].trace` | `false` | Enable detailed trace logging for git analysis and environment operations |
| `[server].disable_telemetry` | `false` | Disable anonymous usage telemetry (see [Telemetry](#telemetry)) |

### Routing settings

| Key | Default | Description |
|---|---|---|
| `[routing].mode` | `port` | `port` — direct host port mapping; `traefik` — reverse proxy with auto-HTTPS |
| `[routing].acme_email` | *(empty)* | Let's Encrypt email for TLS certificates. Required when `mode = "traefik"` and `tls = true` |
| `[routing].tls` | `true` | Traefik only. `true`: Traefik terminates TLS (:443, HTTP→HTTPS redirect, Let's Encrypt). `{}`: HTTPS on :443 with the default certificate (self-signed unless supplied), redirect, no ACME/email requirement. `false`: plain HTTP on :80 only, no redirect/ACME — for a TLS-terminating upstream (e.g. a Cloudflare tunnel). Public URLs stay `https://` either way unless `public_scheme` says otherwise |
| `[routing].public_scheme` | *(derived)* | Scheme of every URL Oduflow hands out (dashboard links, MCP endpoints, share links, reported environment/service URLs). Derived by default: `https` in traefik mode, `http` in port mode. Set to `http` alongside `tls = false` when **nothing** terminates TLS in front — this also stops Traefik trusting inbound `X-Forwarded-*` on :80 (unless a per-team override still resolves to `https`). Overridable per team with `[team.X] public_scheme` |

### Database settings

| Key | Default | Description |
|---|---|---|
| `[database].user` | `odoo` | PostgreSQL user for the shared database container |
| `[database].password` | *(generated)* | PostgreSQL password. The bundled config omits it and one is auto-generated on first launch; set explicitly to override |
| `[database].image` | `postgres:15` | PostgreSQL Docker image |

### Storage settings

| Key | Default | Description |
|---|---|---|
| `[storage].data_dir` | `/srv/oduflow` or `~/.oduflow/data` | Base directory for all data. Team data directories are `team_{ID}` subdirectories inside |
| `[storage].overlay_threshold_mb` | `50` | Template filestore size threshold (MB). Templates smaller than this use a simple copy per environment; larger templates use fuse-overlayfs. The decision is stored in `metadata.json` at template creation time |
| `[lifecycle].auto_stop_hours` | `48` | Auto-stop environments after N hours without work (env-scoped MCP calls or dashboard actions). `0` disables. Protected environments are exempt |
| `[lifecycle].auto_delete_hours` | `0` | Auto-delete stopped environments N hours after they stopped (manual stops count). Default `0` = **disabled** — auto-delete is opt-in and destructive; set a positive value to enable. Protected environments are exempt; `pull_and_apply` wakes a stopped environment automatically |
| `[lifecycle].prod_purge_hours` | `0` | Purge the database and workspace kept by `delete_production` N hours after the deletion (tombstoned leftovers only; a re-created production is never purged). Default `0` = **disabled** — leftovers are kept forever; `oduflow cleanup --purge-deleted-productions --force` purges them immediately |

### Agent settings

The global `[agent]` section holds deployment-wide settings for the per-team coding agent (see [Coding Agent](agent.md)). Per-team enablement lives in the `[team.*]` sections below.

| Key | Default | Description |
|---|---|---|
| `[agent].image` | `oduist/oduflow-coder:0.3.1` | Immutable image for the per-team coding-agent container (Claude Code + OpenAI Codex + OpenCode); the default is coupled to the Oduflow release |
| `[agent].claude_model` | *(empty)* | Optional Claude model override for the agent; empty = CLI default |
| `[agent].codex_model` | *(empty)* | Optional Codex model override for the agent; empty = CLI default |
| `[agent].opencode_model` | *(empty)* | Optional OpenCode model override in `provider/model` format; empty = OpenCode default |

### Production settings

Production hosting is opt-in and is documented in detail in
[Production Hosting](production.md). Production dashboard REST routes and the dashboard tab
are registered only when `[production].enabled = true`. The `/production` MCP
surface remains discoverable with a production credential and reports disabled
hosting at call time.

| Key | Default | Description |
|---|---|---|
| `[production].enabled` | `false` | Enable long-lived production environments and their dedicated PostgreSQL cluster. Requires Traefik routing |
| `[production].postgres_image` | *(empty)* | PostgreSQL image for the production cluster. Empty uses `oduist/oduflow-postgres:15-bookworm-1` with CA certificates when `[database].image` is the default `postgres:15`; custom database images/majors are inherited |
| `[production].walg_version` | *(empty)* | WAL-G release override. Empty uses the version pinned by Oduflow |
| `[production].odumcp_repo_url` | `https://github.com/oduflow/oduflow-client-addons.git` | Fallback source for automatic OduMCP installation when production repositories do not provide the addon |
| `[production].odumcp_ref` | `19.0` | Connector branch or tag; must contain Odoo 19 addon version 19.0.1.1.0 or later with managed-key support |
| `[production].workers_cap` | `8` | Upper bound for automatically calculated Odoo workers; must be at least `1` |
| `[production].wal` | *(defaults below)* | Nested `[production.wal]` table for cluster-wide WAL timeouts and disk protection; active whenever production hosting is enabled |
| `[production.wal].upload_timeout` | `120` | Seconds per WAL upload before termination; forced kill follows after 5 seconds |
| `[production.wal].warn_after` | `120` | Seconds without archive progress while a queue exists before warning |
| `[production.wal].stall_after` | `300` | Seconds without progress before error; must be at least `warn_after` |
| `[production.wal].stop_free_gb` | `2` | GiB available to postgres, excluding root reserve, at which production is stopped |
| `[production.wal].resume_free_gb` | `4` | Required GiB before recovery/release; must exceed `stop_free_gb` |
| `[production.wal].stop_within` | `300` | Stop early if measured disk consumption predicts reaching the reserve within this many seconds |
| `[production.wal].warn_queue_gb` | `2` | Warn when unarchived WAL reaches this size in GiB; recovery release requires a smaller queue |
| `[production.wal].stop_queue_gb` | `8` | Stop production at this queued WAL size in GiB, even with ample free disk; must exceed `warn_queue_gb` |

### Backup settings

The `[backup]` section is optional. If it is present, `bucket`, `access_key`,
and `secret_key` are all required; remove the whole section to disable backups.

| Key | Default | Description |
|---|---|---|
| `[backup].bucket` | *(required)* | S3-compatible bucket name |
| `[backup].access_key` | *(required)* | S3 access key |
| `[backup].secret_key` | *(required)* | S3 secret key |
| `[backup].endpoint` | *(empty)* | Custom S3 endpoint for MinIO, R2, or another compatible service; enables path-style addressing |
| `[backup].region` | *(empty)* | S3 region |
| `[backup].prefix` | `oduflow` | Object-key prefix, normalized without leading or trailing `/` |
| `[backup].snapshot_time` | `02:00` | Default daily per-production snapshot time in server-local `HH:MM` |
| `[backup].basebackup_time` | `03:30` | Daily WAL-G base-backup time in server-local `HH:MM` |
| `[backup].keep` | `["30:180", "7:30", "1:7"]` | Snapshot retention tiers as `interval_days:age_days` pairs |
| `[backup].walg_keep_full` | `7` | Number of WAL-G full base backups to retain; must be at least `1` |
| `[backup].upload_threads` | `16` | Concurrent filestore chunk uploads per snapshot; `1` uploads sequentially. A running snapshot buffers up to `max(64 MiB, threads x 4 MiB)` of chunk data in memory, so lower it on small-RAM hosts |

### Per-team settings

Each `[team.*]` section defines an isolated team with its own workspaces, templates, credentials, and services. At least one team is required.

| Key | Default | Description |
|---|---|---|
| `hostname` | *(required)* | Unique team hostname and host-relative OAuth identity. In port mode environment URLs use `http://{hostname}:{port}`; in traefik mode they use `https://{slug}.{hostname}`. Behind Cloudflare Tunnel, publish this same hostname and use split DNS for direct LAN access when needed |
| `base_domain` | *(empty — legacy layout)* | The team's DNS zone (traefik mode only), e.g. `demo.example.com`. When set, environments and services get hostnames directly under it (`feature.demo.example.com`), `hostname` defaults to `oduflow.{base_domain}` (the dashboard), and production domains must be the zone apex or a subdomain of it (the apex is the default for the team's first production; client-owned domains go in a production's `extra_domains`). The zone is exclusive: another team's hostname or base_domain may not live inside it. Requires `*.{base_domain}` DNS pointing at this server. Existing environments move into the zone on their next update |
| `environment_slots` | `20` | Maximum concurrent development environments for the team in port or Traefik mode. Stopped environments count; deleting one frees its reservation. `0` disables the cap |
| `environment_hostname_mode` | `branch` | Traefik public hostname strategy. `branch` keeps environment-derived names such as `feature.dev.example.com`; `slots` reuses `dev1.example.com` through `devN.example.com` and requires `environment_slots > 0` |
| `service_slots` | `10` | Maximum number of managed auxiliary services for the team. Stopped services count; deleting a service frees its slot. `0` disables the cap |
| `production_token` | *(empty)* | Separate 32..512 character Bearer credential for `/production`; required for new production creation and synchronized to the Odoo administrator by OduMCP. Must differ from every dev and production token |
| `auth_token` | *(generated in fresh config)* | Bearer token for MCP HTTP auth and OAuth client secret. Empty disables MCP auth only when explicitly allowed with `[server].allow_insecure_http = true`; otherwise HTTP startup refuses it |
| `ui_password` | *(generated in fresh config)* | Password for Web UI login (user: `admin`). Separate from MCP auth token. Empty disables UI auth only when explicitly allowed with `[server].allow_insecure_http = true`; otherwise HTTP startup refuses it |
| `port_range` | `[50000, 50100]` | Port range for Odoo containers `[start, end)` — supports up to 100 concurrent environments |
| `agent_enabled` | `false` | Enable the per-team coding agent (dashboard Agent Chat / Agent CLI). Off by default |
| `agent_default` | `claude` | Which agent consoles/chats open by default: `claude`, `codex`, or `opencode` |
| `db_quota_gb` | `50` | Combined size cap for the team's environment and template PostgreSQL databases. `0` disables the check |
| `disk_quota_gb` | `0` | Kernel-enforced cap for team files and databases when the data filesystem supports XFS project quotas. `0` disables it |
| `public_scheme` | *(empty — global value)* | Per-team override of `[routing].public_scheme` (`http` or `https`) for the URLs handed out for this team. Lets one `tls = false` deployment mix a plain-HTTP LAN team with a team fronted by a TLS-terminating upstream such as a Cloudflare tunnel — see [Traefik routing](traefik.md#mixing-http-and-https-teams-in-one-deployment). Same wire-reality rules as the global setting: `https` is rejected in port mode, `http` is rejected with `tls = true` or `tls = {}` |
| `[team.X.agent_env]` | *(empty)* | Sub-table of environment variables injected into the team's agent container — provider credentials (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENCODE_API_KEY`, or any provider-specific OpenCode variable) and custom vars |
| `[team.X.image_registry]` | *(absent — image building disabled)* | Sub-table enabling the container image build/publish MCP tools for the team. `repository_prefix` (required) is the registry namespace agents may publish under — the authorization boundary; `host` (default `docker.io`) is a plain registry hostname; `username` + `token` (set together) provide request-scoped push credentials directly from the Oduflow config — omit both to use the host Docker daemon's own `docker login` credentials. Resource bounds are `build_timeout_seconds` (default `1800`, hard wall-clock deadline), `max_context_mb` (default `512`), `max_log_mb` (default `16`), and `max_concurrent_builds` (default `2`). `keep_images` (default `10`, `0` disables pruning) retains that many local staging builds; temporary publish tags are removed after push and older untagged image objects are deleted once unused. Protect the config file and use a least-privilege registry token restricted to the prefix |

`environment_hostname_mode = "slots"` requires Traefik and a hostname with a
distinct host prefix and parent domain, such as `dev.example.com`. A bare
registrable domain such as `example.com` has no prefix to number and is
rejected. The default `branch` mode works with the existing
`*.dev.example.com` DNS and wildcard-certificate layout.

Team data is stored at `{data_dir}/team_{ID}/`:

```
team_{ID}/
├── workspaces/           # Per-branch environments
├── templates/            # Reusable database snapshots
├── shared_repos/         # Extra addon repositories (bare clones)
├── ports.json            # Port registry
├── hostnames.json        # Environment capacity reservations and optional reusable hostnames
├── .git-credentials      # Git credentials for this team
└── agent_guides/         # AI agent guides (markdown)
```

### Configuration file overrides

On first startup, Oduflow generates `postgresql.conf` from one host-wide
resource plan and copies the bundled `odoo.conf` if it does not exist. These
files take **priority** over the bundled defaults — edit them to customize
PostgreSQL tuning or Odoo settings globally:

```
/etc/oduflow/             (or ~/.oduflow/conf/)
  oduflow.toml            ← main configuration file
  postgresql.conf         ← dev PostgreSQL tuning (used by oduflow-db)
  postgresql-prod.conf    ← production PostgreSQL tuning (created lazily)
  odoo.conf               ← custom Odoo defaults (used by new environments)
  license.key             ← license file (optional)
  traefik/                ← Traefik dynamic configuration (auto-generated)
```

`pg_hba.conf` remains in the PostgreSQL data volume rather than this config
directory. On every startup Oduflow reads the active file reported by
PostgreSQL and reconciles a marked block containing password-authenticated
rules for the real Docker IPAM subnets of the shared and per-team networks.
All standard and operator-managed rules outside that block are preserved. This
also repairs an existing data volume whose original image initialization did
not add a rule for Docker clients.

The resource plan considers `[production].enabled`. With production disabled,
the lean dev PostgreSQL profile targets about 10% of host RAM for
`shared_buffers` (128 MB–1 GB). With production enabled, the planner budgets
the host as a whole: dev PostgreSQL targets 5% (128–512 MB), production
PostgreSQL targets 20% (512 MB–8 GB), production Odoo worker sizing gets a 45%
RAM budget, and 20% stays reserved for the OS and other services. CPU values
are concurrency ceilings, not Docker reservations.

Generated configs contain an `ODUFLOW-TUNE` fingerprint. Oduflow warns when
CPU, RAM, or the production mode no longer matches that fingerprint, but never
rewrites or restarts PostgreSQL during a normal startup or package upgrade.
Preview and explicitly apply a new plan with:

```bash
oduflow retune-postgres          # plan + unified diff; writes nothing
oduflow retune-postgres --apply  # backup/write and stage managed configs
```

`--apply` refuses a custom config unless `--force` is also given. Existing
files are backed up with a UTC timestamp. For each existing production it also
regenerates the derived `odoo.conf` and stages it inside the Odoo container.
Restart the PostgreSQL and Odoo containers listed by the command to activate
the new database and worker settings.

If a repository contains an `odoo.conf` in its `.oduflow/` directory (`<repo>/.oduflow/odoo.conf`), it takes priority over both the bundled and system-level versions for that specific environment.

## Telemetry

Oduflow collects **anonymous** usage telemetry to help us understand adoption and prioritize development. Two events are sent:

- **`first_run`** — sent once on the very first startup (when the instance ID is created).
- **`env_created`** — sent each time a new environment is provisioned.

Each event contains only:

- The event name
- The oduflow version
- A random instance ID (UUID)

**No** personal data, hostnames, IP addresses, branch names, repository URLs, or environment details are collected.

### Opt out

Add to your `oduflow.toml`:

```toml
[server]
disable_telemetry = true
```

## Auto-start with systemd

On Linux servers, Oduflow can be registered as a systemd service so it starts automatically on boot.

### Prerequisites

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install oduflow as a tool (as root)
uv tool install oduflow

# Create the configuration file (optional — Oduflow auto-creates a default oduflow.toml on first start)
```

### Install the service

```bash
oduflow systemd-install
```

This will:

1. Generate a systemd unit file at `/etc/systemd/system/oduflow.service`
2. Write `/etc/needrestart/conf.d/oduflow.conf` (only if needrestart is installed)
3. Run `systemctl daemon-reload`
4. Enable the service for auto-start on boot

The unit is ordered after `docker.service` and `containerd.service`, restarts
always, and has no start-rate limit, so a host that upgrades Docker underneath
Oduflow cannot leave the service parked in `failed`.

The needrestart snippet excludes `oduflow.service` from needrestart's automatic
restarts. Oduflow drives the Docker daemon; when `unattended-upgrades` restarts
a library, needrestart would otherwise restart Oduflow in the same batch as
containerd and Docker, and Oduflow's startup would race a daemon that is itself
going down. With the exclusion in place, needrestart lists Oduflow as needing a
manual restart instead:

```bash
systemctl restart oduflow
```

Already installed the service with an older Oduflow? Re-run
`oduflow systemd-install` to refresh the unit and add the needrestart override,
then `systemctl daemon-reload && systemctl restart oduflow`.

### Manage the service

```bash
# Start
systemctl start oduflow

# Status
systemctl status oduflow

# Logs (follow)
journalctl -u oduflow -f

# Restart after config changes
systemctl restart oduflow
```

### Remove the service

```bash
oduflow systemd-uninstall
```

This stops, disables, and removes the unit file, along with the needrestart
override.
