#!/usr/bin/env bash
# Runtime setup for the Oduflow per-team agent container.
#
# A single container serves every environment of one team, but every
# environment has its own Linux uid and HOME (created by env-user.sh, seeded
# by seed-home.sh, both driven by Oduflow via `docker exec`). This entrypoint,
# running as the shared `agent` user, only handles what is genuinely
# team-wide:
#
#   1. Install Claude Code + its ACP adapter at FIRST start, from npm, into the
#      npm prefix on the persistent home volume, and keep that prefix
#      readable-but-not-writable for the environment uids that run it. They are
#      proprietary (Anthropic Commercial Terms), so the published image must
#      not redistribute them; the container downloads them directly, once, and
#      refreshes them in the background on every later start (the environment
#      uids cannot self-update a shared install).
#   2. Stay alive. Per-environment identities, checkouts and agent processes
#      are created on demand by Oduflow (`docker exec`).
#
# MCP wiring is per SESSION, not per container: the container holds no Oduflow
# token at all. Each console/chat exec runs as the environment's uid and
# injects ODUFLOW_MCP_TOKEN — the SCOPED per-environment token (see ADR 0028)
# — plus the /mcp/<env> URL into its own environment, which other
# environments' uids cannot read. See
# specs/0079-per-environment-agent-identities.md.
#
# Provider auth (team-wide, from [team.X.agent_env]) is in the container env
# and seeded into each environment's HOME by seed-home.sh:
#   CLAUDE_CODE_OAUTH_TOKEN | ANTHROPIC_API_KEY   (+ optional ANTHROPIC_MODEL)
#   OPENAI_API_KEY                                 (+ optional CODEX_MODEL)
#   OPENCODE_API_KEY | any provider-specific variables supported by OpenCode
# Mounts:
#   HOME (/home/agent)   persistent home volume (shared npm prefix, team git
#                        credentials/SSH key, per-environment HOMEs in envs/)
#   /workspace           persistent workspace volume (per-env checkouts)

set -euo pipefail

log() { printf '[agent-entrypoint] %s\n' "$*" >&2; }

CLAUDE_PACKAGES=(@anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp)

# Environment uids run Claude Code from here but must never write to it: a
# binary one environment could replace would run in every other's session.
share_npm_prefix() {
    chmod -R go+rX,go-w -- "$NPM_CONFIG_PREFIX" 2>/dev/null || true
}

# --- 1. Claude Code: runtime install onto the home volume --------------------
# Not baked into the published image (proprietary; see the Dockerfile header).
# NPM_CONFIG_PREFIX points at $HOME/.npm-global (persistent volume), so this
# downloads once per volume, not once per container. Failure is non-fatal:
# Codex still works; Claude consoles/chats will report the missing binary.
umask 022
if command -v claude >/dev/null 2>&1 && command -v claude-agent-acp >/dev/null 2>&1; then
    log "Claude Code present ($(command -v claude)); refreshing in the background"
    ( npm install -g "${CLAUDE_PACKAGES[@]/%/@latest}" >/dev/null 2>&1 \
        && share_npm_prefix \
        && log "Claude Code refreshed" ) &
else
    log "installing Claude Code + ACP adapter from npm (first start; persists on the home volume)"
    # The ACP adapter moved orgs: @zed-industries/claude-code-acp is deprecated
    # and frozen; @agentclientprotocol/claude-agent-acp (bin: claude-agent-acp)
    # is the maintained successor. The presence check above looks for the new
    # bin, so a home volume seeded with the old adapter reinstalls onto the new
    # one on its next start. Drop the deprecated package so its stale
    # claude-code-acp bin doesn't linger on PATH (best-effort).
    npm uninstall -g @zed-industries/claude-code-acp >/dev/null 2>&1 || true
    if npm install -g "${CLAUDE_PACKAGES[@]}" >/dev/null 2>&1; then
        log "Claude Code installed"
    else
        log "WARNING: Claude Code install failed (no network?); Claude consoles/chats will not work until it succeeds on a restart"
    fi
fi
share_npm_prefix

# --- 2. stay alive -----------------------------------------------------------
log "ready; per-environment identities, checkouts and agent processes are created on demand"
exec sleep infinity
