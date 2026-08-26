"""Concurrent offers for the same person.

The matcher excludes a candidate committed elsewhere, and offer_placement
re-checks at the moment of offering. Both are reads followed by a write, so two
coordinators offering the same person at once each pass before either commits.
Demonstrated before the fix: both succeeded, and one worker was placed on two
overlapping shifts -- with both employers told someone was coming.

These tests use real threads against a real database, because the thing being
protected only exists under concurrency and a mocked version would prove
nothing.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.matching.repository import find_matches
from app.operations.requests import RequestError, offer_placement

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

MIGRATIONS = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))


@pytest.fixture
def live_db(scratch_database):
    """A migrated database with committed seed data.

    Its own database, and committed: threads open separate connections and
    cannot see anything held in the shared session's rolled-back transaction.
    """
    dsn = scratch_database.replace("postgresql+psycopg://", "postgresql://")
    combined = "\n".join(p.read_text() for p in MIGRATIONS)
    result = subprocess.run(
        ["psql", dsn, "-q", "-v", "ON_ERROR_STOP=1"],
        input=combined, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(scratch_database, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        staff = c.execute(
            text(
                "INSERT INTO staff (full_name, phone, role) "
                "VALUES ('C','+250780000001','owner') RETURNING staff_id"
            )
        ).scalar_one()
        c.execute(
            text("SELECT set_config('app.staff_id', :s, false)"),
            {"s": str(staff)},
        )
        candidate = c.execute(
            text(
                "INSERT INTO candidate_identity (legal_first_name, "
                " legal_last_name, date_of_birth, phone_primary) "
                "VALUES ('A','U', CURRENT_DATE - INTERVAL '22 years', "
                " '+250780000002') RETURNING candidate_id"
            )
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO candidates (candidate_id, display_name, gender, "
                " district, sector) VALUES (:c,'Aline','F','Gasabo','Remera')"
            ),
            {"c": candidate},
        )
        c.execute(
            text(
                "INSERT INTO consent_records (candidate_id, policy_version, "
                " purpose, granted, captured_via) "
                "VALUES (:c,'v1.0','placement',true,'paper')"
            ),
            {"c": candidate},
        )
        c.execute(
            text(
                "INSERT INTO availability (candidate_id, day_of_week, "
                " start_time, end_time) "
                "SELECT :c, d, '06:00','22:00' FROM generate_series(0,6) d"
            ),
            {"c": candidate},
        )
        requests = []
        for name in ("Employer A", "Employer B"):
            employer = c.execute(
                text(
                    "INSERT INTO employers (business_name, sector, district, "
                    " tier, site_lat, site_lng) "
                    "VALUES (:n,'cleaning','Gasabo','active',-1.9550,30.1150) "
                    "RETURNING employer_id"
                ),
                {"n": name},
            ).scalar_one()
            requests.append(
                c.execute(
                    text(
                        "INSERT INTO work_requests (employer_id, title, "
                        " work_type, headcount, starts_on, pay_rwf, pay_unit, "
                        " shift_start, shift_end) "
                        "VALUES (:e,'Shift','shift',1,CURRENT_DATE,5000,'day',"
                        " '08:00','16:00') RETURNING request_id"
                    ),
                    {"e": employer},
                ).scalar_one()
            )
    engine.dispose()
    return {
        "url": scratch_database, "staff": staff,
        "candidate": candidate, "requests": requests,
    }


def offer_concurrently(live_db, request_ids) -> list[str]:
    """Two threads, both past the overlap check before either commits."""
    barrier = threading.Barrier(len(request_ids))
    results: list[str] = []
    lock = threading.Lock()

    def run(request_id):
        engine = create_engine(live_db["url"])
        try:
            with engine.connect() as conn:
                tx = conn.begin()
                conn.execute(
                    text("SELECT set_config('app.staff_id', :s, true)"),
                    {"s": str(live_db["staff"])},
                )
                session = Session(
                    bind=conn, join_transaction_mode="create_savepoint"
                )
                try:
                    find_matches(session, request_id)
                    barrier.wait(timeout=15)
                    offer_placement(session, request_id, live_db["candidate"])
                    tx.commit()
                    outcome = "placed"
                except RequestError as exc:
                    tx.rollback()
                    outcome = f"refused: {exc}"
                except Exception as exc:  # noqa: BLE001
                    tx.rollback()
                    outcome = f"error: {type(exc).__name__}"
            with lock:
                results.append(outcome)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=run, args=(r,)) for r in request_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def live_placements(live_db) -> int:
    engine = create_engine(live_db["url"])
    try:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT count(*) FROM placements WHERE candidate_id = :c "
                    "AND status IN ('offered','accepted','active')"
                ),
                {"c": live_db["candidate"]},
            ).scalar_one()
    finally:
        engine.dispose()


def test_only_one_of_two_concurrent_offers_succeeds(live_db):
    """The application check passes in both transactions; the database has the
    last word."""
    results = offer_concurrently(live_db, live_db["requests"])

    assert sorted(r.split(":")[0] for r in results) == ["placed", "refused"]
    assert live_placements(live_db) == 1


def test_the_loser_gets_something_a_coordinator_can_act_on(live_db):
    """Not a raw database error -- the coordinator has to know to pick someone
    else, right now, with an employer waiting."""
    results = offer_concurrently(live_db, live_db["requests"])
    refused = next(r for r in results if r.startswith("refused"))
    assert "committed to overlapping work" in refused
    assert "Aline" in refused
    assert "exclusion_violation" not in refused


def test_the_survivor_is_a_complete_placement(live_db):
    """The winning transaction must not be left half-applied."""
    offer_concurrently(live_db, live_db["requests"])

    engine = create_engine(live_db["url"])
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT match_reason, agreed_pay_rwf FROM placements "
                    "WHERE candidate_id = :c"
                ),
                {"c": live_db["candidate"]},
            ).mappings().one()
    finally:
        engine.dispose()
    assert row["match_reason"].startswith("matched on: ")
    assert row["agreed_pay_rwf"] == 5000


def test_the_guard_holds_against_a_direct_insert(live_db):
    """It is a trigger, so it does not depend on going through the
    application."""
    engine = create_engine(live_db["url"], isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO placements (request_id, candidate_id, status, "
                    " agreed_pay_rwf, pay_unit) "
                    "VALUES (:r, :c, 'offered', 5000, 'day')"
                ),
                {"r": live_db["requests"][0], "c": live_db["candidate"]},
            )
            with pytest.raises(Exception, match="overlapping work"):
                conn.execute(
                    text(
                        "INSERT INTO placements (request_id, candidate_id, "
                        " status, agreed_pay_rwf, pay_unit) "
                        "VALUES (:r, :c, 'offered', 5000, 'day')"
                    ),
                    {"r": live_db["requests"][1], "c": live_db["candidate"]},
                )
    finally:
        engine.dispose()
    assert live_placements(live_db) == 1
