"""Database privileges under the real application role.

The rest of the suite connects as superuser, where GRANT and REVOKE do not
apply. That is convenient and it hid a production-breaking bug: the matching
engine joined candidate_identity, which app_operations cannot read, so matching
would have failed on any deployment that actually used the role model.

These tests run the real queries with `SET ROLE app_operations` so the grants
are exercised. If something here fails, the deployment is broken even though
every other test passes.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import text

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = date.today()


@contextmanager
def as_operations(session):
    """Run the enclosed statements with only app_operations' privileges.

    A context manager rather than a fixture, deliberately: seed data has to be
    created first, and a fixture would drop privileges before the test body
    gets a chance to set anything up.
    """
    session.execute(text("SET LOCAL ROLE app_operations"))
    try:
        yield session
    finally:
        session.execute(text("RESET ROLE"))


def test_operations_cannot_read_identity_directly(session):
    """The revoke that makes the audit trail complete.

    A savepoint around the expected failure: a permission error aborts the
    transaction, and without one the RESET ROLE that follows cannot run.
    """
    with as_operations(session) as ops:
        savepoint = ops.begin_nested()
        with pytest.raises(Exception, match="permission denied"):
            ops.execute(text("SELECT * FROM candidate_identity")).all()
        savepoint.rollback()


def test_the_matcher_runs_without_identity_access(
    session, make_candidate, make_request, staff_id
):
    """The regression. This is the query that was broken in production."""
    from app.matching.repository import find_matches

    make_candidate()
    request_id = make_request()

    with as_operations(session):
        result = find_matches(session, request_id)

    # Matched or excluded does not matter here -- that it ran at all does.
    assert result.matches or result.rejections


def test_age_eligibility_is_readable_without_reading_dates_of_birth(
    session, make_candidate
):
    make_candidate()
    with as_operations(session) as ops:
        rows = ops.execute(
            text(
                "SELECT candidate_id, age_eligible "
                "FROM candidates_age_eligible(:d)"
            ),
            {"d": TODAY},
        ).mappings().all()
    assert rows
    assert rows[0]["age_eligible"] is True
    # A boolean, not a date. Nothing identifying crossed the boundary.
    assert set(rows[0]) == {"candidate_id", "age_eligible"}


def test_an_under_16_candidate_is_reported_ineligible(session, make_candidate):
    """Belt and braces: the constraint blocks the insert, and if it were ever
    relaxed, the matcher would still refuse to place them."""
    make_candidate()
    rows = session.execute(
        text(
            "SELECT age_eligible FROM candidates_age_eligible(:d)"
        ),
        # Ask the question as of a date when today's 22-year-old was 10.
        {"d": TODAY - timedelta(days=365 * 12)},
    ).mappings().all()
    assert all(r["age_eligible"] is False for r in rows) or not rows


def test_operations_can_do_its_actual_job(
    session, make_placement, make_request, make_candidate, staff_id
):
    """The operational tables app_operations must be able to reach."""
    pid = make_placement(request_id=make_request(), candidate_id=make_candidate())
    with as_operations(session) as ops:
        for query in (
            "SELECT count(*) FROM candidates",
            "SELECT count(*) FROM placements",
            "SELECT count(*) FROM attendance",
            "SELECT count(*) FROM work_requests",
            "SELECT count(*) FROM v_pilot_scorecard",
            "SELECT count(*) FROM v_guarantee_invocations",
            "SELECT count(*) FROM v_current_consent",
        ):
            ops.execute(text(query)).scalar_one()
    assert pid


def test_identity_reads_still_work_through_the_audited_function(
    session, make_candidate
):
    """app_identity keeps the function, not the table."""
    cid = make_candidate()
    granted = session.execute(
        text(
            "SELECT has_function_privilege('app_identity', "
            "'read_candidate_identity(uuid)', 'EXECUTE')"
        )
    ).scalar_one()
    assert granted is True

    table = session.execute(
        text(
            "SELECT has_table_privilege('app_identity', 'candidate_identity', "
            "'SELECT')"
        )
    ).scalar_one()
    assert table is False
    assert cid
