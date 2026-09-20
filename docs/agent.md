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
remote MCP client uses.

The image also includes **Agent Browser MCP** backed by Debian Chromium. It is
wired automatically into Claude, Codex, and OpenCode with the complete Agent
Browser tool set. Each environment gets a separate browser profile, while
browser data persists with the team's agent HOME volume across container
recreation.

Lifecycle is automatic: the container is created on startup for each enabled
team and removed for disabled ones; `create_environment` adds the environment's
checkout, `delete_environment` removes it. The container carries a hash of its
injected config as a label and is **recreated automatically** when the config
changes. The only runtime state is a durable ACP conversation-history file in
the team's data directory; transcripts remain owned by the agent adapters.

## Enabling it

Configuration lives entirely in `oduflow.toml` — there is no runtime editing.
The global `[agent]` section holds deployment-wide settings; per-team
enablement and credentials live in the `[team.*]` sections:

```toml
# Deployment-wide (optional)
[agent]
image = "oduist/oduflow-coder:0.3.1"
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
OPENAI_API_KEY = ""            # Codex API key
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
interactive `/login` stored on the team's persistent agent home volume. Known
credential values are trimmed when loaded, so whitespace accidentally copied
around a token is not sent to Anthropic. A configured environment credential
always overrides the persisted interactive login; if Anthropic rejects it,
Agent Chat fails closed with mode-specific recovery guidance instead of silently
trying another account or billing method.

OpenCode is provider-neutral. Any provider environment variable can be placed
under `[team.X.agent_env]`; `OPENCODE_API_KEY` is the standard OpenCode Zen
credential and is also inherited from the server environment in single-team
deployments. Alternatively, open **Agent CLI** and run `opencode auth login`;
the resulting provider credentials live on the team's persistent HOME volume
and survive container recreation. OpenCode's runtime self-update is disabled,
so its executable changes only when Oduflow moves to a new immutable coder
image.

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
  boundary is the unprivileged `agent` user inside the per-team Docker
  container, not per-tool prompts. Codex CLI uses
  `--dangerously-bypass-approvals-and-sandbox` and Codex ACP starts in
  `agent-full-access` (no nested Bubblewrap sandbox). Claude matches this: the
  Agent CLI console runs `claude --dangerously-skip-permissions`, and Agent
  Chat's ACP adapter (`claude-agent-acp`) starts in `bypassPermissions`, seeded
  via the container's user-tier `~/.claude/settings.json`
  (`permissions.defaultMode`). OpenCode CLI uses `--auto`; both CLI and its
  native `opencode acp` runtime receive a high-precedence session config with
  `permission = "allow"`. So installed MCP tools run without interactive
  permission prompts for any hosted agent.

## Limitations

- The agent UI is hidden for live-mount (`local_path`) environments.
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
downloaded directly from npm by the end user's container.
