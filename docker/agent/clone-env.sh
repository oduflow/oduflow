#!/usr/bin/env bash
# Create/refresh one environment's working checkout inside the team's agent
# container. Invoked from the env_ops create hook via `docker exec` AS THE
# ENVIRONMENT'S OWN UID, with HOME set to that environment's HOME (git
# credentials are wired there by seed-home.sh). /workspace/<SLUG> already
# exists, empty and owned by that uid (env-user.sh). Idempotent.
#
# Usage: clone-env.sh REPO_URL BRANCH SLUG [MCP_URL] [GIT_USER]
#   REPO_URL    origin repo URL (no embedded creds; git creds are wired globally)
#   BRANCH      branch to check out
#   SLUG        filesystem-safe env slug; checkout goes to /workspace/<SLUG>
#   MCP_URL     this environment's SCOPED Oduflow MCP endpoint (/mcp/<env>;
#               optional; enables the Oduflow entry in Claude's .mcp.json)
#   GIT_USER    commit author for this checkout (optional; global default otherwise)
#
# No token argument on purpose: nothing secret is ever written to the checkout.
# .mcp.json references ${ODUFLOW_MCP_TOKEN}, which each console/chat session
# injects into its own `docker exec` environment with the per-environment
# scoped token (see web_ui.ws_agent_console / ws_agent_acp).
#
# Full clone (not --depth 1): the agent commits and pushes. The checkout lives on
# the persistent workspace volume, so it survives container recreation. Other
# environments' checkouts are deliberately NOT reachable (each is private to
# its own uid); other branches are read through git (`git fetch`,
# `git show origin/<branch>:<path>`). See
# specs/0079-per-environment-agent-identities.md.

set -euo pipefail

log() { printf '[clone-env] %s\n' "$*" >&2; }

REPO_URL="${1:-}"
BRANCH="${2:-}"
SLUG="${3:-}"
MCP_URL="${4:-}"
GIT_USER="${5:-}"

if [ -z "$REPO_URL" ] || [ -z "$BRANCH" ] || [ -z "$SLUG" ]; then
    log "WARNING: REPO_URL/BRANCH/SLUG required; skipping"
    exit 0
fi

DEST="/workspace/$SLUG"

if [ ! -d "$DEST/.git" ]; then
    log "cloning $BRANCH into $DEST"
    git clone --branch "$BRANCH" "$REPO_URL" "$DEST"
else
    # Refresh an existing checkout to the target branch at origin's tip. Fetch
    # failures are NOT suppressed (set -e aborts) so a broken refresh surfaces to
    # the caller instead of silently opening a console on stale code.
    log "checkout exists; refreshing $BRANCH in $DEST"
    git -C "$DEST" fetch origin "$BRANCH"
    # Only *tracked* modifications count as work to protect: `checkout -B` cannot
    # clobber untracked files (e.g. the generated .mcp.json), so ignoring them
    # keeps the refresh from being permanently blocked by our own artifacts.
    if [ -n "$(git -C "$DEST" status --porcelain --untracked-files=no)" ]; then
        # The agent has uncommitted work here. Never clobber it — but be loud so a
        # "refresh" is not mistaken for a clean sync to origin.
        log "WARNING: uncommitted changes to tracked files in $DEST; leaving the working tree as-is (NOT synced to origin/$BRANCH)"
    else
        # Clean tree: nothing to lose. Pin it exactly on the target branch at
        # origin's tip (fixes both a wrong branch and a stale commit).
        git -C "$DEST" checkout -B "$BRANCH" "origin/$BRANCH"
        log "synced $DEST to origin/$BRANCH"
    fi
fi

# Per-checkout commit author (the HOME's global identity is the team default).
if [ -n "$GIT_USER" ]; then
    git -C "$DEST" config user.name "$GIT_USER"
    git -C "$DEST" config user.email "${GIT_USER}@${ODUFLOW_BRAND_SLUG:-oduflow}.local"
fi

# Claude Code: project-scoped .mcp.json at the checkout root. Agent Browser is
# always available as a local stdio MCP. When present, the scoped `development`
# server's Authorization value is a ${VAR} placeholder — Claude expands it from the scoped token in the
# session's exec env, so no secret is written to the checkout.
jq -n \
    --arg url "$MCP_URL" \
    '{mcpServers: ({agent_browser: {type: "stdio", command: "agent-browser", args: ["mcp", "--tools", "all"]}} + (if $url == "" then {} else {development: {type: "http", url: $url, headers: {Authorization: "Bearer ${ODUFLOW_MCP_TOKEN}"}}} end))}' \
    > "$DEST/.mcp.json"
# Not secret (the Oduflow value is a placeholder, not a token), but still a
# generated artifact a `git add -A` by the agent should never push. Local
# exclude, so the repo's own .gitignore stays untouched.
mkdir -p "$DEST/.git/info"
if ! grep -qxF '/.mcp.json' "$DEST/.git/info/exclude" 2>/dev/null; then
    echo '/.mcp.json' >> "$DEST/.git/info/exclude"
fi
if [ -n "$MCP_URL" ]; then
    log "wrote $DEST/.mcp.json -> Agent Browser + $MCP_URL (git-excluded)"
else
    log "wrote $DEST/.mcp.json -> Agent Browser (git-excluded)"
fi
