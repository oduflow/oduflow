# Oduflow per-team coding-agent image ("oduflow-coder").
#
# Published to Docker Hub as `oduist/oduflow-coder`. A SINGLE container serves
# one team (see env_ops lifecycle hooks): its HOME (auth + sessions) and
# /workspace (one checkout per environment at /workspace/<slug>) live on
# persistent named volumes, so a login done once survives container recreation
# and is shared by every environment's console. It drives environments purely
# through the Oduflow MCP server (git push -> pull_and_apply) -- it never
# touches host files. The web console execs the chosen agent on demand
# (`docker exec` with a TTY, workdir = the env checkout); ACP adapters serve
# the structured browser chat over the same bridge. The container just stays
# alive.
#
# LICENSING: the published image contains redistributable open-source software.
# Codex CLI, codex-acp and agent-browser are Apache-2.0; OpenCode is MIT; ruff is
# MIT/Apache-2.0; Debian's Chromium package carries its upstream
# BSD-style/component licenses and preserves the corresponding notices under
# /usr/share/doc. Claude Code
# (@anthropic-ai/claude-code) and its ACP adapter are PROPRIETARY-adjacent
# (Anthropic Commercial Terms; the adapter pulls Anthropic's closed SDK), so
# they are NOT part of the published image: entrypoint.sh npm-installs them at
# first container start into an npm prefix on the persistent HOME volume. The
# end user downloads them from npm directly (like npx) -- we never
# redistribute them.
#
# Bump CODER_VERSION on every change; CI publishes the immutable :<version>
# tag only (see AGENTS.md "Publishing the Coder Image").
# See specs/0029-agent-console-and-chat.md.

FROM node:24-bookworm-slim

ARG CODER_VERSION=0.3.1
LABEL org.opencontainers.image.title="oduflow-coder" \
      org.opencontainers.image.version="${CODER_VERSION}" \
      org.opencontainers.image.source="https://github.com/oduflow/oduflow"

# Shell tools the coding agent reaches for from Bash: curl (ad-hoc HTTP), less
# (pager other CLIs shell out to), ripgrep (rg; fast code search), fd-find +
# tree (file find + dir overview), and python3/pip (host for ruff, below).
# fd-find installs its binary as `fdfind` on Debian to avoid a name clash, so
# symlink `fd` onto PATH for the command name agents expect.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        fd-find \
        jq \
        less \
        python3 \
        python3-pip \
        ripgrep \
        tree \
        chromium \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(command -v fdfind)" /usr/local/bin/fd

# Ruff: fast lint + format for Odoo Python, so the agent can validate a change
# locally before git push + pull_and_apply. Self-contained binary; Debian marks
# the system env externally-managed (PEP 668), hence --break-system-packages.
RUN python3 -m pip install --no-cache-dir --break-system-packages ruff

# The upstream Node image already reserves uid/gid 1000 for its `node` user.
# Rename that account instead of inventing another numeric identity: files in
# the persistent HOME/workspace volumes have one stable owner on every host.
RUN usermod --login agent --home /home/agent --move-home --shell /bin/bash node \
    && groupmod --new-name agent node \
    && mkdir -p /workspace \
    && chown agent:agent /workspace

# Apache-2.0 npm components (redistribution OK):
#   - OpenAI Codex CLI + its ACP (Agent Client Protocol) adapter;
#   - agent-browser, including its stdio MCP server. Pin the browser package so
#     its MCP surface is deterministic; current releases require Node >= 24.
# MIT component:
#   - OpenCode, including its native `opencode acp` server. The coder image is
#     immutable, and Oduflow disables OpenCode's runtime self-update per session.
# NOTE: the Codex ACP bridge package is still settling (published
# `@agentclientprotocol/codex-acp`, `@zed-industries/codex-acp` on GitHub, and an
# emerging native `codex --acp`). Confirm the working one at build time.
RUN npm install -g \
        @openai/codex \
        @agentclientprotocol/codex-acp \
        opencode-ai \
        agent-browser@0.32.3 \
    && npm cache clean --force

# Agent auth + sessions live under HOME (mounted from the persistent home
# volume: ~/.claude.json, ~/.claude/ incl. projects/ = sessions, ~/.codex/,
# ~/.config/opencode and ~/.local/share/opencode).
# Per-env checkouts live under /workspace (persistent workspace volume) at
# /workspace/<slug>. Config dirs are pinned under HOME explicitly so they land
# on the volume regardless of each CLI's default resolution. The npm prefix
# for the runtime-installed Claude packages also sits on the HOME volume
# (one-time download, survives recreation); its bin dir is on PATH for both
# the entrypoint and `docker exec` sessions.
ENV HOME=/home/agent \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    CODEX_HOME=/home/agent/.codex \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY clone-env.sh /usr/local/bin/clone-env.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/clone-env.sh

USER agent
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
