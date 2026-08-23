#!/usr/bin/env bash
# Apply migrations in order. Idempotent only in the sense that a failed
# migration rolls back: each file is wrapped in its own transaction.
set -euo pipefail

DSN="${1:-postgresql://placement:placement@localhost:5432/placement_ops}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../migrations" && pwd)"

for f in "$DIR"/*.sql; do
    echo "==> $(basename "$f")"
    psql "$DSN" -q -v ON_ERROR_STOP=1 -f "$f"
done

echo "All migrations applied."
