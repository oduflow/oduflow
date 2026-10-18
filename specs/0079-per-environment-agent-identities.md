# Per-environment identities inside the team agent container

**Status:** Accepted | **Type:** Security / tenancy inside a team | **First introduced:** 2026-10-17

**Key code:** `agent_uids.py` (uid registry), `docker/agent/env-user.sh` +
`seed-home.sh` (accounts, private directories, HOME seeding),
`agent_exec_identity` / `agent_session_command` / `agent_env_uid` /
`_agent_sync_identities` / `_agent_purge_removed` / `_agent_add_env` /
`_agent_remove_env` in `docker_ops/env_ops.py`, `ws_agent_console` +
`ws_agent_acp` in `web_ui.py`, `agent_uploads.py`

## Context

[[0029-agent-console-and-chat]] runs one coding-agent container per team, and
[[0028-scoped-environment-mcp-access]] gives every console/chat session only its
own environment's scoped MCP token, injected into that session's `docker exec`
environment. The record claimed a leaked credential could only reach the one
environment the session already controls.

That was not true inside the container. Every session of every environment ran
as the same `agent` uid, and the kernel lets a uid read its own processes, so a
session could read another environment's token from `/proc/<pid>/environ`. It
could also read and edit every other checkout under `/workspace`, read every
environment's transcripts in the shared HOME, and write files that other
sessions execute: the npm-installed `claude` binary, `settings.json` hooks,
`~/.gitconfig`, shell rc files. The per-environment token gave no isolation
between environments of the same team.

The goal also changed: an environment should be safe to share with an external
user, which needs real isolation between environments of the same team.

## Decision

**Every environment gets its own Linux uid inside the team's container.** Its
sessions, checkout, attachments and HOME belong to that uid:

- **Uids come from a per-team registry** (`<data_dir>/agent_uids.json`,
  starting at 20000) and are **never reused**, so files a deleted environment
  left behind can never become readable to a later one. A rename keeps the
  uid. A corrupt registry is an error, not a reset: a reset would hand an
  existing environment's data to another one.
- **Private by default:** `/workspace/<slug>`, the attachment directory,
  `/home/agent/envs/<uid>` (the environment's HOME, on the persistent home
  volume) and `/tmp/user/<uid>` (its `TMPDIR`, in the container layer) are
  0700 and owned by the uid, and sessions start with umask 077, so even files
  a tool writes elsewhere stay private. `/workspace`, the upload root,
  `/home/agent` and `/tmp/user` can be traversed but not listed. The npm
  prefix that holds Claude Code is shared read-only. Everything else the
  `agent` user owns is private.
- **Root sets up, the environment's uid seeds.** `env-user.sh`, run as root
  through `docker exec`, creates the accounts and directories. `seed-home.sh`
  runs *as the environment's uid* (via `setpriv`) and writes its HOME: git
  identity, private copies of the team git credentials and SSH key, Claude
  onboarding and approval-free settings, Codex config and API-key login. Root
  passes the team git credentials and SSH key to it as open file descriptors,
  so root never writes into a directory the environment controls.
- **Interactive logins are per environment** (Claude `/login`,
  `codex login`, OpenCode auth). Teams that set credentials in
  `[team.X.agent_env]` notice nothing, because those still come from the
  container environment. A shared login was rejected: Claude's config
  directory also holds hooks and MCP servers, so sharing it would give every
  environment a way to run code in every other one. Copying one login into
  each HOME breaks on refresh-token rotation.

Two alternatives were considered and rejected. `hidepid` on `/proc` closes
only the token leak, not the shared files. A container per environment gives
the strongest isolation, but it multiplies the containers per team and the
Claude Code installs and image pulls behind them. Per-environment uids get
kernel-level isolation within the existing container.

## How it works (macro)

- **Create:** `_agent_add_env` allocates the uid, runs
  `env-user.sh provision`, then runs `clone-env.sh` as the uid with that
  environment's HOME. **Delete:** the uid is retired into the registry's
  `pending_removal`, then `env-user.sh remove` kills its processes and removes
  its checkout, attachments, HOME, `TMPDIR` and account. The uid leaves
  `pending_removal` only once that succeeded. A deletion while the container
  is stopped or gone is finished before the next provisioning or sync, so a
  new environment with the same slug never inherits the old one's files.
  Provisioning hands a directory over only from the shared-uid era, never from
  another environment's uid, and `remove` never deletes a directory another
  uid has taken over. **Rename:** root moves the directories and the registry
  entry follows.
- **Container (re)creation:** accounts live in the container layer, so
  `_ensure_agent_container` runs `env-user.sh sync` for every environment of
  the team and re-seeds every HOME with the current config. The same sync is
  the one-time migration from the shared-uid layout. Environments without a
  uid get one. Their checkouts and attachments are handed over, and their
  Claude projects and Codex rollouts move from the old shared HOME into the
  new private one. The history move happens only until the first sync has
  completed (a marker on the home volume): a later environment that reuses
  the slug of one deleted before the upgrade must not receive its
  transcripts. OpenCode history is not split, because it is keyed by
  repository rather than checkout.
- **Sessions:** the console and chat resolve the uid once with
  `agent_env_uid`: the uid exists, has an account, HOME and `TMPDIR`, and owns
  its checkout. If the check fails, the identity and checkout are repaired on
  demand, as missing checkouts already were. The session's `docker exec` then
  runs as `<uid>:<uid>` with `HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and
  `TMPDIR` pointed at its private directories, under umask 077. Attachment
  operations use the same identity.
- The container runs with `init` so killed and finished sessions are reaped.
  The entrypoint refreshes Claude Code in the background on every start,
  because environment uids cannot update the shared install.

## Consequences

- One session's `/proc/<pid>/environ`, checkout, attachments, transcripts and
  logins are closed to every other environment's sessions. A leaked session
  token now really reaches only its own environment. This is the basis for
  sharing one environment with an external user.
- **Not isolated, by decision:** the container's network namespace, so local
  ports and browser sessions of other environments stay reachable, which the
  owner accepted; and team-wide provider keys in the container environment,
  which are shared by design.
- An agent can no longer read neighbouring checkouts. Other branches are read
  through git in its own checkout.
- Subscription users log in once per environment instead of once per team.
- `agent_uids.json` is new host-side state per team. The coder image must
  ship `env-user.sh`, and older images are refused at container creation.
- **Future:** per-environment credentials fit this model. One planned
  example is a LiteLLM key with its own budget, minted when an environment is
  created and injected only into that environment's sessions.

## History

- 2026-10-17 — per-environment uids, private HOMEs/checkouts/attachments/temp
  files, root-run `env-user.sh` + uid-run `seed-home.sh`, deferred cleanup of
  deleted environments, one-time migration of the shared-uid layout, coder
  image 0.4.0.
