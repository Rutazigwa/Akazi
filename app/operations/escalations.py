"""Escalations: a named owner and a defined response time.

The blueprint asks for an in-app harassment report with a named escalation path
and a defined response time. Both halves matter. A flag in a database that
nobody is accountable for answering is a reporting line, not a safeguard, and a
worker who reports harassment and hears nothing has learned something about us
that no dashboard will undo.

So an escalation is raised with an owner (a person, recorded now, so the
question "who was supposed to deal with this" has an answer months later even
if the rota changed) and a deadline derived from what was reported.

The response times below are the operator's commitment. Change them only as a
deliberate decision -- shortening them is easy to type and hard to honour, and
a target that is quietly missed is worse than a longer one that is met.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# How long we have to acknowledge, by kind. Harassment and safety are same-day
# and short: the person may still be at the site.
RESPONSE_TIMES: dict[str, timedelta] = {
    "harassment": timedelta(hours=2),
    "safety": timedelta(hours=4),
    "pay": timedelta(hours=24),
    "transport": timedelta(hours=48),
    "hours": timedelta(hours=48),
    "other": timedelta(hours=48),
}

# issue_flag values that must always raise an escalation rather than sit in a
# follow-up note.
ALWAYS_ESCALATE = {"harassment", "safety"}


class EscalationError(Exception):
    pass


def default_owner(session: Session, kind: str) -> UUID | None:
    """Who owns an escalation of this kind when nobody is named explicitly.

    Harassment goes to the owner of the business, not to whoever happens to be
    on shift: it is the one report where the person receiving it may need to end
    a commercial relationship, and a coordinator cannot be asked to make that
    call about an employer they manage day to day.

    Returns None when there is nobody -- the caller must fail loudly rather than
    raise an unowned escalation.
    """
    preferred = ("owner",) if kind in ALWAYS_ESCALATE else ("supervisor", "owner")
    for role in preferred:
        found = session.execute(
            text(
                "SELECT staff_id FROM staff "
                "WHERE role = CAST(:role AS staff_role) AND is_active "
                "ORDER BY created_at LIMIT 1"
            ),
            {"role": role},
        ).scalar_one_or_none()
        if found:
            return found
    return session.execute(
        text("SELECT staff_id FROM staff WHERE is_active ORDER BY created_at LIMIT 1")
    ).scalar_one_or_none()


def raise_escalation(
    session: Session,
    kind: str,
    *,
    candidate_id: UUID | None = None,
    placement_id: UUID | None = None,
    inbound_id: UUID | None = None,
    follow_up_id: UUID | None = None,
    detail: str | None = None,
    owner_staff_id: UUID | None = None,
) -> UUID:
    if kind not in RESPONSE_TIMES:
        raise EscalationError(f"unknown escalation kind {kind!r}")

    owner = owner_staff_id or default_owner(session, kind)
    if owner is None:
        # Refusing is the right failure. An escalation with no owner is a
        # record that looks like a safeguard and is not one.
        raise EscalationError(
            "no active staff member to own this escalation -- refusing to "
            "raise one nobody is accountable for"
        )

    window = RESPONSE_TIMES[kind]
    return session.execute(
        text(
            """
            INSERT INTO escalations (kind, candidate_id, placement_id,
                                     inbound_id, follow_up_id, owner_staff_id,
                                     respond_by, detail)
            VALUES (CAST(:kind AS escalation_kind), :candidate_id, :placement_id,
                    :inbound_id, :follow_up_id, :owner,
                    clock_timestamp() + CAST(:window AS interval), :detail)
            RETURNING escalation_id
            """
        ),
        {
            "kind": kind,
            "candidate_id": str(candidate_id) if candidate_id else None,
            "placement_id": str(placement_id) if placement_id else None,
            "inbound_id": str(inbound_id) if inbound_id else None,
            "follow_up_id": str(follow_up_id) if follow_up_id else None,
            "owner": str(owner),
            "window": f"{int(window.total_seconds())} seconds",
            "detail": detail,
        },
    ).scalar_one()


def acknowledge(session: Session, escalation_id: UUID, staff_id: UUID) -> None:
    """Someone has picked this up. Stops the response clock, not the work."""
    updated = session.execute(
        text(
            """
            UPDATE escalations
               SET status = 'acknowledged', acknowledged_at = clock_timestamp(),
                   acknowledged_by = :staff
             WHERE escalation_id = :eid AND status = 'open'
            RETURNING escalation_id
            """
        ),
        {"eid": str(escalation_id), "staff": str(staff_id)},
    ).scalar_one_or_none()
    if updated is None:
        raise EscalationError("this escalation is not open")


def resolve(
    session: Session,
    escalation_id: UUID,
    staff_id: UUID,
    resolution: str,
    no_action: bool = False,
) -> None:
    """Close it, on the record.

    A resolution note is required. "Resolved" with no account of what was done
    is indistinguishable from ignoring it, and this is the record someone may
    have to defend.
    """
    if not resolution.strip():
        raise EscalationError("a resolution must say what was done")

    updated = session.execute(
        text(
            """
            UPDATE escalations
               SET status = CASE WHEN :no_action
                                 THEN 'closed_no_action'::escalation_status
                                 ELSE 'resolved'::escalation_status END,
                   resolved_at = clock_timestamp(), resolved_by = :staff,
                   resolution = :resolution,
                   -- Closing without acknowledging still stops the clock.
                   acknowledged_at = COALESCE(acknowledged_at, clock_timestamp()),
                   acknowledged_by = COALESCE(acknowledged_by, :staff)
             WHERE escalation_id = :eid
               AND status IN ('open','acknowledged')
            RETURNING escalation_id
            """
        ),
        {
            "eid": str(escalation_id),
            "staff": str(staff_id),
            "resolution": resolution,
            "no_action": no_action,
        },
    ).scalar_one_or_none()
    if updated is None:
        raise EscalationError("this escalation is already closed")


def open_escalations(session: Session) -> list[dict]:
    """Most urgent first, by deadline rather than by when it arrived."""
    rows = session.execute(
        text(
            """
            SELECT e.escalation_id, e.kind::text AS kind, e.status::text AS status,
                   e.raised_at, e.respond_by, e.detail,
                   (now() > e.respond_by AND e.acknowledged_at IS NULL) AS overdue,
                   c.display_name, s.full_name AS owner_name,
                   emp.business_name
              FROM escalations e
              LEFT JOIN candidates c ON c.candidate_id = e.candidate_id
              LEFT JOIN staff s      ON s.staff_id = e.owner_staff_id
              LEFT JOIN placements p ON p.placement_id = e.placement_id
              LEFT JOIN work_requests wr ON wr.request_id = p.request_id
              LEFT JOIN employers emp ON emp.employer_id = wr.employer_id
             WHERE e.status IN ('open','acknowledged')
             ORDER BY e.respond_by ASC
            """
        )
    ).mappings()
    return [dict(r) for r in rows]


def response_performance(session: Session) -> list[dict]:
    """Did we meet our own response times? Measured, or it decays into a form."""
    rows = session.execute(
        text(
            """
            SELECT kind,
                   count(*) AS raised,
                   count(*) FILTER (WHERE answered_in_time) AS in_time,
                   count(*) FILTER (WHERE overdue) AS currently_overdue,
                   ROUND(avg(hours_to_acknowledge), 2) AS avg_hours
              FROM v_escalation_response
             GROUP BY kind ORDER BY kind
            """
        )
    ).mappings()
    return [dict(r) for r in rows]
