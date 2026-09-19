#!/bin/sh
set -eu
wal="${PGDATA:-/var/lib/postgresql/data}/pg_wal"
# Segments and .ready files appear and disappear while the archiver works, so
# find exits 1 on an entry that vanished mid-scan. Accept that partial listing
# instead of failing the whole sample; a directory that is genuinely missing
# must still fail, because then the listing would be silently empty.
scan() {
    dir="$1"
    shift
    test -d "$dir"
    find "$dir" -maxdepth 1 "$@" 2>/dev/null || test -d "$dir"
}
printf 'DISK\n'
df -P -B1 "$wal/"
printf 'WAL\n'
scan "$wal/" -type f -printf '%f %s\n'
printf 'READY\n'
scan "$wal/archive_status/" -name '*.ready' -printf '%f %T@\n'
printf 'CURRENT\n'
cat /tmp/oduflow-wal/current 2>/dev/null || true
printf 'LAST\n'
cat /tmp/oduflow-wal/last 2>/dev/null || true
