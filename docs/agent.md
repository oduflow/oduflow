# Coding Agent

Oduflow can host a **coding agent** for a team: an opt-in feature where the
client grows their Odoo by chatting with an AI agent directly from the browser
dashboard. Oduflow runs one agent container per team
(`oduist/oduflow-coder`, running Claude Code + OpenAI Codex + OpenCode) and
exposes two front-ends for every environment.

!!! note "Hosting feature — off by default"
    The coding agent is for **hosted** deployments. A local developer already
    has the code and their own agents, so it is disabled unless you set
    `agent_enabled` for the team. It is also **hidden for live-mount
    (`local_path`) environments** — there is nothing for the containerized
    agent to clone.

## Agent CLI vs Agent Chat

Both drive the same agent container over the dashboard's existing
WebSocket ↔ `docker exec` bridge:

- **Agent CLI** — the agent's own terminal UI (TUI) rendered in the browser,
  exec'd with a PTY at the environment's git checkout. Full access to the
  agent's native command-line experience.
- **Agent Chat** — a structured, framework-free browser chat that speaks the
  **Agent Client Protocol (ACP)** to the agent's adapter. Each environment has
  a durable, bounded **conversation history**: use **History** to resume one of
  the 20 most recent conversations, titled from its first prompt. Chats also
  minimize to a dock, so several can run in parallel. Assistant messages render
  as markdown, with collapsible reasoning, tool-call cards, plans, and inline
  approve/deny prompts for permission requests.

## How it works

The agent never touches host files. It holds one full git checkout per
environment (at `/workspace/<slug>` in the container), edits its own clone,
`git push`es, and then drives the environment **only through the Oduflow MCP
server** (`pull_and_apply`, `run_odoo_tests`, etc.) — the same closed loop a
remote MCP client uses. Inside the agent this server is named `development`, so
its tools appear as `development_*` in OpenCode and `mcp__development__*` in
Claude Code and Codex. Checkouts created by earlier releases had it under
`oduflow`; that entry is replaced before the next session.

One container serves the whole team, but **every environment runs as its own
Linux user**. Each environment's sessions, checkout, chat attachments and HOME
(logins, conversation transcripts, settings) are private to it: an agent
working in one environment cannot see other environments' checkouts, sessions
or credentials. Other branches remain readable through git
(`git fetch`, `git show origin/<branch>:<path>`).

The image also includes **Agent Browser MCP** backed by Debian Chromium. It is
wired automatically into Claude, Codex, and OpenCode with the complete Agent
Browser tool set. Each environment gets a separate browser profile, kept
in that environment's own HOME on the persistent agent volume across container
recreation.

Lifecycle is automatic: the container is created on startup for each enabled
team and removed for disabled ones; `create_environment` adds the environment's
identity and checkout, `delete_environment` removes them, together with the
environment's HOME and running sessions. If the agent container is stopped or
gone at that moment, the removal is completed the next time it runs, before any
environment (including a new one with the same name) is set up, so a later
environment never inherits a deleted one's files. The container carries a hash
of its injected config as a label and is **recreated automatically** when the
config changes. The only runtime state is a durable ACP conversation-history
file in the team's data directory; transcripts remain owned by the agent
adapters.

The coder image includes OpenSSH client tools for Git remotes authenticated
with a team SSH deploy key. Use an SSH repository URL for deploy-key access;
HTTPS remotes still require HTTPS credentials.

## Enabling it

Configuration lives entirely in `oduflow.toml` — there is no runtime editing.
The global `[agent]` section holds deployment-wide settings; per-team
enablement and credentials live in the `[team.*]` sections:

```toml
# Deployment-wide (optional)
[agent]
image = "oduist/oduflow-coder:0.4.0"
# claude_model = ""     # optional Claude model override; empty = CLI default
# codex_model = ""      # optional Codex model override; empty = CLI default
# opencode_model = ""   # optional provider/model override; empty = OpenCode default

[team.1]
hostname = "localhost"
auth_token = "…"
agent_enabled = true    # turn the coding agent on for this team
agent_default = "claude"  # "claude" | "codex" | "opencode"

# Provider credentials injected into the team's agent container
[team.1.agent_env]
CLAUDE_CODE_OAUTH_TOKEN = ""   # Claude subscription token (`claude setup-token`); outranks the API key
ANTHROPIC_API_KEY = ""         # Claude API key (used when no OAuth token)
OPENAI_API_KEY = ""            # Codex API key; replaces a ChatGPT sign-in
OPENCODE_API_KEY = ""          # OpenCode Zen; other providers use their own variables
```

The default coder image is an immutable versioned tag coupled to this Oduflow
release. Oduflow pulls a changed tag before replacing the running container; a
failed pull leaves the previous container intact. The former official
`oduist/oduflow-coder:latest` value resolves to the current pinned default
when the configuration is loaded.

When `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` is configured, the
container automatically marks Claude Code's first-run onboarding as complete,
so Agent CLI opens directly in the REPL without asking to select a login method.

Claude supports three alternative authentication modes. Oduflow selects exactly
one for each team: a non-empty `CLAUDE_CODE_OAUTH_TOKEN` wins, otherwise a
non-empty `ANTHROPIC_API_KEY` uses Console API billing, otherwise Claude uses the
interactive `/login` stored in the environment's own HOME on the persistent
agent volume. Known
credential values are trimmed when loaded, so whitespace accidentally copied
around a token is not sent to Anthropic. A configured environment credential
always overrides the persisted interactive login; if Anthropic rejects it,
Agent Chat fails closed with mode-specific recovery guidance instead of silently
trying another account or billing method.

OpenCode is provider-neutral. Any provider environment variable can be placed
under `[team.X.agent_env]`; `OPENCODE_API_KEY` is the standard OpenCode Zen
credential and is also inherited from the server environment in single-team
deployments. Alternatively, open **Agent CLI** and run `opencode auth login`;
the resulting provider credentials live in the environment's own HOME on the
persistent agent volume and survive container recreation. OpenCode's runtime
self-update is disabled, so its executable changes only when Oduflow moves to a
new immutable coder image. The same applies to Codex: Agent CLI starts it with the startup update
check turned off.

### Codex with a ChatGPT subscription

Instead of `OPENAI_API_KEY`, Codex can use a ChatGPT plan (Plus, Pro, Business
or Enterprise). Sign in once per environment (each environment keeps its own
login):

1. With Codex as the team's default agent (`agent_default = "codex"`), open
   **More ▾ → Agent CLI** on the environment.
2. Choose **2. Sign in with Device Code**. Option 1, **Sign in with ChatGPT**,
   does not work here: its callback goes to `localhost` inside the agent
   container, which your browser cannot reach.
3. Open <https://auth.openai.com/codex/device> in any browser, sign in to
   ChatGPT and enter the one-time code that Codex shows. The code expires after
   15 minutes.
4. Codex reports **Signed in to Codex**. Agent CLI and Agent Chat of that
   environment now use the subscription.

Business and Enterprise workspaces may need a workspace admin to enable
device-code sign-in for Codex first.

The sign-in is stored in the environment's own HOME on the persistent agent
home volume and survives container recreation. A configured `OPENAI_API_KEY`
overrides it: every environment signs in with the key
(`codex login --with-api-key`) when the container starts and when the key
changes. The key's sign-in stays in the HOME after the key is removed, so to
switch to the subscription, remove the key, restart Oduflow, then run `/logout`
in Agent CLI and sign in again. To check or reset an environment's sign-in from
the host, run Codex as that environment's user (the container is
`<prefix><team>-agent`; the environment's uid is in the team's
`agent_uids.json`):

```bash
uid=20000   # from <team data_dir>/agent_uids.json
docker exec -u $uid:$uid -e HOME=/home/agent/envs/$uid \
    -e CODEX_HOME=/home/agent/envs/$uid/.codex oduflow-1-agent codex login status
```

Use `codex logout`, or `docker exec -it ... codex login --device-auth` to sign
in without Agent CLI. Many concurrent sessions of one environment can fail a
token refresh with `refresh_token_reused`; run `/logout` in Agent CLI and sign
in again, or use an API key for a busy team. When Codex is not signed in, Agent Chat's error
message includes these recovery steps.

### OpenAI-compatible gateway for OpenCode

OpenCode's built-in `openai` provider uses the OpenAI Responses API. Many
OpenAI-compatible gateways (LiteLLM, vLLM, self-hosted proxies) accept only
Chat Completions. Declare such a gateway once as a custom OpenCode provider:

```toml
[agent]
opencode_model = "litellm/glm-5.3"   # "<provider id>/<model>"

[agent.opencode_provider]
id = "litellm"
name = "LiteLLM"                     # optional; defaults to id
models = ["glm-5.3"]
# base_url_env = "OPENCODE_PROVIDER_BASE_URL"   # default; the URL must include /v1
# api_key_env = "OPENCODE_PROVIDER_API_KEY"     # default

[team.1.agent_env]
OPENCODE_PROVIDER_BASE_URL = "https://llm.example.com/v1"
OPENCODE_PROVIDER_API_KEY = "sk-…"
```

The default variable names are deliberately not `OPENAI_BASE_URL` and
`OPENAI_API_KEY`: Codex and OpenCode's built-in `openai` provider read those
from the same agent container, so pointing them at the gateway would break
Codex and send the real OpenAI key to the gateway.

Every OpenCode session that Oduflow starts, in both Agent CLI and Agent Chat,
gets the provider through OpenCode's bundled `@ai-sdk/openai-compatible`
package. Requests go to `POST <base URL>/chat/completions`, and OpenCode's
session-title requests use the same gateway. Agent Chat's model picker lists
the declared models.

The section holds only the *names* of the variables. Their values stay in
each team's `[team.X.agent_env]`, and OpenCode receives `{env:…}` placeholders
that resolve inside the agent container, so no URL or key is written into the
OpenCode config. The settings are validated when Oduflow starts and when you
save in the settings console:

- `id` must be lowercase and must not be `anthropic`, `google`, `openai`,
  `opencode` or `openrouter`. Other ids from OpenCode's provider catalog are
  allowed: Oduflow pins the provider to the declared models and variables, so
  the catalog adds no models or keys of its own.
- The two `*_env` values must be variable names.
- `models` must be a non-empty list.
- Keys such as `api_key` are rejected.
- If `opencode_model` names this provider, it must have the form
  `<id>/<model>` and the model must be listed in `models`.

When `opencode_model` selects this provider and a team's agent environment
lacks either variable, Agent Chat shows a warning and Agent CLI waits for
Enter before it starts OpenCode. The variables come only from
`[team.X.agent_env]`; the server environment is forwarded only for the known
provider keys of a single-team server. Changes to the section apply to new
sessions without a restart. Changes to `agent_env` recreate the agent
container on the next restart. Without the section, OpenCode behaves exactly
as before.

See the [`[agent]`](installation.md#agent-settings) and
[per-team](installation.md#per-team-settings) settings tables for the full
reference.

## Security model

!!! warning "A console/chat is arbitrary code execution"
    Opening an Agent CLI or Agent Chat is arbitrary code execution **inside the
    team's agent container**. It is confined to that container, its clones, and
    the session's scoped MCP token.

- **Per-team isolation.** Each team gets its own agent container, volumes, and
  network. Cross-team reach is blocked; the dashboard auth middleware resolves
  the team, so a team can only ever reach its own agent.
- **Per-environment isolation.** Inside the team's container, each
  environment's sessions run as that environment's own unprivileged uid (never
  reused, even after the environment is deleted). Its checkout, attachments and
  HOME are private to it, and so are its temporary files (a private `TMPDIR`,
  and sessions start with umask `077`). No session can read another
  environment's process environment (where its scoped token lives). The
  npm-installed Claude Code is shared read-only, so no environment can alter
  what another runs.
  Not separated between environments: the container network (local ports,
  browser sessions) and the team-wide provider credentials from
  `[team.X.agent_env]`.
- **Scoped MCP access.** The team `auth_token` never enters the agent
  container. Each session injects that **environment's** scoped per-environment
  token, which grants only the dev-loop allowlist on the one environment the
  session already controls. The agent **cannot** create, delete, or stop
  environments, or touch templates, services, or volumes — those remain
  operator actions.
- **Credentials.** Server-level provider keys are inherited by the container
  only in single-team deployments; with several teams, each team sets its own
  keys in `[team.X.agent_env]` so an operator credential never leaks to
  tenants.
- **Sandbox and approvals.** All three agents run approval-free — the security
  boundary is the environment's unprivileged uid inside the per-team Docker
  container, not per-tool prompts. Codex CLI uses
  `--dangerously-bypass-approvals-and-sandbox` and Codex ACP starts in
  `agent-full-access` (no nested Bubblewrap sandbox). Claude matches this: the
  Agent CLI console runs `claude --dangerously-skip-permissions`, and Agent
  Chat's ACP adapter (`claude-agent-acp`) starts in `bypassPermissions`, seeded
  via the environment's user-tier `~/.claude/settings.json`
  (`permissions.defaultMode`). OpenCode CLI uses `--auto`; both CLI and its
  native `opencode acp` runtime receive a high-precedence session config with
  `permission = "allow"`. So installed MCP tools run without interactive
  permission prompts for any hosted agent.

## Limitations

- The agent UI is hidden for live-mount (`local_path`) environments.
- Interactive logins (Claude `/login`, `codex login`, `opencode auth login`) are
  per environment: log in once in each environment, or set the team's
  credentials in `[team.X.agent_env]`, which every environment uses
  automatically.
- On the upgrade to per-environment users, existing Claude and Codex
  conversations move to their environment once, on the agent container's first
  start; OpenCode conversations from before the upgrade are not carried over.
- Environments created before per-environment tokens existed have no scoped
  token; their consoles warn and the agent works without MCP until the
  environment is updated/recreated.
- Opening a previous Codex conversation is best-effort because its ACP
  `session/load` support is still maturing. A failed switch restores the current
  conversation when possible, otherwise it starts a new one without deleting
  the history entry.

The published image contains redistributable open-source software: Codex CLI,
Codex ACP and Agent Browser are Apache-2.0, OpenCode is MIT, and Debian Chromium
includes its upstream component license notices. Claude Code and its adapter
are installed at first container start onto the persistent home volume —
downloaded directly from npm by the end user's container — and refreshed in the
background on every later container start.
