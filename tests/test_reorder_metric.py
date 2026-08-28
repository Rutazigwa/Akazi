"""Repeat business, and the number the pivot decision rests on.

"Employer reorder rate >= 40%" is a pilot target, and the blueprint's go/no-go
says to pivot if the guarantee generates no pricing power. Reordering was a
button that copied a request's fields and recorded nothing, so the rate could
not be computed at all.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.operations.employer_portal import post_request, reorder
from app.operations.requests import create_work_request



TOMORROW = kigali_today() + timedelta(days=1)
NEXT_WEEK = kigali_today() + timedelta(days=8)


def _request(session, employer_id, **over):
    fields = dict(
        title="Morning cleaner", work_type="shift", headcount=1,
        starts_on=TOMORROW, pay_rwf=5000, pay_unit="day",
    )
    fields.update(over)
    return post_request(session, employer_id, **fields)


def _fill(session, request_id, candidate_id):
    """A placement that actually happened, which is what being served means."""
    return session.execute(
        text(
            """
            INSERT INTO placements (request_id, candidate_id, status,
                                    agreed_pay_rwf, pay_unit, est_transport_rwf)
            VALUES (:rid, :cid, 'completed', 5000, 'day', 500)
            RETURNING placement_id
            """
        ),
        {"rid": str(request_id), "cid": str(candidate_id)},
    ).scalar_one()


def _scorecard(session):
    return session.execute(
        text("SELECT * FROM v_pilot_scorecard")
    ).mappings().one()


def test_reordering_records_which_request_it_repeats(session, make_employer):
    employer = make_employer()
    first = _request(session, employer)
    repeat = reorder(session, employer, first, NEXT_WEEK)

    repeats = session.execute(
        text("SELECT reorders_request FROM work_requests WHERE request_id = :r"),
        {"r": str(repeat)},
    ).scalar_one()
    assert repeats == first


def test_a_first_request_repeats_nothing(session, make_employer):
    employer = make_employer()
    first = _request(session, employer)
    assert session.execute(
        text("SELECT reorders_request FROM work_requests WHERE request_id = :r"),
        {"r": str(first)},
    ).scalar_one() is None


def test_a_reorder_carries_the_original_terms_to_the_new_date(
    session, make_employer
):
    employer = make_employer()
    first = _request(session, employer, pay_rwf=7000, transport_covered=True)
    repeat = reorder(session, employer, first, NEXT_WEEK)

    row = session.execute(
        text("SELECT pay_rwf, transport_covered, starts_on FROM work_requests "
             "WHERE request_id = :r"),
        {"r": str(repeat)},
    ).mappings().one()
    assert row["pay_rwf"] == 7000
    assert row["transport_covered"] is True
    assert row["starts_on"] == NEXT_WEEK


def test_a_reorder_cannot_point_at_another_employers_request(
    session, make_employer
):
    """Otherwise one employer's repeat business is credited to another."""
    theirs = _request(session, make_employer())
    mine = make_employer()

    with pytest.raises(Exception) as caught:
        create_work_request(
            session, employer_id=mine, title="Cleaner", work_type="shift",
            headcount=1, starts_on=TOMORROW, pay_rwf=5000, pay_unit="day",
            reorders_request=theirs,
        )
    assert "same employer" in str(caught.value)
    session.rollback()


def test_a_request_cannot_reorder_itself(session, make_employer):
    employer = make_employer()
    request_id = _request(session, employer)
    with pytest.raises(Exception) as caught:
        session.execute(
            text("UPDATE work_requests SET reorders_request = request_id "
                 "WHERE request_id = :r"),
            {"r": str(request_id)},
        )
    assert "cannot reorder itself" in str(caught.value)
    session.rollback()


def test_an_employer_nobody_was_placed_with_is_not_counted(
    session, make_employer
):
    """They have not declined to reorder -- they have not been served yet."""
    employer = make_employer()
    _request(session, employer)

    served = session.execute(
        text("SELECT count(*) FROM v_employer_reorder WHERE employer_id = :e"),
        {"e": str(employer)},
    ).scalar_one()
    assert served == 0


def test_a_served_employer_who_came_back_counts_as_a_reorder(
    session, make_employer, make_candidate
):
    employer = make_employer()
    first = _request(session, employer)
    _fill(session, first, make_candidate())
    reorder(session, employer, first, NEXT_WEEK)

    row = session.execute(
        text("SELECT requests_posted, requests_after_serving, "
             "reorders_via_button, has_reordered "
             "FROM v_employer_reorder WHERE employer_id = :e"),
        {"e": str(employer)},
    ).mappings().one()
    assert row["requests_posted"] == 2
    assert row["requests_after_serving"] == 1
    assert row["reorders_via_button"] == 1
    assert row["has_reordered"] is True


def test_coming_back_without_the_button_still_counts(
    session, make_employer, make_candidate
):
    """The metric measures repeat business, not use of a particular button.

    Counting only button presses would understate the rate and could produce a
    false pivot signal.
    """
    employer = make_employer()
    first = _request(session, employer)
    _fill(session, first, make_candidate())
    _request(session, employer, starts_on=NEXT_WEEK)

    row = session.execute(
        text("SELECT reorders_via_button, has_reordered "
             "FROM v_employer_reorder WHERE employer_id = :e"),
        {"e": str(employer)},
    ).mappings().one()
    assert row["reorders_via_button"] == 0
    assert row["has_reordered"] is True


def test_a_served_employer_who_never_returned_drags_the_rate_down(
    session, make_employer, make_candidate
):
    came_back = make_employer()
    first = _request(session, came_back)
    _fill(session, first, make_candidate())
    reorder(session, came_back, first, NEXT_WEEK)

    once_only = make_employer()
    only = _request(session, once_only)
    _fill(session, only, make_candidate())

    card = _scorecard(session)
    assert card["employers_served"] == 2
    assert float(card["employer_reorder_pct"]) == pytest.approx(50.0)


def test_the_scorecard_reports_the_reorder_rate(session):
    """It is one of the ten targets; it must appear on the owner's scorecard."""
    assert "employer_reorder_pct" in _scorecard(session)


def test_two_roles_posted_before_anyone_arrives_are_not_repeat_business(
    session, make_employer, make_candidate
):
    """The sharpest way to inflate this number, and the one that matters.

    An employer posting a cleaner and a guard on the same morning has ordered
    twice, but has not come back -- nothing has been delivered to them yet, so
    that second request says nothing about whether the guarantee is worth
    paying for. Counting it would push the pivot metric up precisely when the
    evidence for pivoting is strongest.
    """
    employer = make_employer()
    first = _request(session, employer, title="Morning cleaner")
    _request(session, employer, title="Night guard")
    _fill(session, first, make_candidate())

    row = session.execute(
        text("SELECT requests_posted, requests_after_serving, has_reordered "
             "FROM v_employer_reorder WHERE employer_id = :e"),
        {"e": str(employer)},
    ).mappings().one()
    assert row["requests_posted"] == 2
    assert row["requests_after_serving"] == 0
    assert row["has_reordered"] is False


def test_a_reorder_in_the_same_transaction_still_follows_the_placement(
    session, make_employer, make_candidate
):
    """now() would tie here, and a tie reads as "did not reorder".

    Tests batch everything into one transaction; production usually does not,
    which is what kept this hidden. clock_timestamp() advances within a
    transaction, so the ordering is real either way.
    """
    employer = make_employer()
    first = _request(session, employer)
    _fill(session, first, make_candidate())
    reorder(session, employer, first, NEXT_WEEK)

    assert session.execute(
        text("SELECT has_reordered FROM v_employer_reorder "
             "WHERE employer_id = :e"),
        {"e": str(employer)},
    ).scalar_one() is True
