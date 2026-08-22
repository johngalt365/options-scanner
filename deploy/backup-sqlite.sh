#!/bin/sh
set -eu

DB=${OPTIONS_SCANNER_DB:?OPTIONS_SCANNER_DB is required}
DEST=${OPTIONS_SCANNER_BACKUP_DIR:-/var/backups/options-scanner}
KEEP_DAYS=${OPTIONS_SCANNER_BACKUP_RETENTION_DAYS:-14}
case "$KEEP_DAYS" in *[!0-9]*|'') echo "invalid retention" >&2; exit 2;; esac
[ -f "$DB" ] || { echo "database does not exist: $DB" >&2; exit 3; }
umask 077
mkdir -p "$DEST"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp="$DEST/.beta-$stamp.sqlite3.tmp"
out="$DEST/beta-$stamp.sqlite3"
trap 'rm -f "$tmp"' EXIT HUP INT TERM
sqlite3 "$DB" ".timeout 10000" ".backup '$tmp'"
sqlite3 "$tmp" "PRAGMA quick_check" | grep -qx ok || { echo "backup integrity check failed" >&2; exit 4; }
chmod 600 "$tmp"
mv "$tmp" "$out"
find "$DEST" -type f -name 'beta-*.sqlite3' -mtime "+$KEEP_DAYS" -delete
echo "$out"
