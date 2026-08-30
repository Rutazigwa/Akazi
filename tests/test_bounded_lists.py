"""Every list on a screen is bounded, and says what it is not showing.

Measured on a database with a year of operating in it -- 5,000 placements,
2,000 candidates, 182,000 audit rows:

    /ui/            1,177 KB   3,969 table rows  (3,590 of them follow-ups)
    /ui/candidates  1,464 KB   4,002 table rows

That is not a work queue. It is 90% of the dashboard burying the hundred things
that need a response, on a page a coordinator opens every morning from a phone.
Bounded, they are 78 KB and 45 KB.

The bound is the easy half. The half that matters is that the heading keeps
reporting the true total, because a page silently showing 25 of 3,590 is worse
than a slow one -- it looks finished.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from datetime import timedelta

from app.clock import kigali_today
from app.rules import DASHBOARD_ROWS, REGISTRY_ROWS


@pytest.fixture
def many_follow_ups(session, make_placement, make_candidate, make_request):
    """More overdue follow-ups than a screen will show."""
    made = []
    for i in range(DASHBOARD_ROWS + 8):
        placement_id = make_placement(candidate_id=make_candidate(name=f"Person {i}"),
                                      request_id=make_request())
        session.execute(
            text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
                 "VALUES (:p, 'day_1', :d)"),
            {"p": str(placement_id), "d": kigali_today() - timedelta(days=i + 1)},
        )
        made.append(placement_id)
    return made


def test_the_follow_up_queue_is_bounded(session, many_follow_ups):
    from app.operations.follow_ups import due_follow_ups

    rows = due_follow_ups(session, kigali_today(), limit=DASHBOARD_ROWS)
    assert len(rows) == DASHBOARD_ROWS


def test_the_total_is_still_the_truth(session, many_follow_ups):
    """The number in the heading is what makes a bound honest."""
    from app.operations.follow_ups import due_follow_ups

    rows = due_follow_ups(session, kigali_today(), limit=DASHBOARD_ROWS)
    assert rows[0]["total_rows"] == DASHBOARD_ROWS + 8, (
        "the count must be of everything due, not of what fitted"
    )


def test_the_bound_keeps_the_most_overdue(session, many_follow_ups):
    """Ordering plus a limit is only safe if the order puts the urgent first.

    These are seeded with the oldest last, so a limit applied before the sort
    would keep exactly the wrong ones.
    """
    from app.operations.follow_ups import due_follow_ups

    rows = due_follow_ups(session, kigali_today(), limit=5)
    days = [r["days_overdue"] for r in rows]
    assert days == sorted(days, reverse=True), days
    assert days[0] == DASHBOARD_ROWS + 8, "the oldest was dropped"


def test_no_limit_still_returns_everything(session, many_follow_ups):
    """A caller that wants the lot -- a report, an export -- still gets it."""
    from app.operations.follow_ups import due_follow_ups

    assert len(due_follow_ups(session, kigali_today())) == DASHBOARD_ROWS + 8


def test_the_dashboard_says_how_many_it_is_not_showing(web, session,
                                                       many_follow_ups):
    session.commit()
    page = web.get("/ui/").text
    assert f"Follow-ups due ({DASHBOARD_ROWS + 8})" in page
    assert "8 more are due" in page


def test_the_registry_can_be_searched(web, session, make_candidate):
    """2,000 rows with no way to find anybody is not a registry, it is a dump."""
    make_candidate(name="Uwase Distinctive")
    make_candidate(name="Someone Else")
    session.commit()

    # Scoped to the Registry card: the readiness queue above it is a different
    # list answering a different question, and is not filtered by the search.
    found = web.get("/ui/candidates?q=Distinctive").text
    registry = found[found.index("<h2>Registry"):]
    assert "Uwase Distinctive" in registry
    assert "Someone Else" not in registry


def test_an_empty_search_is_not_a_filter(web, session, make_candidate):
    """Guards the guard: a query that matched nothing on a blank q would make
    every assertion above pass against an empty page."""
    make_candidate(name="Uwase Distinctive")
    session.commit()
    page = web.get("/ui/candidates").text
    assert "Uwase Distinctive" in page[page.index("<h2>Registry"):]


def test_the_registry_bound_is_larger_than_the_dashboard_bound():
    """A list you went to a page to read may be longer than one on a summary."""
    assert REGISTRY_ROWS > DASHBOARD_ROWS


# --- what a bound does to a triage list ------------------------------------

def test_a_harassment_report_is_never_pushed_off_by_stale_pay_issues(
    session, staff_id, make_candidate
):
    """The bound is what makes this ordering safety-critical.

    While the list showed everything, ordering by deadline was fine -- a
    harassment report appeared somewhere. Capped at 25, twenty-five pay issues
    that breached weeks ago all have earlier deadlines than one raised this
    morning, and it would not be on the screen at all.
    """
    from app.operations.escalations import open_escalations, raise_escalation

    for i in range(DASHBOARD_ROWS + 5):
        eid = raise_escalation(session, "pay", candidate_id=make_candidate(),
                               detail="unpaid", owner_staff_id=staff_id)
        session.execute(
            text("UPDATE escalations SET raised_at = now() - INTERVAL '30 days', "
                 "respond_by = now() - INTERVAL '29 days' WHERE escalation_id = :e"),
            {"e": str(eid)},
        )
    raise_escalation(session, "harassment", candidate_id=make_candidate(),
                     detail="reported this morning", owner_staff_id=staff_id)

    shown = open_escalations(session, limit=DASHBOARD_ROWS)
    assert shown[0]["kind"] == "harassment", [r["kind"] for r in shown[:3]]


def test_the_ordering_matches_the_response_times_it_claims_to_mirror():
    """The SQL ranks kinds by hand. If RESPONSE_TIMES is ever re-tuned and the
    ordering is not, the list silently stops agreeing with the promise."""
    import re
    from pathlib import Path

    from app.operations.escalations import RESPONSE_TIMES

    source = Path("app/operations/escalations.py").read_text()
    block = source[source.index("ORDER BY CASE e.kind"):source.index("e.respond_by ASC")]
    ranked = re.findall(r"WHEN '(\w+)'\s+THEN (\d)", block)

    by_window = sorted(RESPONSE_TIMES, key=lambda k: RESPONSE_TIMES[k])
    for kind, rank in ranked:
        others = [k for k in by_window if RESPONSE_TIMES[k] < RESPONSE_TIMES[kind]]
        assert len(others) <= int(rank), (
            f"{kind} is ranked {rank} but {len(others)} kinds have a shorter "
            f"response window: {others}"
        )
