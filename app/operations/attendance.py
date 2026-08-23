"""Attendance logging and the reliability guarantee.

This is the product. Everything else in the system is setup: the guarantee is
either honoured here or it is not, and the record left behind is the only
evidence an employer will accept.

Two status values look interchangeable and are not:

    no_show   The worker did not arrive. This is what happened, and it stays
              on the placement permanently. Coverage is proved by a separate
              placement row pointing back at it, not by editing this one.
    replaced  The placement ended early and someone else took over for a
              reason other than a no-show.

Overwriting a no_show once it has been covered would erase the invocation from
v_guarantee_invocations and quietly inflate the reliability numbers. Don't.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.operations.follow_ups import schedule_follow_ups

# The promise made to the employer: a no-show is covered same day.
GUARANTEE_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class GuaranteeInvocation:
    """Raised when a no-show puts the guarantee on the clock."""

    failed_placement_id: UUID
    request_id: UUID
    invoked_at: datetime

    @property
    def due_by(self) -> datetime:
        return self.invoked_at + GUARANTEE_WINDOW


class AttendanceError(Exception):
    pass


def start_placement(
    session: Session, placement_id: UUID, started_on: date
) -> dict[str, date]:
    """Mark a placement active and lay down its check-in schedule."""
    result = session.execute(
        text(
            """
            UPDATE placements
               SET status = 'active', started_on = :started_on
             WHERE placement_id = :pid
               AND status IN ('offered','accepted')
            RETURNING placement_id
            """
        ),
        {"pid": str(placement_id), "started_on": started_on},
    ).first()
    if result is None:
        raise AttendanceError(
            f"placement {placement_id} is not in a startable state "
            f"(expected 'offered' or 'accepted')"
        )
    return schedule_follow_ups(session, placement_id, started_on)


def log_attendance(
    session: Session,
    placement_id: UUID,
    work_date: date,
    present: bool,
    confirmed_by: str,
    hours_worked: float | None = None,
    absence_reason: str | None = None,
) -> GuaranteeInvocation | None:
    """Record one working day.

    Returns a GuaranteeInvocation when this absence is the worker failing to
    turn up at all -- that is, the first attendance record on the placement.
    A later absence on a placement that has already run is an absence, not a
    no-show: the shift was covered on day one and the employer got what they
    bought.
    """
    if not present and not absence_reason:
        raise AttendanceError(
            "an absence needs a reason -- it is the input to the follow-up "
            "conversation and to the transport/pay diagnosis"
        )

    row = session.execute(
        text(
            """
            INSERT INTO attendance (placement_id, work_date, present,
                                    hours_worked, confirmed_by, absence_reason)
            VALUES (:pid, :work_date, :present, :hours, :by, :reason)
            ON CONFLICT (placement_id, work_date) DO UPDATE
                SET present        = EXCLUDED.present,
                    hours_worked   = EXCLUDED.hours_worked,
                    confirmed_by   = EXCLUDED.confirmed_by,
                    absence_reason = EXCLUDED.absence_reason,
                    confirmed_at   = now()
            RETURNING confirmed_at
            """
        ),
        {
            "pid": str(placement_id),
            "work_date": work_date,
            "present": present,
            "hours": hours_worked,
            "by": confirmed_by,
            "reason": absence_reason,
        },
    ).first()
    confirmed_at = row[0]

    if present:
        return None

    # Was this the worker's first scheduled day? Count days actually attended:
    # a placement whose only records are absences never started.
    attended = session.execute(
        text(
            """
            SELECT count(*) FROM attendance
             WHERE placement_id = :pid AND present
            """
        ),
        {"pid": str(placement_id)},
    ).scalar_one()

    if attended > 0:
        return None

    request_id = session.execute(
        text(
            """
            UPDATE placements
               SET status = 'no_show'
             WHERE placement_id = :pid
            RETURNING request_id
            """
        ),
        {"pid": str(placement_id)},
    ).scalar_one()

    return GuaranteeInvocation(
        failed_placement_id=placement_id,
        request_id=request_id,
        invoked_at=confirmed_at,
    )


def record_replacement(
    session: Session,
    failed_placement_id: UUID,
    candidate_id: UUID,
    match_reason: str,
    agreed_pay_rwf: int | None = None,
    est_transport_rwf: int = 0,
    est_commute_min: int | None = None,
) -> UUID:
    """Cover a no-show with another worker, preserving the chain.

    Pay terms default to those of the placement being replaced: the employer
    bought a covered shift at an agreed rate, and the replacement worker is
    owed the same. Passing agreed_pay_rwf overrides that deliberately.
    """
    failed = session.execute(
        text(
            """
            SELECT request_id, agreed_pay_rwf, pay_unit, status
              FROM placements
             WHERE placement_id = :pid
            """
        ),
        {"pid": str(failed_placement_id)},
    ).mappings().first()

    if failed is None:
        raise AttendanceError(f"placement {failed_placement_id} does not exist")
    if failed["status"] != "no_show":
        raise AttendanceError(
            f"placement {failed_placement_id} has status "
            f"{failed['status']!r}; replacements cover no-shows"
        )

    new_id = session.execute(
        text(
            """
            INSERT INTO placements (request_id, candidate_id, status,
                                    agreed_pay_rwf, pay_unit,
                                    est_transport_rwf, est_commute_min,
                                    match_reason, replaces_placement)
            VALUES (:request_id, :candidate_id, 'offered',
                    :pay, :pay_unit, :transport, :commute,
                    :reason, :replaces)
            RETURNING placement_id
            """
        ),
        {
            "request_id": failed["request_id"],
            "candidate_id": str(candidate_id),
            "pay": agreed_pay_rwf or failed["agreed_pay_rwf"],
            "pay_unit": failed["pay_unit"],
            "transport": est_transport_rwf,
            "commute": est_commute_min,
            "reason": match_reason,
            "replaces": str(failed_placement_id),
        },
    ).scalar_one()
    return new_id


def open_guarantees(session: Session) -> list[dict]:
    """No-shows still without a replacement, most urgent first."""
    rows = session.execute(
        text(
            """
            SELECT g.failed_placement_id,
                   g.request_id,
                   g.invoked_at,
                   g.invoked_at + INTERVAL '24 hours' AS due_by,
                   (now() > g.invoked_at + INTERVAL '24 hours') AS breached,
                   e.business_name,
                   wr.title
            FROM v_guarantee_invocations g
            JOIN work_requests wr ON wr.request_id = g.request_id
            JOIN employers e      ON e.employer_id = wr.employer_id
            WHERE g.replacement_placement_id IS NULL
            ORDER BY g.invoked_at ASC
            """
        )
    ).mappings()
    return [dict(r) for r in rows]
