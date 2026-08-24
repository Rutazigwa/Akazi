"""Follow-up scheduling.

The checkpoints are not reminders, they are the measurement instrument. 30-day
retention is a headline pilot metric and it is only knowable if someone actually
asked the worker at day 30 -- so the schedule is created automatically when a
placement starts rather than left to a coordinator's memory.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.operations.escalations import ALWAYS_ESCALATE

# Days after the start date at which each check-in falls due.
CHECKPOINT_OFFSETS: dict[str, int] = {
    "day_1": 1,
    "week_1": 7,
    "day_30": 30,
    "day_90": 90,
}


def checkpoint_schedule(started_on: date) -> dict[str, date]:
    """Due dates for every checkpoint, given a start date."""
    return {
        checkpoint: started_on + timedelta(days=offset)
        for checkpoint, offset in CHECKPOINT_OFFSETS.items()
    }


def schedule_follow_ups(
    session: Session, placement_id: UUID, started_on: date
) -> dict[str, date]:
    """Create the four check-ins for a placement.

    Idempotent: re-running after a start date correction moves the due dates of
    check-ins that have not happened yet, and leaves completed ones alone. A
    completed check-in is a record of a conversation that took place; its due
    date is history, not a plan.
    """
    schedule = checkpoint_schedule(started_on)
    for checkpoint, due_on in schedule.items():
        session.execute(
            text(
                """
                INSERT INTO follow_ups (placement_id, checkpoint, due_on)
                VALUES (:pid, CAST(:cp AS checkpoint), :due)
                ON CONFLICT (placement_id, checkpoint) DO UPDATE
                    SET due_on = EXCLUDED.due_on
                    WHERE follow_ups.completed_at IS NULL
                """
            ),
            {"pid": str(placement_id), "cp": checkpoint, "due": due_on},
        )
    return schedule


def due_follow_ups(session: Session, as_of: date) -> list[dict]:
    """The coordinator's work queue, most overdue first.

    Ordered by due date rather than by placement so the oldest unanswered
    check-in surfaces first -- an overdue day_30 is a retention number we are
    about to lose.
    """
    rows = session.execute(
        text(
            """
            SELECT f.follow_up_id,
                   f.placement_id,
                   f.checkpoint::text AS checkpoint,
                   f.due_on,
                   (CAST(:as_of AS date) - f.due_on) AS days_overdue,
                   c.display_name,
                   e.business_name
            FROM follow_ups f
            JOIN placements p     ON p.placement_id = f.placement_id
            JOIN candidates c     ON c.candidate_id = p.candidate_id
            JOIN work_requests wr ON wr.request_id = p.request_id
            JOIN employers e      ON e.employer_id = wr.employer_id
            WHERE f.completed_at IS NULL
              AND f.due_on <= :as_of
            ORDER BY f.due_on ASC
            """
        ),
        {"as_of": as_of},
    ).mappings()
    return [dict(r) for r in rows]


def complete_follow_up(
    session: Session,
    follow_up_id: UUID,
    still_working: bool,
    worker_rating: int | None = None,
    employer_rating: int | None = None,
    issue_flag: str | None = None,
    notes: str | None = None,
) -> None:
    """Record the outcome of a check-in.

    `issue_flag='harassment'` is not just a label: it is the trigger for the
    named escalation path, and it is deliberately a constrained value rather
    than free text so that it can be counted and responded to.
    """
    placement_id = session.execute(
        text("SELECT placement_id FROM follow_ups WHERE follow_up_id = :fid"),
        {"fid": str(follow_up_id)},
    ).scalar_one_or_none()

    session.execute(
        text(
            """
            UPDATE follow_ups
               SET completed_at    = now(),
                   still_working   = :still_working,
                   worker_rating   = :worker_rating,
                   employer_rating = :employer_rating,
                   issue_flag      = :issue_flag,
                   notes           = :notes
             WHERE follow_up_id = :fid
            """
        ),
        {
            "fid": str(follow_up_id),
            "still_working": still_working,
            "worker_rating": worker_rating,
            "employer_rating": employer_rating,
            "issue_flag": issue_flag,
            "notes": notes,
        },
    )

    # A flag raised on a call is the same report as one sent by text. Routing
    # only the texted ones would mean the safeguard depends on how someone
    # happened to tell us.
    if issue_flag in ALWAYS_ESCALATE and placement_id is not None:
        from app.operations.escalations import (
            EscalationError,
            raise_escalation,
        )

        candidate_id = session.execute(
            text("SELECT candidate_id FROM placements WHERE placement_id = :pid"),
            {"pid": str(placement_id)},
        ).scalar_one()
        try:
            raise_escalation(
                session,
                issue_flag,
                candidate_id=candidate_id,
                placement_id=placement_id,
                follow_up_id=follow_up_id,
                detail=notes,
            )
        except EscalationError:
            # Re-raised: a harassment report that silently fails to escalate
            # is the worst outcome in this system.
            raise
