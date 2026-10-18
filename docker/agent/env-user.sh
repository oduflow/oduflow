#!/usr/bin/env bash
# Per-environment Linux identities inside the team's coding-agent container.
# Invoked by Oduflow as root via `docker exec -u root`. Idempotent.
#
# Usage:
#   env-user.sh sync SLUG:UID...   provision every listed environment (after
#                                  the container is (re)created: /etc/passwd
#                                  lives in the container layer)
#   env-user.sh provision SLUG UID provision one environment
#   env-user.sh remove SLUG UID    kill its processes, delete its checkout,
#                                  attachments, HOME and TMPDIR, drop the
#                                  account; exits non-zero if data remains
#
# One container serves every environment of a team, but each environment's
# console/chat sessions run as their own uid (allocated by Oduflow, never
# reused), so the kernel keeps environments apart: no reading another
# environment's /proc/<pid>/environ (its scoped MCP token), checkout,
# attachments or transcripts. Layout:
#
#   /workspace                     agent 0711  traversable, not listable
#   /workspace/<slug>              uid   0700  the environment's checkout
#   /workspace/.oduflow-uploads    agent 0711
#   /workspace/.oduflow-uploads/<slug>  uid 0700  Agent Chat attachments
#   /home/agent                    agent 0711  (persistent home volume)
#   /home/agent/.npm-global        agent 0755  Claude Code, run by everyone
#   /home/agent/envs/<uid>         uid   0700  the environment's HOME
#   /tmp/user                      root  0711  (from the image)
#   /tmp/user/<uid>                uid   0700  the environment's TMPDIR
#   anything else in /home/agent   agent, no group/other access (the team git
#                                  credential store and SSH key are copied
#                                  from here into each environment's HOME)
#
# Root never writes into an environment-owned directory the environment has
# already been able to touch: anything that seeds such a HOME runs as the
# environment's uid (seed-home.sh via setpriv), with root-only sources passed
# as already-open file descriptors. See
# specs/0079-per-environment-agent-identities.md.

set -euo pipefail

log() { printf '[env-user] %s\n' "$*" >&2; }

AGENT_HOME=/home/agent
ENV_HOMES="$AGENT_HOME/envs"
UPLOADS=/workspace/.oduflow-uploads
TMP_ROOT=/tmp/user
# Present once the shared-HOME history has been split (see below).
HISTORY_MARK="$AGENT_HOME/.oduflow-history-migrated"
# Matches agent_uids.UID_BASE: anything below is an account of the image.
UID_BASE=20000

if [ "$(id -u)" != 0 ]; then
    log "must run as root"
    exit 64
fi

valid_slug() { [[ "$1" =~ ^[a-z0-9_-]{1,63}$ ]]; }
valid_uid() { [[ "$1" =~ ^[0-9]{5,9}$ ]] && [ "$1" -ge "$UID_BASE" ]; }

# --- team-level permissions ----------------------------------------------------
harden_shared() {
    if [ -L "$TMP_ROOT" ]; then
        log "refusing symlink at $TMP_ROOT"
        return 1
    fi
    chmod 0711 "$AGENT_HOME" /workspace
    install -d -m 0711 -o agent -g agent "$ENV_HOMES" "$UPLOADS"
    install -d -m 0711 -o root -g root "$TMP_ROOT"
    # The agent user's own HOME content (team git credentials, SSH key, the
    # transcripts of the shared-HOME era) is private; only the npm prefix that
    # holds Claude Code is shared, read-only (the entrypoint keeps it that way
    # after every install). Checkouts/attachments not owned by an environment
    # (leftovers of the shared-uid era) must not stay readable by path either.
    # Only entries that are still open are touched; one that vanishes
    # meanwhile (npm reshuffling its cache) is no reason to stop.
    find "$AGENT_HOME" -mindepth 1 -maxdepth 1 \
        ! -path "$ENV_HOMES" ! -path "$AGENT_HOME/.npm-global" \
        ! -type l -perm /077 -exec chmod go-rwx -- {} + 2>/dev/null ||
        log "WARNING: could not make every entry of $AGENT_HOME private"
    find /workspace "$UPLOADS" -mindepth 1 -maxdepth 1 ! -path "$UPLOADS" \
        ! -type l -perm /077 -exec chmod go-rwx -- {} + 2>/dev/null ||
        log "WARNING: could not make every entry of /workspace private"
}

# --- account + private directories -------------------------------------------
# Every per-environment step reports failure through its return status (bash
# ignores `set -e` inside functions called from a condition), so one broken
# environment is logged and skipped instead of aborting the whole sync.
ensure_account() {
    local uid="$1"
    local name="env$uid"
    getent group "$uid" >/dev/null || groupadd --gid "$uid" "$name" || return 1
    getent passwd "$uid" >/dev/null ||
        useradd --uid "$uid" --gid "$uid" --no-create-home --no-user-group \
            --home-dir "$ENV_HOMES/$uid" --shell /bin/bash "$name"
}

# Make DIR a 0700 directory owned by UID. Prints "created" when it did not
# exist yet. A directory of the shared-uid era is handed over recursively (-h
# never follows symlinks). One owned by ANOTHER environment's uid is a deleted
# environment's leftover whose cleanup has not run yet: it is never handed to
# a new owner.
own_private_dir() {
    local dir="$1" uid="$2" owner
    if [ -L "$dir" ]; then
        log "refusing symlink at $dir"
        return 1
    fi
    if [ ! -e "$dir" ]; then
        install -d -m 0700 -o "$uid" -g "$uid" -- "$dir" || return 1
        echo created
        return 0
    fi
    owner="$(stat -c %u -- "$dir")" || return 1
    if [ "$owner" != "$uid" ]; then
        if [ "$owner" -ge "$UID_BASE" ]; then
            log "refusing $dir: it still belongs to uid $owner"
            return 1
        fi
        chown -hR "$uid:$uid" -- "$dir" || return 1
    fi
    chmod 0700 -- "$dir"
}

# --- one-time history migration ------------------------------------------------
# Before per-environment uids every session shared /home/agent, so Claude and
# Codex transcripts of all environments lie there. Until the first sync has
# completed (HISTORY_MARK), each environment's own move into its fresh HOME
# (only ever into a HOME created in this same run, so the environment has not
# been able to plant anything there). Never afterwards: a later environment
# that reuses the slug of one deleted before the upgrade must not receive its
# transcripts. OpenCode keys its storage by repository, not checkout, so its
# history is not split.
migrate_claude_history() {
    local slug="$1" uid="$2"
    local name src dest_root
    # Claude names a project directory after its cwd, every
    # non-alphanumeric character replaced by "-".
    name="$(printf '%s' "/workspace/$slug" | sed 's/[^a-zA-Z0-9]/-/g')"
    src="$AGENT_HOME/.claude/projects/$name"
    dest_root="$ENV_HOMES/$uid/.claude"
    if [ ! -d "$src" ] || [ -L "$src" ]; then
        return 0
    fi
    install -d -m 0700 -o "$uid" -g "$uid" -- "$dest_root" "$dest_root/projects" &&
        mv -- "$src" "$dest_root/projects/$name" &&
        chown -hR "$uid:$uid" -- "$dest_root/projects/$name" &&
        log "moved Claude history of $slug"
}

# Codex keeps rollouts by date; the first line of each records its cwd. One
# pass over all of them, routing each to the environment that owns its cwd.
# Arguments: SLUG:UID pairs of environments whose HOME was just created.
migrate_codex_history() {
    local src_root="$AGENT_HOME/.codex/sessions"
    if [ "$#" -eq 0 ] || [ ! -d "$src_root" ]; then
        return 0
    fi
    local -A fresh=()
    local pair file cwd slug uid dest
    for pair in "$@"; do
        fresh["${pair%%:*}"]="${pair##*:}"
    done
    while IFS= read -r -d '' file; do
        cwd="$(head -n 1 -- "$file" | jq -r '.payload.cwd // .cwd // empty' 2>/dev/null || true)"
        [[ "$cwd" == /workspace/* ]] || continue
        slug="${cwd#/workspace/}"
        slug="${slug%%/*}"
        uid="${fresh[$slug]:-}"
        [ -n "$uid" ] || continue
        dest="$ENV_HOMES/$uid/.codex/sessions/${file#"$src_root"/}"
        setpriv --reuid="$uid" --regid="$uid" --clear-groups --no-new-privs \
            mkdir -p -m 0700 -- "$(dirname -- "$dest")" &&
            mv -- "$file" "$dest" &&
            chown -h "$uid:$uid" -- "$dest" ||
            log "WARNING: could not move Codex rollout $file"
    done < <(find "$src_root" -type f -name 'rollout-*.jsonl' -print0)
}

# --- HOME seeding (as the environment) ---------------------------------------
seed_home() {
    local uid="$1"
    local home="$ENV_HOMES/$uid"
    local cred="$AGENT_HOME/.git-credentials"
    local key="$AGENT_HOME/.ssh/id_ed25519"
    local has_cred=0 has_key=0 rc=0
    if [ -f "$cred" ] && [ ! -L "$cred" ]; then has_cred=1; fi
    if [ -f "$key" ] && [ ! -L "$key" ]; then has_key=1; fi
    # Root opens the agent-only sources; the unprivileged seeder only ever
    # sees their content on fds 3/4.
    exec 3</dev/null 4</dev/null
    if [ "$has_cred" = 1 ]; then exec 3<"$cred"; fi
    if [ "$has_key" = 1 ]; then exec 4<"$key"; fi
    env HOME="$home" \
        CLAUDE_CONFIG_DIR="$home/.claude" \
        CODEX_HOME="$home/.codex" \
        TMPDIR="$TMP_ROOT/$uid" \
        SEED_GIT_CREDENTIALS="$has_cred" \
        SEED_SSH_KEY="$has_key" \
        setpriv --reuid="$uid" --regid="$uid" --clear-groups --no-new-privs \
        /usr/local/bin/seed-home.sh || rc=$?
    exec 3<&- 4<&-
    return "$rc"
}

# --- commands ------------------------------------------------------------------
# provision_many MODE SLUG:UID... — accounts and directories first, then the
# one-time history migration for HOMEs created now, then seeding. MODE "sync"
# also completes the history migration once and for all.
provision_many() {
    local mode="$1"
    shift
    local pair slug uid home_state failed=0 migrate=0
    local -a fresh=() ready=()
    harden_shared
    if [ ! -e "$HISTORY_MARK" ]; then migrate=1; fi
    for pair in "$@"; do
        slug="${pair%%:*}"
        uid="${pair##*:}"
        if ! valid_slug "$slug" || ! valid_uid "$uid"; then
            log "WARNING: skipping invalid environment $pair"
            failed=1
            continue
        fi
        if ! ensure_account "$uid" ||
            ! home_state="$(own_private_dir "$ENV_HOMES/$uid" "$uid")" ||
            ! own_private_dir "$TMP_ROOT/$uid" "$uid" >/dev/null ||
            ! own_private_dir "/workspace/$slug" "$uid" >/dev/null ||
            ! own_private_dir "$UPLOADS/$slug" "$uid" >/dev/null; then
            log "WARNING: could not provision environment $slug (uid $uid)"
            failed=1
            continue
        fi
        if [ "$home_state" = created ] && [ "$migrate" = 1 ]; then
            fresh+=("$slug:$uid")
            migrate_claude_history "$slug" "$uid" ||
                log "WARNING: could not move Claude history of $slug"
        fi
        ready+=("$uid")
    done
    migrate_codex_history "${fresh[@]}"
    if [ "$mode" = sync ] && [ "$migrate" = 1 ]; then
        install -m 0600 -o agent -g agent /dev/null "$HISTORY_MARK"
        log "shared-HOME history migration complete"
    fi
    for uid in "${ready[@]}"; do
        if ! seed_home "$uid"; then
            log "WARNING: seeding HOME of uid $uid failed"
            failed=1
        fi
    done
    return "$failed"
}

# Delete a slug-keyed directory of a removed environment, unless another
# environment has taken the slug over since (it then belongs to that uid).
remove_slug_dir() {
    local dir="$1" uid="$2" owner
    if [ ! -e "$dir" ] && [ ! -L "$dir" ]; then
        return 0
    fi
    owner="$(stat -c %u -- "$dir")" || return 1
    if [ "$owner" != "$uid" ] && [ "$owner" -ge "$UID_BASE" ]; then
        log "keeping $dir: it belongs to uid $owner"
        return 0
    fi
    rm -rf -- "$dir"
}

remove_env() {
    local slug="$1" uid="$2" rc=0
    if ! valid_slug "$slug" || ! valid_uid "$uid"; then
        log "refusing to remove invalid environment $slug:$uid"
        exit 64
    fi
    pkill -KILL -u "$uid" 2>/dev/null || true
    remove_slug_dir "/workspace/$slug" "$uid" || rc=1
    remove_slug_dir "$UPLOADS/$slug" "$uid" || rc=1
    rm -rf -- "$ENV_HOMES/$uid" "$TMP_ROOT/$uid" || rc=1
    # The account lives in the container layer and its uid is never reused,
    # so one that lingers is harmless; only the data decides success.
    if getent passwd "$uid" >/dev/null; then
        userdel "$(getent passwd "$uid" | cut -d: -f1)" ||
            log "WARNING: could not delete the account of uid $uid"
    fi
    if getent group "$uid" >/dev/null; then
        groupdel "$(getent group "$uid" | cut -d: -f1)" ||
            log "WARNING: could not delete the group of uid $uid"
    fi
    if [ "$rc" = 0 ]; then
        log "removed environment $slug (uid $uid)"
    else
        log "WARNING: files of environment $slug (uid $uid) remain"
    fi
    return "$rc"
}

cmd="${1:-}"
shift || true
case "$cmd" in
    sync) provision_many sync "$@" ;;
    provision)
        [ "$#" -eq 2 ] || { log "usage: env-user.sh provision SLUG UID"; exit 64; }
        provision_many provision "$1:$2"
        ;;
    remove)
        [ "$#" -eq 2 ] || { log "usage: env-user.sh remove SLUG UID"; exit 64; }
        remove_env "$1" "$2"
        ;;
    *)
        log "usage: env-user.sh sync SLUG:UID... | provision SLUG UID | remove SLUG UID"
        exit 64
        ;;
esac
