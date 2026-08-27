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
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Throwaway: the role exists only inside a scratch database that is dropped
# at the end of the module.
APP_ROLE_TEST_PASSWORD = "test-only"

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

    # Swap the database name properly rather than string-replacing "/postgres?",
    # which silently does nothing when the URL has no query string -- and the
    # tests would then run unmigrated against the admin database.
    base, _, query = admin.partition("?")
    target = base.rsplit("/", 1)[0] + f"/{db_name}" + (f"?{query}" if query else "")
    psql_dsn = target.replace("postgresql+psycopg://", "postgresql://")
    assert db_name in psql_dsn, "test database name did not make it into the DSN"
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


@contextmanager
def _scratch_database():
    """Create an empty database, yield its URL, and drop it afterwards."""
    admin = _admin_url()
    if admin is None:
        pytest.skip("no test database available (see scripts/testdb.sh)")

    name = f"akazi_scratch_{uuid.uuid4().hex[:8]}"
    engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))

    base, _, query = admin.partition("?")
    try:
        yield base.rsplit("/", 1)[0] + f"/{name}" + (f"?{query}" if query else "")
    finally:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = :db"),
                {"db": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        engine.dispose()


@pytest.fixture
def scratch_database():
    """An empty throwaway database, with nothing applied to it.

    For tests that have to COMMIT -- backup and the migration runner both shell
    out to psql on their own connections, so they cannot see anything held in
    the shared session's rolled-back transaction. Committing into that shared
    database instead would leak rows into every later test, and audit_log is
    append-only by rule, so the leak could not even be cleaned up afterwards.
    """
    with _scratch_database() as url:
        yield url


@pytest.fixture(scope="module")
def scratch_database_module():
    """The same, shared across a module.

    For read-only work like taking backups, where a per-test database means
    applying every migration again -- twenty-one psql invocations each time,
    which dominated the runtime of those tests.
    """
    with _scratch_database() as url:
        yield url


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
    """A coordinator, stamped on the transaction as the acting staff member.

    Mirrors production: every authenticated request sets app.staff_id, and the
    audit triggers read it. Tests that call operations directly would otherwise
    write unattributed audit rows, which is exactly what the code refuses to do.
    """
    staff_id = session.execute(
        text(
            """
            INSERT INTO staff (full_name, phone, role, can_view_identity)
            VALUES ('Coordinator', :phone, 'owner', true)
            RETURNING staff_id
            """
        ),
        {"phone": f"+25078{uuid.uuid4().int % 10_000_000:07d}"},
    ).scalar_one()
    session.execute(
        text("SELECT set_config('app.staff_id', :sid, true)"),
        {"sid": str(staff_id)},
    )
    return staff_id


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
def make_employer(session):
    """Several employers in one test -- repeat business is a rate across them."""
    def _make(name: str | None = None, is_cooperative: bool = False,
              tier: str = "active"):
        return session.execute(
            text(
                """
                INSERT INTO employers (business_name, sector, district, tier,
                                       is_cooperative)
                VALUES (:name, 'cleaning', 'Gasabo', CAST(:tier AS employer_tier),
                        :coop)
                RETURNING employer_id
                """
            ),
            {
                "name": name or f"Employer {uuid.uuid4().hex[:6]}",
                "tier": tier,
                "coop": is_cooperative,
            },
        ).scalar_one()

    return _make


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


# --- auth fixtures --------------------------------------------------------

STAFF_PASSWORD = "correct horse battery staple"


@pytest.fixture
def staff_login(session, staff_id):
    """A staff account with a password and a confirmed second factor.

    TOTP is enrolled here because identity access requires it. Tests that need
    to exercise the un-enrolled path clear it explicitly.
    """
    import pyotp

    from app.auth import set_password
    from app.mfa import begin_enrolment, confirm_enrolment

    set_password(session, staff_id, STAFF_PASSWORD)
    enrolment = begin_enrolment(session, staff_id)
    confirm_enrolment(session, staff_id, pyotp.TOTP(enrolment.secret).now())

    phone = session.execute(
        text("SELECT phone FROM staff WHERE staff_id = :sid"), {"sid": staff_id}
    ).scalar_one()
    return {
        "phone": phone,
        "password": STAFF_PASSWORD,
        "staff_id": staff_id,
        "totp_secret": enrolment.secret,
    }


def totp_now(secret: str, skew_steps: int = 0) -> str:
    """A current TOTP code, optionally from a neighbouring time step.

    Tests that present two codes in a row need distinct ones: the replay guard
    refuses a code at or below the last accepted counter, which is the whole
    point of it.
    """
    import time

    import pyotp

    totp = pyotp.TOTP(secret)
    return totp.at(int(time.time()) + skew_steps * 30)


@pytest.fixture
def api(session):
    """A TestClient bound to the test transaction, with no session token yet."""
    from fastapi.testclient import TestClient

    from app.deps import db_session
    from app.main import app

    app.dependency_overrides[db_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def client(api, staff_login):
    """An authenticated, MFA-elevated TestClient.

    Logs in and presents a second factor through the real endpoints rather than
    forging a token, so every test exercises the actual path a coordinator takes.
    """
    r = api.post(
        "/auth/login",
        json={"phone": staff_login["phone"], "password": staff_login["password"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["mfa_required"] is True
    api.headers["Authorization"] = f"Bearer {r.json()['token']}"

    # A code from the next time step: enrolment already consumed the current
    # one, and the replay guard refuses anything at or below it.
    elevated = api.post(
        "/auth/mfa", json={"code": totp_now(staff_login["totp_secret"], 1)}
    )
    assert elevated.status_code == 200, elevated.text
    return api


@pytest.fixture(scope="module")
def restricted_url(scratch_database_module) -> str:
    """A fully migrated database, connected as the unprivileged app role.

    Every other fixture connects as the database owner, who bypasses grants
    entirely. That is why a whole class of defect stayed invisible: the app
    could not create a staff account, could not register a candidate, could
    not resolve an inbound reply and could not accept an erasure request, and
    the suite was green throughout. Anything exercised through this fixture is
    exercised the way production runs it.
    """
    dsn = scratch_database_module.replace("postgresql+psycopg://", "postgresql://")
    root = Path(__file__).parent.parent

    applied = subprocess.run(
        [str(root / "scripts" / "migrate.sh"), dsn],
        capture_output=True, text=True,
    )
    assert applied.returncode == 0, applied.stderr

    role = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q",
         "-c", "SELECT set_config('akazi.app_password', "
               f"'{APP_ROLE_TEST_PASSWORD}', false);",
         "-f", str(root / "scripts" / "create_app_role.sql")],
        capture_output=True, text=True,
    )
    assert role.returncode == 0, role.stderr

    # Swap the connecting user for the restricted login role, carrying the
    # password create_app_role.sql just set. A local socket authenticating by
    # trust ignores it; CI connects over TCP and does not, and dropping it
    # here made the fixture pass locally and fail there.
    scheme, _, rest = scratch_database_module.partition("://")
    hostpart = rest.rpartition("@")[2] if "@" in rest else rest
    return f"{scheme}://akazi_app:{APP_ROLE_TEST_PASSWORD}@{hostpart}"


@pytest.fixture(scope="module")
def restricted_session(restricted_url):
    """A committing session on the restricted database.

    Deliberately not wrapped in a rollback: privilege failures surface at
    statement time, and the point of these tests is to run the real writes.
    """
    engine = create_engine(restricted_url)
    with Session(engine) as s:
        yield s
    engine.dispose()
