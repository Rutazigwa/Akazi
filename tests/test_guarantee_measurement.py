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
