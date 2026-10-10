# 0029 — Per-team coding agent: browser console and ACP chat

**Status:** Adopted (MVP), ported from odusphere-cli (its ADRs 0005 + 0008)
**Type:** Architecture — new capability
**First introduced:** this change (2026-07-03), branch `litnimax/port-env-chat-agent`
**Key code today:** `docker/agent/` (Dockerfile + `entrypoint.sh` + `clone-env.sh`, image `oduist/oduflow-coder`); `[agent]` + per-team `agent_*` config in `settings.py`; `agent_config.py` (type validation) + `agent_sessions.py` (bounded chat history); `_ensure_agent_container` / `_agent_add_env` / `ensure_agent_env_checkout` / `_agent_remove_env` in `docker_ops/env_ops.py`; `ws_agent_console` + `ws_agent_acp` + `api_agent_acp_*` + `api_agent_info` in `web_ui.py`; vendored `templates/static/acp-client.js` + `chat.js` + `marked.min.js`; `#agent-modal` / `#chat-modal` / `#chat-dock` and the card **Agent Chat** button in `templates/dashboard.html`

## Context

The dashboard already streams two `WebSocket ↔ docker exec` consoles per
environment (Odoo shell, psql) rendered with xterm.js. Odusphere-cli built on
that same bridge a full coding-agent surface: a per-installation agent
container (now Claude Code + OpenAI Codex + OpenCode in one image) whose
interactive CLI opens in a browser terminal, plus a structured **ACP chat**
(Agent Client Protocol,
JSON-RPC over stdio, bridged to the browser over the same WebSocket pattern)
with durable per-environment conversations. This change ports that capability.

The port is not a copy: odusphere is single-tenant (one Sphere, one operator),
while Oduflow is **multi-team with hard tenant isolation**
([[0027-hard-tenant-isolation]]) and has **no Sphere** — only ephemeral
environments. Everything Sphere-specific in the source (the `_sphere` control
checkout bind-mount, upstream-merge conflict resolution, Sphere Chat/CLI
buttons) was deliberately dropped. The agent is also framed differently: it is
a **hosting feature** — a client of a hosted Oduflow grows their Odoo by
chatting with the agent from the browser. A local developer already has the
code and their own agents, so the feature is opt-in per team and hidden for
live-mount environments.

## Decision

**One coding-agent container per team** (`oduflow-{team}-agent`), **opt-in and
off by default** (`agent_enabled` in the `[team.X]` TOML section), joined to
the team's isolated network, with two persistent named volumes — HOME (auth +
sessions) and `/workspace` (one full git checkout per environment at
`/workspace/<slug>`). The dashboard drives it two ways over the existing
WebSocket↔`docker exec` bridge:

- **Agent CLI** (`ws_agent_console`): the agent's own TUI in xterm.js, exec'd
  with a PTY at the environment's checkout.
- **Agent Chat** (`ws_agent_acp`): a TTY-less, line-framed relay to the agent's
  ACP runtime (`claude-agent-acp` / `codex-acp` / native `opencode acp`),
  rendered by a vendored, framework-free browser client (`acp-client.js` +
  `chat.js`). A current
  session and bounded recent history per (environment, agent) are persisted;
  any selected conversation resumes via ACP `session/load`. Chats minimize to
  a dock so several run in parallel.

The coder also ships a local Agent Browser MCP backed by Debian Chromium. Both
agents receive it automatically; browser profiles are separated by environment
slug. Codex sessions run without approval prompts or a nested process sandbox:
the CLI bypasses approvals and sandboxing, while ACP uses its
`agent-full-access` mode. This deliberate trust choice is bounded by the
dedicated unprivileged user and per-team Docker container described in
[[0037-built-in-agent-browser-and-noninteractive-codex]].

The agent never touches host files: it edits its own clone → `git push` → drives
the environment through the Oduflow MCP server (`pull_and_apply`, tests). MCP
access is **scoped and per session**: the team `auth_token` never enters the
agent container (a console can read anything held by its unprivileged container
user). Instead each console/chat exec injects that
environment's per-env token ([[0028-scoped-environment-mcp-access]]) plus its
`/mcp/<env>` URL into its own exec environment; Claude resolves them through
`${VAR}` placeholders in the checkout's `.mcp.json` (which therefore contains
no secret), Codex CLI through per-session `-c mcp_servers.*` overrides, and
Codex ACP through the adapter's client-provided HTTP MCP session contract. A
leaked session credential grants only the ADR-0028 dev-loop allowlist on the
one environment the session already controls.

**Configuration lives in `oduflow.toml`, not in the dashboard.** Per team:
`agent_enabled` (default **false**), `agent_default` (claude | codex | opencode)
and the
`[team.X.agent_env]` table (provider credentials + custom vars injected into
the container). The global `[agent]` section holds only deployment-wide bits
(image, optional model overrides). There is no runtime editing and no Agents
tab: config is the source of truth — the container carries a hash of its
injected config as a label and is recreated automatically on the next ensure
(server start, environment create, console open) when the config changed;
disabling the agent removes the container on the next server start. The only
runtime state is `agent_sessions.json` (current and recent ACP session ids) in
`<team.data_dir>`.

## How it works (macro)

- **Lifecycle:** `init_system` (every server start) ensures one agent
  container per enabled team and removes leftovers for disabled ones;
  `create_environment` execs `clone-env.sh` to add the env's checkout (and its
  project-scoped Claude `.mcp.json`); `delete_environment` removes the checkout
  and forgets its chat sessions; `destroy_system` removes the containers and
  volumes (they would otherwise pin the team networks). Opening a console/chat
  heals a missing checkout on demand from the Odoo container labels.
- **Chat open:** the card's **Agent Chat** button lazy-loads the vendored
  assets, fetches `agent-acp/info` (cwd + current session + history), connects the
  WebSocket, `initialize`s, then `session/load` (resume) or `session/new`
  (persisted back). `session/update` notifications render as markdown,
  collapsible reasoning, tool-call cards and plans; `session/request_permission`
  becomes an inline approve/deny panel.
- **Auth:** the modal and relay ride the dashboard session cookie; the team is
  resolved by the same auth middleware as every other dashboard route, so a
  team can only ever reach its own agent container.

## Consequences

- Two new first-class development surfaces with zero new transport machinery
  and no build step; the dashboard stays a single no-CDN page.
- A console or chat is **arbitrary code execution** in the team's agent
  container — confined to that container, its clones, and its session's
  scoped per-env MCP token; cross-team reach is blocked by per-team
  containers/volumes/networks. The agent cannot create/delete/stop
  environments or touch templates/services/volumes (default-deny allowlist of
  [[0028-scoped-environment-mcp-access]]); those stay operator actions.
- Environments created before per-env tokens existed have no
  `oduflow.mcp_token` label; their consoles warn and the agent works without
  MCP until the environment is updated/recreated. Both Claude and Codex chat
  receive the same environment-scoped MCP authority once a token exists.
- The long-lived container, interactive CLIs, checkout hooks and ACP adapters
  run as the dedicated unprivileged `agent` user. A short-lived networkless
  root init container only migrates ownership of legacy persistent volumes and
  copies the team git credential store; it exits before any agent starts.
- Server-level provider keys (`ANTHROPIC_API_KEY` etc.) are inherited by the
  container **only in single-team deployments**; with several teams each team
  must set its own keys in the dashboard, so an operator credential never
  leaks to tenants.
- **Live-mount environments get no agent UI at all** (the card hides Agent
  Chat / Agent CLI): there is nothing for the containerized agent to clone,
  the host path is deliberately not mounted into it, and the local developer
  uses their own agents anyway.
- **The published image contains redistributable open-source software.** Codex
  CLI, Codex ACP and Agent Browser are Apache-2.0; Debian Chromium preserves
  its upstream BSD-style/component license notices in the image. Claude Code
  and its adapter are under Anthropic's Commercial Terms, so `entrypoint.sh`
  npm-installs them at first container start onto the persistent HOME volume —
  downloaded by the end user's container directly from npm, never shipped by
  us.
- The browser client self-heals an adapter's failed session resume by starting
  a fresh chat.

## Evolution

Agent Chat initially kept one effectively permanent session id per
(environment, agent), so **New conversation** overwrote the only pointer even
though the old transcript still existed on the agent HOME volume. It now keeps
`current` plus an MRU history of at most 20 session ids, titled from the first
user prompt. Legacy string values are coerced when read and rewritten on the
next mutation, avoiding a startup migration and tolerating mixed state files.

Resume remains an ACP boundary: Oduflow calls `session/load` for the selected
id and does not inspect Claude Code or Codex transcript files. This keeps the
integration independent of private CLI storage formats; it also means Codex
history loading remains best-effort while its adapter matures.

OpenCode was later added as a third first-class runtime. Its MIT-licensed CLI is
baked into the immutable coder image, Agent CLI receives a high-precedence
session-only config with approval-free permissions and scoped MCP placeholders,
and Agent Chat uses native `opencode acp`. Because OpenCode supports
client-provided HTTP/SSE MCP servers, its ACP sessions share the same
session-open injection used by Codex. The browser client accepts both the
legacy ACP `models` response and modern `configOptions`, selecting the matching
model-change method per session.

Deployments that route models through their own OpenAI-compatible gateway
(LiteLLM) hit a protocol mismatch: OpenCode's built-in `openai` provider speaks
the Responses API, while such gateways often accept only Chat Completions. An
optional `[agent.opencode_provider]` therefore declares one custom provider. It
is deployment-wide, like `opencode_model`, and is rendered into the same
session-only OpenCode config for both CLI and ACP. OpenCode's bundled
`@ai-sdk/openai-compatible` package makes the requests, so the immutable coder
image did not change. The provider keeps the credential model of the rest of
the agent: the TOML holds only the *names* of agent-environment variables and
OpenCode receives `{env:…}` placeholders. Per-team values stay in `agent_env`,
and the parser rejects anything that looks like an inline URL or key. The
default variable names are the provider's own rather than `OPENAI_*`, because
Codex and OpenCode's built-in `openai` provider read those from the same
container. OpenCode merges a provider whose id is in its models.dev catalog
with the built-in entry, and that catalog keeps growing; instead of a
denylist, the generated config pins the provider to the declared models and
variables, and only the providers Oduflow's agents rely on are reserved.
Without the section the generated config is unchanged.

## History

- 2026-10-10 — added an optional custom OpenAI-compatible (Chat Completions)
  OpenCode provider, `[agent.opencode_provider]`, applied to Agent CLI and
  Agent Chat, with variable-name-only credentials and startup/console
  validation.

- 2026-07-24 — added OpenCode as a third hosted agent with CLI and native ACP
  chat, generic provider authentication, Agent Browser, scoped Oduflow MCP,
  modern ACP model options, and isolated conversation history.
- 2026-07-22 — replaced the rolling coder-image tag and manual runtime epoch
  with the immutable, release-coupled image contract in
  [[0040-versioned-coder-image-contract]].
- 2026-07-21 — evolved durable chat state from one session id to a bounded MRU
  conversation history with first-prompt titles and dashboard switching;
  legacy values normalize on read and resume still goes exclusively through
  ACP `session/load`.
- 2026-07-03 — ported from odusphere-cli ADRs 0005 (agent console; including
  its evolution to a single persistent container with HOME/workspace volumes)
  and 0008 (ACP chat UI), reshaped for multi-team Oduflow: per-team container/
  volumes/state, team-network placement, team-token MCP wiring, no Sphere
  surfaces. New image `oduist/oduflow-coder` published by
  `.github/workflows/publish-coder.yml`.
- 2026-07-03 — reframed as an opt-in hosting feature (review feedback): the
  Agents tab and runtime-editable stores were dropped in favour of static
  per-team TOML config (`agent_enabled` default false, `agent_default`,
  `[team.X.agent_env]`) with automatic container recreation on config drift;
  agent UI hidden for live-mount environments; Claude Code moved out of the
  published image to a first-start npm install (licensing).
- 2026-07-03 — the team `auth_token` was removed from the agent container
  entirely (review feedback: a console user could read it). Sessions now carry
  scoped per-environment tokens from [[0028-scoped-environment-mcp-access]] in
  their own exec env; `.mcp.json` holds a `${ODUFLOW_MCP_TOKEN}` placeholder
  instead of a secret, and the global Codex config holds no Oduflow endpoint.
- 2026-07-21 — moved the long-lived coder container and every interactive/ACP
  exec from root to the dedicated `agent` user. Existing per-team HOME and
  workspace volumes are migrated in place by a short-lived, networkless root
  init container, preserving logins, runtime-installed Claude packages and
  checkouts; the host git credential store is copied into the migrated HOME
  with `0600` permissions before the unprivileged container starts.
- 2026-07-21 — completed scoped MCP wiring for Codex ACP chat after
  `codex-acp` gained per-session config support. The relay injects a
  client-provided HTTP MCP entry into ACP session-open frames for the
  environment's `/mcp/<env>` endpoint; the bearer travels only over that
  exec's local stdin and is never exposed to the browser or written to disk.
  API-key auth was also adapted to current Codex releases by materializing
  persistent `auth.json` through `codex login --with-api-key` at startup.
- 2026-07-21 — added the built-in Agent Browser MCP and Chromium runtime for
  Claude and Codex (later extended to OpenCode), with per-environment browser
  profiles. Codex CLI and
  ACP sessions now run non-interactively without approval prompts or a nested
  process sandbox; see [[0037-built-in-agent-browser-and-noninteractive-codex]].
