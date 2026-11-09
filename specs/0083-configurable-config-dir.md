# Configurable config directory for Docker-out-of-Docker bind sources

**Status:** Accepted | **Type:** Runtime / deployment model | **First introduced:** 2026-11-09

**Key code:** `settings.py` (`resolve_etc_dir`, `validate_etc_dir`,
`_resolve_etc_dir` as the first-run probe), `config_schema.py`
(`[storage].etc_dir`, LOCKED), `docker_ops/system_ops.py` (`_resolve_conf`,
`_ensure_pg_container`, `_ensure_prod_pg_container`, `_ensure_traefik`),
`health.py` (`_self_mount_verdict`, `warn_if_dirs_unreachable`),
`templates/dashboard.html` (`MOUNTS` chip)

## Context

Oduflow can run inside a container that drives the host's Docker daemon
(Docker-out-of-Docker). Every bind mount it requests is resolved by that
daemon on the **host**, not by the Oduflow process. The config directory was
hard-coded to `/etc/oduflow` (or `~/.oduflow/conf`) — a path of the Oduflow
*process* — and three things were bind-mounted straight from it:
`postgresql.conf` into `oduflow-db`, `postgresql-prod.conf` into the
production PostgreSQL container, and `traefik-dynamic/` into Traefik. When
that path does not exist on the host, Docker silently creates an empty
directory in its place. PostgreSQL then parses a directory as its config and
crash-loops (`input in flex scanner failed at file
"/etc/postgresql/postgresql.conf"`); Traefik watches an empty directory and
per-team routing never loads.

The data directory never had this problem, for one reason only:
`[storage].data_dir` is configurable, so operators already point it at a
daemon-visible path. The config directory had no such knob. The files read
in-process (`oduflow.toml`, `license.key`, branding, `odoo.conf`) were never
affected.

## Decision

Make the config directory **configurable, symmetric with `data_dir`**, and
nothing more:

- `[storage].etc_dir` is the explicit override, registered as a **locked**
  key: changing it on a live system would leave every existing container
  mounting the old path, so it is a Raw-TOML edit followed by a deliberate
  recreate, not a console toggle.
- The **default is the directory of the `oduflow.toml` that actually
  loaded**. For both lookup locations this is exactly the old answer, so
  existing installations are untouched; and `ODUFLOW_TOML=<host-visible
  path>` relocates the config directory on its own. The old writability probe
  survives only as the **first-run fallback**: bootstrap has no loaded file to
  take a directory from, so it still writes the first `oduflow.toml` to
  `/etc/oduflow` when writable, else `~/.oduflow/conf` — after which the
  loaded-file default yields that same directory.
- Startup **validates** that the resolved directory exists and is writable
  (Oduflow generates `postgresql.conf` into it) and fails with one actionable
  line instead of a crash-looping container later.
- Every consumer reads `settings.etc_dir`; no module-level guess is left on
  any path that becomes a bind source.
- A **diagnostic** (salvaged from the rejected attempt below) makes the rule
  observable: inside a container, both `data_dir` and `etc_dir` must sit under
  a bind of Oduflow's own container whose host `Source` equals its
  `Destination`. Anything else — named volume, differing host path, writable
  layer — is a startup warning and a non-critical `self_mounts` check on
  `/healthz` (dashboard `MOUNTS` chip). It never raises, never degrades
  `/healthz`, reports `off` when the own container cannot be identified, and
  names no host paths in the public detail.

## Alternatives considered

- **A daemon-visible mirror under `base_data_dir`** (copy the three files to
  `<data_dir>/conf-mirror/` and mount that). Implemented and reverted. The
  mirror itself was cheap; *healing* containers created against the old
  mount source was not: it meant recreating a working shared PostgreSQL
  cluster on every upgrade (with the dev/prod blast-radius split that
  implies), and it broke Traefik's live watch on operator drop-ins, which
  now had to be re-mirrored on a schedule instead of being picked up by the
  file provider. Two copies of one config also invite drift questions the
  product then has to answer.
- **Pushing the config into the container through the Docker API at create
  time.** The `pg_hba.conf` precedent (`_install_pg_hba_content`) only looks
  similar: it targets a *running* container and writes into the PGDATA
  *volume*. A `postgresql.conf` pushed into a container's writable layer
  would be lost on every recreate and invisible from the host, and Traefik's
  directory watch has no equivalent at all.
- **Putting `postgresql.conf` into the PGDATA volume.** Clean for PostgreSQL
  (that is where it natively lives) but has no counterpart for Traefik, so
  it would trade one mechanism for two and still leave the config dir
  unreachable.

The configurable path wins because it is the mechanism `data_dir` already
proved: one setting, one directory, the daemon sees the real files, and
operator edits take effect exactly as they do on a native install.

## How it works (macro)

`Settings.from_raw` resolves `etc_dir` as explicit → `dirname(toml_path)` →
first-run probe, and `_get_settings()` validates it at the settings boundary
(so a bad directory is a `ConfigError`, like any other config mistake). The
PostgreSQL and Traefik ensure paths derive their bind sources from
`settings.etc_dir` only. `_ensure_initialized` runs the self-mount check
before `init_system` hands the first mount to the daemon; `collect_health`
runs the same verdict for `/healthz`.

## Consequences

- Docker-out-of-Docker deployments bind both `/srv/oduflow` and `/etc/oduflow`
  from the host at the same path (the docs' Compose example used a named
  `oduflow_etc` volume — the trap itself — and is corrected).
- Nothing changes for native installs; the default is byte-for-byte the old
  location.
- No auto-repair: an installation already broken by the old behaviour is
  fixed by hand (`docker rm oduflow-db` and let init recreate it).
- Any future file that becomes a bind source belongs under one of the two
  configurable directories; the diagnostic covers exactly those two.

## History

- 2026-11-09 — first introduced, replacing the reverted conf-mirror branch
  (`dood-config-mounts`, not merged).
