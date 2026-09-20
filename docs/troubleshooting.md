# Troubleshooting

Recovery playbooks for the operational issues most likely to hit a self-hosted
Oduflow deployment, organized by symptom. Commands assume the default data
directory `/srv/oduflow` and team `1`; adjust the paths for your setup.

Oduflow runs as **root** (it needs the Docker socket, `iptables`, host-side
`chown` and XFS project quotas). All commands below are run on the host.

---

## The server won't start / keeps restarting

`systemctl status oduflow` shows the service failing and restarting in a loop,
and `journalctl -u oduflow` ends in a traceback.

The most common cause is the **shared PostgreSQL container not being ready** when
Oduflow initializes. On startup Oduflow waits for `oduflow-db` with `pg_isready`;
if that container is not running — still starting, or crash-looping — the wait
now retries and, on timeout, fails with a clear message pointing at its logs
(older versions crashed with a raw `docker.errors.APIError: 409`).

```bash
# Is the DB container actually up?
docker inspect oduflow-db --format '{{.State.Status}} restarting={{.State.Restarting}} restarts={{.RestartCount}} exit={{.State.ExitCode}}'

# Why is PostgreSQL dying? (the decisive check)
docker logs --tail 200 oduflow-db

# Very common underlying cause — the disk is full:
df -h /srv/oduflow
```

The Oduflow version is irrelevant here — the blocker is Docker/container state,
so upgrading or downgrading Oduflow will not help until `oduflow-db` stays `Up`.
See [Disk full](#disk-full) below. Once the DB container is healthy:

```bash
systemctl restart oduflow
journalctl -u oduflow -f
```

---

## The server is "active" but nothing responds after a system upgrade

`systemctl status oduflow` says `active (running)`, yet the dashboard, `/mcp`
and `/healthz` all time out, and `journalctl -u oduflow` stops a few lines into
startup — typically right after `Initializing system` — with no error.

The cause is a Docker call that never returns. Startup (migrations,
`init_system`, quotas) runs **before** the HTTP listener binds, and docker-py
disables the socket timeout while reading exec output, so a daemon that is
restarting underneath Oduflow can block a readiness probe indefinitely. The
classic trigger: `unattended-upgrades` upgrades a library, and `needrestart`
restarts `oduflow.service` in the same batch as `containerd` and `docker`.

Current versions defend on three fronts, all applied by re-running
`oduflow systemd-install`:

- A **startup watchdog** aborts the process when startup emits no log line for
  15 minutes, dumping every thread's stack to the journal first, so systemd
  restarts the service instead of leaving it wedged.
- The **unit** is ordered after `containerd.service`, uses `Restart=always`, and
  has no start-rate limit.
- A **needrestart override** (`/etc/needrestart/conf.d/oduflow.conf`) keeps
  Oduflow out of automatic restart batches.

Immediate recovery is a plain restart — it completes in seconds once the daemon
is settled:

```bash
systemctl restart oduflow
journalctl -u oduflow -f

# Confirm the defenses are in place on this host:
systemctl cat oduflow | grep -E 'After=|Restart='
cat /etc/needrestart/conf.d/oduflow.conf
```

If the journal contains a `Startup made no progress for …s` line followed by
thread stacks, that is the watchdog reporting where the start hung — include it
in any bug report.

If a start is legitimately slower than the window (a very slow link pulling
images, say) and the watchdog keeps cutting it short, widen it — or set `0` to
switch it off — via the environment, e.g. in a
`systemctl edit oduflow` drop-in:

```ini
[Service]
Environment=ODUFLOW_STARTUP_STALL_SECONDS=3600
```

---

## Disk full

A full disk cascades: PostgreSQL cannot write and crash-loops, new
environments fail to provision, and Odoo reports "did not become ready".

```bash
df -h /srv/oduflow          # bytes
df -i /srv/oduflow          # inodes — can be exhausted even when bytes are free
```

Find what is using space. Note that **each environment always copies its
database** (a PostgreSQL `CREATE DATABASE ... TEMPLATE` is a full copy — a few GB
per env is normal and unavoidable), while the much larger **filestore is shared
via an overlay** and should cost only a small delta per env (see
[Overlay filestore](#overlay-filestore)):

```bash
# Per-environment on-disk cost (upper layer + repo + sessions; the shared
# template filestore is NOT counted here):
du -sh /srv/oduflow/team_1/workspaces/*/filestore_upper
du -sh /srv/oduflow/team_1/workspaces/*/repo

# Templates (the shared lower layers + dumps):
du -sh /srv/oduflow/team_1/templates/*
```

To reclaim space, delete unused environments or templates through Oduflow
(`delete_environment` / `delete_template`, the dashboard, or `oduflow call`) so
databases, overlays and workspaces are torn down cleanly. **Do not** `rm -rf` a
template directory by hand while environments still use it — see
[Deleting a template fails](#deleting-a-template-fails).

---

## An environment won't start / Odoo "did not become ready"

Work down this checklist:

```bash
# 1. Is the shared DB up and accepting connections?
docker exec oduflow-db pg_isready -U odoo

# 2. Is the Odoo container running, and what does it say?
docker ps -a --filter name=<env-slug>
docker logs --tail 200 oduflow-1-<env-slug>-odoo

# 3. Is the filestore mounted and readable inside the container?
docker exec oduflow-1-<env-slug>-odoo \
  sh -c 'ls /var/lib/odoo/.local/share/Odoo/filestore/*/ 2>&1 | head'
```

If step 3 reports `Transport endpoint is not connected` or an empty filestore,
the overlay mount is broken — see [Overlay filestore](#overlay-filestore).
Otherwise the failure is usually inside Odoo (a module install/upgrade error);
read the container logs.

### PostgreSQL reports `no pg_hba.conf entry`

An error such as:

```text
FATAL: no pg_hba.conf entry for host "172.20.0.3", user "u_1_main",
database "postgres", no encryption
```

means Docker networking already works: the client reached PostgreSQL, but the
active HBA file has no matching host rule. Oduflow normally self-heals this on
startup by reading the actual Docker IPAM subnets and reconciling its marked
`ODUFLOW MANAGED NETWORKS` block in the active file. It then reloads PostgreSQL
and validates `pg_hba_file_rules`; a failed candidate is rolled back.

The generated rules use `md5` while any role still holds a pre-PostgreSQL-14
md5 verifier, and `scram-sha-256` once every role has migrated. `md5` is not a
downgrade: PostgreSQL performs a SCRAM exchange whenever the stored verifier is
SCRAM. Reset the affected passwords under `password_encryption =
scram-sha-256` to move an old cluster over.

Restart Oduflow and inspect its startup log first. If reconciliation fails, the
message names the invalid subnet, unsupported authentication method, existing
HBA parse error, or file operation that blocked it. These read-only commands
show the source state without guessing a Docker subnet:

```bash
docker exec oduflow-db psql -U odoo -d postgres -Atc \
  'SHOW hba_file; SHOW password_encryption;'
docker exec oduflow-db psql -U odoo -d postgres -P pager=off -c \
  'SELECT line_number, type, address, auth_method, error FROM pg_hba_file_rules ORDER BY line_number;'
docker network inspect oduflow-1-net --format '{{json .IPAM.Config}}'
```

Do not add a fixed `172.x` rule or replace the whole HBA manually. Docker may
allocate a different subnet after a network recreate, and replacing the file
can discard local or replication rules that PostgreSQL needs.

---

## An environment runs out of database connections

Two different limits produce two different errors — read the message before
changing anything.

**`psycopg2.pool.PoolError: The Connection Pool Is Full`** — the *environment*
hit its own `db_maxconn` (default `8`). Raise it for that container and
restart it:

```bash
# 1. Read the current config
oduflow call read_file_in_odoo '{"env_name": "feature-login", "path": "/etc/odoo/odoo.conf"}'

# 2. Write it back with a higher db_maxconn (the write replaces the whole file)
oduflow call write_file_in_odoo '{"env_name": "feature-login", "path": "/etc/odoo/odoo.conf", "content": "[options]\n...\ndb_maxconn = 16\n", "user": "odoo"}'

# 3. Odoo reads the config only at startup
oduflow call restart_environment feature-login
```

The edit lives in the container's writable layer: it survives restarts and
`pull_and_apply`, but is lost when the container is recreated
(`update_environment`) or when the repository's `.oduflow/odoo.conf` changes
and is reapplied.

**`FATAL: sorry, too many clients already`** — the *shared PostgreSQL* hit
`max_connections`. Raising `db_maxconn` makes this worse. Stop idle
environments, or raise `max_connections` in the cluster config and restart it:

```bash
docker exec oduflow-db psql -U odoo -c \
  "SELECT count(*), datname FROM pg_stat_activity GROUP BY datname ORDER BY 1 DESC;"
$EDITOR /etc/oduflow/postgresql.conf     # or ~/.oduflow/conf/postgresql.conf
docker restart oduflow-db
```

To change the default for **all** of a team's environments instead of one
container, edit the team's `odoo.conf` in its data directory
(`<data_dir>/team_<id>/odoo.conf`, seeded from the bundled template at init).
Existing containers keep their current config until they are recreated
(`update_environment`) or their config is reapplied. A single repository can
override everything for its own environments with `.oduflow/odoo.conf`.

Budget the two limits together: `max_connections` must cover `db_maxconn` ×
the number of environments you expect to run at once, plus headroom for
productions and maintenance connections.

---

## Agent Chat: Claude returns `401 Invalid bearer token`

The ACP session may open successfully and fail only on the first prompt:

```text
Failed to authenticate. API Error: 401 ... Invalid bearer token
```

This is a Claude provider credential failure, not an Oduflow MCP-token failure.
Claude authentication is selected in this order:

1. `CLAUDE_CODE_OAUTH_TOKEN` (subscription setup token)
2. `ANTHROPIC_API_KEY` (Console API billing)
3. the interactive `/login` saved on the team's persistent agent home volume

A configured setup token or API key overrides the interactive login. Oduflow
does not automatically fall back after an authentication error because doing so
could silently switch the account or billing method.

To keep subscription authentication, generate a fresh token on a trusted
machine while signed in to the intended Claude account:

```bash
claude setup-token
```

Replace `CLAUDE_CODE_OAUTH_TOKEN` under `[team.<id>.agent_env]` in
`oduflow.toml`. In a single-team deployment, the Oduflow systemd service may
instead supply this variable through its server environment; update the source
that is actually in use. Do not print the token with `docker inspect`, `env`, or
diagnostic shell commands.

Restart Oduflow so the changed config hash recreates the agent container. Its
home and workspace volumes are persistent, so conversations, login state, and
checkouts survive:

```bash
systemctl restart oduflow
journalctl -u oduflow --since "5 minutes ago" \
  | grep -E 'Agent config changed|Claude auth:'
```

The log should report subscription auth. Send a real Agent Chat prompt to
verify the new token; local auth-status output alone does not prove that
Anthropic accepts it.

To use interactive authentication instead, remove both
`CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` from the team config and, for
single-team deployments, from the server environment. Restart Oduflow, open
Agent CLI, run `/login`, complete sign-in, and reopen Agent Chat.

The separate `$/ping` "Method not found" line is harmless adapter noise and is
not the cause of the `401`.

---

## Overlay filestore

Large template filestores are shared with `fuse-overlayfs` instead of copied.
Per environment, under `/srv/oduflow/team_1/workspaces/<env-slug>/`:

| Path | Role |
|------|------|
| `filestore` | the **merged** mountpoint (bind-mounted into the container) |
| `filestore_upper` | this env's **own** writes (the only real disk it adds) |
| `filestore_work` | fuse-overlayfs work dir (kept tiny) |

The **lower** (read-only base) layer is the template's filestore at
`/srv/oduflow/team_1/templates/<template>/filestore`, shared by every
environment created from that template.

Inspect the mounts and sizes:

```bash
# Active overlay mounts and their lower layers:
grep fuse-overlayfs /proc/mounts

# The upper layer should be small; the merged view shows the full tree
# (lower + upper) and will look ~template-sized — that is expected:
du -sh /srv/oduflow/team_1/workspaces/<env-slug>/filestore_upper   # small = healthy
du -sh /srv/oduflow/team_1/workspaces/<env-slug>/filestore         # ~= template size
```

### A broken mount (`Transport endpoint is not connected`)

The `fuse-overlayfs` process for a mount died. Two ways that happens:

- **Oduflow runs in Docker and was restarted.** The daemon lives in the Oduflow
  container's PID namespace and dies with it, while the mount itself lives in
  the host's mount namespace (it has to — Odoo containers bind-mount the merged
  path, which Docker resolves on the host) and survives. So *every* overlay
  environment goes stale at once on a restart. Running Oduflow directly on the
  host, the daemon is detached from the Oduflow process and a restart leaves it
  alone.
- **The daemon was killed** — an OOM kill, or the disk filling up.

Oduflow repairs this by itself: on every start it detects stale overlays,
detaches them, remounts each against its template's lower layer **keeping the
environment's own `filestore_upper` deltas**, and restarts the affected Odoo
containers (required — a running container's bind mount still points at the
dead mount). Recovery fails closed: Oduflow does not detach a mount unless the
container is confirmed stopped, does not restart the container unless the new
overlay is confirmed live, and always reuses the existing upper layer even if
the template's default mode was later changed to copy. The `/healthz` endpoint
and the dashboard's `OVERLAY` chip report stale or unexpectedly absent overlays;
full filesystem paths stay in server logs. An environment whose template is
gone is logged for manual recovery rather than touched.

Note that a stale mount reads as *absent*, not as *mounted*: `stat` on the
mountpoint fails with `ENOTCONN`, so `ls`, `os.path.ismount()` and
`os.path.isdir()` all behave as if nothing is there.

To detach one by hand — remount then happens through Oduflow (`restart` the
server, or `update_environment`):

```bash
umount /srv/oduflow/team_1/workspaces/<env-slug>/filestore \
  || umount -l /srv/oduflow/team_1/workspaces/<env-slug>/filestore
```

### fuse-overlayfs prerequisites

On Linux, Oduflow auto-installs `fuse-overlayfs` on first launch when it starts
as root on a Debian/Ubuntu host. If it is still missing (non-root, non-Debian, or
no network), install it by hand. On macOS the binary is never needed — overlays
fall back to a plain copy automatically.

```bash
which fuse-overlayfs          # install: sudo apt install fuse-overlayfs
ls -l /dev/fuse               # must exist (present by default on Ubuntu)
```

`fuse-overlayfs` is mounted with `allow_other` so the Odoo container's (non-root)
user can read it. Running Oduflow **as root** (the supported setup) needs no
further configuration. Only when running Oduflow as a **non-root user** must you
uncomment `user_allow_other` in `/etc/fuse.conf`.

!!! note "AppArmor `fusermount3` on Ubuntu 24.04+ (historical)"
    Older Oduflow unmounted overlays via the setuid `fusermount` helper, which
    the `fusermount3` AppArmor profile on Ubuntu 24.04+ **denies**, forcing a
    lazy fallback. Oduflow now unmounts with a direct root `umount` (not mediated
    by that profile), so this no longer affects root deployments. If you run a
    non-root/rootless setup and hit `apparmor="DENIED" ... fusermount3`, allow it
    with a local override — add `umount /srv/oduflow/**,` to
    `/etc/apparmor.d/local/fusermount3` and run `apparmor_parser -r
    /etc/apparmor.d/fusermount3`.

---

## Deleting a template fails

```
Cannot delete template 'X': used by environments: a, b. Delete those environments first.
```

This is intentional. A template's filestore is the overlay **lower layer** for
every environment built from it; deleting the template would pull the base out
from under those live overlays and break them. Delete the listed environments
first (or keep the template). The same guard applies to renaming a template.

---

## A brand-new environment fails its first upgrade

An environment is a *new* database cloned from a template plus *your* branch's
code. The template database is a snapshot of some branch at some commit, so the
two can drift in either direction — and the failure only surfaces on the first
`-u`.

```bash
# What the template was snapshotted from:
oduflow call list_templates
# → - prod: DB=loaded, ..., Source=prod @ c0ffee12 @ snapshot 2026-08-01
```

`create_environment` compares that commit with the branch checkout and reports
the drift in its response:

* **"Code is behind the template database"** — your branch does not contain the
  snapshot commit. The database already holds views and records written by newer
  code, so upgrading the older branch fails validation (typically a `ParseError`
  on a view referencing a method your branch does not have). **Merge the
  template's source branch** into yours, push, then `pull_and_apply`.
* **"Code is ahead of the template database"** — the database predates your
  code. Apply the drift explicitly with the arguments reported by Oduflow, for
  example `pull_and_apply(install="new_module", upgrade="a,b,c")`, listing
  dependencies before dependents. Brand-new modules need `install=`; existing
  modules with schema/data drift need `upgrade=`. Modules whose manifest version
  was not bumped are never upgraded automatically, so a `column ... does not
  exist` or a missing external ID means "find the module that owns it and add it
  to `upgrade=`".

Templates created before Oduflow recorded provenance — and templates imported
from a running Odoo — have no commit to compare against, so no drift is
reported. That is not an error; the rule above still applies, you just have to
apply it by hand.

!!! warning "Recreating the environment does not fix it"
    The template is unchanged, so the same drift comes straight back — and the
    environment's data is gone. Reconcile with a merge or an explicit
    install/upgrade action.

---

## `BusyError`: "Another operation ... is in progress"

The message names the holder and its age:

```
Another operation on environment 'main' (pull_and_apply, running for 4m12s) is in progress.
```

Locks are held for exactly as long as the operation runs and are released when
it finishes. A long-running install, upgrade or test run legitimately holds one
for minutes — including when the *client* gave up waiting and timed out, because
the work continues server-side. Wait for it to finish and retry; restarting or
recreating the environment interrupts real work instead of clearing anything.

Background operations take the same locks and are named too: `auto-stop`,
`auto-delete`, `scheduled backup`, `webhook deploy`.

---

## The Odoo web client loads blank

The page is served, the JS bundles arrive, the browser console is clean — and
nothing renders. This is an Odoo-side asset/registry problem (commonly a custom
systray or a legacy widget that never resolves during `startWebClient`), not an
environment provisioning problem: restarting the container or rebuilding assets
does not help, and headless browser tooling tends to hang on such a page.

Verify server-side instead — it works normally while the web client does not:

```bash
oduflow call run_odoo_shell '{"env_name": "my-branch", "python_code":
  "print(env[\"res.partner\"].fields_get([\"name\"]).keys())", "auto_commit": false}'
oduflow call run_odoo_tests '{"env_name": "my-branch", "modules": "my_module"}'
```

Then narrow the failing asset with `get_environment_logs` and by disabling the
suspect module's assets.

---

## Reporting a bug or sending feedback

If none of the above helps — or Oduflow itself is at fault — file an issue on
[github.com/oduflow/oduflow](https://github.com/oduflow/oduflow/issues).

Three ways to get there, all producing a prefilled issue form:

- **Dashboard** — the **Feedback** action in the header. Pick the kind (bug,
  feature request, feedback), describe it, and press *Open on GitHub*.
- **Coding agent** — ask your agent to report it; the `report_issue` MCP tool
  returns the same prefilled link for you to open.
- **CLI** — `oduflow call report_issue '{"kind": "bug", "details": "..."}'`.

In every case Oduflow only *builds the link*: it holds no GitHub credentials
and never files anything on your behalf. You submit it from your own GitHub
account and can edit it first — which matters, because the report reaches
maintainers under your name and stays public.

Attached automatically: Oduflow version, Python version, platform, transport
and routing mode. Never attached: hostnames, team names, repository URLs,
branch or database names. Add logs yourself where they help, and check them for
secrets before submitting.
