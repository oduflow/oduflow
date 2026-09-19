#!/bin/sh
# PostgreSQL calls this as postgres. Never acknowledge an unarchived segment.
set -u
runtime=/tmp/oduflow-wal
umask 077
mkdir -p "$runtime" || exit 1
test ! -e /etc/walg/archive-paused || exit 1
started=$(date +%s)
timeout --kill-after=5s "${1}s" /opt/oduflow-bin/wal-g --config /etc/walg/walg.json wal-push "$2" &
child=$!
trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit 1' TERM INT
printf '%s %s %s\n' "$child" "$started" "$3" > "$runtime/current.tmp"
mv "$runtime/current.tmp" "$runtime/current"
wait "$child"
result=$?
printf '%s %s %s %s\n' "$started" "$(date +%s)" "$result" "$3" > "$runtime/last.tmp"
mv "$runtime/last.tmp" "$runtime/last"
rm -f "$runtime/current"
if [ "$result" -ne 0 ]; then
    echo "oduflow: WAL archive $3 failed (exit $result)" >&2
    # Signals / codes >125 can escape pg_stat_archiver failure accounting.
    exit 1
fi
exit 0
