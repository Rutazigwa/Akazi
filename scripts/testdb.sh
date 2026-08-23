#!/usr/bin/env bash
# Start a throwaway local Postgres for the integration tests and print its DSN.
# Sandbox-only convenience: production runs the compose file, not this.
set -euo pipefail
PGBIN=/usr/lib/postgresql/16/bin
DATA=/var/lib/pgtest/data
RUN=/var/lib/pgtest/run

id postgres >/dev/null 2>&1 || useradd -m -s /bin/bash postgres
mkdir -p "$DATA" "$RUN" && chown -R postgres:postgres /var/lib/pgtest

if [ ! -f "$DATA/PG_VERSION" ]; then
    su postgres -c "PATH=$PGBIN:\$PATH initdb -D $DATA -U postgres --auth=trust" >/dev/null
fi
if ! su postgres -c "PATH=$PGBIN:\$PATH pg_ctl -D $DATA status" >/dev/null 2>&1; then
    su postgres -c "PATH=$PGBIN:\$PATH pg_ctl -D $DATA -o '-k $RUN -p 5433 -c listen_addresses=' -l $DATA/server.log start" >/dev/null
    sleep 2
fi
echo "postgresql://postgres@/postgres?host=$RUN&port=5433"
