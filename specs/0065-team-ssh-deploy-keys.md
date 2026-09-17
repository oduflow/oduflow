# 0065 — Team SSH deploy keys for git access

**Status:** Adopted
**Type:** Auth / Git integration
**First introduced:** 2026-09-17
**Key code today:** `git_ops.py` (`ensure_ssh_key`, `team_ssh_command`, `validate_repo_url`), `settings.py` (`TeamSettings.ssh_dir`), `server.py` (`get_ssh_public_key`, startup key generation), `web_ui.py` (`/api/ssh-key*`), `docker_ops/env_ops.py` (`_prepare_agent_volumes`)

## Context

Git authentication was HTTPS-only: a personal access token per host in the
team's git credential store, fed to every git subprocess via
`git_env_for_team()`. SSH URLs were rejected outright — not for a security
reason, but because ssh's interactive host-key prompt could hang the server.
Tokens are a poor fit for some users: they expire, carry account-wide scope
on most providers, and several corporate git servers only expose SSH.
Meanwhile the natural SSH answer — a deploy key — needs the *server's* public
key in the user's hands, which Oduflow had no way to produce or show.

## Decision

Give each team one Oduflow-managed SSH identity, and make its public half a
first-class, copyable artifact in the dashboard:

- an **ed25519 keypair per team**, generated automatically at server start
  (never fatal when `ssh-keygen` is absent — HTTPS+PAT keeps working),
  stored under `TeamSettings.ssh_dir()` next to the credential store;
- the **public key is exposed** in the dashboard Credentials tab (copy +
  regenerate), over REST (`GET /api/ssh-key`,
  `POST /api/ssh-key/generate`), and over MCP (`get_ssh_public_key`); no
  API surface returns the private key (it is, however, provisioned into
  the team's coding-agent container, like the git credential store);
- **SSH repository URLs are accepted** (`git@host:path` and `ssh://…`)
  everywhere a repo URL is taken, guarded by the same SSRF host checks as
  HTTPS.

## How it works (macro)

`git_env_for_team()` — the single choke point every managed git subprocess
already flows through — additionally pins `GIT_SSH_COMMAND`:
`IdentitiesOnly` + the team key as the *only* permitted identity,
`BatchMode=yes` (a prompt can never hang the server again — the original
reason SSH was banned), and `StrictHostKeyChecking=accept-new` against a
team-local `known_hosts` (trust-on-first-use, later host-key changes
refuse). The command is set even when no key exists yet, so a keyless team
fails deterministically with "Permission denied" instead of falling back to
the host user's ambient ssh identities. Because the wiring lives in that one
function, clone, fetch, pull, deploy, webhooks and extra addon repos all
gain SSH with no per-call-site changes; callers that hold a team pass
`TeamSettings.ssh_dir()` explicitly, with a sibling-of-the-credential-file
fallback for the few that only carry a credential path.

URL sanitizers learned that an SSH URL's `git@` user is protocol, not a
credential: `sanitize_repo_url` and inline-credential extraction now only
strip userinfo from HTTP(S) URLs.

The team coder container reuses the existing credential-injection path: the
init container copies the private key into `/home/agent/.ssh` alongside
`.git-credentials`, and the agent-container config hash includes the key's
fingerprint, so first generation *and* regeneration recreate the container.

## Consequences

- Private repos work with read-only, repo-scoped deploy keys; no token to
  expire or over-scope. GitHub's one-repo-per-deploy-key rule is documented
  in the UI hint (machine user for multi-repo access).
- Regeneration is destructive by design (old key dies everywhere at once);
  the dashboard confirms before sending `force`.
- Trust-on-first-use host pinning is a deliberate trade-off: no interactive
  verification exists in a server context, and a pinned key still detects
  later substitution.
- One key per team, not per repo: consistent with the credential store's
  host-level granularity and the per-team lock that already serialises
  credential writes ([[0015-granular-locking]]).

## History

- Introduced with the SSH deploy key feature (dashboard Credentials tab
  section, `get_ssh_public_key` MCP tool, SSH URL acceptance).
