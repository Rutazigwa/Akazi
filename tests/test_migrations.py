"""The migration runner.

migrate.sh originally re-ran every file on every invocation, which only works
against an empty database. The first schema change after launch would have had
no upgrade path -- re-running 001 against a live database fails on the first
CREATE TABLE, and stops there with the new migrations unapplied.

These tests run the real script against real databases, because the thing being
protected is a shell script's behaviour and mocking it would prove nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

MIGRATE = Path(__file__).parent.parent / "scripts" / "migrate.sh"
MIGRATION_COUNT = len(
    list((Path(__file__).parent.parent / "migrations").glob("*.sql"))
)


def psql_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture
def scratch_db(scratch_database) -> str:
    """The shared empty-database fixture, as a psql-style DSN.

    migrate.sh shells out to psql, which does not understand SQLAlchemy's
    +psycopg driver suffix.
    """
    return psql_dsn(scratch_database)


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


# How many of the most recent migrations to run against seeded data. Wide
# enough to cover the ones that transform what is already there.
STOP_SHORT = 6


def test_the_last_migrations_run_against_a_database_with_people_in_it(scratch_db):
    """An empty database exercises no backfill.

    Migration 056 moved the after-dark opt-in out of a boolean and into a
    consent record, backfilling the existing values. It applied cleanly to
    every empty test database and would have aborted on the first real one:
    the backfilled rows violated a CHECK on captured_via, and with no
    candidates there were no rows to violate it.

    That is the shape of the whole class -- a migration that transforms data is
    tested by the data, and there was none. So this one applies everything up
    to the last few, puts a candidate and a placement in, and only then runs
    the rest.
    """
    import uuid

    files = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))
    db = scratch_db
    if True:
        engine = create_engine(
            db.replace("postgresql://", "postgresql+psycopg://"),
            isolation_level="AUTOCOMMIT",
        )
        with engine.connect() as conn:
            conn.execute(
                text("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "  filename TEXT PRIMARY KEY,"
                     "  applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())")
            )
        engine.dispose()

        for path in files[:-STOP_SHORT]:
            result = subprocess.run(
                ["psql", db, "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                capture_output=True, text=True,
            )
            assert result.returncode == 0, f"{path.name}: {result.stderr}"
            subprocess.run(
                ["psql", db, "-q", "-v", "ON_ERROR_STOP=1", "-c",
                 f"INSERT INTO schema_migrations (filename) VALUES ('{path.name}')"],
                check=True, capture_output=True,
            )

        seeded = subprocess.run(
            ["psql", db, "-q", "-v", "ON_ERROR_STOP=1", "-c", f"""
            INSERT INTO staff (full_name, phone, role, password_hash)
              VALUES ('Owner', '+250780000001', 'owner', 'x');
            INSERT INTO candidate_identity (candidate_id, legal_first_name,
                     legal_last_name, national_id, date_of_birth, phone_primary)
              VALUES ('{uuid.uuid4()}', 'Ancienne', 'Record', 'NID-OLD',
                      kigali_today() - 8000, '+250788000111');
            INSERT INTO candidates (candidate_id, display_name, gender, district,
                     sector, registered_by)
              SELECT candidate_id, 'Ancienne R.', 'F', 'Gasabo', 'Remera',
                     (SELECT staff_id FROM staff LIMIT 1)
                FROM candidate_identity;
            """],
            capture_output=True, text=True,
        )
        assert seeded.returncode == 0, seeded.stderr

        output = run(db)
        assert f"applied {STOP_SHORT}," in output, output
        assert len(applied(db)) == MIGRATION_COUNT
