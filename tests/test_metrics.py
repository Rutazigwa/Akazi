"""The pilot scorecard, computed from operational records.

Each metric here is one the blueprint commits to publicly. A metric that can be
set independently of the events it describes is a metric that will eventually
be wrong, so all of these derive from the underlying rows.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text

from app.operations.attendance import (
    log_attendance,
    record_replacement,
    start_placement,
)
from app.operations.follow_ups import complete_follow_up

TODAY = date.today()


def scorecard(session) -> dict:
    return dict(
        session.execute(text("SELECT * FROM v_pilot_scorecard")).mappings().one()
    )


def test_net_pay_view_flags_a_transport_heavy_placement(session, make_placement):
    """RWF 3,000/day against RWF 1,600 fare: the blueprint's cautionary case."""
    make_placement(pay_rwf=3000, transport_rwf=1600)
    row = session.execute(
        text("SELECT net_daily_rwf, transport_pct FROM v_placement_net_pay")
    ).one()
    assert row.net_daily_rwf == 1400
    assert float(row.transport_pct) == 53.3


def test_guarantee_fill_rate_counts_only_fills_inside_24h(
    session, make_placement, make_candidate
):
    covered = make_placement()
    start_placement(session, covered, TODAY)
    log_attendance(session, covered, TODAY, False, "employer",
                   absence_reason="no-show")
    record_replacement(session, covered, make_candidate(name="Cover"), "matched")

    uncovered = make_placement()
    start_placement(session, uncovered, TODAY)
    log_attendance(session, uncovered, TODAY, False, "employer",
                   absence_reason="no-show")

    card = scorecard(session)
    assert card["guarantee_invocations"] == 2
    assert float(card["guarantee_filled_24h_pct"]) == 50.0


def test_a_late_fill_counts_as_a_breach(
    session, make_placement, make_candidate
):
    pid = make_placement()
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer",
                   absence_reason="no-show")
    record_replacement(session, pid, make_candidate(name="Late cover"), "matched")

    # Backdate the invocation so the fill lands outside the 24-hour window.
    session.execute(
        text(
            "UPDATE attendance SET confirmed_at = now() - INTERVAL '30 hours' "
            "WHERE placement_id = :pid"
        ),
        {"pid": pid},
    )
    row = session.execute(
        text(
            "SELECT filled_within_24h, hours_to_fill "
            "FROM v_guarantee_invocations"
        )
    ).one()
    assert row.filled_within_24h is False
    assert float(row.hours_to_fill) >= 29


def test_retention_counts_only_answered_day_30_checkins(
    session, make_placement
):
    stayed = make_placement()
    start_placement(session, stayed, TODAY - timedelta(days=31))
    left = make_placement()
    start_placement(session, left, TODAY - timedelta(days=31))
    unanswered = make_placement()
    start_placement(session, unanswered, TODAY - timedelta(days=31))

    for pid, still in ((stayed, True), (left, False)):
        fid = session.execute(
            text(
                "SELECT follow_up_id FROM follow_ups "
                "WHERE placement_id = :pid AND checkpoint = 'day_30'"
            ),
            {"pid": pid},
        ).scalar_one()
        complete_follow_up(session, fid, still_working=still)

    # Two answered (one yes, one no) = 50%. The unanswered one is missing data,
    # not a failure, and must not drag the number down.
    assert float(scorecard(session)["retention_30day_pct"]) == 50.0


def test_pay_accuracy_requires_both_in_full_and_on_time(
    session, make_placement
):
    pid = make_placement()
    start_placement(session, pid, TODAY)

    rows = [
        # on time, confirmed by the worker -> accurate
        (TODAY, TODAY, True),
        # paid late -> not accurate
        (TODAY, TODAY + timedelta(days=3), True),
        # on time but the worker never confirmed receipt -> not accurate
        (TODAY, TODAY, False),
    ]
    for i, (due, paid, confirmed) in enumerate(rows):
        session.execute(
            text(
                """
                INSERT INTO pay_records (placement_id, period_start, period_end,
                                         gross_rwf, due_on, paid_on,
                                         worker_confirmed, method)
                VALUES (:pid, :start, :end, 5000, :due, :paid, :confirmed, 'momo')
                """
            ),
            {
                "pid": pid,
                "start": TODAY - timedelta(days=7 * (i + 1)),
                "end": TODAY - timedelta(days=7 * i),
                "due": due,
                "paid": paid,
                "confirmed": confirmed,
            },
        )

    assert float(scorecard(session)["pay_accuracy_pct"]) == 33.3


def test_women_placed_counts_active_and_completed_placements(
    session, make_placement, make_candidate
):
    for gender in ("F", "F", "F", "M"):
        pid = make_placement(candidate_id=make_candidate(gender=gender))
        start_placement(session, pid, TODAY)

    assert float(scorecard(session)["women_placed_pct"]) == 75.0


def test_time_to_fill_ignores_replacements(
    session, make_placement, make_candidate, make_request
):
    """A replacement is coverage, not a first fill; counting it would flatter us."""
    rid = make_request()
    pid = make_placement(request_id=rid)
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer", absence_reason="no-show")
    record_replacement(session, pid, make_candidate(name="Cover"), "matched")

    rows = session.execute(text("SELECT * FROM v_time_to_fill")).all()
    assert len(rows) == 1


def test_an_empty_scorecard_does_not_divide_by_zero(session):
    card = scorecard(session)
    assert card["active_employers"] == 0
    assert card["guarantee_invocations"] == 0
    assert card["retention_30day_pct"] is None
