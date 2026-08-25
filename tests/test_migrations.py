"""The migration runner.

migrate.sh originally re-ran every file on every invocation, which only works
against an empty database. The first schema change after launch would have had
no upgrade path -- re-running 001 against a live database fails on the first
CREATE TABLE, and stops there with the new migrations unapplied.

These tests run the real script against real databases, because the thing being
protected is a shell script's behaviour and mocking it would prove nothing.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

MIGRATE = Path(__file__).parent.parent / "scripts" / "migrate.sh"
MIGRATION_COUNT = len(
    list((Path(__file__).parent.parent / "migrations").glob("*.sql"))
)


def admin_dsn() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url.replace("postgresql+psycopg://", "postgresql://")
    socket = "/var/lib/pgtest/run"
    if Path(socket).is_dir():
        return f"postgresql://postgres@/postgres?host={socket}&port=5433"
    return None


@pytest.fixture
def scratch_db():
    """A throwaway database with nothing in it."""
    admin = admin_dsn()
    if admin is None:
        pytest.skip("no test database available (see scripts/testdb.sh)")

    name = f"akazi_mig_{uuid.uuid4().hex[:8]}"
    engine = create_engine(
        admin.replace("postgresql://", "postgresql+psycopg://"),
        isolation_level="AUTOCOMMIT",
    )
    with engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))

    base, _, query = admin.partition("?")
    dsn = base.rsplit("/", 1)[0] + f"/{name}" + (f"?{query}" if query else "")
    yield dsn

    with engine.connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                 "WHERE datname = :db"),
            {"db": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    engine.dispose()


def run(dsn: str, *args: str) -> str:
    result = subprocess.run(
        [str(MIGRATE), dsn, *args], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def applied(dsn: str) -> list[str]:
    engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg://"))
    try:
        with engine.connect() as conn:
            return list(
                conn.execute(
                    text("SELECT filename FROM schema_migrations ORDER BY filename")
                ).scalars()
            )
    finally:
        engine.dispose()


def test_a_fresh_database_gets_every_migration(scratch_db):
    output = run(scratch_db)
    assert f"applied {MIGRATION_COUNT}, already had 0." in output
    assert len(applied(scratch_db)) == MIGRATION_COUNT


def test_running_twice_applies_nothing_the_second_time(scratch_db):
    """The property that makes this usable on a live database."""
    run(scratch_db)
    output = run(scratch_db)
    assert f"applied 0, already had {MIGRATION_COUNT}." in output


def test_a_database_partway_through_catches_up(scratch_db):
    """Every deploy after the first one looks like this.

    Built by genuinely applying all but the last three migrations, rather than
    faking the tracking table -- the point is that the remaining files run
    against a database whose earlier schema really is there.
    """
    files = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))
    engine = create_engine(
        scratch_db.replace("postgresql://", "postgresql+psycopg://"),
        isolation_level="AUTOCOMMIT",
    )
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  filename TEXT PRIMARY KEY,"
                "  applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"
            )
        )
    engine.dispose()

    for path in files[:-3]:
        result = subprocess.run(
            ["psql", scratch_db, "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        subprocess.run(
            ["psql", scratch_db, "-q", "-v", "ON_ERROR_STOP=1", "-c",
             f"INSERT INTO schema_migrations (filename) VALUES ('{path.name}')"],
            check=True, capture_output=True,
        )

    output = run(scratch_db)
    assert f"applied 3, already had {MIGRATION_COUNT - 3}." in output
    assert len(applied(scratch_db)) == MIGRATION_COUNT


def test_baseline_records_without_running(scratch_db):
    """Adopting a database migrated before tracking existed.

    Nothing is executed -- so no table exists afterwards, which is exactly what
    distinguishes baselining from applying.
    """
    run(scratch_db, "--baseline", str(MIGRATION_COUNT))
    assert len(applied(scratch_db)) == MIGRATION_COUNT

    engine = create_engine(
        scratch_db.replace("postgresql://", "postgresql+psycopg://")
    )
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT to_regclass('public.candidates')")
            ).scalar_one()
    finally:
        engine.dispose()
    assert exists is None, "baseline must record, never execute"
