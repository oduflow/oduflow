# 0037 — Built-in Agent Browser MCP and non-interactive Codex

**Status:** Adopted
**Type:** Architecture — agent runtime capability and trust model
**First introduced:** this change (2026-07-21), branch `litnimax/codex-agent-env-key`
**Key code today:** `docker/agent/Dockerfile`; `docker/agent/entrypoint.sh`; `docker/agent/clone-env.sh`; `_wire_client_acp_mcp`, `ws_agent_console`, and `ws_agent_acp` in `web_ui.py`; `_ensure_agent_container` in `docker_ops/env_ops.py`

## Context

The hosted coding agent already receives environment-scoped Oduflow MCP access
([[0028-scoped-environment-mcp-access]]) through its CLI and ACP chat surfaces
([[0029-agent-console-and-chat]]). Odoo development also needs a real browser
for UI flows, screenshots, interaction testing, and frontend debugging. Installing
browser automation ad hoc in a persistent HOME is slow and makes capabilities
depend on which team happened to initialize its container first.

Codex's default approval policy also interrupts hosted, unattended MCP loops.
The user opening Agent CLI or Agent Chat has already authorized arbitrary code
execution in the team's dedicated coder container, so a second interactive
Codex approval boundary adds friction without separating a different principal.

## Decision

The published coder image includes a pinned Agent Browser CLI/MCP package and
Debian Chromium. Agent Browser is configured as a local stdio MCP with all of
its tools for Claude, Codex, and OpenCode. Every console/chat exec sets a
profile name derived from the environment slug, preventing two environments in
the same team container from sharing a browser daemon or session accidentally.

Codex runs without interactive approval requests or a nested process sandbox:
Agent CLI passes `--dangerously-bypass-approvals-and-sandbox`; Codex ACP starts
in the adapter's `agent-full-access` mode, which maps to approval `never` and
danger-full-access. This is an explicit relocation of the security boundary to
Docker and the dedicated unprivileged `agent` account, not an assertion that
browser or MCP actions are intrinsically safe. It also avoids Bubblewrap's
unprivileged-user-namespace requirement, which Docker's default seccomp profile
blocks; Oduflow does not weaken that outer profile to make a nested sandbox run.

## How it works

- The multi-architecture image moves to Node 24, installs a pinned
  `agent-browser` package, and installs Debian's Chromium package for both
  amd64 and arm64. `AGENT_BROWSER_EXECUTABLE_PATH` selects that system browser.
- The Codex HOME config declares `agent_browser`; per-checkout Claude `.mcp.json`
  declares the same stdio server. Codex ACP session-open frames carry only the
  scoped Oduflow HTTP server: Codex already has `agent_browser` from its HOME
  config, and codex-acp rejects a client server whose name the config defines.
  OpenCode, which has no such config entry, receives `agent_browser` in its
  session-open frames.
- `AGENT_BROWSER_SESSION` equals the checkout basename on every CLI/ACP exec.
  Agent Browser state remains on the persistent HOME volume, but profiles do
  not collide across environments.
- The long-lived container receives a larger shared-memory allocation for
  Chromium stability. It and every agent process still run as uid/gid 1000
  (`agent`); no host browser socket or host filesystem is mounted. Codex CLI
  bypasses its own Bubblewrap sandbox while Docker's seccomp remains enabled.

## Consequences

- Browser automation is deterministic and immediately available from either
  hosted agent, including fresh HOME volumes and both supported CPU families.
- Codex can invoke all installed MCP methods without pausing for approvals.
  A compromised prompt can therefore exercise the full authority already
  granted to that session: its checkout, browser profile, outbound network, and
  environment-scoped Oduflow MCP allowlist. It still cannot obtain the team
  token or cross Docker's per-team container/network boundary by design.
- The coder image is larger because Chromium is preinstalled, and its base must
  remain on a Node version supported by Agent Browser.
- Redistribution remains permitted: Agent Browser/Codex components are
  Apache-2.0, while Debian Chromium carries and preserves its upstream
  BSD-style/component notices. Proprietary Claude packages remain runtime-only.

## History

- 2026-10-16 — Codex ACP sessions no longer receive `agent_browser` in
  session-open frames: codex-acp 2.2.2 skipped the duplicate of the HOME-config
  entry and reported it as a failed tool call in every new Agent Chat. The
  config entry forwards `AGENT_BROWSER_SESSION`, so per-environment profiles
  are unchanged. The image now pins Codex to the version codex-acp itself
  depends on (the adapter runs that package, not the `codex` on PATH), so
  Agent CLI and Agent Chat run one Codex.
- 2026-07-24 — extended the same browser and container-boundary trust model to
  OpenCode: Agent CLI uses auto approval, native ACP uses `permission = allow`,
  and both receive the environment-specific Agent Browser profile.
- 2026-07-21 — adopted built-in Agent Browser MCP, per-environment browser
  profiles, and non-interactive Codex approval policy.
