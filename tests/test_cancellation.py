"""Withdrawing a shift that is no longer happening.

The point of care here is whose record carries the decision. A worker who
accepted a shift the employer then cancelled must not end up with a 'declined'
against their name -- that is the employer's decision written onto the worker,
and prior behaviour feeds the ranking.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.matching.repository import find_matches
from app.operations.attendance import log_attendance, start_placement
from app.operations.employer_portal import EmployerPortalError, cancel_request
from app.operations.registry import record_consent, register_employer
from app.operations.requests import (
    RequestError,
    cancel_work_request,
    respond_to_offer,
)

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


@pytest.fixture
def matchable(session, make_candidate, staff_id):
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


def placement_status(session, placement_id) -> str:
    return session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :p"),
        {"p": placement_id},
    ).scalar_one()


# --- whose decision it was -------------------------------------------------

def test_a_cancelled_shift_is_not_recorded_as_the_worker_declining(
    session, matchable, make_placement, make_request
):
    """The whole reason 'cancelled' exists as a status of its own."""
    placement = make_placement(
        candidate_id=matchable(), request_id=(rid := make_request())
    )
    respond_to_offer(session, placement, accepted=True)

    cancel_work_request(session, rid, "site closed for repairs")
    assert placement_status(session, placement) == "cancelled"


def test_an_untouched_offer_is_cancelled_too(
    session, matchable, make_placement, make_request
):
    placement = make_placement(
        candidate_id=matchable(), request_id=(rid := make_request())
    )
    cancel_work_request(session, rid, "no longer needed")
    assert placement_status(session, placement) == "cancelled"


def test_the_worker_is_told_and_told_it_was_not_them(
    session, matchable, make_placement, make_request
):
    """Someone who accepted work and then hears nothing assumes they were
    dropped, and the next offer is harder to fill."""
    make_placement(candidate_id=matchable(), request_id=(rid := make_request()))
    cancel_work_request(session, rid, "site closed")

    body = session.execute(
        text(
            "SELECT body FROM messages WHERE template_key = 'placement_cancelled'"
        )
    ).scalar_one()
    assert "cancelled by the employer" in body
    assert "not your doing" in body


def test_queued_messages_stop_before_the_cancellation_goes_out(
    session, matchable, make_placement, make_request
):
    """A shift reminder arriving after 'it is cancelled' would be worse than
    either message alone."""
    placement = make_placement(
        candidate_id=matchable(), request_id=(rid := make_request())
    )
    respond_to_offer(session, placement, accepted=True)
    start_placement(session, placement, TODAY + timedelta(days=3))

    session.execute(
        text("UPDATE placements SET status = 'accepted' WHERE placement_id = :p"),
        {"p": placement},
    )
    cancel_work_request(session, rid, "site closed")

    reminders = session.execute(
        text(
            "SELECT count(*) FROM messages WHERE placement_id = :p "
            "AND template_key = 'shift_reminder' AND status = 'queued'"
        ),
        {"p": placement},
    ).scalar_one()
    assert reminders == 0


# --- what cancellation refuses ---------------------------------------------

def test_work_that_has_started_cannot_be_cancelled(
    session, matchable, make_placement, make_request
):
    """It cannot be un-happened. The honest record is a termination."""
    placement = make_placement(
        candidate_id=matchable(), request_id=(rid := make_request())
    )
    start_placement(session, placement, TODAY)

    with pytest.raises(RequestError, match="already started"):
        cancel_work_request(session, rid, "changed our mind")
    assert placement_status(session, placement) == "active"


def test_a_reason_is_required(session, make_request):
    with pytest.raises(RequestError, match="must say why"):
        cancel_work_request(session, make_request(), "   ")


def test_cancelling_twice_is_refused(session, make_request):
    rid = make_request()
    cancel_work_request(session, rid, "first time")
    with pytest.raises(RequestError, match="already cancelled"):
        cancel_work_request(session, rid, "again")


# --- the worker is freed ---------------------------------------------------

def test_the_worker_is_available_for_other_work_again(
    session, matchable, make_placement, make_request, staff_id
):
    cid = matchable()
    rid = make_request()
    placement = make_placement(candidate_id=cid, request_id=rid)
    respond_to_offer(session, placement, accepted=True)

    other = register_employer(
        session, business_name="Rival", sector="retail", district="Gasabo",
        account_owner=staff_id, site_lat=-1.9550, site_lng=30.1150,
    )
    clash = session.execute(
        text(
            "INSERT INTO work_requests (employer_id, title, work_type, "
            "headcount, starts_on, pay_rwf, pay_unit, shift_start, shift_end) "
            "VALUES (:e, 'Rival', 'shift', 1, kigali_today(), 5000, 'day', "
            "'08:00', '16:00') RETURNING request_id"
        ),
        {"e": other},
    ).scalar_one()

    assert "Aline" not in [
        m.candidate.display_name for m in find_matches(session, clash).matches
    ]
    cancel_work_request(session, rid, "site closed")
    assert "Aline" in [
        m.candidate.display_name for m in find_matches(session, clash).matches
    ]


def test_the_candidate_status_returns_to_available(
    session, matchable, make_placement, make_request
):
    cid = matchable()
    placement = make_placement(candidate_id=cid, request_id=(rid := make_request()))
    respond_to_offer(session, placement, accepted=True)
    assert session.execute(
        text("SELECT status::text FROM candidates WHERE candidate_id = :c"),
        {"c": cid},
    ).scalar_one() == "placed"

    cancel_work_request(session, rid, "site closed")
    assert session.execute(
        text("SELECT status::text FROM candidates WHERE candidate_id = :c"),
        {"c": cid},
    ).scalar_one() == "registered"


# --- it does not pollute the numbers ---------------------------------------

def test_a_cancelled_shift_is_not_a_failure_to_cover(
    session, matchable, make_placement, make_request
):
    """A shift the employer withdrew is not one we failed to fill."""
    make_placement(candidate_id=matchable(), request_id=(rid := make_request()))
    cancel_work_request(session, rid, "site closed")

    card = session.execute(
        text("SELECT paid_placements, guarantee_invocations FROM v_pilot_scorecard")
    ).mappings().one()
    assert card["paid_placements"] == 0
    assert card["guarantee_invocations"] == 0


def test_a_cancelled_placement_is_not_a_no_show(
    session, matchable, make_placement, make_request
):
    placement = make_placement(
        candidate_id=matchable(), request_id=(rid := make_request())
    )
    cancel_work_request(session, rid, "site closed")
    with pytest.raises(Exception):
        log_attendance(
            session, placement, TODAY, False, "employer",
            absence_reason="did not arrive",
        )


# --- the employer does it themselves ---------------------------------------

def test_an_employer_can_cancel_their_own_shift(
    session, matchable, make_placement, make_request, employer_id
):
    placement = make_placement(
        candidate_id=matchable(), request_id=(rid := make_request())
    )
    result = cancel_request(session, employer_id, rid, "site closed")
    assert result["placements_cancelled"] == 1
    assert placement_status(session, placement) == "cancelled"


def test_an_employer_cannot_cancel_someone_elses_shift(
    session, make_request, staff_id
):
    """The request id in the URL proves nothing."""
    other = register_employer(
        session, business_name="Rival", sector="retail", district="Gasabo",
        account_owner=staff_id,
    )
    with pytest.raises(EmployerPortalError, match="no such work request"):
        cancel_request(session, other, make_request(), "not mine to cancel")


def test_cancelling_over_the_api(client, session, matchable, make_placement,
                                 make_request):
    rid = make_request()
    make_placement(candidate_id=matchable(), request_id=rid)

    refused = client.post(f"/work-requests/{rid}/cancel", json={"reason": ""})
    assert refused.status_code == 422  # empty reason fails validation

    done = client.post(
        f"/work-requests/{rid}/cancel", json={"reason": "site closed"}
    )
    assert done.status_code == 200
    assert done.json()["placements_cancelled"] == 1


def test_attendance_cannot_be_logged_against_a_finished_placement(
    session, matchable, make_placement, make_request
):
    from app.operations.attendance import complete_placement

    placement = make_placement(
        candidate_id=matchable(), request_id=make_request()
    )
    start_placement(session, placement, TODAY)
    complete_placement(session, placement)

    with pytest.raises(Exception, match="completed placement"):
        log_attendance(session, placement, TODAY, True, "employer")


def test_a_mistaken_no_show_can_be_corrected(
    session, matchable, make_placement, make_request
):
    """Otherwise it stands forever as a guarantee invocation against us, and
    as a no-show against the worker."""
    placement = make_placement(
        candidate_id=matchable(), request_id=make_request()
    )
    start_placement(session, placement, TODAY)
    log_attendance(session, placement, TODAY, False, "employer",
                   absence_reason="did not arrive")
    assert placement_status(session, placement) == "no_show"
    assert session.execute(
        text("SELECT count(*) FROM v_guarantee_invocations")
    ).scalar_one() == 1

    log_attendance(session, placement, TODAY, True, "employer", hours_worked=8)
    assert placement_status(session, placement) == "active"
    assert session.execute(
        text("SELECT count(*) FROM v_guarantee_invocations")
    ).scalar_one() == 0
