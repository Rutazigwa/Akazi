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

# Record the run, so a backup cron that stopped is visible before restore day
# rather than on it. Recording is best-effort on purpose: a backup that
# succeeded but could not write its heartbeat is still a backup, and losing it
# over a bookkeeping failure would be absurd. A warning is enough -- the
# absence of runs is itself the alarm.
record() {  # record <ok> <detail-json> [error]
    local ok="$1" detail="$2" error="${3:-}"
    psql "$DSN" -v ON_ERROR_STOP=1 -qtAc "
        INSERT INTO job_runs (job_name, finished_at, ok, detail, error)
        VALUES ('backup', clock_timestamp(), $ok,
                \$json\$$detail\$json\$::jsonb,
                NULLIF('$(printf '%s' "$error" | sed "s/'/''/g")', ''))
    " >/dev/null 2>&1 || echo "warning: could not record this run in job_runs" >&2
}

# Anything that exits non-zero from here on is a failed backup, and a failed
# backup has to be recorded as one -- otherwise the last row still says
# success and the gap looks like a healthy quiet night.
trap 'record false "{}" "backup failed at line $LINENO"' ERR

pg_dump --no-owner --no-privileges "$DSN" \
    | gzip -9 \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -pass env:BACKUP_PASSPHRASE \
        -out "$OUT"

chmod 600 "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Verify the file decrypts and unpacks whole. An unverified backup is a guess,
# and the day you find out is the worst possible day.
#
# The check reads the ENTIRE stream and looks for pg_dump's completion marker,
# which it only writes after the last row. That proves the file is not
# truncated -- a partial dump decrypts and unpacks perfectly happily, and its
# first bytes look exactly like a good one.
#
# It deliberately does not pipe into `head`: head exits after its bytes, the
# upstream processes take SIGPIPE, and under `set -o pipefail` the whole
# pipeline reports failure. That produced false alarms on perfectly good
# backups -- and a verification that cries wolf is worse than none, because it
# trains whoever reads the cron mail to ignore it.
verify() {
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
        -pass env:BACKUP_PASSPHRASE -in "$OUT" 2>/dev/null \
        | gunzip 2>/dev/null \
        | grep -c "PostgreSQL database dump complete" || true
}

if [ "$(verify)" -ge 1 ]; then
    echo "verified: decrypts, unpacks, and the dump is complete"
else
    record false "{}" "verification failed for $OUT"
    echo "VERIFICATION FAILED -- do not rely on $OUT" >&2
    exit 1
fi

deleted=$(find "$DEST" -name 'akazi-*.sql.gz.enc' -mtime "+$RETENTION_DAYS" -print -delete | wc -l)
[ "$deleted" -gt 0 ] && echo "pruned $deleted backup(s) older than $RETENTION_DAYS days"

trap - ERR
record true "{\"bytes\": $(stat -c%s "$OUT"), \"verified\": true, \"pruned\": $deleted}"
exit 0
