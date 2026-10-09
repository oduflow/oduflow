# Server settings console: oduflow.toml managed from the browser

**Status:** Accepted | **Type:** Configuration / operator surface | **First introduced:** 2026-10-08

**Key code:** `config_schema.py` (key registry, apply classes), `config_store.py`
(patch/validate/write/history, live overlay), `admin_ui.py` (`/admin` routes and
session), `templates/admin.html`, `server.py` (`_swap_settings`,
`_request_restart`, `oduflow admin`)

## Context

`oduflow.toml` is the single configuration source
([[0016-configuration-model]]): global sections plus one `[team.*]` table per
tenant ([[0014-team-based-multi-tenancy]]). Every change meant SSH, a text
editor and a restart, with three recurring failure modes: a typo or a
cross-key rule (overlapping port ranges, duplicate hostnames, a team without a
token) discovered only when the restarted server refused to boot; secrets
copied around by hand; and no record of what changed. Operators hosting several
teams wanted to manage all of it, including adding teams, without touching
the file.

The file is machine-readable and writable, but three things stand in the way
of a naive "settings form": the config is heavily commented and still read by
humans; only part of it can change in a running process (the auth provider,
background threads, Traefik and agent reconciliation, route registration and
quotas are consumed at startup, routing labels are frozen into containers);
and it holds every team's credentials, while teams are isolated tenants
([[0027-hard-tenant-isolation]]).

## Decision

Add a deployment-wide **Server settings console** at `/admin` that edits the
whole file, as a separate operator surface rather than a dashboard tab:

- **Own credential.** `[admin] password` (generated on fresh installs,
  `oduflow admin enable` on existing ones, optional TOTP through the existing
  `ui_totp` machinery). A team password or session never opens it, and it
  never opens a team dashboard. Empty password means the console does not
  exist (404). It is not exposed over MCP: agents cannot reconfigure the server.
- **The boot parser is the validator.** Candidates are dry-run through
  `Settings.from_raw` + `validate` + the HTTP fail-closed auth checks (shared
  with `_start_http`). The console cannot save a file the next start refuses.
- **Comment-preserving writes** with `tomlkit`; a key the template documents
  as `# key = …` is uncommented in place.
- **Every key carries an apply class** in a declarative registry: *live*
  (swapped into the running Settings at once), *restart*, *recreate* (restart
  plus recreating existing containers) or *locked* (read-only, because the
  file alone cannot change it: PostgreSQL credentials and image, data dir).

## How it works (macro)

- The server keeps the parsed document next to the cached Settings. After a
  save, only the live-class differences are overlaid onto that running
  document and swapped in. Restart-class values stay at their boot value until
  a restart, so the process never runs a half-old, half-new configuration. The
  remaining difference between file and process is shown as "restart pending".
  Classification itself asks the *boot* configuration, not the live-swapped
  one: whether a live key needs a restart depends on what startup built (the
  reaper thread exists only if lifecycle was enabled then), and live swaps
  never build or tear that down.
- Writes are optimistic (the revision the editor started from must still be
  current), atomic, owner/mode-preserving, and snapshot before/after texts to
  a private history directory next to the config for diff and revert.
- Restart is a graceful uvicorn stop followed by re-exec of the same argv
  (same PID under systemd and in the container), refused while any lock is
  held. The normal startup path then reconciles Traefik, agents, quotas, teams.
- The browser never receives secrets unless one is explicitly revealed
  (logged); diffs mask them.

## Consequences

- Operators manage teams, routing, production, backups and agent credentials
  without SSH, and every change is validated, classified and recorded.
- The registry duplicates knowledge of the keys (labels, defaults, apply
  classes). Tests pin its defaults to the parser's and its sections to the
  parsed ones; a new TOML key needs a registry entry to be editable in the
  forms, though Raw TOML always covers it.
- The console is the most privileged web surface Oduflow has. It is off unless
  a password exists, isolated from team sessions, short-lived (12 h), and
  CSRF- and rate-limit-protected.
- `tomlkit` is a new runtime dependency.

## History

- 2026-10-08 — console, registry, store, `oduflow admin enable|disable`,
  `ui-2fa --admin`, fresh-install console password.
