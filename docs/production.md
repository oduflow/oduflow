# Production Hosting

Oduflow can host **production** Odoo environments alongside the dev
environments it was built for. Productions get special treatment:

- a **dedicated PostgreSQL cluster** (`oduflow-prod-db`) — physically
  separate from the dev one, auto-tuned for production workloads;
- a **custom domain** per production (`erp.customer.com`), routed by Traefik
  with a Let's Encrypt certificate — plus optional **extra domains** routed to
  the same production (e.g. the client's own public domain);
- an **auto-tuned production `odoo.conf`** (workers from host CPU/RAM, cron
  enabled, proxy mode) — never the dev profile;
- a choice of **server mode**: sync multi-process workers or one async gevent
  process, with Traefik routing to the matching Odoo port;
- **no sanitization/neutralization**, no idle reaper, no `--dev=xml`;
- deploys with **automatic code rollback** on failure;
- **S3 backups**: continuous WAL archiving (WAL-G), scheduled snapshots
  (database dump + deduplicated filestore), disaster-recovery PITR.

Productions are managed by their own MCP tool stack (`create_production`,
`update_production`, …) and a dedicated **Production** tab in the dashboard —
they never mix with dev environment tooling.

## Separate production MCP access

Developer credentials connect to `/mcp`; production administrators connect to
`/production` on the same Oduflow host using a **different** Bearer credential.
The production endpoint accepts the team's `production_token`, not the dev token
or dev OAuth tokens. Token validation uses local configuration, so start, restore
and other infrastructure operations do not depend on a working Odoo API.
The shared `read_output` helper remains available for long production logs and
deploy output; cached production results are bound to their team and cannot be
read with development credentials. Production tools remain listed on this
endpoint when hosting is disabled; calls
return a configuration error. They are not callable from the developer endpoint.
The dashboard and local CLI remain owner administration interfaces. On a
multi-team server, cluster-wide PITR and WAL controls require these owner
interfaces because they affect other teams; a team production token cannot
perform those global mutations. Deliberate
production-to-dev copies through `create_environment(from_production=...)` retain
the existing per-production `allow_copy_to_dev_mcp` policy.

```toml
[team.1]
auth_token = "<existing-development-token>"
production_token = "<separate-random-token-at-least-32-characters>"
```

Generate a random production credential (for example with
`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`), store it in the
private TOML configuration, restart Oduflow and configure the MCP client to send
it as a Bearer token to `/production`. Production tokens must be unique across
teams and distinct from every development token. One production token authorizes
all productions in its team; it does not identify individual administrators.

### Automatic OduMCP installation

New production creation requires a configured production token. Until one is set,
the dashboard's Production tab and its Create Production dialog show a setup
notice, and the dialog cannot be submitted. Creation automatically
attempts to install `odumcp` if it is absent. Automatic provisioning supports
**Odoo 16 and later**. Older productions can still be managed with infrastructure
tools.

**Oduflow ships `odumcp` as a managed component by default.** When neither the
main nor the extra repositories contain the module, Oduflow clones the connector
repository at the branch of the production's own Odoo version (`18.0` for
`odoo:18.0`) into a managed checkout and mounts **only** its `odumcp` addon
read-only. The version comes from the image tag; for a custom image without a
version in its tag, Oduflow asks the image's `odoo --version`. This managed
checkout survives container recreation and is never silently updated: neither
production creation nor deployments pull newer connector code. It changes in
exactly two cases:

- **Odoo version change.** When `reconfigure_production` moves a production to
  another Odoo version, the checkout is replaced with the new version's branch
  before the container is recreated; run `sync_production_mcp` afterwards to
  upgrade the installed module. A synchronization also replaces a checkout made
  for another version.
- **Explicit refresh.** `sync_production_mcp(name, refresh=true)` re-clones the
  same version's branch from `odumcp_repo_url`, recreates the container and
  upgrades the module (even at an unchanged manifest version, so data and view
  changes land). This is the only way the managed module is updated, and it is
  an operator action — nothing refreshes it automatically. A refresh requires
  an explicit `name`: one production per call, never the whole team.

**A client takes the module over by committing `odumcp` into their own
repository** (main or extra, where Odoo scans it) **and deploying.** The deploy
that brings the module in completes the takeover by itself: once the new code is
verified healthy, Oduflow recreates the container without the managed mount and
removes its managed checkout, records `odumcp_takeover: completed` in the deploy
history entry and says so in the deploy result (`odumcp now comes from the
repository; the managed copy was removed`). This holds for manual,
webhook and auto-update deploys alike, and also when the deploy classified the
change as nothing to apply (the detection keys on the repository state after the
pull, not on the changed files). `sync_production_mcp` does the same for a
production whose takeover is still pending (for example because that recreate
failed). From then on the module is client code: Oduflow never touches it again —
`refresh=true` is a documented no-op for a client-shipped module, and
installs/upgrades follow the repository's deployments like any other addon.
Removing the module from the repository again puts Oduflow back in charge on the
next synchronization.

Whether the managed copy is mounted at all is decided by the repository state
whenever a container is built: any recreate (reconfiguration, image change,
synchronization) leaves the mount out as soon as the client repositories ship
`odumcp`. A synchronization recreates the container whenever the container does
not see the current checkout (or still mounts a removed one), which repairs an
earlier synchronization whose recreation failed. Provisioning refuses to install
an addon built for a different Odoo version than the one running:

```toml
[production]
enabled = true
odumcp_repo_url = "https://github.com/oduflow/oduflow-client-addons.git"
```

The addon must implement `_set_oduflow_key`; an older release cannot synchronize
credentials. Source checkout failures, unsupported Odoo versions, unavailable or
incompatible modules and failed key provisioning do not fail production creation.
Production remains available with a warning and an OduMCP status of `sync_failed`,
which the dashboard shows as an **MCP FAILED** badge on the production card. A
production on Odoo 15 or older gets the status `unsupported` instead, shown as
**MCP N/A**: no synchronization can help there. Infrastructure tools continue to work. The
`production_odoo_*` tools remain listed but return a configuration error until
`sync_production_mcp` succeeds. Fix the addon source or compatibility issue and
retry synchronization; recreating production is unnecessary.

A fixed internal Odoo shell operation registers the configured production token
as an MCP-only key on `base.user_admin`. It does not expose arbitrary production
shell execution through MCP. Odoo stores a password hash, not the plaintext key.
Personal keys are preserved. An existing MCP profile and suspended MCP access
are preserved; an administrator without a profile receives a read-only profile.
Configure write/model/method policies in Odoo before requesting business changes.

### Business operations and approval

No standalone `odumcp_server` is needed. Oduflow calls `/odumcp/v1/execute`
directly using the configured production key. Model and field policies, Odoo ACLs,
approvals and audit records remain enforced by the addon. Calls authenticated
with the managed key have `source = oduflow` in the Odoo audit log.

- `production_odoo_info(name)` returns Odoo/profile information.
- `production_odoo_read(name, operation, params)` supports schema, records,
  counts, aggregates, attachments and reports.
- `production_odoo_preview_change(name, action, payload, idempotency_key)` stores
  an exact change plan. Optional `batch_key` groups plans for review.
- `production_odoo_change_status(name, approval_id)` retrieves its state/result.
- `production_odoo_execute_change(name, approval_id)` executes an approved plan.

Approve plans in Odoo. Explicit Odoo auto-approval policies still apply; Oduflow
never grants approval itself. After a timeout, check the existing approval status
before making another plan. Infrastructure actions (deploy, delete, restore)
remain Oduflow operations and are not governed by Odoo business approvals.

### Existing productions and rotation

After changing `production_token` in TOML and restarting Oduflow, connect with the
new token and call `sync_production_mcp(name)`; omit `name` to process all team
productions. The call installs/configures the addon and replaces only its managed
key. It reports each production independently; start stopped productions and retry
failures. Adding a missing managed addon mount — or dropping it once the client
repository provides `odumcp` — recreates the container briefly. Reapplying the
same key is a no-op for the key itself.

Each result states where the addon comes from (`addon_source`: `managed` or
`repository`), what happened to the managed checkout (`checkout`: `kept`,
`cloned`, `replaced`, `refreshed`, `removed` or `repository`), whether the
container was recreated and, for a managed checkout, the connector branch and
commit it is at. Pass `refresh=true` (with an explicit `name`) to re-clone the
managed checkout from `odumcp_repo_url` for the same Odoo version and upgrade the
module; the result reports `refreshed` and, for a client-shipped module, a note
that nothing was refreshed.

The new token immediately controls the Oduflow endpoint after restart. Each Odoo
accepts the new key only after its synchronization succeeds; until then its old
managed key may still authenticate directly to Odoo. Rotation is not atomic across
multiple databases. Also synchronize after restoring an older database backup,
which can restore an old key or old module state. Disabling or removing a token
in Oduflow alone does not revoke the stored Odoo API key; revoke that key in Odoo
when retiring the integration.

## Requirements

- `routing_mode = "traefik"` (custom domains are Traefik `Host()` rules).
- The production's DNS record must point at the server.
- For backups: an S3-compatible bucket (AWS, MinIO, Cloudflare R2, …).
- Debian-based `postgres:*` images (the default; `-alpine` images do not run
  the WAL-G binary).

## Configuration

Production hosting is disabled by default. Enable it globally in TOML and
restart Oduflow; productions themselves are then created at runtime:

```toml
[production]
enabled = true          # required; restart Oduflow after changing
workers_cap = 8         # upper bound for auto-tuned Odoo workers
health_timeout = 180    # seconds a production may take to become healthy

[backup]                # configures backups; production must also be enabled
bucket = "acme-backups"
access_key = "AKIA..."
secret_key = "..."
endpoint = ""           # empty = AWS; set for MinIO/R2 (path-style implied)
region = "eu-central-1"
# defaults you normally leave alone:
# prefix = "oduflow"
# snapshot_time = "02:00"      (daily per-production snapshots)
# basebackup_time = "03:30"    (daily WAL-G base backup)
# keep = ["30:180", "7:30", "1:7"]  (snapshot retention: interval:age days)
# walg_keep_full = 7           (base backups retained)
# upload_threads = 16          (parallel filestore chunk uploads; buffers
#                               up to max(64 MiB, threads x 4 MiB) in RAM)
```

While disabled, the dashboard tab and production HTTP/webhook routes are not
registered, production MCP tools return an enablement error, and scheduled
backup work does not run.

The production PostgreSQL cluster is provisioned lazily and idempotently. If
production hosting is disabled, Oduflow stops every managed production Odoo
container and then its dedicated PostgreSQL container without deleting any
container, volume, database, filestore, or registry data. Re-enabling starts
PostgreSQL first and then starts all managed production Odoo containers.

Enabling production also changes the unified host resource plan. New configs
coordinate dev PostgreSQL, production PostgreSQL, and production Odoo workers
instead of letting each profile size itself against the whole host. Existing
configs are not silently replaced: after changing `enabled`, run
`oduflow retune-postgres` to inspect the new plan, then
`oduflow retune-postgres --apply`. The apply step also stages regenerated
worker settings in every existing production Odoo container; restart the
PostgreSQL and Odoo containers it lists.

## Creating a production

```text
create_production(
    name="erp",
    repo_url="https://github.com/acme/odoo-erp.git",
    branch="production",
    domain="erp.acme.com",
    odoo_image="odoo:18.0",
    template_name="acme-prod",   # optional: seed DB+filestore from a template
    auto_update=False,
    allow_copy_to_dev_mcp=True,  # may agents copy this production into dev?
    server_mode="workers",       # or "gevent", see Server mode below
)
```

### Domains

In a team with [`base_domain`](traefik.md#team-base-domain) configured, the
primary `domain` must lie in the team zone — the zone apex
(`demo.example.com`) or a subdomain (`erp.demo.example.com`) — and may be
omitted: the team's **first** production defaults to the apex, later ones to
`<name>.<base_domain>`. Without a base domain, `domain` is required and may be
any FQDN.

`extra_domains` adds further public FQDNs routed to the same production —
typically the client's own domain alongside the team-zone name:

```text
create_production(name="erp", domain="demo.example.com",
                  extra_domains=["myodoo.pl"], ...)
```

All domains share the production's Traefik routes (`Host(a) || Host(b)`),
each with its own Let's Encrypt certificate; every DNS record must point at this server.
Extra domains may be arbitrary FQDNs but must not fall inside another team's
zone, and every domain — primary or extra — must be unused anywhere else in
the deployment (other productions, team hostnames, static routes).

`template_name` is the migration path for an existing production: import it
first (e.g. [from Odoo.sh](templates.md)), then create the production from
that template — the database is copied into the production cluster and the
filestore into the production's plain (non-overlay) directory.

The clone is **full** (not shallow): the branch's commit history is the
production's deploy history and the source of rollback targets.

### Server mode

`server_mode` picks how Odoo serves HTTP and where Traefik sends the traffic:

| Mode | Odoo | Traefik routes |
|---|---|---|
| `workers` (default, sync) | auto-tuned HTTP worker processes on 8069, plus the gevent process on 8072 | pages to **8069**; the live bus (`/websocket`, and `/longpolling` on Odoo ≤ 15) to **8072** |
| `gevent` (async) | one HTTP worker on 8069 (kept for in-container calls) and the gevent process on 8072 | **everything to 8072** |

Both modes run Odoo's multi-process server, so cron workers run either way,
and the gevent process is always present. In `workers` mode it only holds the
bus connections: a worker process refuses a websocket ("Is the connection
opened on the evented port?"), so without the bus route Discuss and live
notifications would not work. In `gevent` mode a single cooperative process
serves every request: it handles many concurrent long-lived or I/O-bound
requests (long polling, slow external calls) cheaply, but one CPU-heavy
request stalls everyone else, and it uses one CPU core. `db_maxconn` keeps its
full auto-tuned size in both modes; in `gevent` mode it is the connection
pool that bounds how many requests that process serves at once. Because that
one process is the whole site, `gevent` mode also raises its memory limits
(`limit_memory_soft_gevent`/`limit_memory_hard_gevent`, and the general
`limit_memory_*` for Odoo ≤ 15) to half the Odoo RAM budget instead of a
worker's 2 GB, so it does not recycle itself — dropping every connection — under
normal load.

The listen settings are pinned in the generated `odoo.conf` whatever the base
conf (team `odoo.prod.conf`) says: `http_interface = 0.0.0.0` (Odoo 20
otherwise listens only on `127.0.0.1`, which Traefik cannot reach),
`http_port = 8069`, `gevent_port`/`longpolling_port = 8072`,
`http_enable = True`, and the legacy `xmlrpc`, `xmlrpc_port` and
`xmlrpc_interface` keys are dropped; a base conf that set them is logged.
The deploy health check requires **both** ports to answer `/web/health` in
either mode, so a dead gevent process (a broken bus in `workers` mode, a
dead site in `gevent` mode) fails the deploy and triggers the code rollback.

Change the mode with `reconfigure_production(name="erp", server_mode="gevent")`
(the container is recreated). The listen ports are managed keys (see
[odoo.conf overrides](#odooconf-overrides)), and a `workers` override must stay
at least 1: with `workers = 0` Odoo would switch to its threaded server, which
does not listen on 8072.

### Promoting a dev environment

`from_environment` turns an existing dev environment into the seed — the
promotion path from "it works on the branch" to "it serves customers":

```text
create_production(name="erp", domain="erp.acme.com", from_environment="feature-x")
```

The environment's database and filestore are copied (its Odoo container is
briefly stopped so the pair is consistent, then restarted), and omitted
`repo_url` / `branch` / `odoo_image` / `git_user` / `extra_addons` default to
the environment's own — explicit arguments still win. Unlike the
save-as-template detour, no intermediate template is created and the source
environment is **not reset** — it keeps living as a dev environment.
`from_environment` and `template_name` are mutually exclusive.

Promotion also inherits the source environment's user environment variables.
`secret:<name>` references stay in the production registry and container labels;
values are resolved from the team's secret store only for the container runtime.
They survive domain/image/branch reconfiguration. Missing secrets are rejected
before the source is stopped. Pass `env_vars={...}` to replace the inherited set,
or `{}` to inherit none. `reconfigure_production(env_vars={...})` replaces the
stored set; omitting it preserves the existing variables. The managed database
variables `HOST`, `PORT`, `USER`, and `PASSWORD` cannot be overridden.

Every process in the container inherits these variables: the server, the
deploy's `odoo -u` and the pip installs. For that reason, a new set may not
contain `HOME`, `PYTHONHOME`, `PYTHONUSERBASE` or `PYTHONNOUSERSITE`. Odoo
keeps its filestore and sessions under `HOME`, and the repositories'
requirements are installed into the `odoo` user's pip `--user` site.
Overriding these variables hides the installed packages or moves data out of
the mounted volumes. `PYTHONPATH` may not list a `site-packages` or
`dist-packages` directory either. Python searches `PYTHONPATH` before the
standard library, so a backport installed there (such as `dataclasses`)
shadows the stdlib module and Odoo fails to start. A production that already
stores such a variable keeps it and still recreates; only setting a new
variable set is refused.

No sanitization happens — the data goes *into* production. One caveat: if
the source environment was itself created from a production
(`from_production`), its data was sanitized on that copy, and the new
production starts with that sanitized data (the result warns about this).

The dashboard offers the same via **More → Promote to Production** on an
environment card, which opens the create-production form pre-filled.

To promote into a production that **already exists**, use
`restore_production(from_environment=…)` instead — see
[Backups](#backups) for the restore mechanics.

## Reconfiguring a production

A production's settings are not frozen at creation.
`reconfigure_production` changes any of the domain, extra domains, Odoo
image, deployed branch, repository URL, git user, the extra addon repos, or
the [server mode](#server-mode), then **recreates the container** to match — the database and filestore live outside the
container and are preserved; expect a brief downtime:

```text
reconfigure_production(name="erp", domain="erp.newcustomer.com")
reconfigure_production(name="erp", extra_domains=["myodoo.pl"])  # [] removes all
reconfigure_production(name="erp", branch="18.0-stable")
reconfigure_production(name="erp", extra_addons={"acme-addons": "production"})
reconfigure_production(name="erp", server_mode="gevent")
```

Omitted arguments are left unchanged (`git_user=""` explicitly clears the
git user). The registry record is updated first and the workspace/container
are converged to it, so re-running the same call after a mid-way failure
repairs a missing container or checkout instead of reporting a no-op. The same
applies to a container whose Traefik routing no longer matches the record
(e.g. a production created before the bus route existed). Such a container is
also recreated, without a separate maintenance window, by the next deploy
that changes code (`update_production` or an auto-deploy) and by
`restart_production`; a plain `reconfigure_production(name="erp")` does it
immediately.

After recreating the container, reconfigure waits up to
`[production] health_timeout` seconds (default 180) for Odoo to become healthy. A container that crash-loops, for example on an environment
variable that breaks Python, fails the wait early: Docker restarts it twice
during the wait, and the production lock is released for the next call. The
same rule applies to every production health wait, including deploy
verification and the rollback restart.

**Applying a rotated secret.** Secret values reach the container only when it
is created, so `restart_production` keeps running with the old value. After
replacing a secret's value in the dashboard, call
`reconfigure_production(name="erp")`: it compares the current value of every
`secret:<name>` reference with what the container runs with, recreates the
container when one differs and lists the affected variables by name. A
reference to a deleted secret is not treated as a change; the next recreate
reports it. `recreate=true` recreates the container even when nothing differs.

Two things reconfigure deliberately does **not** do:

- Changing `odoo_image` does not migrate the database. A minor image refresh
  is safe; a major Odoo version bump additionally needs an explicit module
  upgrade plan.
- Changing `branch`/`repo_url` deploys the new code as-is (restart only).
  Run `update_production(install=..., upgrade=...)` afterwards if the new
  code needs module changes.

The dashboard offers the same settings on each production card under
**More → Settings**, together with the *agent copy to dev* gate
(dashboard-only, see [Copying production data to dev](#copying-production-data-to-dev)).

### odoo.conf overrides

The generated production `odoo.conf` merges a base conf chain (team
`odoo.prod.conf` > bundled) with [auto-tuned](#configuration) worker/limit
settings. Per-production overrides sit on top of both and survive deploys,
retunes and reconfigures:

```text
set_production_odoo_conf(name="erp", options={"limit_time_real": "300"})
set_production_odoo_conf(name="erp", unset="limit_time_real")
```

The repository is never part of the chain: a `.oduflow/odoo.prod.conf`
(honoured by earlier versions) is ignored and logged, so pushing to the
deployed branch cannot change how the production server runs. Move such
settings into overrides or the team `odoo.prod.conf`.

An explicit override beats the auto-tuned value (e.g. pin `workers`; it must
be at least 1, see [Server mode](#server-mode)). Keys managed by Oduflow are
refused: `addons_path` and `data_dir` are generated, the `db_*` connection
keys come from container env vars, and the listen settings (`http_interface`,
`http_port`, `gevent_port`, `longpolling_port`, `http_enable` and the legacy
`xmlrpc`, `xmlrpc_port`, `xmlrpc_interface`) are pinned to what Traefik
routes to.
Such keys saved before they became managed are ignored (and logged) and are
removed from the record by the next override save or reconfigure (option names
are compared case-insensitively and stored lowercased, matching how Odoo
reads them). Current overrides are shown by `get_production_info`; the
dashboard edits them in the same **Settings** panel. Applying restarts the
container (brief downtime) unless `restart=false`; a call that leaves the
overrides unchanged skips the restart entirely.

#### Database manager and the master password

The generated conf sets `list_db = False`, which is what closes Odoo's
database manager: Odoo gates every database management function (create,
duplicate, drop, backup, restore) on that setting, and nothing else stands
between that page and the databases except the **master password**
(`admin_passwd`). Oduflow never writes one, so Odoo's compiled-in default —
the literal `admin` — applies.

Turning the manager on therefore requires choosing a real master password in
the same override set (or in the team `odoo.prod.conf`):

```text
set_production_odoo_conf(name="erp", options={
    "list_db": "True",
    "admin_passwd": "<at least 12 characters, not 'admin'>",
})
```

`list_db = True` without one is refused, and so is dropping `admin_passwd`
while the manager stays on. A `list_db` value other than `True` or `False` is
refused too: Odoo's conf loader converts only those spellings to a boolean, so
`list_db = 0` (or `no`, or `FALSE`) would read as a non-empty, truthy string
and *enable* database management. A hand-edited `productions.json` that opens
the manager unlocked is overruled on the next conf rebuild, which logs the
reason and keeps `list_db = False`.

Credential-valued overrides (`admin_passwd`, `smtp_password`) are reported
back masked by `get_production_info` and `set_production_odoo_conf`, so an
agent with production MCP access never receives them; the dashboard, which is
the operator's own console, keeps showing the values. The generated conf is
written owner-only (`0600`) on the host, since it is the one managed file that
can carry a credential — the `db_*` connection keys travel as container
environment variables instead.

## Deploys and rollback

Module preflight and post-install checks use the production PostgreSQL cluster
and the production's own database role. Development requests retain their
separate cluster. An exception after source synchronization, including a module
preflight SQL failure, triggers code rollback just like a failed module command.

`update_production(name)` pulls the branch (and extra-addon worktrees),
classifies the changes (or takes explicit `install=` / `upgrade=` /
`restart=true`), applies them, and **verifies** the deploy — module exit
codes plus an in-container health check. In auto mode (no explicit
`install`/`upgrade`/`restart`), only modules **installed in the database** are
upgraded: an extra-addon repo typically bundles many modules of which a few
are installed, and a routine fast-forward touching uninstalled ones must not
fail the deploy — their changes are dropped with a warning and the rest is
classified again, so a JS or view change in an installed module still
reloads and a changed `.py` still restarts. A module already in the database
is upgraded even when it looks new (moved between directories or repos), and
new modules are never auto-installed on a production. Explicit
`install=`/`upgrade=` requests keep the strict behaviour. A changed `.oduflow/requirements.txt`
or `.oduflow/apt_packages.txt` — in the main repo or an extra-addon repo —
reinstalls the pip/apt dependencies into the container before the restart
(they are also installed from scratch whenever the container is recreated:
create, reconfigure). In production a "refresh"-class
change (XML/JS only) still restarts the container: there is no `--dev=xml`.

If verification fails, the checkout is reset to the pre-deploy commit, the
config re-applied and the container restarted — **the code rolls back
automatically**. The database is *never* rolled back automatically: if a
module upgrade left it inconsistent, restore a snapshot explicitly
(`restore_production`). A snapshot is taken automatically before every deploy
when backups are configured — but only once the pull has found something to
apply: a deploy with nothing new on the branch, or with Markdown-only changes
(and no explicit `install` / `upgrade` / `restart`), skips it. The snapshot is
taken with the checkout still on the pre-deploy commit, so it records the code
its database ran against.

While a deploy runs, its current step (fetching code, pre-update snapshot with
file progress, applying changes, verifying health, rolling back code) is shown
next to the production's status on the dashboard, returned as `progress` by
`list_productions` / `get_production_info`, and quoted in the `BusyError` a
concurrent call on the same production receives.

A deploy started from the dashboard (or by a GitHub push) while another
operation holds the production — a scheduled snapshot, a restart, another
deploy — queues instead of failing: the production shows a `queued` badge with
the waiting time and what it waits for, and a second click while one is already
queued is refused. If the production stays busy past the wait limit (5 min for
the dashboard, 30 min for webhooks), the deploy is not started and a `skipped`
entry with the reason lands in the deploy history.

Every deploy lands in the production's history (`production_deploys`):
commits, action, modules, status (`success` / `rolled_back` /
`rollback_failed` / `skipped`), trigger (mcp / ui / webhook / schedule).

The health checks after a deploy, rollback, reconfigure, restore or Stack
apply wait up to `[production] health_timeout` seconds (default 180). Raise it
for workers-mode productions with many modules — they can take minutes to
start serving. A production stuck with an `unhealthy` flag after a timed-out
rollback recovers with `restart_production` or `start_production`: on a
flagged production either one re-judges health and clears the flag once the
service answers again (and says so when it still does not).

Manual code rollback to any commit: `rollback_production(name, to_commit)`.

### Diagnosing a failed deploy

A large `-u` can outlast the MCP client's request timeout. The deploy still
finishes on the server, but the client never receives the response or its
Odoo output. So a failed deploy keeps its evidence in the deploy record
itself:

| Field | Content |
|---|---|
| `failed_step` | `dependencies` (apt/pip reinstall), `install` (`odoo -i`), `upgrade` (`odoo -u`), `health` (every command succeeded, but Odoo did not answer `/web/health`), or `exception` (Oduflow raised an exception, for example a module preflight query) |
| `exit_code` | The exit code of that step |
| `dependencies_exit_code` | The apt/pip reinstall's own exit code, when one ran: a failed upgrade next to a failed reinstall usually means a missing Python package |
| `failure_excerpt` | The Odoo log record that explains the failure, with its traceback: the last `CRITICAL` record, else the last `ERROR` one, else the last bare Python traceback |
| `output_tail` | The last 200 lines (16 KB at most) of the failed step's output. For a failed health check, this is the server log instead |
| `error` | One line: the step, the exit code, the rollback, and the cause (the exception that closes the traceback) |

`production_deploys` returns `failure_excerpt` by default. Pass
`include_output=true` for `output_tail` too. The dashboard shows `error`
under the deploy's status. The server log gets the same excerpt on the
`deploy failed at step …` line.

What the exit codes mean:

| Exit code | Meaning |
|---|---|
| `255` from `install`/`upgrade` | Odoo could not load or upgrade the registry: `preload_registries()` caught an exception, logged `CRITICAL Failed to initialize database` with the traceback, and returned `-1`. The excerpt says why |
| `1` from `dependencies` | An apt or pip install failed. Oduflow reports 1 for any failed dependency step |
| `1` from `exception` | Oduflow raised an exception after the code moved. The exception's text is in `error` |
| `137` | The command was killed by `SIGKILL`, typically by the container's memory limit |
| `0` with `failed_step: health` | The commands succeeded, but the restarted server did not answer within `[production] health_timeout` seconds |

**Where the upgrade runs.** `odoo -i` / `odoo -u` is a `docker exec` into the
production's running container: `/entrypoint.sh odoo -d <db>
--stop-after-init --no-http -u <modules>`. It uses the same image, the same
`/etc/odoo/odoo.conf`, the same environment variables (including
`env_vars`) and the same `odoo` user with `HOME=/var/lib/odoo`, so it sees
the same pip `--user` packages under `/var/lib/odoo/.local` as the server.
Two things differ from the server process:

- **The upgrade runs next to the live server.** The production's HTTP
  workers and its cron worker keep using the same database during the
  upgrade. A cron job or request can hold locks on rows or tables the upgrade
  rewrites. PostgreSQL then aborts the upgrade's transaction with
  `could not serialize access due to concurrent update` or
  `deadlock detected`, and Odoo exits with 255. A dev environment never has
  this concurrency, because it runs with `workers = 0` and
  `max_cron_threads = 0`, so the same `-u` can pass there. The production
  PostgreSQL cluster logs lock waits (`log_lock_waits = on`).
- With `workers > 0`, the upgrade is Odoo's prefork master process, which
  does not apply `limit_memory_hard` to itself. It shares the container's
  memory limit with the workers.

If the team's `odoo.prod.conf` sets `logfile`, Odoo writes its log there
instead of to the command's output, and the excerpt stays empty.

## GitHub webhooks (auto-deploy)

Point a GitHub webhook at `POST https://<server>/api/webhooks/github`
(content type `application/json`) with the team's webhook secret — shown in
the dashboard's Production tab, auto-generated with the first production.
Requests are authenticated by their `X-Hub-Signature-256` HMAC.

A push deploys only productions that match the repo + branch **and** have
`auto_update` enabled (`set_production_auto_update`). Dev environments are
never touched by webhooks. Failed webhook deploys roll back like any other.

## Backups

Two complementary layers (both to S3, enabled by `[backup]`):

**Snapshots — per-production restore.** A snapshot is a consistent triple:
`pg_dump` of the production database (streamed to S3, no temp disk), a
deduplicated filestore revision, and a manifest recording the deployed commit
sha. Taken daily (`snapshot_time`, per-production override via
`set_production_backup_schedule`), before every deploy, and on demand
(`snapshot_production`). Restore with:

```text
restore_production(name="erp", snapshot_id="20260711T020000Z", confirm="erp")
```

Restore is swap-based: the dump is restored into a scratch database and
swapped in by rename; the filestore is rebuilt beside the live one and
swapped in. A failed restore leaves the previous state untouched. If the
snapshot's commit differs from the checkout, the result warns you to
`rollback_production` to the matching commit.

**Restoring from a dev environment.** The same tool also promotes a dev
environment's data into an *existing* production — the counterpart of
`create_production(from_environment=…)` for productions that already live:

```text
restore_production(name="erp", from_environment="feature-x", confirm="erp")
```

The environment's database and filestore are copied while its Odoo is
briefly stopped (the environment is **not reset**), staged, and swapped in
with the same all-or-nothing mechanics as a snapshot restore. No
sanitization happens — the data goes *into* production — and no `[backup]`
configuration is required. The production's code checkout is not touched;
the result warns when the environment's commit differs from the deployed
one. Take a `snapshot_production` first if the current production data may
still be needed. `snapshot_id` and `from_environment` are mutually
exclusive.

The filestore engine (a clean-room, duplicacy-inspired content-defined
chunking store) deduplicates across daily revisions *and* across a team's
productions; retention (`keep`) is applied weekly with safe two-step fossil
collection.

**WAL-G — cluster disaster recovery.** Continuous WAL archiving plus daily
base backups of the whole production cluster. This is the "server burned
down" path. Because it rewrites **every team's** productions at once, it is
an operator action, not an MCP tool: run it from the server settings console
(**/admin → Cluster DR**) or the owner CLI:

```bash
oduflow restore-cluster --target-time "2026-07-10 12:00:00+00" --confirm RESTORE-CLUSTER
```

Either surface restores the **entire cluster** (every production database at
once — including auxiliary service databases created with `cluster="prod"`)
from the newest suitable base backup + WAL replay — optionally to a point in
time (empty `--target-time` replays the whole archive). The displaced data
directory is kept inside the Docker volume for manual cleanup. Because the
state lives in S3, a **fresh Oduflow server** with the same `[backup]`
section can resurrect the cluster the same way with the CLI command above.

`production_backup_status()` shows per-production snapshot state, WAL
archiver health (`pg_stat_archiver`), base backup inventory, and S3
reachability.

### WAL-G certificates and storage health

Oduflow writes a CA bundle alongside `walg.json` in
`<base_data_dir>/walg/` and configures WAL-G to read it through the existing
read-only `/etc/walg` directory mount. This works even when the PostgreSQL
image has no system CA bundle, and covers WAL uploads, base backups, and PITR
helpers. Restarting Oduflow refreshes these files for existing containers;
PostgreSQL does not need to be recreated for this fix.

The CA source is `AWS_CA_BUNDLE`, then `SSL_CERT_FILE`, then the server's
default CA file, with the boto3/botocore bundle as a fallback when there is no
default file. Set an explicit override in the Oduflow service environment for
a private certificate authority. Invalid overrides fail rather than disabling
TLS verification or replacing a working bundle.

The background WAL monitor checks storage list access using WAL-G **inside
production PostgreSQL, as the postgres user**. The dashboard's **WAL-G** health
chip and `/healthz` use its cached result. The
diagnostic command has a five-second timeout, with forced termination after
another two seconds. A TLS/access error degrades `/healthz` even when the
separate S3 check from the Oduflow server succeeds. List access does not prove
upload permissions, successful archiving, or a complete PITR chain. The backup
status API retains local archiver statistics when the remote inventory query
fails; inventory commands have a 30-second timeout.

### Production readiness at deployment

The production cluster uses the same image as the development cluster,
`[database].image` (default: the official `postgres:16`). WAL-G trusts the
persistent mounted CA bundle described above, so no custom PostgreSQL image is
needed. Existing containers are reused and keep their major version until
`[database].image` names a newer one. The upgrade then carries every production,
including its roles and passwords, into a new cluster and deletes the old
WAL-G archive, as described in
[Upgrading PostgreSQL](installation.md#upgrading-postgresql). Odoo 20 needs
PostgreSQL 16 or newer: creating a production with an Odoo 20 image, or
switching one to it, is refused on an older cluster.

With `[backup]` configured, provisioning and production start/restart/deploy
require a successful check **inside PostgreSQL as the postgres user**:

1. Check the actual disk and queue safety thresholds.
2. Check the mounted CA bundle, executable and credentials file, then use
   WAL-G to list storage, upload a unique probe, read it back, compare its
   content and delete the probe. The storage phase is limited to 30 seconds,
   with bounded cleanup/forced termination. The credentials need read, list,
   write and delete permissions for the probe under
   `<backup.prefix>/walg/oduflow-preflight/`; backup objects are not modified.
3. Enable the managed archive command and confirm PostgreSQL applied it.
4. Generate a restore point and switch WAL, then wait up to
   `upload_timeout + 75` seconds for that segment to be archived. Only then
   allow the production application to start. This confirms current archive
   delivery; a recoverable PITR chain additionally requires a base backup.

Step 4 is skipped for an already admitted production — `restart_production`,
a deploy or a rollback — when the sample from step 1 already proves archiving
is healthy: the managed archive command is active, archiving is not stalled,
and the storage listing in that same sample succeeded. A hung production stays
restartable during a brief storage outage instead of waiting minutes for a new
segment. Admitting a new production, or starting a stopped one, always runs
the full check.

An error blocks the operation and appears as **Last startup check** in the
WAL panel. It stops driving the overall status once a newer healthy sample
supersedes it; live sampling then reports the current state. It does not convert an existing archive command to `/bin/true` or
delete retained WAL. An old no-op command is changed to an empty, retaining
command before preflight; fresh generated configurations also retain WAL
until provisioning decides. Existing working archiving continues during a
preflight failure; the disk/queue guard remains responsible for emergency
shutdown. Development environments can still start if production is unready.

No package installation is performed inside PostgreSQL during deployment or
emergency recovery.

### WAL monitoring and automatic disk protection

PostgreSQL records every change in 16 MiB WAL segments, and WAL-G uploads each
finished segment to S3; that stream is what makes point-in-time recovery
possible. PostgreSQL cannot delete or recycle a segment until it has been
archived, so when uploads stop, WAL keeps accumulating on the same disk as the
production databases. The monitor watches two things: whether archiving is
making progress, and whether the disk will run out before it does.

The **PostgreSQL WAL** panel in Production is shared by every production and
team. Free space is measured on the actual WAL filesystem; space reserved for
root is excluded. A dedicated daemon samples
locally every 15 seconds, independently of backup jobs and dashboard polling;
the storage probe runs only after the local protection decision. Missing or
older-than-60-second samples are errors, never healthy results.

An empty queue is idle, not stalled. With a queue, lack of successful archive
progress triggers warning/error thresholds. A draining but old backlog stays
visible. WAL upload attempts have a hard timeout and return failure on timeout
or termination; PostgreSQL retains the segment and retries. Failed downloads
of the WAL-G executable never switch configured archiving to a success no-op.

#### Reading the WAL panel

The badge is `ok`, `warn`, `error`, `off` (archiving not configured, or
production PostgreSQL not provisioned) or `unknown` (the sample is stale, or
the browser could not fetch the status), always with a text summary such as
*Archiving is idle* (empty queue), *Archiving WAL*, or the reason for a
warning or error.

| Field | Meaning |
| --- | --- |
| **Queue** | Segments waiting for upload (`.ready` files in `pg_wal/archive_status`) and their total size. |
| **Queue stop limit** | `stop_queue_gb`. A queue this large trips protection even with ample free disk. A queue above `warn_queue_gb` only warns. |
| **Oldest waiting** | Age of the oldest segment still waiting for upload. |
| **No progress** | How long archiving has made no progress while a queue exists. Warns after `warn_after`, errors after `stall_after`. |
| **WAL on disk** | Total size of `pg_wal`, including already-archived segments that PostgreSQL keeps for reuse. With an empty queue, anything between `min_wal_size` and `max_wal_size` (512 MiB to a few GiB with the default tuning) is normal. |
| **Available to PostgreSQL** | Free space on the filesystem holding `pg_wal` (the WAL filesystem), as available to the non-root postgres user. The status warns below 15% of the filesystem. |
| **Safety reserve** | `stop_free_gb`. When free space reaches it, protection stops the cluster. Releasing protection requires `resume_free_gb`. |
| **Disk consumption** | How fast free space is shrinking: the larger of the five-minute average and the slope since the previous sample. It covers the whole filesystem, not just WAL. |
| **Estimated time to reserve** | `(available − reserve) / consumption`: how long until the reserve is reached at the current rate. |
| **Archived** | `pg_stat_archiver.archived_count`: segments archived since the statistics were last reset. |
| **Last archive** | Time of the last successful upload, in the browser's local time. It should keep advancing while WAL is generated. |
| **Failed attempts** | `pg_stat_archiver.failed_count`: failed `archive_command` runs. |
| **PG restarts** | Docker restart count of the shared PostgreSQL container. |
| **Current upload** | Shown only while an upload runs: the segment and its elapsed time. |
| **Last upload failed** | Shown only when the last `wal-push` exited non-zero: its exit code. Details are in the PostgreSQL logs. |
| **Storage probe** | How storage access was verified: a WAL-G list run inside production PostgreSQL (see above). |
| **Last startup check** | Result of the deployment preflight, when one has run. |
| **Sample** | When the shown data was collected, in the browser's local time. |

**Pause archiving** / **Resume archiving**, **Retry upload**, **Start
PostgreSQL recovery** and **Release protection** correspond to the `pause` /
`resume`, `retry`, `recover` and `release` actions below.

Defaults can be changed in TOML:

```toml
[production.wal]
upload_timeout = 120    # seconds; TERM, then KILL after another 5 seconds
warn_after = 120        # seconds without archive progress, with a queue
stall_after = 300
stop_free_gb = 2        # GiB available to postgres: safety reserve
resume_free_gb = 4      # recovery headroom; must exceed stop_free_gb
stop_within = 300       # estimated seconds until the reserve is reached
warn_queue_gb = 2       # GiB of unarchived WAL before warning
stop_queue_gb = 8       # GiB of unarchived WAL before protective stop
```

Protection is active whenever production hosting is enabled, even without
S3 backups or while archiving is paused. It trips as soon as free space
reaches the reserve. A prediction alone is not enough: consumption measured
between the two most recent samples must predict reaching the reserve within
`stop_within`, and that prediction must hold for two consecutive samples.
`df` covers the whole filesystem, so a finished burst from an unrelated
consumer never stops the cluster, while a continuing leak still does.
It also trips when the unarchived queue reaches
`stop_queue_gb`, even on a large disk. Protection saves a persistent latch,
disables Docker restart policies, stops managed production applications,
then stops the shared PostgreSQL container. Failed stops are reported and retried. The latch blocks production
starts, deploys, restores and new backup jobs, including after Oduflow or
Docker restarts. Protection can interrupt in-flight jobs; it does not wait for
their locks while the disk fills.

Set the reserve for peak write volume and shutdown time. The monitor requires
Oduflow and Docker to be responsive; it cannot guarantee protection against
arbitrarily fast disk exhaustion or other host processes filling the same
filesystem after PostgreSQL stops. Monitor `/healthz` externally as well.

The server settings console (**/admin → Cluster DR**) and the owner CLI
(`oduflow wal <action> --confirm ALL-PRODUCTIONS`) offer these cluster-wide
actions:

| Action | Effect |
| --- | --- |
| `pause` | Retains unarchived WAL and interrupts the active upload. The queue can grow; disk protection stays active. |
| `resume` | Refreshes WAL-G config/certificates and resumes archiving. Does not restart stopped PostgreSQL. |
| `retry` | Terminates only the current `wal-push`, including a legacy upload predating the timeout wrapper; PostgreSQL retries it. |
| `recover` | Requires recovery headroom; starts only PostgreSQL, keeping applications stopped and rejecting network database connections. Local maintenance and outbound S3 access remain available. Requests a WAL switch to verify real archiving. |
| `release` | Requires recovery headroom, a safe consumption rate, working storage access, and a successful archive after recovery began. Restores normal connections and restart policies; applications remain stopped until explicitly started. |

Use `production_wal_status()` (MCP), `GET /api/productions/wal-status`
(dashboard) or `oduflow wal status` for cached read-only diagnostics;
mutating actions are operator-only — the `/admin` console or
`oduflow wal <action> --confirm ALL-PRODUCTIONS`. Environment-scoped access
is excluded even from the read-only status.
Queue size warns at `warn_queue_gb` and protects at `stop_queue_gb`. During
fenced PostgreSQL-only recovery the queue may exceed the stop threshold so
it can drain; disk pressure still stops PostgreSQL. Releasing protection
requires the queue to fall below `warn_queue_gb` as well as free-space headroom
and confirmed archive progress.

The monitor's latch is stored in `<base_data_dir>/wal_guard.json`; do not
delete it to bypass recovery checks. Recovery temporarily replaces the managed
`PGDATA/pg_hba.conf`, preserving the original alongside it, and restores it on
release. Custom HBA paths require operator intervention.

### Recovering from a full WAL disk

Production tables and WAL live together in the `oduflow-prod-db-data` Docker
volume, unlike the per-team tablespaces used for development. Inspect free
space on the filesystem containing that volume, including the space available
to the non-root postgres user. `max_wal_size` is not a hard limit when WAL
cannot be archived.

If logs report `No space left on device` and WAL-G reports an unknown
certificate authority:

1. Stop application writers to reduce additional WAL generation. Free several
   GiB from known disposable files **outside PostgreSQL data**, or expand the
   volume's filesystem. Never manually delete files from `pg_wal`.
2. Deploy the fix and restart Oduflow to refresh the mounted CA bundle and
   WAL-G configuration. If protection is active, use **Resume archiving** if
   paused, then **Start PostgreSQL recovery** after sufficient space is free.
   No PostgreSQL recreation is required.
3. Check the WAL-G health result and PostgreSQL logs. Confirm actual progress:
   `pg_stat_archiver.archived_count` increases and the queue of `.ready` files
   in `pg_wal/archive_status` drains. A successful list probe alone is not
   enough. An already-running WAL-G attempt uses its old configuration until
   it exits; **Retry upload** interrupts it without discarding the segment.
4. Allow PostgreSQL to recycle eligible WAL itself and verify free space
   recovers before releasing protection and explicitly starting the desired
   applications. Other retention requirements, such as replication slots,
   can also keep WAL on disk.

Do not switch `archive_command` to `/bin/true` to drain the queue: it reports
success without saving the segments and can break PITR continuity.

## Copying production data to dev

Productions are seeded from templates; the same road runs backwards, so a
developer can reproduce a bug on real data:

```text
save_production_as_template(prod_name="erp", template_name="erp-2026-09")
create_environment(branch="bugfix-invoice", from_production="erp")
```

Both dump the production database out of the production cluster with a
consistent `pg_dump` and restore it into the **dev** cluster, and snapshot the
production filestore as the template's baseline. **The production keeps
serving** — nothing is stopped or modified on its side.

`create_environment(from_production=…)` routes through one managed template per
production, `prod-<name>`, published on first use and reused afterwards; refresh
it with `save_production_as_template(name, "prod-<name>", overwrite=True)`. See
[Create a Template from Production](templates.md#create-a-template-from-production)
and [Creating an Environment from Production](environments.md#creating-an-environment-from-production).

!!! danger "The copy is unsanitized until an environment is created"
    The template carries real customer data and credentials. Environments made
    from it are neutralized and run the repository's sanitize scripts by
    default; the template itself is production-confidential.

**`allow_copy_to_dev_mcp`** (default `true`, set at `create_production`) gates
**new copies**: when it is `false`, an MCP/CLI agent asking for either tool gets
a refusal — and no MCP tool can turn the flag back on. It is a gate on *agents*,
not on people: the dashboard's Production tab is never gated and is the only
place the flag can be toggled, so an agent cannot re-enable its own access.
Productions created before the flag existed behave as `true`.

The flag does **not** revoke a copy that already exists. A `prod-<name>` (or
any) template published from the production stays usable through
`create_environment(template_name=...)` like every other template — its data is
neutralized on the way into each environment. The one thing agents lose is the
raw form: `sanitize=false` on a template whose `source_production` has the flag
off is refused. To withdraw the data itself, `delete_template` the copy.

## Health

`GET /healthz` (public, no auth, no secrets) returns 200 when healthy and
503 when degraded — point your uptime monitor at it. Checks: dev PostgreSQL,
production PostgreSQL, Traefik, S3 (HeadBucket), disk usage (warn at 85%),
and productions flagged unhealthy by a failed rollback. The dashboard's
status bar shows the same checks as chips.

## Deleting a production

`delete_production` (or **Delete** in the dashboard) removes the container and
the registry record, but **keeps the database and the workspace** (filestore,
repo, deploy history) on disk — productions are precious, deleting bytes is
opt-in. Pass `drop_database=true` over MCP/CLI to remove everything at once.

Kept leftovers are *tombstoned* (a `deleted.json` marker in the workspace) so
they can be reclaimed later:

- **Deferred purge** — set `[lifecycle] prod_purge_hours = N` in
  `oduflow.toml` and the background sweep permanently purges the leftovers
  (database, PostgreSQL role, workspace) N hours after the deletion. `0`
  (default) keeps them forever.
- **Immediate purge** — `oduflow cleanup --purge-deleted-productions`
  lists tombstoned leftovers; add `--force` to purge them now, regardless of
  age.

Only tombstoned leftovers are ever purged: a workspace without the marker is
presumed alive and is never touched (`oduflow cleanup` skips the whole
`prod-*` namespace for the same reason).

A deleted production's name stays taken while its leftovers are kept:
`create_production` refuses a name whose database is still in the production
cluster, or whose workspace is still on disk. Creating over a kept workspace
would collide with its repository and mix the old filestore and deploy history
into the new production. Copy out anything you still need, purge the
leftovers, and create again.

A failed `create_production` rolls back what it created. If the rollback
cannot drop the new database, it tombstones the attempt, so the same purge
reclaims it. The refusal names the remedy that applies: purge for tombstoned
leftovers; for unmarked ones (a production deleted before tombstones existed,
for example), a manual `DROP DATABASE` and removal of the workspace, after you
check that they hold nothing you need.

## MCP tool reference

| Tool | Purpose |
|---|---|
| `create_production` | Provision a production (optionally from a template) |
| `list_productions` / `get_production_info` | Status, deployed commit, history, backups |
| `update_production` | Deploy latest commits with auto code rollback |
| `rollback_production` | Manual code rollback to a commit |
| `production_deploys` | Deploy history |
| `production_logs` | Container logs |
| `start_production` / `stop_production` / `restart_production` | Lifecycle |
| `set_production_auto_update` | Toggle webhook auto-deploy |
| `reconfigure_production` | Change domain/image/branch/repo/extra addons; recreates the container |
| `set_production_odoo_conf` | Per-production odoo.conf overrides on top of auto-tuning |
| `snapshot_production` / `list_production_snapshots` | Snapshots to S3 |
| `restore_production` | Restore DB + filestore from a snapshot or a dev environment |
| `set_production_backup_schedule` | Per-production snapshot time / off |
| `production_backup_status` | Backup posture (snapshots + WAL-G + S3) |
| `save_production_as_template` | Publish the production's DB + filestore as a dev template (unsanitized) |
| `prune_production_backups` | Apply retention now |
| `delete_production` | Remove (database/files kept unless `drop_database`) |

Cluster-wide disaster recovery (whole-cluster PITR) and WAL archiving control
are deliberately **not** MCP tools: they rewrite or pause every team's
productions at once, so they live on the operator surfaces only — the server
settings console (**/admin → Cluster DR**) and the owner CLI
(`oduflow restore-cluster`, `oduflow wal`).
