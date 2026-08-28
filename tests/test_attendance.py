"""Attendance, the reliability guarantee, and the follow-up queue.

These run against a real database. What they protect is the promise the
business is sold on: the shift gets covered, and if it doesn't, we cover it.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.operations.attendance import (
    AttendanceError,
    log_attendance,
    open_guarantees,
    record_replacement,
    start_placement,
)
from app.operations.follow_ups import (
    checkpoint_schedule,
    complete_follow_up,
    due_follow_ups,
)

TODAY = date.today()


# --- follow-up scheduling --------------------------------------------------

def test_checkpoint_offsets_are_the_blueprint_ones():
    schedule = checkpoint_schedule(date(2026, 9, 1))
    assert schedule == {
        "day_1": date(2026, 9, 2),
        "week_1": date(2026, 9, 8),
        "day_30": date(2026, 10, 1),
        "day_90": date(2026, 11, 30),
    }


def test_starting_a_placement_schedules_all_four_checkpoints(
    session, make_placement
):
    pid = make_placement()
    start_placement(session, pid, TODAY)

    rows = session.execute(
        text(
            "SELECT checkpoint::text, due_on FROM follow_ups "
            "WHERE placement_id = :pid ORDER BY due_on"
        ),
        {"pid": pid},
    ).all()
    assert [r[0] for r in rows] == ["day_1", "week_1", "day_30", "day_90"]


def test_a_placement_cannot_start_twice(session, make_placement):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    with pytest.raises(AttendanceError, match="not in a startable state"):
        start_placement(session, pid, TODAY)


def test_rescheduling_moves_pending_checkins_but_not_completed_ones(
    session, make_placement
):
    """A completed check-in is a conversation that happened; its date is history."""
    pid = make_placement()
    start_placement(session, pid, TODAY)

    day_1 = session.execute(
        text(
            "SELECT follow_up_id FROM follow_ups "
            "WHERE placement_id = :pid AND checkpoint = 'day_1'"
        ),
        {"pid": pid},
    ).scalar_one()
    complete_follow_up(session, day_1, still_working=True)

    from app.operations.follow_ups import schedule_follow_ups

    schedule_follow_ups(session, pid, TODAY + timedelta(days=5))

    rows = dict(
        session.execute(
            text(
                "SELECT checkpoint::text, due_on FROM follow_ups "
                "WHERE placement_id = :pid"
            ),
            {"pid": pid},
        ).all()
    )
    assert rows["day_1"] == TODAY + timedelta(days=1)      # untouched
    assert rows["day_30"] == TODAY + timedelta(days=35)    # moved


def test_due_queue_surfaces_the_most_overdue_first(session, make_placement):
    pid = make_placement()
    start_placement(session, pid, TODAY - timedelta(days=40))

    queue = due_follow_ups(session, as_of=TODAY)
    assert [q["checkpoint"] for q in queue] == ["day_1", "week_1", "day_30"]
    assert queue[0]["days_overdue"] == 39
    assert queue[0]["business_name"] == "Isuku Cooperative"


def test_completed_checkins_leave_the_queue(session, make_placement):
    pid = make_placement()
    start_placement(session, pid, TODAY - timedelta(days=40))
    queue = due_follow_ups(session, as_of=TODAY)
    complete_follow_up(session, queue[0]["follow_up_id"], still_working=True)
    assert len(due_follow_ups(session, as_of=TODAY)) == len(queue) - 1


# --- attendance ------------------------------------------------------------

def test_an_absence_requires_a_reason(session, make_placement):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    with pytest.raises(AttendanceError, match="needs a reason"):
        log_attendance(session, pid, TODAY, present=False, confirmed_by="employer")


def test_attendance_is_idempotent_per_day(session, make_placement):
    """A correction overwrites the day rather than creating a second record."""
    pid = make_placement()
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, True, "employer", hours_worked=8)
    log_attendance(session, pid, TODAY, True, "coordinator", hours_worked=6)

    rows = session.execute(
        text(
            "SELECT hours_worked, confirmed_by FROM attendance "
            "WHERE placement_id = :pid"
        ),
        {"pid": pid},
    ).all()
    assert len(rows) == 1
    assert rows[0][0] == 6
    assert rows[0][1] == "coordinator"


# --- the guarantee ---------------------------------------------------------

def test_a_first_day_absence_invokes_the_guarantee(session, make_placement):
    pid = make_placement()
    start_placement(session, pid, TODAY)

    invocation = log_attendance(
        session, pid, TODAY, present=False, confirmed_by="employer",
        absence_reason="did not arrive",
    )
    assert invocation is not None
    assert invocation.failed_placement_id == pid
    assert invocation.due_by - invocation.invoked_at == timedelta(hours=24)

    status = session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :pid"),
        {"pid": pid},
    ).scalar_one()
    assert status == "no_show"


def test_a_later_absence_is_not_a_no_show(session, make_placement):
    """The shift was covered on day one; the employer got what they bought."""
    pid = make_placement()
    start_placement(session, pid, TODAY - timedelta(days=3))
    log_attendance(session, pid, TODAY - timedelta(days=3), True, "employer")

    invocation = log_attendance(
        session, pid, TODAY, present=False, confirmed_by="employer",
        absence_reason="sick",
    )
    assert invocation is None
    status = session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :pid"),
        {"pid": pid},
    ).scalar_one()
    assert status == "active"


def test_present_days_never_invoke_the_guarantee(session, make_placement):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    assert log_attendance(session, pid, TODAY, True, "employer") is None


def test_a_replacement_preserves_the_chain(
    session, make_placement, make_candidate
):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer",
                   absence_reason="did not arrive")

    cover = make_candidate(name="Cover Worker")
    new_id = record_replacement(
        session, pid, cover, match_reason="matched on: availability, 8-min commute"
    )

    row = session.execute(
        text(
            "SELECT replaces_placement, agreed_pay_rwf, status::text "
            "FROM placements WHERE placement_id = :pid"
        ),
        {"pid": new_id},
    ).first()
    assert row[0] == pid
    assert row[1] == 5000        # pay terms inherited from the failed placement
    assert row[2] == "offered"


def test_the_failed_placement_stays_a_no_show(
    session, make_placement, make_candidate
):
    """Covering a no-show must not erase that it happened.

    If the original row were flipped to 'replaced', the invocation would vanish
    from v_guarantee_invocations and the reliability numbers would silently
    improve. The chain is the evidence; the status is the truth.
    """
    pid = make_placement()
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer",
                   absence_reason="did not arrive")
    record_replacement(session, pid, make_candidate(name="Cover"), "matched on: x")

    status = session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :pid"),
        {"pid": pid},
    ).scalar_one()
    assert status == "no_show"

    count = session.execute(
        text("SELECT count(*) FROM v_guarantee_invocations")
    ).scalar_one()
    assert count == 1


def test_only_no_shows_can_be_replaced(session, make_placement, make_candidate):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    with pytest.raises(AttendanceError, match="replacements cover no-shows"):
        record_replacement(session, pid, make_candidate(), "matched on: x")


def test_a_placement_can_only_be_replaced_once(
    session, make_placement, make_candidate
):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer", absence_reason="no-show")
    record_replacement(session, pid, make_candidate(name="A"), "matched on: x")

    with pytest.raises(Exception):
        record_replacement(session, pid, make_candidate(name="B"), "matched on: y")
        session.flush()


def test_open_guarantees_lists_uncovered_no_shows(session, make_placement):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer", absence_reason="no-show")

    open_ = open_guarantees(session)
    assert len(open_) == 1
    assert open_[0]["failed_placement_id"] == pid
    assert open_[0]["business_name"] == "Isuku Cooperative"
    assert open_[0]["breached"] is False


def test_a_covered_no_show_leaves_the_open_list(
    session, make_placement, make_candidate
):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer", absence_reason="no-show")
    record_replacement(session, pid, make_candidate(name="Cover"), "matched on: x")
    assert open_guarantees(session) == []


# --- correcting a mistake, and the one correction that is not one ----------

def test_a_no_show_recorded_in_error_can_be_corrected(session, make_placement):
    """It would otherwise stand forever as a guarantee invocation against us,
    and against the worker's record."""
    from datetime import date

    placement_id = make_placement()
    session.execute(
        text("UPDATE placements SET status = 'active', started_on = CURRENT_DATE "
             "WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    log_attendance(session, placement_id, date.today(), present=False,
                   confirmed_by="employer", absence_reason="did not arrive")
    assert session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one() == "no_show"

    log_attendance(session, placement_id, date.today(), present=True,
                   confirmed_by="employer")
    assert session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one() == "active"
    assert session.execute(
        text("SELECT count(*) FROM v_guarantee_invocations")
    ).scalar_one() == 0


def test_a_covered_absence_cannot_be_quietly_corrected(
    session, make_placement, make_candidate, make_request
):
    """Once somebody has been sent this is not a correction, it is a decision
    about two people who both turned up -- and one of them travelled because
    we told them to.

    Reverting quietly erased the invocation, improved the reliability figure,
    and hid a cost we actually bore. The module docstring forbade it from the
    start and the code did it anyway.
    """
    from datetime import date

    request_id = make_request()
    placement_id = make_placement(request_id=request_id,
                                  candidate_id=make_candidate())
    session.execute(
        text("UPDATE placements SET status = 'active', started_on = CURRENT_DATE "
             "WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    log_attendance(session, placement_id, date.today(), present=False,
                   confirmed_by="employer", absence_reason="did not arrive")
    record_replacement(session, placement_id, make_candidate(),
                       "cover: can be there by 09:10")

    with pytest.raises(AttendanceError, match="already covered"):
        log_attendance(session, placement_id, date.today(), present=True,
                       confirmed_by="employer")

    # The record is intact, which is the part my first attempt got wrong: the
    # insert is an upsert, so a check made after it had already flipped the
    # attendance row to present before refusing.
    assert session.execute(
        text("SELECT present FROM attendance WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one() is False
    assert session.execute(
        text("SELECT count(*) FROM v_guarantee_invocations")
    ).scalar_one() == 1
    assert session.execute(
        text("SELECT count(*) FROM placements WHERE request_id = :r"),
        {"r": str(request_id)},
    ).scalar_one() == 2


def test_the_refusal_says_what_to_do_instead(session, make_placement,
                                              make_candidate, make_request):
    """A refusal a coordinator cannot act on is just an obstacle."""
    from datetime import date

    placement_id = make_placement(request_id=make_request(),
                                  candidate_id=make_candidate())
    session.execute(
        text("UPDATE placements SET status = 'active', started_on = CURRENT_DATE "
             "WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    log_attendance(session, placement_id, date.today(), present=False,
                   confirmed_by="employer", absence_reason="did not arrive")
    record_replacement(session, placement_id, make_candidate(), "cover")

    with pytest.raises(AttendanceError) as caught:
        log_attendance(session, placement_id, date.today(), present=True,
                       confirmed_by="employer")
    message = str(caught.value)
    assert "Cancel or end the cover placement first" in message
    assert "owed for turning up" in message
