#!/usr/bin/env bash
# Seed one environment's HOME inside the team's coding-agent container.
#
# Runs AS THE ENVIRONMENT'S OWN UID (env-user.sh drops to it with setpriv), so
# nothing here can be tricked into writing outside that HOME. Idempotent; runs
# whenever the environment is provisioned and after every container
# (re)creation, so a rotated key or credential reaches every environment.
#
# Expected environment (set by env-user.sh, plus the container's team env):
#   HOME, CLAUDE_CONFIG_DIR, CODEX_HOME,  this environment's private paths
#   TMPDIR
#   SEED_GIT_CREDENTIALS=1                fd 3 carries the team git store
#   SEED_SSH_KEY=1                        fd 4 carries the team SSH deploy key
#   ODUFLOW_GIT_USER                      default commit identity (optional)
#   ODUFLOW_BRAND_SLUG                    white-label brand for that identity
#                                         (optional; stock stays oduflow)
# Provider auth (subscription takes precedence over key, per provider):
#   CLAUDE_CODE_OAUTH_TOKEN | ANTHROPIC_API_KEY
#   OPENAI_API_KEY
#
# Interactive logins (Claude /login, codex login, OpenCode auth) are per
# environment by design: they land in this HOME and no other environment can
# read or replace them. See specs/0079-per-environment-agent-identities.md.

set -euo pipefail
umask 077

log() { printf '[seed-home] %s\n' "$*" >&2; }

# Replace FILE with stdin atomically.
write_file() {
    local file="$1"
    local tmp
    tmp="$(mktemp "$file.tmp.XXXXXX")"
    cat > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$file"
}

# --- 1. git identity + credentials ------------------------------------------
BRAND_SLUG="${ODUFLOW_BRAND_SLUG:-oduflow}"
git config --global user.name  "${ODUFLOW_GIT_USER:-${BRAND_SLUG}-coder}"
git config --global user.email "${ODUFLOW_GIT_USER:-agent}@${BRAND_SLUG}.local"
if [ "${SEED_GIT_CREDENTIALS:-0}" = 1 ]; then
    # A private copy: a credential refresh in one environment never writes
    # back to the team store or into another environment.
    write_file "$HOME/.git-credentials" <&3
    git config --global credential.helper "store --file=$HOME/.git-credentials"
fi

# --- 2. SSH deploy key -------------------------------------------------------
if [ "${SEED_SSH_KEY:-0}" = 1 ]; then
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    write_file "$HOME/.ssh/id_ed25519" <&4
    # Oduflow's SSH policy lives in its own file, rewritten on every seed so
    # policy changes reach existing (persistent) homes; the user-editable
    # config only carries the Include. Mirrors the server-side
    # team_ssh_command options.
    printf '%s\n' 'Host *' '  IdentitiesOnly yes' '  BatchMode yes' \
        '  StrictHostKeyChecking accept-new' | write_file "$HOME/.ssh/oduflow_config"
    if [ ! -f "$HOME/.ssh/config" ]; then
        printf 'Include oduflow_config\n' | write_file "$HOME/.ssh/config"
    elif ! grep -q 'Include oduflow_config' "$HOME/.ssh/config"; then
        { printf 'Include oduflow_config\n'; cat "$HOME/.ssh/config"; } \
            | write_file "$HOME/.ssh/config"
    fi
fi

# --- 3. Claude: onboarding + approval-free sessions --------------------------
# An environment credential authenticates Claude Code but does not complete its
# first-run wizard. Pre-seed that persistent state when either credential is
# configured; without one, leave the normal interactive login path untouched.
merge_json() {
    local file="$1"
    local filter="$2"
    shift 2
    local src="$file"
    if [ ! -f "$file" ] || ! jq -e 'type == "object"' "$file" >/dev/null 2>&1; then
        if [ -e "$file" ]; then
            log "WARNING: replacing invalid JSON at $file"
        fi
        src=/dev/null
    fi
    local out
    if [ "$src" = /dev/null ]; then
        out="$(jq --null-input "$@" "$filter")" || return 1
    else
        out="$(jq "$@" "$filter" "$file")" || return 1
    fi
    printf '%s\n' "$out" | write_file "$file"
}

mkdir -p "$CLAUDE_CONFIG_DIR"
claude_api_key_suffix=""
claude_auth=""
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    claude_auth="subscription"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    claude_auth="API key"
    claude_api_key_suffix="$ANTHROPIC_API_KEY"
    if [ "${#claude_api_key_suffix}" -gt 20 ]; then
        claude_api_key_suffix="${claude_api_key_suffix: -20}"
    fi
fi
if [ -n "$claude_auth" ]; then
    merge_json "$CLAUDE_CONFIG_DIR/.claude.json" '
        .hasCompletedOnboarding = true
        | if $api_key_suffix != "" then
            .customApiKeyResponses = (
                if (.customApiKeyResponses | type) == "object"
                then .customApiKeyResponses
                else {}
                end
            )
            | .customApiKeyResponses.approved = (
                ((.customApiKeyResponses.approved // [])
                    | if type == "array" then . else [] end)
                + [$api_key_suffix]
                | unique
            )
          else .
          end
    ' --arg api_key_suffix "$claude_api_key_suffix" \
        || log "WARNING: could not pre-seed Claude onboarding for $claude_auth auth"
fi
unset claude_api_key_suffix

# Browser Agent Chat/CLI run without per-tool approval prompts, exactly like
# Codex (agent-full-access): Docker + this environment's unprivileged uid are
# the security boundary. The maintained ACP adapter honors
# permissions.defaultMode from the USER-tier settings file; the CLI console
# additionally passes --dangerously-skip-permissions. Merge (never clobber) so
# the user's own settings survive; only the defaultMode key is set.
merge_json "$CLAUDE_CONFIG_DIR/settings.json" '
    .permissions = (
        if (.permissions | type) == "object" then .permissions else {} end
    )
    | .permissions.defaultMode = "bypassPermissions"
' || log "WARNING: could not seed Claude approval-free settings"

# --- 4. Codex: base config + API key login -----------------------------------
# Streamable-HTTP MCP needs the rmcp client. The Oduflow MCP server itself is
# deliberately NOT configured here: its URL and token are per session (scoped
# tokens, ADR 0028) and come from the web console as `codex -c` overrides +
# the session's exec env. Agent Browser is local and credentialless.
mkdir -p "$CODEX_HOME"
{
    echo "[features]"
    echo "experimental_use_rmcp_client = true"
    echo
    echo "[mcp_servers.agent_browser]"
    echo 'command = "agent-browser"'
    echo 'args = ["mcp", "--tools", "all"]'
    echo 'env_vars = ["AGENT_BROWSER_SESSION", "AGENT_BROWSER_EXECUTABLE_PATH"]'
} | write_file "$CODEX_HOME/config.toml"

# Current Codex releases require an explicit login to materialize auth.json;
# merely inheriting OPENAI_API_KEY is not enough. Re-run only when the key
# changed (a fingerprint, never the key, is kept), so re-seeding every
# environment after a container recreation stays cheap. The key is passed on
# stdin and never appears in argv/logs.
if [ -n "${OPENAI_API_KEY:-}" ]; then
    key_sha="$(printf '%s' "$OPENAI_API_KEY" | sha256sum | cut -d' ' -f1)"
    marker="$CODEX_HOME/.oduflow-api-key.sha256"
    if [ ! -f "$CODEX_HOME/auth.json" ] || [ "$(cat "$marker" 2>/dev/null)" != "$key_sha" ]; then
        if printf '%s' "$OPENAI_API_KEY" | codex login --with-api-key >/dev/null 2>&1; then
            printf '%s\n' "$key_sha" | write_file "$marker"
        else
            log "WARNING: codex login --with-api-key failed (Codex will prompt for auth)"
        fi
    fi
fi
