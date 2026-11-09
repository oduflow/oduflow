# 0083 — Daemon-visible mirror for bind-mounted configs

**Status:** Adopted
**Type:** Runtime / deployment model
**First introduced:** 2026-11-09
**Key code:** `docker_ops/system_ops.py` (`sync_dev_pg_conf_mirror`,
`sync_prod_pg_conf_mirror`, `sync_traefik_dynamic_mirror`, `_mount_source_differs`,
and the mount-drift branches of `_ensure_pg_container` / `_ensure_traefik` /
`_ensure_prod_pg_container`), `health.py` (`_check_self_mounts`,
`warn_if_data_dir_unreachable`), `server.py` (`_run_retune_postgres`,
`_ensure_initialized`)

## Context

Oduflow has two filesystem roots with different audiences. The **config
directory** (`/etc/oduflow`, or `~/.oduflow/conf` without root) holds what an
operator reads and edits: `oduflow.toml`, the tuned `postgresql.conf` /
`postgresql-prod.conf` with their `# KEEP` contract, and the Traefik
`traefik-dynamic/` directory that accepts operator drop-ins. The **data
directory** (`[storage].data_dir`, `/srv/oduflow` by default) holds
everything the managed containers touch: filestores, tablespaces, the
pg_exchange staging area. Three config-dir artifacts were nevertheless
bind-mounted directly into containers — the two PostgreSQL configs as single
files, the Traefik dynamic directory as a whole.

That worked only as long as the Oduflow process and the Docker daemon shared a
filesystem. Oduflow also runs as a container itself (Docker-out-of-Docker,
[[0018-onboarding-stdio-default-auto-init]] made the image a first-class
install path): the host daemon's socket is mounted in, and every bind mount
Oduflow requests is resolved by **that daemon on the host**, not inside the
Oduflow container. The daemon has no `/etc/oduflow`, and Docker's behaviour
for a missing bind source is to silently create an empty directory. The
result was a crash-looping `oduflow-db` ("input in flex scanner failed" —
PostgreSQL parsing a directory as its config), a Traefik with no per-team
routes, and a production cluster with the same latent fault. The data mounts
kept working only because `data_dir` is configurable and the operator had
already pointed it at a daemon-visible path. The config dir was neither
configurable nor daemon-visible, and nothing told the operator why things
were broken.

Two forces shaped the fix. The operator-facing files had to stay where they
are: documentation, `retune-postgres`, the staleness warnings and the `# KEEP`
never-rewrite contract all name `/etc/oduflow/postgresql.conf`. And the fix
had to be a property of the system rather than a documented foot-gun, since
the failure mode is silent by construction.

## Decision

**Containers never mount the config directory. They mount a derived,
daemon-visible mirror under the data directory.** `base_data_dir` was already
required to be a path the daemon can see — every environment depends on it —
so it is made to carry the mounted configs too. `<data_dir>/conf-mirror/`
holds a copy of `postgresql.conf`, `postgresql-prod.conf` and the whole
`traefik-dynamic/` directory (generated file and operator drop-ins alike).
The originals in the config dir remain the single source operators edit; the
mirror is a build artifact, refreshed by the code paths that create or start
the consuming container and by the one command that regenerates a source
(`retune-postgres --apply`). Each mirrored PostgreSQL config carries a header
saying it is a copy and naming the file to edit.

Containers created under the old layout are healed according to their blast
radius: the shared dev cluster and Traefik are recreated automatically when
their mount source is found to differ from the mirror (both keep all state in
volumes or data-dir binds, and the dev cluster is recreated on the image it
was running so a repair can never double as a major upgrade); the production
cluster is only *reported*, with the exact operator step, because bouncing
every production unannounced is worse than running a little longer on a
working legacy mount.

Finally, the class of failure is made visible: when Oduflow detects it runs in
a container, it inspects its own mounts and warns — at startup and on
`/healthz` — if `data_dir` is not a bind whose host source equals its
container destination. A named volume, a differing host path and a data dir
in the writable layer all break every mount Oduflow hands out, and all used to
fail silently.

## How it works (macro)

Startup still writes the originals exactly as before (tuned PG configs under
the `# KEEP` contract, `traefik-dynamic/oduflow.yml` on every start). Right
before each container is ensured, its source is copied into the mirror —
world-readable, rewritten in place so an already-mounted file keeps its
inode — and the container is created with the mirror as mount source. On an
existing container, the inspect output's `Mounts` is compared against the
mirror path; unreadable mount data is treated as "no drift" so a mock or a
truncated inspect never triggers a recreate. The Traefik mirror is a full
sync (copy everything, prune what vanished), which moves drop-in pickup from
"within a second" to "on the next Oduflow start" — the price of not mounting
the config dir, documented as such.

## Consequences

- Running Oduflow in a container now needs exactly one mount rule — the data
  directory must be a host directory at the same path on both sides — and
  violating it produces a warning that names the mount and the fix instead of
  a crash-looping database. The config dir may be a named volume.
- Operators lose nothing: same files, same paths, same `retune-postgres`
  workflow; the mirror files identify themselves as copies.
- Traefik drop-ins require an Oduflow restart to take effect; the docs say so.
- `oduflow-db` and Traefik containers from older installs are recreated once
  on first start after upgrade (a restart, not data loss); production
  clusters need an operator-scheduled recreate, announced in the log.
- Any future config that must reach a container should go through the mirror
  rather than add a new config-dir bind mount.

## History

- 2026-11-09 — Introduced on branch `dood-config-mounts` after a macOS Docker
  Desktop deployment of the Oduflow image hit the `oduflow-db` crash loop.
