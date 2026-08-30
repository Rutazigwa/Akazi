"""What "replaced within 24 hours" is measured from, and what counts as filled.

This is the number the whole thesis rests on -- "if a placed worker does not
arrive, we fill the slot free of charge, same day" -- and it is shown to the
employer on their own dashboard. It had three defects.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import KIGALI, kigali_today
from app.operations.registry import record_consent
from app.operations.attendance import (
    log_attendance,
    open_guarantees,
    record_replacement,
    start_placement,
)


def a_no_show(session, make_placement, make_candidate, days_ago=0):
    pid = make_placement(candidate_id=make_candidate())
    day = kigali_today() - timedelta(days=days_ago)
    start_placement(session, pid, day)
    log_attendance(session, pid, day, False, "employer", absence_reason="no-show")
    return pid


def invocation(session, pid):
    return session.execute(
        text("SELECT * FROM v_guarantee_invocations WHERE failed_placement_id = :p"),
        {"p": str(pid)},
    ).mappings().one()


def accept(session, cover):
    session.execute(
        text("UPDATE placements SET status = 'accepted' WHERE placement_id = :p"),
        {"p": str(cover)},
    )


# --- 1. the clock runs from the shift, not from when we noticed ------------

def test_noticing_late_does_not_buy_more_time(session, make_placement,
                                              make_candidate):
    """The window used to start at min(attendance.confirmed_at).

    A no-show at 08:00 recorded at 16:00 gave us until 16:00 the next day, so
    the slower we were to notice, the easier the target. That is a metric that
    improves when the operation performs worse -- the same shape as the
    escalation response time fixed in migration 050.
    """
    pid = a_no_show(session, make_placement, make_candidate)
    before = invocation(session, pid)["invoked_at"]

    session.execute(
        text("UPDATE attendance SET confirmed_at = confirmed_at + INTERVAL '9 hours' "
             "WHERE placement_id = :p"),
        {"p": str(pid)},
    )
    after = invocation(session, pid)

    assert after["invoked_at"] == before, (
        "noticing nine hours later moved the deadline nine hours out"
    )
    assert after["noticed_at"] > after["invoked_at"], (
        "how long the employer stood there is still recorded, separately"
    )


def test_the_window_runs_from_the_shift_start(session, make_placement,
                                              make_candidate):
    """Shift work carries hours (migration 053), so this is knowable."""
    pid = a_no_show(session, make_placement, make_candidate)
    # Stored as timestamptz and returned in UTC; the shift is 08:00 Kigali,
    # which is 06:00 UTC. Asserting on the raw hour would be asserting on the
    # server's timezone rather than on the business's.
    invoked = invocation(session, pid)["invoked_at"].astimezone(KIGALI)
    assert (invoked.hour, invoked.minute) == (8, 0), invoked


# --- 2. an offer is not a fill --------------------------------------------

def test_an_unanswered_offer_is_not_a_covered_shift(session, make_placement,
                                                    make_candidate):
    pid = a_no_show(session, make_placement, make_candidate)
    record_replacement(session, pid, make_candidate(name="Asked"), "matched")

    assert invocation(session, pid)["filled_within_24h"] is False
    still = open_guarantees(session)
    assert len(still) == 1
    assert still[0]["awaiting_reply_name"] == "Asked", (
        "it must say who is being waited on, or a coordinator rings a second "
        "person and two workers turn up"
    )


def test_a_declined_offer_is_not_a_covered_shift(session, make_placement,
                                                 make_candidate):
    """Somebody we rang who said no was recorded as the slot being covered.

    The promise is that the shift gets worked, not that we made a phone call.
    """
    pid = a_no_show(session, make_placement, make_candidate)
    cover = record_replacement(session, pid, make_candidate(name="No"), "matched")
    session.execute(
        text("UPDATE placements SET status = 'declined' WHERE placement_id = :p"),
        {"p": str(cover)},
    )

    assert invocation(session, pid)["filled_within_24h"] is False
    assert len(open_guarantees(session)) == 1


def test_an_accepted_offer_is(session, make_placement, make_candidate):
    """Guards the guard: a change that never counted anything as filled would
    pass every test above while making the guarantee unreportable."""
    pid = a_no_show(session, make_placement, make_candidate)
    cover = record_replacement(session, pid, make_candidate(name="Yes"), "matched")
    accept(session, cover)

    assert invocation(session, pid)["filled_within_24h"] is True
    assert open_guarantees(session) == []


# --- 3. a decline must not consume the only attempt ------------------------

def test_somebody_else_can_be_offered_after_a_decline(session, make_placement,
                                                      make_candidate):
    """The most serious of the three.

    idx_one_replacement_per_placement was unique on replaces_placement whatever
    the status, so the first cover consumed the only slot. Ringing round at 6am
    and having the first person say no is the ordinary case, and the system
    then refused to record anybody else -- with a raw IntegrityError, not even
    a domain error. The guarantee could not be honoured by anyone, ever.
    """
    pid = a_no_show(session, make_placement, make_candidate)
    first = record_replacement(session, pid, make_candidate(name="First"), "matched")
    session.execute(
        text("UPDATE placements SET status = 'declined' WHERE placement_id = :p"),
        {"p": str(first)},
    )

    second = record_replacement(session, pid, make_candidate(name="Second"),
                                "next on the list")
    accept(session, second)
    assert invocation(session, pid)["filled_within_24h"] is True


def test_two_people_are_still_never_sent_to_the_same_shift(session,
                                                           make_placement,
                                                           make_candidate):
    """The invariant the index existed for, which the fix must keep.

    One cover on the table at a time: a declined offer is not on the table, an
    outstanding one is.
    """
    pid = a_no_show(session, make_placement, make_candidate)
    record_replacement(session, pid, make_candidate(name="Already asked"), "matched")

    with pytest.raises(Exception, match="idx_one_live_replacement_per_placement"):
        record_replacement(session, pid, make_candidate(name="Also asked"), "matched")


def test_the_dashboard_says_who_has_been_asked(web, session, make_placement,
                                               make_candidate):
    """"Cover it" next to a shift somebody has already been asked about is how
    two workers end up sent to the same shift."""
    pid = a_no_show(session, make_placement, make_candidate)
    record_replacement(session, pid, make_candidate(name="Chantal"), "matched")
    session.commit()

    page = " ".join(web.get("/ui/").text.split())
    assert "asked Chantal" in page
    assert "waiting for a reply" in page


# --- covering a shift does not suspend the filters -------------------------

def after_dark_no_show(session, make_placement, make_candidate, make_request):
    request_id = make_request(shift_start="14:00", shift_end="22:00")
    pid = make_placement(request_id=request_id, candidate_id=make_candidate())
    start_placement(session, pid, kigali_today())
    log_attendance(session, pid, kigali_today(), False, "employer",
                   absence_reason="no-show")
    return pid


def test_cover_without_consent_is_refused(session, make_placement,
                                          make_candidate, make_request):
    """offer_placement re-runs the matching filters rather than trusting its
    caller. record_replacement trusted its caller completely -- and it is the
    path used under the most time pressure, which is exactly when a safeguard
    gets skipped.
    """
    pid = a_no_show(session, make_placement, make_candidate)
    nobody = make_candidate(name="Never Asked", consented=False)

    with pytest.raises(Exception, match="no consent on record"):
        record_replacement(session, pid, nobody, "coordinator choice")


def test_cover_after_dark_without_an_opt_in_is_refused(
    session, make_placement, make_candidate, make_request
):
    """A woman sent to a shift finishing at 22:00, with no employer-covered
    transport and no opt-in, was recordable as cover. The blueprint makes this
    a product requirement, not a reporting line."""
    pid = after_dark_no_show(session, make_placement, make_candidate, make_request)

    with pytest.raises(Exception, match="after dark"):
        record_replacement(session, pid, make_candidate(name="Sent Anyway",
                                                        gender="F"),
                           "coordinator choice")


def test_her_own_opt_in_is_what_permits_it(session, make_placement,
                                           make_candidate, make_request,
                                           staff_id):
    """Guards the guard, and says who may decide.

    The opt-in belongs to the candidate. A coordinator cannot consent on her
    behalf, so there is no override here -- the escape is the one the blueprint
    names, and it is hers.
    """
    pid = after_dark_no_show(session, make_placement, make_candidate, make_request)
    opted = make_candidate(name="Opted In", gender="F")
    # Recorded, not set. The column is gone: an opt-in has to say when she gave
    # it and who wrote it down, and has to be withdrawable.
    record_consent(session, opted, purpose="after_dark", granted=True,
                   captured_via="paper", captured_by=staff_id)
    cover = record_replacement(session, pid, opted, "she has opted in")
    assert cover is not None


def test_an_ordinary_cover_is_not_obstructed(session, make_placement,
                                             make_candidate):
    """Guards the guard: a check that refused everybody would pass every test
    above while making the guarantee impossible to honour."""
    pid = a_no_show(session, make_placement, make_candidate)
    assert record_replacement(session, pid, make_candidate(name="Fine"),
                              "nearest available") is not None


def test_a_shift_that_has_already_ended_still_checks_the_person(
    session, make_placement, make_candidate, make_request
):
    """The hole in the first version of this guard.

    It asked find_cover whether the candidate was excluded. find_cover answers
    "who could still get there", and returns early with no per-candidate
    rejections at all once the window has closed -- so every candidate came
    back unexcluded for every shift already over, which is most of the ones
    being recorded after the fact. The rights filters do not depend on the
    clock and are asked separately now.
    """
    request_id = make_request(shift_start="06:00", shift_end="07:00")
    pid = make_placement(request_id=request_id, candidate_id=make_candidate())
    start_placement(session, pid, kigali_today())
    log_attendance(session, pid, kigali_today(), False, "employer",
                   absence_reason="no-show")

    with pytest.raises(Exception, match="no consent on record"):
        record_replacement(session, pid, make_candidate(name="Late", consented=False),
                           "recorded after the shift ended")


def test_she_can_withdraw_the_opt_in(session, make_placement, make_candidate,
                                     make_request, staff_id):
    """The direction that matters most, and the one that was impossible.

    accepts_after_dark was a boolean set once at registration with no update
    path anywhere in the application. Somebody who said yes could never take it
    back. A safety consent that cannot be revoked is not a consent.
    """
    woman = make_candidate(name="Changed Her Mind", gender="F")
    record_consent(session, woman, purpose="after_dark", granted=True,
                   captured_via="paper", captured_by=staff_id)

    first = after_dark_no_show(session, make_placement, make_candidate, make_request)
    assert record_replacement(session, first, woman, "she has opted in")

    record_consent(session, woman, purpose="after_dark", granted=False,
                   captured_via="whatsapp", captured_by=staff_id)

    second = after_dark_no_show(session, make_placement, make_candidate, make_request)
    with pytest.raises(Exception, match="after dark"):
        record_replacement(session, second, woman, "she has not")


def test_the_history_of_what_she_agreed_to_survives(session, make_candidate,
                                                    staff_id):
    """"Did she agree to that, and when?" is the question this exists to
    answer, and it is asked after something has happened."""
    woman = make_candidate(name="History", gender="F")
    record_consent(session, woman, purpose="after_dark", granted=True,
                   captured_via="paper", captured_by=staff_id)
    record_consent(session, woman, purpose="after_dark", granted=False,
                   captured_via="whatsapp", captured_by=staff_id)

    history = session.execute(
        text("SELECT granted, captured_via, captured_at FROM consent_records "
             "WHERE candidate_id = :c AND purpose = 'after_dark' "
             "ORDER BY captured_at"),
        {"c": str(woman)},
    ).mappings().all()

    # Registration's row, then the grant, then the withdrawal. Nothing is
    # overwritten -- the table refuses updates outright.
    assert [r["granted"] for r in history] == [False, True, False]
    assert all(r["captured_at"] is not None for r in history)
