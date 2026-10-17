# Oduflow per-team coding-agent image ("oduflow-coder").
#
# Published to Docker Hub as `oduist/oduflow-coder`. A SINGLE container serves
# one team (see env_ops lifecycle hooks), but every environment of the team
# runs as its OWN Linux uid with its own HOME (logins + sessions) and its own
# checkout (/workspace/<slug>), all on persistent named volumes. Oduflow
# creates those identities with env-user.sh (as root, via `docker exec`), which
# seeds each HOME through seed-home.sh running as that environment's uid. The
# web console execs the chosen agent on demand as the environment's uid
# (`docker exec` with a TTY, workdir = the env checkout); ACP adapters serve
# the structured browser chat over the same bridge. The agent drives its
# environment purely through the Oduflow MCP server (git push ->
# pull_and_apply) -- it never touches host files. The container just stays
# alive. See specs/0079-per-environment-agent-identities.md.
#
# LICENSING: the published image contains redistributable open-source software.
# Codex CLI, codex-acp and agent-browser are Apache-2.0; OpenCode and the
# GitHub CLI (gh) are MIT; ruff is MIT/Apache-2.0; Debian's Chromium package
# carries its upstream
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

ARG CODER_VERSION=0.4.0
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
        openssh-client \
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

# GitHub CLI (gh; MIT): PR/issue/API workflows from the console. Debian
# bookworm ships an old gh (2.23), so install a current one from GitHub's
# official apt repo (curl + ca-certificates come from the layer above).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Ruff: fast lint + format for Odoo Python, so the agent can validate a change
# locally before git push + pull_and_apply. Self-contained binary; Debian marks
# the system env externally-managed (PEP 668), hence --break-system-packages.
RUN python3 -m pip install --no-cache-dir --break-system-packages ruff

# The upstream Node image already reserves uid/gid 1000 for its `node` user.
# Rename that account instead of inventing another numeric identity: files in
# the persistent HOME/workspace volumes have one stable owner on every host.
# /tmp/user holds each environment's private TMPDIR (env-user.sh); created
# root-owned here, before any environment runs, so none can pre-create
# another's directory.
RUN usermod --login agent --home /home/agent --move-home --shell /bin/bash node \
    && groupmod --new-name agent node \
    && mkdir -p /workspace \
    && chown agent:agent /workspace \
    && install -d -m 0711 -o root -g root /tmp/user

# Apache-2.0 npm components (redistribution OK):
#   - OpenAI Codex CLI + its ACP (Agent Client Protocol) adapter;
#   - agent-browser, including its stdio MCP server. Pin the browser package so
#     its MCP surface is deterministic; current releases require Node >= 24.
# MIT component:
#   - OpenCode, including its native `opencode acp` server. The coder image is
#     immutable, and Oduflow disables OpenCode's runtime self-update per session.
# Codex and its ACP adapter are pinned like agent-browser: the image is immutable
# and Agent CLI disables Codex's own update check, so a new Codex arrives only with
# a new coder image. codex-acp does not run the `codex` on PATH: it resolves the
# @openai/codex package it depends on, so the adapter version decides Agent Chat's
# Codex. Pin the CLI to that same version so Agent CLI and Agent Chat run one
# Codex over one ~/.codex; the build fails when a bump moves only one of them.
RUN npm install -g \
        @openai/codex@0.160.1 \
        @agentclientprotocol/codex-acp@2.2.2 \
        opencode-ai \
        agent-browser@0.32.3 \
    && npm cache clean --force \
    && root="$(npm root -g)" \
    && cli="$(node -p "require('$root/@openai/codex/package.json').version")" \
    && chat="$(node -p "require(require.resolve('@openai/codex/package.json', {paths: ['$root/@agentclientprotocol/codex-acp/dist']})).version")" \
    && if [ "$cli" != "$chat" ]; then \
           echo "Codex CLI $cli differs from codex-acp's Codex $chat" >&2; exit 1; \
       fi

# HOME is the persistent home volume. For the shared `agent` user it holds the
# npm prefix for the runtime-installed Claude packages (one-time download,
# survives recreation; read-only to environments) and the team git
# credentials/SSH key copied in by Oduflow. Each environment's own HOME is
# /home/agent/envs/<uid>; its sessions override HOME, CLAUDE_CONFIG_DIR and
# CODEX_HOME per exec. The npm prefix bin dir is on PATH for everyone.
ENV HOME=/home/agent \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    CODEX_HOME=/home/agent/.codex \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /workspace

COPY entrypoint.sh clone-env.sh env-user.sh seed-home.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/clone-env.sh \
        /usr/local/bin/env-user.sh /usr/local/bin/seed-home.sh

USER agent
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
