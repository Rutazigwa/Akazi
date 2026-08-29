"""The demo seeder's refusal to run against real data.

scripts/seed_demo.py writes invented people with invented national
identifiers. The one database it must never reach is one holding real ones,
and the only part of it worth a test is the check that stops it.

These use a scratch database rather than the shared session, because the
seeder is a separate process: anything held in a rolled-back transaction is
invisible to it, and a guard tested against data it cannot see is not tested
at all.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


ROOT = Path(__file__).parent.parent
SEED = ROOT / "scripts" / "seed_demo.py"
MIGRATE = ROOT / "scripts" / "migrate.sh"
UNREACHABLE = "http://127.0.0.1:1"  # nothing listens here, deliberately


@pytest.fixture
def seeded_target(scratch_database):
    """A migrated, empty database and its psql DSN."""
    dsn = scratch_database.replace("postgresql+psycopg://", "postgresql://")
    applied = subprocess.run([str(MIGRATE), dsn], capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    return scratch_database, dsn


def run(dsn: str) -> str:
    result = subprocess.run(
        # sys.executable, not "python": the script needs the project's
        # dependencies, and the interpreter running the tests has them.
        [sys.executable, str(SEED), UNREACHABLE, "--dsn", dsn],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout + result.stderr


def add_employer(url: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(
            text("INSERT INTO employers (business_name, sector, district) "
                 "VALUES ('Real Employer Ltd', 'cleaning', 'Gasabo')")
        )
    engine.dispose()


def test_it_refuses_a_database_that_already_holds_records(seeded_target):
    url, dsn = seeded_target
    add_employer(url)
    assert "refusing to seed" in run(dsn)


def test_the_guard_runs_before_any_request_is_made(seeded_target):
    """Pointing it at an unreachable server proves the order.

    A guard that ran after signing in could leave half a seed behind, and the
    failure would read as a network problem rather than a refusal.
    """
    url, dsn = seeded_target
    add_employer(url)
    output = run(dsn)
    assert "refusing to seed" in output
    assert "ConnectError" not in output


def test_an_empty_database_gets_past_the_guard(seeded_target):
    """The guard must not be so broad that it blocks its own purpose.

    With nothing to protect it proceeds, and then fails on the unreachable
    server -- which is the next thing it should do, not the first.
    """
    _, dsn = seeded_target
    output = run(dsn)
    assert "refusing to seed" not in output
    assert "ConnectError" in output


# --- the timestamps the demo is judged on ---------------------------------

def interval_sql() -> str:
    """The interval expression from the seeder, lifted out verbatim.

    Read from the script rather than copied, so a change there fails here
    instead of quietly leaving this testing a stale expression.
    """
    source = SEED.read_text()
    start = source.index("mins => 720 +")
    end = source.index(")::int", start) + len(")::int")
    return " ".join(source[start:end].split("=>", 1)[1].split())


def test_the_backdating_interval_stays_inside_the_range_it_claims(seeded_target):
    """Between half a day and four and a half, for any request id.

    A modulo arithmetic slip here does not fail loudly -- it produces a demo
    where a shift was filled a month before it was posted, or in nine seconds,
    and the employer looking at it draws the obvious conclusion about the rest
    of the numbers.
    """
    url, _ = seeded_target
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    expression = interval_sql()
    with engine.connect() as conn:
        minutes = conn.execute(
            text(
                f"SELECT min(m), max(m), count(DISTINCT m) FROM ("
                f"  SELECT ({expression}) AS m"
                f"    FROM (SELECT gen_random_uuid() AS request_id"
                f"            FROM generate_series(1, 500)) wr"
                f") s"
            )
        ).one()
    engine.dispose()

    low, high, distinct = minutes
    assert 720 <= low, f"a request filled in {low} minutes is not a fill time"
    assert high <= 720 + 5760, f"{high} minutes is over the 4.5 day ceiling"
    # Deterministic per id, but it must still vary -- one constant for every
    # request would show every shift filled on exactly the same day.
    assert distinct > 100, f"only {distinct} distinct intervals across 500 ids"


def test_the_same_request_id_always_gets_the_same_interval(seeded_target):
    """Rebuilding the demo must not reshuffle its history.

    Somebody walks an employer through this twice; the numbers moving between
    runs is the kind of small thing that costs a meeting.
    """
    url, _ = seeded_target
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    expression = interval_sql()
    with engine.connect() as conn:
        values = [
            conn.execute(
                text(f"SELECT ({expression}) FROM "
                     f"(SELECT '11111111-2222-3333-4444-555555555555'::uuid "
                     f"AS request_id) wr")
            ).scalar_one()
            for _ in range(3)
        ]
    engine.dispose()
    assert len(set(values)) == 1, values
