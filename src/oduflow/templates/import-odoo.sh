#!/usr/bin/env bash
#
# Oduflow — import an Odoo.sh database into an Oduflow template.
#
# Run this INSIDE an Odoo.sh shell (production build). It finds the platform's
# latest daily backup (dump + filestore), then streams it to your Oduflow
# server, which restores it as a template. Nothing large is written to disk:
# the filestore is tarred on the fly and streamed straight to the upload.
#
# The upload is resumable: on a re-run it asks the server what it already has
# and only sends the missing pieces (manifest, dump, individual filestore
# hash-directories, or addon repositories).
#
#   curl -sSfL https://YOUR-ODUFLOW/import-odoo.sh | bash -s -- \
#        --server https://YOUR-ODUFLOW --token <TOKEN>
#
# Optional flags (usually set for you by the dashboard checkboxes) also bring
# over the addons Odoo.sh ran with, beyond the standard modules in the image:
#   --with-enterprise    download Odoo Enterprise into a local extra-addons repo
#   --with-themes        download Odoo Themes into a local extra-addons repo
#   --with-extra-addons  add the extra repos (OCA etc.): reachable ones are
#                        cloned from their origin (stay updatable), private ones
#                        are downloaded as local extra-addons repos
#   --without-filestore  database (and addons) only: skip the filestore upload;
#                        an existing template keeps its current filestore
#
# The --token is minted by the "Import from Odoo.sh" button in the Oduflow
# dashboard and is valid for 15 minutes.
set -euo pipefail

SERVER=""
TOKEN=""
WITH_ENTERPRISE=0
WITH_THEMES=0
WITH_EXTRA=0
WITHOUT_FILESTORE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="${2:-}"; shift 2 ;;
        --server=*) SERVER="${1#*=}"; shift ;;
        --token) TOKEN="${2:-}"; shift 2 ;;
        --token=*) TOKEN="${1#*=}"; shift ;;
        --with-enterprise) WITH_ENTERPRISE=1; shift ;;
        --with-themes) WITH_THEMES=1; shift ;;
        --with-extra-addons) WITH_EXTRA=1; shift ;;
        --without-filestore) WITHOUT_FILESTORE=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 28
            exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -n "$SERVER" ] || { echo "ERROR: --server is required" >&2; exit 2; }
[ -n "$TOKEN" ]  || { echo "ERROR: --token is required" >&2; exit 2; }
SERVER="${SERVER%/}"

# Resolve redirects up front (e.g. http -> https) so every upload hits the
# final URL directly — POSTs must not rely on redirect-following, which would
# drop the request body.
EFFECTIVE="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "${SERVER}/import-odoo.sh" 2>/dev/null || true)"
if [ -n "$EFFECTIVE" ] && [ "${EFFECTIVE%/import-odoo.sh}" != "$EFFECTIVE" ]; then
    SERVER="${EFFECTIVE%/import-odoo.sh}"
fi

DB="${PGDATABASE:-}"
[ -n "$DB" ] || {
    echo "ERROR: PGDATABASE is not set — run this inside the Odoo.sh shell." >&2
    exit 2
}

BK="$HOME/backup.daily/${DB}_daily"
SQL_GZ="${BK}.sql.gz"
MANIFEST="${BK}.json"
FS="${BK}/home/odoo/data/filestore/${DB}"

if [ ! -f "$SQL_GZ" ] || [ ! -f "$MANIFEST" ]; then
    echo "ERROR: daily backup not found for database '$DB'." >&2
    echo "       Expected:" >&2
    echo "         $SQL_GZ" >&2
    echo "         $MANIFEST" >&2
    echo "       Daily backups are kept on production builds. If it is missing," >&2
    echo "       trigger a backup from the Odoo.sh dashboard and try again." >&2
    exit 3
fi
if [ "$WITHOUT_FILESTORE" != 1 ] && [ ! -d "$FS" ]; then
    echo "ERROR: filestore directory not found: $FS" >&2
    exit 3
fi

AUTH="Authorization: Bearer ${TOKEN}"
API="${SERVER}/api/templates/import"

# Large single payloads (SQL dump, addon tarballs) are uploaded in parts so
# they survive proxies that cap request body size — Cloudflare, for one, rejects
# bodies over 100 MB with a 413. 90 MiB per part stays safely under that.
PART_BYTES=$((90 * 1024 * 1024))

# curl 7.76 added --fail-with-body; Odoo.sh images with older curl still need
# to run the importer, even though their -f fallback cannot preserve error
# bodies. Keep the richer diagnostic whenever the installed curl supports it.
CURL_FAIL=(-f)
if curl --help all 2>/dev/null | grep -q -- '--fail-with-body'; then
    CURL_FAIL=(--fail-with-body)
fi

# ---- helpers ---------------------------------------------------------------

human() {  # bytes -> human readable
    awk -v b="${1:-0}" 'BEGIN{
        split("B KB MB GB TB", u, " "); i=1;
        while (b>=1024 && i<5){ b/=1024; i++ }
        if (i==1) printf "%d%s", b, u[i]; else printf "%.1f%s", b, u[i]
    }'
}

json_field() {  # file expr  (expr is python on obj `d`)
    python3 -c 'import sys,json
d=json.load(open(sys.argv[1]))
print('"$2"')' "$1" 2>/dev/null || true
}

urlencode() {  # one query-string component
    python3 -c 'import sys, urllib.parse
print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

addon_remote_json() {  # name origin branch
    python3 -c 'import json, sys
print(json.dumps({"name": sys.argv[1], "origin_url": sys.argv[2], "branch": sys.argv[3]}))' \
        "$1" "$2" "$3"
}

upload_ranges() {  # file url extra_query start_offset [label]
    # POST a local file to `url` in <=PART_BYTES parts, one request each, so no
    # single request exceeds a proxy's body limit. Each part carries
    # offset=<byte>&total=<size>; the server appends in order and assembles the
    # file once the last part lands. Resumable: pass the server's current byte
    # count as start_offset. dd reads byte ranges directly (no temp splits).
    local file="$1" url="$2" extra="$3" off="${4:-0}" label="${5:-upload}"
    local total len q
    total="$(wc -c < "$file")"
    [ "$off" -lt 0 ] && off=0
    [ "$off" -gt "$total" ] && off=0   # stale/invalid resume point -> restart
    while [ "$off" -lt "$total" ]; do
        len=$PART_BYTES
        [ $((off + len)) -gt "$total" ] && len=$((total - off))
        q="offset=${off}&total=${total}"
        [ -n "$extra" ] && q="${extra}&${q}"
        local tries=0
        while :; do
            if dd if="$file" bs=1048576 iflag=skip_bytes,count_bytes \
                   skip="$off" count="$len" 2>/dev/null \
               | curl -fsS -H "$AUTH" -H "Content-Type: application/octet-stream" \
                      -T - -X POST "${url}?${q}" >/dev/null; then
                break
            fi
            tries=$((tries + 1))
            [ "$tries" -ge 3 ] && return 1
            sleep 2
        done
        off=$((off + len))
        local pct=0
        [ "$total" -gt 0 ] && pct=$(( off * 100 / total ))
        printf '\r>> %s [%3d%%] %s / %s\033[K' \
            "$label" "$pct" "$(human "$off")" "$(human "$total")" >&2
    done
    echo >&2
    return 0
}

# ---- status (drives resume) ------------------------------------------------

echo ">> Importing '$DB' into Oduflow at $SERVER"

STATUS_FILE="$(mktemp)"
trap 'rm -f "$STATUS_FILE"' EXIT
if ! curl -sS "${CURL_FAIL[@]}" -H "$AUTH" "${API}/status" -o "$STATUS_FILE" 2>/dev/null; then
    echo "ERROR: could not reach $SERVER or token is invalid/expired." >&2
    [ -s "$STATUS_FILE" ] && { echo "       Server said:" >&2; head -c 300 "$STATUS_FILE" >&2; echo >&2; }
    echo "       Generate a fresh token from the dashboard and retry." >&2
    exit 4
fi

# Hard gate: the status body must be Oduflow's JSON with ok=true. Anything
# else (a proxy page, a redirect body, an HTML error) means the uploads
# would go nowhere — abort NOW instead of "uploading" gigabytes into a 3xx.
if [ -z "$(json_field "$STATUS_FILE" 'd.get("ok") and 1 or ""')" ]; then
    echo "ERROR: unexpected response from ${API}/status:" >&2
    head -c 300 "$STATUS_FILE" >&2; echo >&2
    echo "       Check that --server points at your Oduflow dashboard URL" >&2
    echo "       (https, no proxy pages in between), then retry." >&2
    exit 4
fi

have_manifest="$(json_field "$STATUS_FILE" 'd.get("progress",{}).get("manifest") and 1 or ""')"
have_dump="$(json_field "$STATUS_FILE" 'd.get("progress",{}).get("dump") and 1 or ""')"
have_dump_bytes="$(json_field "$STATUS_FILE" 'str(d.get("progress",{}).get("dump_bytes",0))')"
[ -n "$have_dump_bytes" ] || have_dump_bytes=0
done_chunks=" $(json_field "$STATUS_FILE" '" ".join(d.get("progress",{}).get("filestore_chunks",[]))') "
done_addons=" $(json_field "$STATUS_FILE" '" ".join(d.get("progress",{}).get("addons",[]))') "
done_remote=" $(json_field "$STATUS_FILE" '" ".join(d.get("progress",{}).get("remote_addons",[]))') "

# ---- manifest --------------------------------------------------------------

if [ -z "$have_manifest" ]; then
    echo ">> uploading manifest"
    curl -fsS -H "$AUTH" -H "Content-Type: application/json" \
        --data-binary @"$MANIFEST" -X POST "${API}/manifest" >/dev/null
else
    echo ">> manifest already uploaded, skipping"
fi

# ---- dump ------------------------------------------------------------------

if [ -z "$have_dump" ]; then
    dump_total="$(wc -c < "$SQL_GZ")"
    echo ">> uploading SQL dump ($(human "$dump_total"), resuming from $(human "$have_dump_bytes"))"
    # Chunked so no single request exceeds a proxy body limit (e.g. Cloudflare's
    # 100 MB); resumes from what the server already has.
    if ! upload_ranges "$SQL_GZ" "${API}/dump" "" "$have_dump_bytes" "dump"; then
        echo "ERROR: failed to upload SQL dump after retries." >&2
        echo "       Re-run the same command to resume." >&2
        exit 5
    fi
else
    echo ">> SQL dump already uploaded, skipping"
fi

# ---- filestore (chunked by top-level hash directory) -----------------------

dir_bytes() {  # exact on GNU coreutils (du -sb); KB-approx fallback elsewhere
    local b
    if b="$(du -sb "$1" 2>/dev/null | cut -f1)"; then
        printf '%s' "$b"
    else
        b="$(du -sk "$1" | cut -f1)"
        printf '%s' "$((b * 1024))"
    fi
}

progress() {  # done total label
    local pct=0
    [ "$2" -gt 0 ] && pct=$(( $1 * 100 / $2 ))
    printf '\r>> filestore [%3d%%] %s / %s  %-14s\033[K' \
        "$pct" "$(human "$1")" "$(human "$2")" "$3" >&2
}

upload_chunk() {  # name
    local c="$1" tries=0
    while :; do
        # -T - streams the tar as it is produced (chunked transfer encoding);
        # --data-binary @- would buffer the whole chunk in RAM first.
        if tar -C "$FS" -cf - "$c" \
            | curl -fsS -H "$AUTH" -H "Content-Type: application/x-tar" \
                   -T - -X POST "${API}/filestore?chunk=${c}" >/dev/null; then
            return 0
        fi
        tries=$((tries + 1))
        [ "$tries" -ge 3 ] && return 1
        sleep 2
    done
}

if [ "$WITHOUT_FILESTORE" = 1 ]; then
    echo ">> filestore: skipped (--without-filestore)"
else
    chunks=()
    total_bytes=0
    for path in "$FS"/*; do
        [ -d "$path" ] || continue
        name="$(basename "$path")"
        if [[ "$name" =~ ^[0-9a-f]{2}$ ]] || [ "$name" = "checklist" ]; then
            chunks+=("$name")
            total_bytes=$((total_bytes + $(dir_bytes "$path")))
        fi
    done

    done_bytes=0
    remaining=()
    for c in "${chunks[@]}"; do
        if [[ "$done_chunks" == *" $c "* ]]; then
            done_bytes=$((done_bytes + $(dir_bytes "$FS/$c")))
        else
            remaining+=("$c")
        fi
    done

    echo ">> filestore: $(human "$total_bytes") in ${#chunks[@]} chunks (${#remaining[@]} to upload)"
    for c in "${remaining[@]}"; do
        progress "$done_bytes" "$total_bytes" "uploading $c"
        if ! upload_chunk "$c"; then
            echo >&2
            echo "ERROR: failed to upload filestore chunk '$c' after retries." >&2
            echo "       Re-run the same command to resume from here." >&2
            exit 5
        fi
        done_bytes=$((done_bytes + $(dir_bytes "$FS/$c")))
        progress "$done_bytes" "$total_bytes" "done $c"
    done
    progress "$total_bytes" "$total_bytes" "complete"
    echo >&2
fi

# ---- addons (optional: enterprise / themes / extra repos) ------------------
#
# The daily backup holds only the database and filestore; the addons Odoo.sh
# runs with live on the build filesystem. When asked (--with-*), inspect the
# running server's --addons-path, classify each entry, and bring over what the
# image doesn't already ship. Reachable extra repos are announced by their
# origin (Oduflow clones them, keeping them updatable); everything else is
# tarred and streamed as a local (remote-less) extra-addons repo.

detect_addons_path() {
    # Prefer the live odoo-bin command line (source of truth), fall back to the
    # generated odoo.conf. Only the "--addons-path=..." (=-joined) form is used
    # by Odoo.sh.
    local ap
    # -e is essential: without it ps only lists processes sharing the caller's
    # controlling terminal, and the odoo-bin daemon (started outside the SSH
    # tty) would never be seen — detection would always fall through to
    # odoo.conf. Verified empirically on an Odoo.sh shell.
    ap="$(ps -eww -o args= 2>/dev/null | tr ' ' '\n' \
          | grep -m1 -E '^--addons-path=' | sed 's/^--addons-path=//')" || true
    if [ -n "$ap" ]; then printf '%s' "$ap"; return 0; fi
    local conf="$HOME/.config/odoo/odoo.conf"
    if [ -f "$conf" ]; then
        ap="$(grep -E '^[[:space:]]*addons_path[[:space:]]*=' "$conf" \
              | head -n1 | sed -E 's/^[^=]*=[[:space:]]*//')" || true
        if [ -n "$ap" ]; then printf '%s' "$ap"; return 0; fi
    fi
    return 1
}

addon_branch() {  # path -> current branch name (may be empty / "HEAD")
    git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || true
}

sanitize_addon_name() {  # path -> [a-z0-9_-] name (<=63)
    local raw="$1" rel
    case "$raw" in
        */src/user/*) rel="${raw##*/src/user/}" ;;
        *) rel="$(basename "$raw")" ;;
    esac
    rel="$(printf '%s' "$rel" | tr '[:upper:]/' '[:lower:]-' | tr -cd 'a-z0-9_-')"
    printf '%s' "${rel:0:63}"
}

upload_addon_files() {  # path name branch category
    # Addon tars can exceed a proxy body limit (Enterprise is hundreds of MB),
    # so buffer the tar to a file and upload it in parts. The temp lives next to
    # the backups ($HOME), which has room; the filestore is streamed elsewhere.
    local path="$1" name="$2" branch="$3" cat="$4"
    local base parent tmptar rc
    base="$(basename "$path")"; parent="$(dirname "$path")"
    tmptar="$(mktemp "$HOME/.oduflow-addon-XXXXXX.tar")"
    if ! tar --exclude='.git' -C "$parent" -cf "$tmptar" "$base"; then
        rm -f "$tmptar"; return 1
    fi
    upload_ranges "$tmptar" "${API}/addon" \
        "name=$(urlencode "$name")&branch=$(urlencode "$branch")&category=$(urlencode "$cat")" \
        0 "addon ${name}"
    rc=$?
    rm -f "$tmptar"
    return "$rc"
}

announce_addon_remote() {  # name origin branch
    curl -fsS -H "$AUTH" -H "Content-Type: application/json" \
        --data-binary "$(addon_remote_json "$1" "$2" "$3")" \
        -X POST "${API}/addon-remote" >/dev/null
}

process_addons() {
    local addons_path="$1"
    local p base name origin branch
    local IFS=','
    local -a paths
    read -ra paths <<< "$addons_path"
    for p in "${paths[@]}"; do
        p="${p%/}"
        [ -n "$p" ] && [ -d "$p" ] || continue
        case "$p" in
            */src/odoo/addons|*/src/odoo/odoo/addons) continue ;;  # in the image
            */src/user) continue ;;                                # customer's own repo
            */src/enterprise)
                [ "$WITH_ENTERPRISE" = 1 ] || continue
                name="enterprise"; branch="$(addon_branch "$p")"
                if [[ "$done_addons" == *" $name "* ]]; then
                    echo ">> enterprise: already uploaded, skipping"; continue
                fi
                echo ">> enterprise: uploading ($(du -sh "$p" 2>/dev/null | cut -f1))"
                upload_addon_files "$p" "$name" "$branch" "enterprise" \
                    || { echo "ERROR: failed to upload enterprise addons." >&2; exit 5; }
                ;;
            */src/themes)
                [ "$WITH_THEMES" = 1 ] || continue
                name="themes"; branch="$(addon_branch "$p")"
                if [[ "$done_addons" == *" $name "* ]]; then
                    echo ">> themes: already uploaded, skipping"; continue
                fi
                echo ">> themes: uploading ($(du -sh "$p" 2>/dev/null | cut -f1))"
                upload_addon_files "$p" "$name" "$branch" "themes" \
                    || { echo "ERROR: failed to upload themes." >&2; exit 5; }
                ;;
            *)
                [ "$WITH_EXTRA" = 1 ] || continue
                name="$(sanitize_addon_name "$p")"
                [ -n "$name" ] || continue
                origin="$(git -C "$p" remote get-url origin 2>/dev/null || true)"
                branch="$(addon_branch "$p")"
                case "$origin" in
                    https://*)
                        if [[ "$done_remote" == *" $name "* ]]; then
                            echo ">> extra '$name': already announced, skipping"; continue
                        fi
                        echo ">> extra '$name': from remote $origin @ ${branch:-?}"
                        announce_addon_remote "$name" "$origin" "$branch" \
                            || { echo "ERROR: failed to announce extra repo '$name'." >&2; exit 5; }
                        ;;
                    *)
                        if [[ "$done_addons" == *" $name "* ]]; then
                            echo ">> extra '$name': already uploaded, skipping"; continue
                        fi
                        echo ">> extra '$name': uploading files (origin: ${origin:-none})"
                        upload_addon_files "$p" "$name" "$branch" "extra" \
                            || { echo "ERROR: failed to upload extra repo '$name'." >&2; exit 5; }
                        ;;
                esac
                ;;
        esac
    done
}

if [ "$WITH_ENTERPRISE" = 1 ] || [ "$WITH_THEMES" = 1 ] || [ "$WITH_EXTRA" = 1 ]; then
    ADDONS_PATH="$(detect_addons_path || true)"
    if [ -z "$ADDONS_PATH" ]; then
        echo ">> WARNING: could not determine the Odoo addons-path; skipping addon download." >&2
        echo "   (the database and filestore import continues normally)" >&2
    else
        process_addons "$ADDONS_PATH"
    fi
fi

# ---- finalize --------------------------------------------------------------

echo ">> finalizing (restoring database — this can take a few minutes)…"
RESULT_FILE="$(mktemp)"
trap 'rm -f "$STATUS_FILE" "$RESULT_FILE"' EXIT
# --fail-with-body keeps the server's error body on HTTP >= 400 (plain -f
# would discard it, hiding the reason the riskiest step failed).
if ! curl -sS "${CURL_FAIL[@]}" -H "$AUTH" -X POST "${API}/finalize" -o "$RESULT_FILE"; then
    echo "ERROR: finalize request failed:" >&2
    cat "$RESULT_FILE" >&2 || true
    echo >&2
    exit 6
fi

python3 -c 'import sys,json
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
try:
    d = json.loads(raw)
except ValueError:
    print(">> ERROR: unexpected finalize response (not Oduflow JSON):")
    print("   " + (raw[:300].strip() or "(empty body)"))
    print("   The import may still be finishing server-side — check the")
    print("   Templates tab in the dashboard before retrying.")
    sys.exit(1)
if not d.get("ok"):
    print(">> ERROR:", d.get("error","unknown error")); sys.exit(1)
r=d.get("result",{})
print(">> Done — template ready:", r.get("template_name"))
print("   DB:", r.get("template_db"), " restore:", r.get("restore_seconds"), "s")
warnings = r.get("addon_warnings") or []
if warnings:
    print(">> Addon warnings:")
    for warning in warnings:
        name = warning.get("name") or "unknown"
        action = warning.get("action") or "warning"
        reason = warning.get("reason") or "No reason reported."
        print("   WARNING:", name, "—", action, "—", reason)' "$RESULT_FILE"
