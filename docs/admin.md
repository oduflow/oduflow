# Server Settings Console

The Server settings console is a browser editor for the whole of
`oduflow.toml`: every global section and every team. It is served at `/admin`
in HTTP mode, so after the first start you never have to SSH into the server
to change the configuration by hand.

It is a deployment-wide tool for whoever operates the server, not part of the
team dashboard. It has its own password, separate from every team login,
because it can read and change every team's tokens and passwords.

## Enable it

Fresh installs generate a console password on first start. Read it from the
config file:

```bash
sudo grep -A1 '^\[admin\]' /etc/oduflow/oduflow.toml
```

On an existing install, enable the console once from the server shell, then
restart Oduflow:

```bash
oduflow admin enable          # prints the generated password once
sudo systemctl restart oduflow
```

Open `/admin` on any hostname the server answers on, for example
`https://dev.example.com/admin` (Traefik) or `http://server:8000/admin`
(port mode). While `[admin] password` is empty the console does not exist:
every `/admin` path answers 404.

For authenticator-app 2FA, run `oduflow ui-2fa setup --admin` on the server
(see [CLI Reference](cli.md#server-settings-console)).

## What you can edit

| Section | What it covers |
|---|---|
| Server | Listener, local-path mounts, insecure-HTTP switch, tracing, telemetry |
| Routing | Port or Traefik mode, TLS, ACME e-mail, public URL scheme |
| Extra routes | `[route.*]` Traefik routes to services Oduflow does not manage |
| Teams | Add and remove teams; per team: hostname, base domain, credentials, port range, slots, quotas, coding agent and its environment, image registry |
| Lifecycle | Auto-stop, auto-delete, deleted-production purge |
| Coding agent | Agent image and model overrides |
| Production | Production hosting, workers cap, WAL safety thresholds |
| Backups | S3 backups: enable, credentials, schedule, retention |
| Storage, Database | Shown read-only (see below) |
| Console access | The console's own password |
| Raw TOML | The whole file, for anything the forms do not cover |
| History | Every save, with a diff and one-click revert |

Every field shows its TOML key, the default that applies when the key is
absent, and a badge saying when a change takes effect. Clearing a field
removes the key from the file, so the default applies again. The file keeps
your comments and layout: a key that the bundled template documents as a
commented-out line is uncommented in place.

## When a change takes effect

| Badge | Meaning |
|---|---|
| **Live** | Applied the moment you save — slots, quotas, lifecycle hours, team dashboard passwords, WAL thresholds, backup schedule and more |
| **Restart** | Saved now, used after Oduflow restarts — auth tokens, new teams, hostnames, ports, agent settings, production and backup enablement, extra routes |
| **Recreate** | Needs a restart, and existing environments, services and productions keep the old value until each is recreated — routing mode and TLS |
| **Read-only** | Changing the file alone would break the deployment — the PostgreSQL user, password and image, and the data directory |

The console applies live changes on its own and keeps everything else out of
the running process until a restart, so the server never runs half on the old
configuration and half on the new one. When saved changes are waiting, a
banner lists them with a **Restart Oduflow** button. The restart stops the
server gracefully and starts it again with the same command line;
environments, services and productions keep running. The console refuses to
restart while an operation (a deploy, an environment build) holds a lock.

Read-only keys need work outside Oduflow first, for example `ALTER ROLE` for
the database password or moving the data directory. Do that, then change the
key in **Raw TOML**.

## Saving safely

1. Edits collect in a draft. **Review & save** shows each change, when it
   takes effect, and the file diff with secrets masked.
2. The candidate file is validated with the same parser and checks the server
   runs at startup, including the HTTP authentication rules. A configuration
   the next start would refuse cannot be saved.
3. The file is replaced atomically, with its owner and owner-only mode kept.
   If it changed since you opened it, by a hand edit or another admin, the
   save is refused and you reload instead of overwriting.
4. The previous and new versions go to `.oduflow-history/` next to the config
   (owner-only, last 50 saves). **History** shows each save's diff, and
   **Revert** restores the file as it was before that save, through the same
   review.

If the file on disk is invalid (for example after a broken hand edit), the
console says so and opens **Raw TOML** so you can fix it. The running server
keeps its last good configuration in the meantime.

## Teams

**Add team** suggests the next team ID and the next free 100-port block, and
generates a dashboard password and an MCP token. Copy them before saving. The
new team becomes reachable after a restart.

**Remove team** removes the `[team.<id>]` section only. The team's data
directory, databases and running containers are not deleted; clean them up
separately.

## Secrets

Secrets (passwords, tokens, S3 keys, registry tokens, agent environment
values) never reach the browser until you press **Reveal** on one field, and
each reveal is logged with the client address. Leave a secret field empty to
keep the stored value, or press **Generate** for a new random one. Diffs and
history show `********` instead of values, plus `(changed)` where a secret
changed.

## Security model

- Separate credential: `[admin] password` must differ from every team
  `ui_password`, `auth_token` and `production_token`. The console accepts a
  new password only if it has at least 12 characters.
- Separate session: a signed cookie with its own salt, valid for 12 hours,
  `HttpOnly`, `SameSite=Strict`. Changing the console password or its 2FA
  signs every console session out.
- Team sessions never reach `/admin`, and a console session does not open the
  team dashboard.
- State-changing requests from another origin are rejected, and failed
  sign-ins are rate-limited per client address.
- Saves, reveals, sign-ins and restarts are written to the server log without
  values.
- The console does not exist for MCP clients. Agents cannot change the server
  configuration.

To turn the console off, run `oduflow admin disable` on the server and
restart.

## REST API

The console page uses a small JSON API under `/admin/api/`. It needs the
console session cookie; every POST must be same-origin.

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/api/state` | Schema, current values with secrets masked, revision, pending changes |
| POST | `/admin/api/preview` | Validate a candidate and return changes, apply classes and the masked diff |
| POST | `/admin/api/apply` | Save a candidate if `revision` still matches; apply live values |
| POST | `/admin/api/reload` | Apply live values from the file on disk (after a hand edit) |
| POST | `/admin/api/reveal` | Return one secret value |
| GET | `/admin/api/raw` | The whole file and its revision |
| GET | `/admin/api/history` | Saved versions, newest first |
| GET | `/admin/api/history/{id}` | One save's masked diff |
| POST | `/admin/api/restart` | Restart Oduflow (refused while operations hold locks) |

`preview` and `apply` take one candidate: `{"changes": [{"path": [...],
"value": ...} | {"path": [...], "unset": true}], "revision": "..."}`, or
`{"raw": "<whole file>", "revision": "..."}`, or `{"restore": "<history id>",
"revision": "..."}`.
