"""Queueing messages from domain events.

Kept in one module so it is possible to answer "what does this system send, and
when?" by reading a single file. Each function is called from inside the
transaction that caused it, so a rolled-back offer takes its message with it.

Nothing here sends. Failures to queue must never break the operation: a message
is how we tell someone about a placement, not the placement itself.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.messaging.outbox import KIGALI, cancel_for_placement, queue
from app.messaging.templates import render, shift_window, transport_line

logger = logging.getLogger("akazi.messaging")

# Reminders go out the evening before, at 18:00 Kigali -- late enough that the
# day is settled, early enough to arrange transport or tell us they cannot go.
REMINDER_HOUR = time(18, 0)


def _placement_context(session: Session, placement_id: UUID) -> dict | None:
    row = session.execute(
        text(
            """
            SELECT p.placement_id, p.candidate_id, p.agreed_pay_rwf, p.pay_unit,
                   p.est_transport_rwf, c.display_name,
                   wr.title, wr.starts_on, wr.shift_start, wr.shift_end,
                   wr.transport_covered, e.business_name, e.employer_id,
                   (SELECT contact_id FROM employer_contacts ec
                     WHERE ec.employer_id = e.employer_id AND ec.is_primary
                     LIMIT 1) AS primary_contact_id
              FROM placements p
              JOIN candidates c     ON c.candidate_id = p.candidate_id
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
             WHERE p.placement_id = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()
    return dict(row) if row else None


def on_placement_offered(session: Session, placement_id: UUID) -> None:
    """Tell the candidate what the work pays before they accept.

    Pay shown transparently before acceptance is a legal requirement, and the
    figure quoted is net of the estimated fare -- gross pay is not what someone
    takes home, and the gap is what kills a placement in week two.
    """
    ctx = _placement_context(session, placement_id)
    if ctx is None:
        return

    body = render(
        "placement_offer",
        business_name=ctx["business_name"],
        title=ctx["title"],
        starts_on=f"{ctx['starts_on']:%d %b}",
        shift=shift_window(ctx["shift_start"], ctx["shift_end"]),
        pay_rwf=f"{ctx['agreed_pay_rwf']:,}",
        pay_unit=ctx["pay_unit"],
        transport_line=transport_line(
            ctx["agreed_pay_rwf"],
            ctx["est_transport_rwf"] or 0,
            ctx["transport_covered"],
        ),
    )
    queue(
        session, template_key="placement_offer", body=body,
        candidate_id=ctx["candidate_id"], placement_id=placement_id,
    )

    if ctx["primary_contact_id"]:
        queue(
            session,
            template_key="employer_worker_assigned",
            body=render(
                "employer_worker_assigned",
                display_name=ctx["display_name"],
                title=ctx["title"],
                starts_on=f"{ctx['starts_on']:%d %b}",
                shift=shift_window(ctx["shift_start"], ctx["shift_end"]),
            ),
            contact_id=ctx["primary_contact_id"],
            placement_id=placement_id,
        )


def on_placement_started(
    session: Session, placement_id: UUID, started_on: date
) -> None:
    """Queue the reminder for the evening before the first day.

    Skipped when the shift starts today or has already started -- a reminder
    for something happening now is noise, and one for yesterday is worse.
    """
    ctx = _placement_context(session, placement_id)
    if ctx is None:
        return

    when = datetime.combine(
        started_on - timedelta(days=1), REMINDER_HOUR, tzinfo=KIGALI
    ).astimezone(timezone.utc)
    if when <= datetime.now(timezone.utc):
        return

    queue(
        session,
        template_key="shift_reminder",
        body=render(
            "shift_reminder",
            title=ctx["title"],
            business_name=ctx["business_name"],
            shift=shift_window(ctx["shift_start"], ctx["shift_end"]),
        ),
        candidate_id=ctx["candidate_id"],
        placement_id=placement_id,
        scheduled_for=when,
    )


def on_follow_ups_scheduled(
    session: Session, placement_id: UUID, schedule: dict[str, date]
) -> None:
    """A nudge on each checkpoint date.

    The coordinator still makes the call -- this is what makes the call land,
    and 30-day retention is only knowable if someone actually answers.
    """
    ctx = _placement_context(session, placement_id)
    if ctx is None:
        return

    for checkpoint, due_on in schedule.items():
        key = f"followup_{checkpoint}"
        when = datetime.combine(due_on, time(9, 0), tzinfo=KIGALI).astimezone(
            timezone.utc
        )
        queue(
            session,
            template_key=key,
            body=render(key, business_name=ctx["business_name"]),
            candidate_id=ctx["candidate_id"],
            placement_id=placement_id,
            scheduled_for=when,
        )


def on_replacement_sent(
    session: Session,
    failed_placement_id: UUID,
    replacement_placement_id: UUID,
    fill_by: datetime,
) -> None:
    """Tell the employer their shift is covered, and by whom."""
    failed = _placement_context(session, failed_placement_id)
    cover = _placement_context(session, replacement_placement_id)
    if failed is None or cover is None or not failed["primary_contact_id"]:
        return

    queue(
        session,
        template_key="employer_cover_sent",
        body=render(
            "employer_cover_sent",
            display_name=failed["display_name"],
            cover_name=cover["display_name"],
            fill_by=f"{fill_by.astimezone(KIGALI):%H:%M on %d %b}",
        ),
        contact_id=failed["primary_contact_id"],
        placement_id=replacement_placement_id,
    )


def on_contract_issued(
    session: Session, placement_id: UUID, contract: dict
) -> None:
    """Send the worker their copy of what was agreed.

    A contract only the operator holds is not much of a protection. Sent as a
    message so it is on their phone rather than in our database.
    """
    from app.operations.contracts import render_contract

    ctx = _placement_context(session, placement_id)
    if ctx is None:
        return

    queue(
        session,
        template_key="placement_contract",
        body=render(
            "placement_contract",
            contract=render_contract(contract["terms"], contract["contract_ref"]),
            contract_ref=contract["contract_ref"],
        ),
        candidate_id=ctx["candidate_id"],
        placement_id=placement_id,
    )


def on_placement_cancelled(
    session: Session, placement_id: UUID, reason: str
) -> None:
    """Tell a worker their shift is off, and that it was not their doing.

    Worth saying explicitly: someone who accepted work and then hears nothing
    reasonably assumes they were dropped, and the next offer is harder to fill.
    """
    ctx = _placement_context(session, placement_id)
    if ctx is None:
        return

    queue(
        session,
        template_key="placement_cancelled",
        body=render(
            "placement_cancelled",
            title=ctx["title"],
            business_name=ctx["business_name"],
            starts_on=f"{ctx['starts_on']:%d %b}",
        ),
        candidate_id=ctx["candidate_id"],
        placement_id=placement_id,
    )


def on_placement_ended(
    session: Session, placement_id: UUID, reason: str
) -> None:
    """Cancel anything still queued.

    A shift reminder for a placement that was declined, replaced or terminated
    sends someone to a job that is not theirs -- worse than sending nothing.
    """
    cancelled = cancel_for_placement(session, placement_id, reason)
    if cancelled:
        logger.info(
            "cancelled %s queued message(s) for placement %s: %s",
            cancelled, placement_id, reason,
        )
