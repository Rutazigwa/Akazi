"""The direction the response-time metric must move.

A metric is not just right or wrong, it has a direction, and this one pointed
backwards. hours_to_acknowledge was COALESCE(acknowledged_at, now()) -
raised_at, so an escalation nobody had answered contributed its
elapsed-time-so-far to the average -- and a report raised a minute ago
contributes almost nothing.

Measured on real data before the fix: one harassment report answered after
5.00 hours against a 2-hour target. Three more harassment reports arrive and
nobody touches them, and the figure improves to 1.25 hours. The number whose
stated purpose is to show whether the safeguard is real got better the more
reports went unanswered.

These tests pin the direction, not the value.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.operations.escalations import (
    acknowledge,
    raise_escalation,
    response_performance,
)


def by_kind(session, kind: str) -> dict:
    for row in response_performance(session):
        if row["kind"] == kind:
            return row
    raise AssertionError(f"no {kind} row in {response_performance(session)}")


def answered_after(session, staff_id, make_candidate, hours: float, kind="harassment"):
    """An escalation raised `hours` ago and answered just now."""
    eid = raise_escalation(session, kind, candidate_id=make_candidate(),
                           detail="report", owner_staff_id=staff_id)
    session.execute(
        text("UPDATE escalations SET raised_at = raised_at - make_interval(mins => :m), "
             "respond_by = respond_by - make_interval(mins => :m) WHERE escalation_id = :e"),
        {"m": int(hours * 60), "e": str(eid)},
    )
    acknowledge(session, eid, staff_id)
    return eid


def test_an_unanswered_report_cannot_improve_the_average(session, staff_id, make_candidate):
    """The defect, stated as a property.

    This is the whole finding: adding failures must not make the number look
    better.
    """
    answered_after(session, staff_id, make_candidate, hours=5)
    before = by_kind(session, "harassment")["avg_hours"]

    for _ in range(3):
        raise_escalation(session, "harassment", candidate_id=make_candidate(),
                         detail="nobody has looked at this", owner_staff_id=staff_id)

    after = by_kind(session, "harassment")["avg_hours"]
    assert after == before, (
        f"three unanswered harassment reports moved the average from {before} "
        f"to {after}; an average of response times may only count responses"
    )


def test_unanswered_reports_are_counted_where_they_cannot_be_missed(
    session, staff_id, make_candidate
):
    """Removing them from the average must not remove them from the page."""
    answered_after(session, staff_id, make_candidate, hours=5)
    for _ in range(3):
        raise_escalation(session, "harassment", candidate_id=make_candidate(),
                         detail="waiting", owner_staff_id=staff_id)

    row = by_kind(session, "harassment")
    assert row["raised"] == 4
    assert row["unanswered"] == 3
    assert row["longest_waiting_hours"] is not None


def test_the_longest_wait_grows_as_a_report_goes_unanswered(
    session, staff_id, make_candidate
):
    """The number that must get worse when nothing happens."""
    eid = raise_escalation(session, "harassment", candidate_id=make_candidate(),
                           detail="waiting", owner_staff_id=staff_id)
    fresh = by_kind(session, "harassment")["longest_waiting_hours"]

    session.execute(
        text("UPDATE escalations SET raised_at = raised_at - INTERVAL '9 hours' "
             "WHERE escalation_id = :e"),
        {"e": str(eid)},
    )
    stale = by_kind(session, "harassment")["longest_waiting_hours"]
    assert stale > fresh and stale >= 9


def test_a_kind_with_nothing_answered_reports_no_average_rather_than_zero(
    session, staff_id, make_candidate
):
    """Zero would read as "we answer instantly"."""
    raise_escalation(session, "harassment", candidate_id=make_candidate(),
                     detail="waiting", owner_staff_id=staff_id)
    row = by_kind(session, "harassment")
    assert row["avg_hours"] is None, (
        f"avg_hours was {row['avg_hours']} with nothing answered; a number "
        "here reads as a response time that never happened"
    )


def test_answering_slowly_still_shows_as_slow(session, staff_id, make_candidate):
    """Guards the guard: a fix that made avg_hours always None would pass the
    tests above while destroying the metric."""
    answered_after(session, staff_id, make_candidate, hours=5)
    assert by_kind(session, "harassment")["avg_hours"] == pytest.approx(5.0, abs=0.05)


def test_the_page_shows_the_unanswered_count(web, session, staff_id, make_candidate):
    """A column nobody renders is a column nobody reads."""
    raise_escalation(session, "harassment", candidate_id=make_candidate(),
                     detail="waiting", owner_staff_id=staff_id)
    session.commit()
    body = web.get("/ui/reports").text
    assert "Still unanswered" in body
    assert "Longest waiting" in body
