#!/usr/bin/env bash
# Apply migrations in order, tracking what has already run.
#
# Each file runs in its own transaction (they all BEGIN/COMMIT), and applied
# filenames are recorded in schema_migrations -- so this is safe to run against
# a live database that is some migrations behind, which is what every deploy
# after the first one looks like. Without tracking, migrate.sh only worked on
# an empty database, and the first schema change post-launch would have had no
# upgrade path.
#
#   ./scripts/migrate.sh "$DSN"                 apply whatever is missing
#   ./scripts/migrate.sh "$DSN" --baseline N    mark the first N files applied
#                                               WITHOUT running them -- only for
#                                               adopting a database that was
#                                               migrated before tracking existed
set -euo pipefail

DSN="${1:-postgresql://placement:placement@localhost:5432/placement_ops}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../migrations" && pwd)"

psql "$DSN" -q -v ON_ERROR_STOP=1 -c "
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);"

if [ "${2:-}" = "--baseline" ]; then
    N="${3:?--baseline needs a count}"
    i=0
    for f in "$DIR"/*.sql; do
        i=$((i+1)); [ "$i" -gt "$N" ] && break
        psql "$DSN" -q -v ON_ERROR_STOP=1 -c \
            "INSERT INTO schema_migrations (filename) VALUES ('$(basename "$f")')
             ON CONFLICT DO NOTHING;"
    done
    echo "baselined first $N migration(s)"
fi

applied=0 skipped=0
for f in "$DIR"/*.sql; do
    name="$(basename "$f")"
    done_already=$(psql "$DSN" -tAc \
        "SELECT 1 FROM schema_migrations WHERE filename = '$name'")
    if [ "$done_already" = "1" ]; then
        skipped=$((skipped+1)); continue
    fi
    echo "==> $name"
    psql "$DSN" -q -v ON_ERROR_STOP=1 -f "$f"
    psql "$DSN" -q -v ON_ERROR_STOP=1 -c \
        "INSERT INTO schema_migrations (filename) VALUES ('$name');"
    applied=$((applied+1))
done

echo "applied $applied, already had $skipped."
