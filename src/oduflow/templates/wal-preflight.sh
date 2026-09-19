set -eu
umask 077
bin=/opt/oduflow-bin/wal-g
conf=/etc/walg/walg.json
if ! test -x "$bin" || ! test -r "$conf" || ! test -s /etc/walg/ca-certificates.crt || ! test -r /etc/walg/ca-certificates.crt; then
    echo 'preflight prerequisites missing' >&2
    exit 1
fi
work=$(mktemp -d /tmp/oduflow-preflight.XXXXXXXX)
# A random, per-attempt namespace: st rm deletes by prefix.
key="oduflow-preflight/$1/probe"
uploaded=0
cleanup() {
    if test "$uploaded" = 1; then
        timeout --kill-after=1s 5s "$bin" --config "$conf" st rm "$key" >/dev/null 2>&1 || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
printf '%s' "$1" > "$work/probe"
"$bin" --config "$conf" st ls >/dev/null
# Cleanup is also attempted after an interrupted PUT with an uncertain result.
uploaded=1
"$bin" --config "$conf" st put --no-compress --no-encrypt "$work/probe" "$key"
"$bin" --config "$conf" st cat "$key" > "$work/readback"
if ! cmp -s "$work/probe" "$work/readback"; then
    echo 'preflight content mismatch' >&2
    exit 1
fi
"$bin" --config "$conf" st rm "$key"
uploaded=0
