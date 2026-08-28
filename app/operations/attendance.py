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


def refresh_candidate_status(session: Session, candidate_id: UUID) -> str | None:
    """Recompute a candidate's status from the placements they actually hold.

    Derived rather than assigned, so it cannot drift: any code path that
    changes a placement calls this and the status follows. It never touches
    'withdrawn' or 'inactive', which are decisions about the person rather
    than summaries of their work.

    Note that status is a display and reporting convenience -- matching
    excludes double-booking by checking overlapping placements directly,
    because a summary that goes stale would silently reopen that hole.
    """
    return session.execute(
        text(
            """
            UPDATE candidates c
               SET status = CASE
                       WHEN EXISTS (SELECT 1 FROM placements p
                                     WHERE p.candidate_id = c.candidate_id
                                       AND p.status IN ('accepted','active'))
                           THEN 'placed'::candidate_status
                       -- Finishing a cohort outranks having been assessed:
                       -- it is the further step, and it is what 'trained' has
                       -- been reserved for since the first migration.
                       WHEN EXISTS (SELECT 1 FROM cohort_members cm
                                     WHERE cm.candidate_id = c.candidate_id
                                       AND cm.outcome = 'completed')
                           THEN 'trained'::candidate_status
                       WHEN EXISTS (SELECT 1 FROM assessment_results ar
                                     WHERE ar.candidate_id = c.candidate_id)
                           THEN 'assessed'::candidate_status
                       ELSE 'registered'::candidate_status END
             WHERE c.candidate_id = :cid
               AND c.status NOT IN ('withdrawn','inactive')
            RETURNING c.status::text
            """
        ),
        {"cid": str(candidate_id)},
    ).scalar_one_or_none()


def _candidate_of(session: Session, placement_id: UUID) -> UUID | None:
    return session.execute(
        text("SELECT candidate_id FROM placements WHERE placement_id = :pid"),
        {"pid": str(placement_id)},
    ).scalar_one_or_none()


def complete_placement(
    session: Session, placement_id: UUID, ended_on: date | None = None
) -> None:
    """The work finished as agreed. Frees the candidate for new matches."""
    updated = session.execute(
        text(
            """
            UPDATE placements
               SET status = 'completed',
                   -- Never before it started: a placement can be active with a
                   -- future start date (the shift reminder depends on that),
                   -- and chk_placement_dates rightly refuses the inversion.
                   ended_on = fn_placement_end_date(:ended_on, started_on)
             WHERE placement_id = :pid AND status = 'active'
            RETURNING candidate_id
            """
        ),
        {"pid": str(placement_id), "ended_on": ended_on},
    ).scalar_one_or_none()
    if updated is None:
        raise AttendanceError("only an active placement can be completed")

    refresh_candidate_status(session, updated)
    from app.messaging.events import on_placement_ended

    on_placement_ended(session, placement_id, "placement completed")


def terminate_placement(
    session: Session, placement_id: UUID, reason: str,
    ended_on: date | None = None,
) -> None:
    """The work ended early. A reason is required -- an unexplained termination
    is indistinguishable from a dropout, and the two mean opposite things for
    retention."""
    if not reason.strip():
        raise AttendanceError("a termination must say why")

    updated = session.execute(
        text(
            """
            UPDATE placements
               SET status = 'terminated',
                   ended_on = fn_placement_end_date(:ended_on, started_on)
             WHERE placement_id = :pid AND status IN ('accepted','active')
            RETURNING candidate_id
            """
        ),
        {"pid": str(placement_id), "ended_on": ended_on},
    ).scalar_one_or_none()
    if updated is None:
        raise AttendanceError("only an accepted or active placement can be ended")

    session.execute(
        text(
            "UPDATE follow_ups SET notes = COALESCE(notes || ' | ', '') || :reason "
            "WHERE placement_id = :pid AND completed_at IS NULL"
        ),
        {"pid": str(placement_id), "reason": f"terminated: {reason}"},
    )
    refresh_candidate_status(session, updated)
    from app.messaging.events import on_placement_ended

    on_placement_ended(session, placement_id, f"terminated: {reason}")


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

    schedule = schedule_follow_ups(session, placement_id, started_on)
    candidate_id = _candidate_of(session, placement_id)
    if candidate_id:
        refresh_candidate_status(session, candidate_id)

    from app.messaging.events import (
        on_follow_ups_scheduled,
        on_placement_started,
    )

    on_placement_started(session, placement_id, started_on)
    on_follow_ups_scheduled(session, placement_id, schedule)
    return schedule


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
    # Attendance only makes sense against work that is on. Without this a
    # cancelled or completed placement could be flipped to 'no_show' by a
    # stray log, which would invent a guarantee invocation for a shift that
    # was not happening -- inflating the failure rate and sending a cover to
    # an employer who cancelled.
    status = session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :pid"),
        {"pid": str(placement_id)},
    ).scalar_one_or_none()
    if status is None:
        raise AttendanceError("no such placement")
    if status not in ("accepted", "active", "no_show"):
        raise AttendanceError(
            f"attendance cannot be recorded against a {status} placement"
        )

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
        # They did arrive after all. A no-show recorded in error would
        # otherwise stand forever as a guarantee invocation against us, and
        # against the worker's record.
        if status == "no_show":
            session.execute(
                text(
                    "UPDATE placements SET status = 'active' "
                    "WHERE placement_id = :pid"
                ),
                {"pid": str(placement_id)},
            )
            refresh_candidate_status(session, _candidate_of(session, placement_id))
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

    # A no-show frees them again: they are not working, whatever the offer said.
    refresh_candidate_status(session, _candidate_of(session, placement_id))

    # Nothing further should go to someone who did not take up the work.
    from app.messaging.events import on_placement_ended

    on_placement_ended(session, placement_id, "no-show")

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

    from app.messaging.events import on_placement_offered, on_replacement_sent

    on_placement_offered(session, new_id)

    invoked_at = session.execute(
        text(
            "SELECT min(confirmed_at) FROM attendance "
            "WHERE placement_id = :pid AND NOT present"
        ),
        {"pid": str(failed_placement_id)},
    ).scalar_one_or_none()
    if invoked_at is not None:
        on_replacement_sent(
            session, failed_placement_id, new_id, invoked_at + GUARANTEE_WINDOW
        )

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


# A shift finishing at 18:00 is not confirmable at 18:01. One clear day gives
# the employer a working morning to answer before anybody is chased.
SILENT_AFTER_DAYS = 2

# Long enough that it is no longer a slow reply. At this point the guarantee
# window has closed unnoticed: if the worker did not arrive, we owed cover and
# never knew.
SILENT_TOO_LONG_DAYS = 5


def unconfirmed_attendance(session: Session) -> list[dict]:
    """Placements nobody has confirmed, longest silence first.

    Silence is not success. An unrecorded no-show is a guarantee we never knew
    we owed, and the employer finds out we were not watching when they decline
    to reorder.
    """
    rows = session.execute(
        text(
            """
            SELECT * FROM v_unconfirmed_attendance
             WHERE days_silent >= :days
             ORDER BY days_silent DESC
            """
        ),
        {"days": SILENT_AFTER_DAYS},
    ).mappings()

    out = []
    for row in rows:
        record = dict(row)
        record["never_confirmed"] = record["records"] == 0
        record["urgent"] = record["days_silent"] >= SILENT_TOO_LONG_DAYS
        record["summary"] = (
            f"nothing recorded since it started {record['days_silent']} days ago"
            if record["never_confirmed"]
            else f"last confirmed {record['last_confirmed_on']}, "
                 f"{record['days_silent']} days ago"
        )
        out.append(record)
    return out


def chase_unconfirmed_attendance(session: Session) -> dict:
    """Ask the employer whether the worker turned up.

    Sent to the employer rather than raised internally, because they are the
    only ones who know. One message per placement per day of silence would be
    nagging, so it goes once and then the placement stays on the coordinator's
    list.
    """
    from app.messaging.outbox import queue
    from app.messaging.templates import render

    asked = 0
    for row in unconfirmed_attendance(session):
        contact_id = session.execute(
            # The primary contact where there is one. employer_contacts has
            # no created_at, so the tie-break is the id rather than an
            # invented ordering.
            text("SELECT contact_id FROM employer_contacts "
                 "WHERE employer_id = :eid AND is_active "
                 "ORDER BY is_primary DESC, contact_id LIMIT 1"),
            {"eid": str(row["employer_id"])},
        ).scalar_one_or_none()
        if contact_id is None:
            continue

        queued = queue(
            session,
            template_key="attendance_unconfirmed",
            body=render(
                "attendance_unconfirmed",
                display_name=row["display_name"],
                title=row["title"],
                started_on=row["started_on"],
            ),
            contact_id=contact_id,
            placement_id=row["placement_id"],
        )
        if queued is not None:
            asked += 1
    return {"asked": asked}
