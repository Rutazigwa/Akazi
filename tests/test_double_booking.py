"""Nobody can be in two places at once.

The whole promise to an employer is that the worker turns up. A system that
sends the same person to two employers for the same hours breaks that promise
by construction, and does it invisibly -- both employers are told someone is
coming.

This was a real bug: candidates.status never became 'placed', and the matcher
filtered on status, so an actively-placed worker stayed in the pool.
"""

from __future__ import annotations

import os
from datetime import time, timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.matching.repository import find_matches
from app.operations.attendance import (
    AttendanceError,
    complete_placement,
    log_attendance,
    refresh_candidate_status,
    start_placement,
    terminate_placement,
)
from app.operations.registry import record_consent, register_employer
from app.operations.requests import respond_to_offer


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


@pytest.fixture
def matchable(session, make_candidate, staff_id):
    """A candidate who passes every other filter, so only conflict is in play."""
    def _make(name="Aline"):
        cid = make_candidate(name=name)
        record_consent(session, cid, "placement", True, "paper", staff_id)
        session.execute(
            text(
                "INSERT INTO availability (candidate_id, day_of_week, "
                "start_time, end_time) "
                "SELECT :c, d, '06:00', '22:00' FROM generate_series(0,6) d"
            ),
            {"c": cid},
        )
        return cid

    return _make


@pytest.fixture
def rival_request(session, staff_id):
    """A second employer's shift, with configurable dates and hours."""
    employer_id = register_employer(
        session, business_name="Rival Retail", sector="retail",
        district="Gasabo", account_owner=staff_id,
        site_lat=-1.9550, site_lng=30.1150,
    )

    def _make(starts_on=TODAY, ends_on=None, shift_start=time(8, 0),
              shift_end=time(16, 0)):
        return session.execute(
            text(
                """
                INSERT INTO work_requests (employer_id, title, work_type,
                                           headcount, starts_on, ends_on,
                                           pay_rwf, pay_unit,
                                           shift_start, shift_end)
                VALUES (:e, 'Rival shift', 'shift', 1, :starts, :ends,
                        5000, 'day', :ss, :se)
                RETURNING request_id
                """
            ),
            {"e": employer_id, "starts": starts_on, "ends": ends_on,
             "ss": shift_start, "se": shift_end},
        ).scalar_one()

    return _make


def names(session, request_id):
    return [m.candidate.display_name for m in find_matches(session, request_id).matches]


def exclusion(session, request_id, display_name):
    return next(
        r for r in find_matches(session, request_id).rejections
        if r.candidate.display_name == display_name
    )


# --- the bug ---------------------------------------------------------------

def test_an_actively_placed_worker_is_not_offered_overlapping_work(
    session, matchable, make_placement, make_request, rival_request
):
    cid = matchable()
    first = make_placement(candidate_id=cid, request_id=make_request())
    start_placement(session, first, TODAY)

    clash = rival_request()
    assert "Aline" not in names(session, clash)
    assert exclusion(session, clash, "Aline").reason == (
        "already committed to overlapping work"
    )


def test_an_outstanding_offer_also_blocks(
    session, matchable, make_placement, make_request, rival_request
):
    """An offer they have not answered still holds the slot -- otherwise two
    employers are each told someone is coming."""
    cid = matchable()
    make_placement(candidate_id=cid, request_id=make_request())
    assert "Aline" not in names(session, rival_request())


def test_declining_frees_them_again(
    session, matchable, make_placement, make_request, rival_request
):
    cid = matchable()
    offered = make_placement(candidate_id=cid, request_id=make_request())
    clash = rival_request()
    assert "Aline" not in names(session, clash)

    respond_to_offer(session, offered, accepted=False)
    assert "Aline" in names(session, clash)


def test_completing_frees_them_again(
    session, matchable, make_placement, make_request, rival_request
):
    cid = matchable()
    placement = make_placement(candidate_id=cid, request_id=make_request())
    start_placement(session, placement, TODAY)
    clash = rival_request()
    assert "Aline" not in names(session, clash)

    complete_placement(session, placement)
    assert "Aline" in names(session, clash)


def test_a_no_show_frees_them_again(
    session, matchable, make_placement, make_request, rival_request
):
    """They are not working, whatever the offer said."""
    cid = matchable()
    placement = make_placement(candidate_id=cid, request_id=make_request())
    start_placement(session, placement, TODAY)
    log_attendance(session, placement, TODAY, False, "employer",
                   absence_reason="did not arrive")
    assert "Aline" in names(session, rival_request())


# --- what counts as a clash ------------------------------------------------

def test_a_different_day_is_not_a_clash(
    session, matchable, make_placement, make_request, rival_request
):
    cid = matchable()
    start_placement(
        session, make_placement(candidate_id=cid, request_id=make_request()), TODAY
    )
    assert "Aline" in names(
        session, rival_request(starts_on=TODAY + timedelta(days=1))
    )


def test_back_to_back_shifts_are_not_a_clash(
    session, matchable, make_placement, make_request, rival_request
):
    """06:00-12:00 then 12:00-17:00 is a long day, not a double booking.

    Both finish before dark on purpose: a later second shift would be excluded
    by the safety filter instead, and the test would pass for the wrong reason.
    """
    cid = matchable()
    request_id = make_request()
    session.execute(
        text(
            "UPDATE work_requests SET shift_start = '06:00', shift_end = '12:00' "
            "WHERE request_id = :r"
        ),
        {"r": request_id},
    )
    start_placement(
        session, make_placement(candidate_id=cid, request_id=request_id), TODAY
    )
    assert "Aline" in names(
        session, rival_request(shift_start=time(12, 0), shift_end=time(17, 0))
    )


def test_overlapping_hours_on_the_same_day_are_a_clash(
    session, matchable, make_placement, make_request, rival_request
):
    cid = matchable()
    request_id = make_request()
    session.execute(
        text(
            "UPDATE work_requests SET shift_start = '08:00', shift_end = '16:00' "
            "WHERE request_id = :r"
        ),
        {"r": request_id},
    )
    start_placement(
        session, make_placement(candidate_id=cid, request_id=request_id), TODAY
    )
    assert "Aline" not in names(
        session, rival_request(shift_start=time(15, 0), shift_end=time(20, 0))
    )


def test_a_multi_day_placement_blocks_days_inside_it(
    session, matchable, make_placement, make_request, rival_request
):
    cid = matchable()
    request_id = make_request()
    session.execute(
        text("UPDATE work_requests SET ends_on = :e WHERE request_id = :r"),
        {"e": TODAY + timedelta(days=10), "r": request_id},
    )
    start_placement(
        session, make_placement(candidate_id=cid, request_id=request_id), TODAY
    )
    assert "Aline" not in names(
        session, rival_request(starts_on=TODAY + timedelta(days=5))
    )


def test_a_request_does_not_conflict_with_itself(
    session, matchable, make_placement, make_request
):
    """Filling a second headcount on the same request must still work."""
    request_id = make_request()
    session.execute(
        text("UPDATE work_requests SET headcount = 2 WHERE request_id = :r"),
        {"r": request_id},
    )
    matchable("Aline")
    second = matchable("Beatrice")
    start_placement(
        session, make_placement(candidate_id=second, request_id=request_id), TODAY
    )
    assert "Aline" in names(session, request_id)


# --- status stays honest ---------------------------------------------------

def test_status_follows_the_placement(
    session, matchable, make_placement, make_request
):
    cid = matchable()
    placement = make_placement(candidate_id=cid, request_id=make_request())

    def status():
        return session.execute(
            text("SELECT status::text FROM candidates WHERE candidate_id = :c"),
            {"c": cid},
        ).scalar_one()

    respond_to_offer(session, placement, accepted=True)
    assert status() == "placed"

    start_placement(session, placement, TODAY)
    assert status() == "placed"

    complete_placement(session, placement)
    assert status() == "registered"


def test_status_never_overwrites_a_withdrawal(
    session, matchable, make_placement, make_request
):
    """'withdrawn' is a decision about the person, not a summary of their work."""
    cid = matchable()
    session.execute(
        text("UPDATE candidates SET status = 'withdrawn' WHERE candidate_id = :c"),
        {"c": cid},
    )
    refresh_candidate_status(session, cid)
    assert session.execute(
        text("SELECT status::text FROM candidates WHERE candidate_id = :c"),
        {"c": cid},
    ).scalar_one() == "withdrawn"


# --- ending a placement ----------------------------------------------------

def test_terminating_requires_a_reason(
    session, matchable, make_placement, make_request
):
    """An unexplained termination is indistinguishable from a dropout, and the
    two mean opposite things for retention."""
    placement = make_placement(
        candidate_id=matchable(), request_id=make_request()
    )
    start_placement(session, placement, TODAY)
    with pytest.raises(AttendanceError, match="say why"):
        terminate_placement(session, placement, "   ")


def test_terminating_frees_the_worker(
    session, matchable, make_placement, make_request, rival_request
):
    cid = matchable()
    placement = make_placement(candidate_id=cid, request_id=make_request())
    start_placement(session, placement, TODAY)
    terminate_placement(session, placement, "site closed")
    assert "Aline" in names(session, rival_request())


def test_only_an_active_placement_can_be_completed(
    session, matchable, make_placement, make_request
):
    placement = make_placement(
        candidate_id=matchable(), request_id=make_request()
    )
    with pytest.raises(AttendanceError, match="only an active placement"):
        complete_placement(session, placement)


def test_ending_a_placement_cancels_its_queued_messages(
    session, matchable, make_placement, make_request
):
    cid = matchable()
    placement = make_placement(candidate_id=cid, request_id=make_request())
    start_placement(session, placement, TODAY + timedelta(days=3))
    complete_placement(session, placement)

    queued = session.execute(
        text(
            "SELECT count(*) FROM messages WHERE placement_id = :p "
            "AND status = 'queued'"
        ),
        {"p": placement},
    ).scalar_one()
    assert queued == 0


# --- over HTTP -------------------------------------------------------------

def test_ending_a_placement_over_http(client, session, matchable,
                                      make_placement, make_request):
    placement = make_placement(
        candidate_id=matchable(), request_id=make_request()
    )
    start_placement(session, placement, TODAY)

    refused = client.post(f"/placements/{placement}/terminate", json={})
    assert refused.status_code == 409

    done = client.post(f"/placements/{placement}/complete", json={})
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
