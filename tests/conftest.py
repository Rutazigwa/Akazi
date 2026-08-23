"""Integration test fixtures.

These tests run against a real PostgreSQL instance because most of what they
protect is enforced by the database -- constraints, the append-only consent
rules, the audit triggers and the metric views. Mocking that out would leave
the tests passing while the schema silently stopped doing its job.

Point TEST_DATABASE_URL at a throwaway server, or run scripts/testdb.sh. If
neither is available the integration tests skip rather than fail: the pure
matching and config tests still run anywhere.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

MIGRATIONS = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))


def _admin_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    socket = "/var/lib/pgtest/run"
    if Path(socket).is_dir():
        return f"postgresql+psycopg://postgres@/postgres?host={socket}&port=5433"
    return None


@pytest.fixture(scope="session")
def database_url() -> str:
    admin = _admin_url()
    if admin is None:
        pytest.skip("no test database available (see scripts/testdb.sh)")

    db_name = f"akazi_test_{uuid.uuid4().hex[:8]}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    target = admin.replace("/postgres?", f"/{db_name}?")
    psql_dsn = target.replace("postgresql+psycopg://", "postgresql://")
    for migration in MIGRATIONS:
        result = subprocess.run(
            ["psql", psql_dsn, "-q", "-v", "ON_ERROR_STOP=1", "-f", str(migration)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"migration {migration.name} failed:\n{result.stderr}")

    yield target

    admin_engine.dispose()
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :db"
            ),
            {"db": db_name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


@pytest.fixture
def session(database_url) -> Session:
    """A session rolled back after each test, so tests cannot leak into each other."""
    engine = create_engine(database_url)
    connection = engine.connect()
    transaction = connection.begin()
    s = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield s
    finally:
        s.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


# --- seed helpers ---------------------------------------------------------

@pytest.fixture
def staff_id(session) -> uuid.UUID:
    return session.execute(
        text(
            """
            INSERT INTO staff (full_name, phone, role, can_view_identity)
            VALUES ('Coordinator', :phone, 'coordinator', true)
            RETURNING staff_id
            """
        ),
        {"phone": f"+25078{uuid.uuid4().int % 10_000_000:07d}"},
    ).scalar_one()


@pytest.fixture
def make_candidate(session, staff_id):
    def _make(gender: str = "F", age_years: int = 22, name: str = "Test Worker"):
        cid = session.execute(
            text(
                """
                INSERT INTO candidate_identity
                    (legal_first_name, legal_last_name, date_of_birth, phone_primary)
                VALUES ('Test', 'Worker', :dob, :phone)
                RETURNING candidate_id
                """
            ),
            {
                "dob": date.today() - timedelta(days=365 * age_years + 10),
                "phone": f"+25079{uuid.uuid4().int % 10_000_000:07d}",
            },
        ).scalar_one()
        session.execute(
            text(
                """
                INSERT INTO candidates (candidate_id, display_name, gender,
                                        district, sector, registered_by)
                VALUES (:cid, :name, :gender, 'Gasabo', 'Remera', :staff)
                """
            ),
            {"cid": cid, "name": name, "gender": gender, "staff": staff_id},
        )
        return cid

    return _make


@pytest.fixture
def employer_id(session) -> uuid.UUID:
    return session.execute(
        text(
            """
            INSERT INTO employers (business_name, sector, district, tier,
                                   is_cooperative)
            VALUES ('Isuku Cooperative', 'cleaning', 'Gasabo', 'active', true)
            RETURNING employer_id
            """
        )
    ).scalar_one()


@pytest.fixture
def make_request(session, employer_id):
    def _make(pay_rwf: int = 5000, headcount: int = 1):
        return session.execute(
            text(
                """
                INSERT INTO work_requests (employer_id, title, work_type,
                                           headcount, starts_on, pay_rwf, pay_unit)
                VALUES (:eid, 'Morning cleaner', 'shift', :headcount,
                        CURRENT_DATE, :pay, 'day')
                RETURNING request_id
                """
            ),
            {"eid": employer_id, "headcount": headcount, "pay": pay_rwf},
        ).scalar_one()

    return _make


@pytest.fixture
def make_placement(session, make_request, make_candidate):
    def _make(candidate_id=None, request_id=None, pay_rwf: int = 5000,
              transport_rwf: int = 500):
        return session.execute(
            text(
                """
                INSERT INTO placements (request_id, candidate_id, status,
                                        agreed_pay_rwf, pay_unit,
                                        est_transport_rwf)
                VALUES (:rid, :cid, 'offered', :pay, 'day', :transport)
                RETURNING placement_id
                """
            ),
            {
                "rid": request_id or make_request(pay_rwf=pay_rwf),
                "cid": candidate_id or make_candidate(),
                "pay": pay_rwf,
                "transport": transport_rwf,
            },
        ).scalar_one()

    return _make
