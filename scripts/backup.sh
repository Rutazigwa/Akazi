#!/usr/bin/env bash
# Encrypted database backup.
#
# The dump contains national ID numbers, home locations and assessment scores.
# An unencrypted backup file is the same personal data as the live database with
# none of the access control, so this script refuses to produce one.
#
#   BACKUP_PASSPHRASE=... ./scripts/backup.sh
#
# Restore:
#   openssl enc -d -aes-256-cbc -pbkdf2 -in akazi-YYYYmmdd.sql.gz.enc \
#       -pass env:BACKUP_PASSPHRASE | gunzip | psql "$DSN"
#
# Store the passphrase somewhere other than the server being backed up. A
# backup encrypted with a key that lives on the same disk protects against
# almost nothing.
set -euo pipefail

DSN="${DATABASE_DSN:-postgresql://${POSTGRES_USER:?}:${POSTGRES_PASSWORD:?}@db:5432/${POSTGRES_DB:-akazi}}"
DEST="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST/akazi-$STAMP.sql.gz.enc"

if [ -z "${BACKUP_PASSPHRASE:-}" ]; then
    echo "BACKUP_PASSPHRASE is not set -- refusing to write an unencrypted dump" >&2
    exit 1
fi

mkdir -p "$DEST"

pg_dump --no-owner --no-privileges "$DSN" \
    | gzip -9 \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -pass env:BACKUP_PASSPHRASE \
        -out "$OUT"

chmod 600 "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Verify the file decrypts and the archive is intact. An unverified backup is
# a guess, and the day you find out is the worst possible day.
if openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
       -pass env:BACKUP_PASSPHRASE -in "$OUT" | gunzip | head -c 200 \
       | grep -q "PostgreSQL database dump"; then
    echo "verified: decrypts and unpacks"
else
    echo "VERIFICATION FAILED -- do not rely on $OUT" >&2
    exit 1
fi

deleted=$(find "$DEST" -name 'akazi-*.sql.gz.enc' -mtime "+$RETENTION_DAYS" -print -delete | wc -l)
[ "$deleted" -gt 0 ] && echo "pruned $deleted backup(s) older than $RETENTION_DAYS days"
exit 0
