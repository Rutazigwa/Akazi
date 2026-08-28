"""Tomorrow's shifts, and what could stop each one happening.

Every other dashboard view reports something that has already gone wrong: an
escalation raised, pay overdue, a guarantee clock already running. Each
arrives too late to prevent the thing it describes. The guarantee is priced
into the fee, so an invocation costs real money and the cheapest one is the
one that never happens.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.operations.readiness import shifts_on, unstaffed_shifts_on



TOMORROW = kigali_today() + timedelta(days=1)


@pytest.fixture
def shift(session, employer_id, make_candidate):
    """A placement starting tomorrow, accepted, with a delivered reminder."""
    def _make(status="accepted", headcount=1, transport_rwf=500,
              pay_rwf=5000, reminder="delivered", has_smartphone=True,
              starts_on=TOMORROW):
        request_id = session.execute(
            text("INSERT INTO work_requests (employer_id, title, work_type, "
                 "headcount, starts_on, shift_start, shift_end, pay_rwf, "
                 "pay_unit) VALUES (:e, 'Morning cleaner', 'shift', :h, :d, "
                 "'08:00', '16:00', :p, 'day') RETURNING request_id"),
            {"e": employer_id, "h": headcount, "d": starts_on, "p": pay_rwf},
        ).scalar_one()
        candidate_id = make_candidate()
        session.execute(
            text("UPDATE candidates SET has_smartphone = :s "
                 "WHERE candidate_id = :c"),
            {"s": has_smartphone, "c": str(candidate_id)},
        )
        placement_id = session.execute(
            text("INSERT INTO placements (request_id, candidate_id, status, "
                 "agreed_pay_rwf, pay_unit, est_transport_rwf) "
                 "VALUES (:r, :c, CAST(:s AS placement_status), :p, 'day', :t) "
                 "RETURNING placement_id"),
            {"r": str(request_id), "c": str(candidate_id), "s": status,
             "p": pay_rwf, "t": transport_rwf},
        ).scalar_one()
        if reminder is not None:
            session.execute(
                text("INSERT INTO messages (candidate_id, placement_id, "
                     "template_key, body, status, sent_at, delivered_at) "
                     "VALUES (:c, :p, 'shift_reminder', 'see you tomorrow', "
                     "CAST(:st AS message_status), "
                     "CASE WHEN :st IN ('sent','delivered') THEN now() END, "
                     "CASE WHEN :st = 'delivered' THEN now() END)"),
                {"c": str(candidate_id), "p": str(placement_id),
                 "st": reminder},
            )
        return {"placement_id": placement_id, "request_id": request_id,
                "candidate_id": candidate_id}
    return _make


def flags_for(session, day=TOMORROW):
    rows = shifts_on(session, day)
    assert len(rows) == 1, f"expected one shift, got {len(rows)}"
    return rows[0]["flags"]


def test_a_shift_with_nothing_outstanding_carries_no_flags(session, shift):
    """Prior work with this employer is the last thing that clears."""
    made = shift()
    # A separate earlier request for the same employer: a placement is unique
    # per (request, candidate), so prior work has to sit on a different one.
    earlier = session.execute(
        text("INSERT INTO work_requests (employer_id, title, work_type, "
             "headcount, starts_on, pay_rwf, pay_unit) "
             "SELECT employer_id, 'Last week', 'shift', 1, "
             "kigali_today() - 14, 5000, 'day' FROM work_requests "
             "WHERE request_id = :r RETURNING request_id"),
        {"r": str(made["request_id"])},
    ).scalar_one()
    session.execute(
        text("INSERT INTO placements (request_id, candidate_id, status, "
             "agreed_pay_rwf, pay_unit) "
             "VALUES (:r, :c, 'completed', 5000, 'day')"),
        {"r": str(earlier), "c": str(made["candidate_id"])},
    )
    assert flags_for(session) == []


def test_an_unaccepted_offer_is_flagged(session, shift):
    shift(status="offered")
    assert any("has not accepted" in f for f in flags_for(session))


def test_a_shift_with_no_reminder_queued_is_flagged(session, shift):
    shift(reminder=None)
    assert "no reminder queued" in flags_for(session)


def test_a_failed_reminder_says_to_call_them(session, shift):
    shift(reminder="failed")
    assert any("call them" in f for f in flags_for(session))


def test_a_reminder_sent_but_unconfirmed_is_distinguished_from_delivered(
    session, shift
):
    """Accepted by the provider is not the same as reaching the handset."""
    shift(reminder="sent")
    assert any("not confirmed delivered" in f for f in flags_for(session))


def test_no_smartphone_means_whatsapp_will_not_reach_them(session, shift):
    shift(has_smartphone=False)
    assert any("no smartphone" in f for f in flags_for(session))


def test_transport_eating_the_wage_is_flagged(session, shift):
    """The blueprint's stated cause of week-two dropout."""
    shift(pay_rwf=3000, transport_rwf=1600)
    assert any("transport is" in f for f in flags_for(session))


def test_transport_is_not_flagged_when_the_employer_covers_it(session, shift):
    made = shift(pay_rwf=3000, transport_rwf=1600)
    session.execute(
        text("UPDATE work_requests SET transport_covered = TRUE "
             "WHERE request_id = :r"),
        {"r": str(made["request_id"])},
    )
    assert not any("transport is" in f for f in flags_for(session))


def test_a_first_shift_with_an_employer_is_flagged(session, shift):
    """A first day is where the arrival risk actually sits."""
    shift()
    assert "first shift with this employer" in flags_for(session)


def test_shifts_on_another_day_are_not_included(session, shift):
    shift(starts_on=TOMORROW + timedelta(days=3))
    assert shifts_on(session, TOMORROW) == []


def test_a_cancelled_placement_is_not_listed(session, shift):
    made = shift()
    session.execute(
        text("UPDATE placements SET status = 'cancelled' "
             "WHERE placement_id = :p"),
        {"p": str(made["placement_id"])},
    )
    assert shifts_on(session, TOMORROW) == []


# --- the worst case: nobody assigned at all --------------------------------

def test_a_shift_nobody_is_assigned_to_is_reported(session, employer_id):
    """The guarantee does not cover a slot that was never filled."""
    session.execute(
        text("INSERT INTO work_requests (employer_id, title, work_type, "
             "headcount, starts_on, pay_rwf, pay_unit) VALUES "
             "(:e, 'Night guard', 'shift', 2, :d, 5000, 'day')"),
        {"e": employer_id, "d": TOMORROW},
    )
    short = unstaffed_shifts_on(session, TOMORROW)
    assert len(short) == 1
    assert short[0]["short_by"] == 2


def test_a_partly_staffed_shift_reports_only_the_shortfall(session, shift):
    shift(headcount=3)
    short = unstaffed_shifts_on(session, TOMORROW)
    assert short[0]["short_by"] == 2
    assert short[0]["assigned"] == 1


def test_a_fully_staffed_shift_is_not_reported_as_short(session, shift):
    shift(headcount=1)
    assert unstaffed_shifts_on(session, TOMORROW) == []


def test_a_cancelled_assignment_makes_the_shift_short_again(session, shift):
    """The whole point: someone dropping out must reopen the gap."""
    made = shift(headcount=1)
    assert unstaffed_shifts_on(session, TOMORROW) == []
    session.execute(
        text("UPDATE placements SET status = 'cancelled' "
             "WHERE placement_id = :p"),
        {"p": str(made["placement_id"])},
    )
    assert unstaffed_shifts_on(session, TOMORROW)[0]["short_by"] == 1


# --- the page --------------------------------------------------------------

def test_the_page_lists_tomorrows_shifts(web, shift):
    shift(status="offered")
    page = web.get("/ui/tomorrow")
    assert page.status_code == 200
    assert "has not accepted" in page.text


def test_the_page_accepts_another_day(web, shift):
    shift(starts_on=TOMORROW + timedelta(days=2))
    page = web.get(f"/ui/tomorrow?day={TOMORROW + timedelta(days=2)}")
    assert "Morning cleaner" in page.text


def test_a_nonsense_date_does_not_error(web):
    page = web.get("/ui/tomorrow?day=not-a-date", follow_redirects=True)
    assert page.status_code == 200
    assert "is not a date" in page.text


def test_the_dashboard_points_at_tomorrow_when_something_is_unresolved(
    web, shift
):
    shift(status="offered")
    dashboard = web.get("/ui/")
    assert "Tomorrow needs attention" in dashboard.text
    assert "/ui/tomorrow" in dashboard.text


def test_the_dashboard_stays_quiet_when_nothing_is_outstanding(web):
    assert "Tomorrow needs attention" not in web.get("/ui/").text
