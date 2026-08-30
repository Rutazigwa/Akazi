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
    # One timestamp, used for both. clock_timestamp() is volatile and advances
    # within a statement, so calling it twice made the deadline a few
    # microseconds more than the response window from the moment it was raised
    # -- and "respond within two hours of the report" should mean exactly that.
    return session.execute(
        text(
            """
            WITH raised AS (SELECT clock_timestamp() AS at)
            INSERT INTO escalations (kind, candidate_id, placement_id,
                                     inbound_id, follow_up_id, owner_staff_id,
                                     raised_at, respond_by, detail)
            SELECT CAST(:kind AS escalation_kind), :candidate_id, :placement_id,
                   :inbound_id, :follow_up_id, :owner,
                   raised.at, raised.at + CAST(:window AS interval), :detail
              FROM raised
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


def open_escalations(
    session: Session, limit: int | None = None
) -> list[dict]:
    """Most urgent first, by deadline rather than by when it arrived."""
    rows = session.execute(
        text(
            """
            SELECT count(*) OVER () AS total_rows,
                   e.escalation_id, e.kind::text AS kind, e.status::text AS status,
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
             -- Gravest kind first, then soonest deadline.
             --
             -- Ordering by deadline alone was fine while the list was
             -- unbounded, because everything appeared. Once it is capped it is
             -- not: seventy-five pay issues that breached weeks ago all have
             -- earlier deadlines than a harassment report raised this morning,
             -- and would push it off the screen. The bound is what makes the
             -- ordering safety-critical.
             --
             -- The rank mirrors RESPONSE_TIMES -- harassment 2h, safety 4h,
             -- pay 24h, the rest 48h -- and tests/test_bounded_lists.py holds
             -- the two together.
             ORDER BY CASE e.kind
                        WHEN 'harassment' THEN 0
                        WHEN 'safety'     THEN 1
                        WHEN 'pay'        THEN 2
                        ELSE 3
                      END,
                      e.respond_by ASC
             LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    return [dict(r) for r in rows]


def response_performance(session: Session) -> list[dict]:
    """Did we meet our own response times? Measured, or it decays into a form.

    avg_hours covers answered escalations only. It used to include the
    unanswered ones at their elapsed-time-so-far, which meant three new
    harassment reports that nobody had looked at pulled the average down from
    5.00 hours to 1.25 -- the metric improved because the safeguard failed.
    Unanswered is now its own column, alongside how long the oldest one has
    been waiting, because an average cannot express "nobody has responded".
    """
    rows = session.execute(
        text(
            """
            SELECT kind,
                   count(*) AS raised,
                   count(*) FILTER (WHERE answered_in_time) AS in_time,
                   count(*) FILTER (WHERE overdue) AS currently_overdue,
                   count(*) FILTER (WHERE acknowledged_at IS NULL) AS unanswered,
                   ROUND(avg(hours_to_acknowledge), 2) AS avg_hours,
                   ROUND(max(hours_elapsed)
                         FILTER (WHERE acknowledged_at IS NULL), 2)
                       AS longest_waiting_hours
              FROM v_escalation_response
             GROUP BY kind ORDER BY kind
            """
        )
    ).mappings()
    return [dict(r) for r in rows]


def alert_on_missed_response_times(session: Session) -> dict:
    """Tell someone about escalations that blew their response time.

    Until this existed, a missed response time turned a pill red on a page.
    If nobody had that page open -- evening, weekend, a coordinator out on a
    site visit -- a harassment report sat unacknowledged and the system was
    content. The blueprint promises a named escalation path and a defined
    response time; the time was defined and nothing enforced it.

    Alerted once, recorded on the escalation. Re-alerting every five minutes
    until someone acknowledges is how an alert becomes noise, and noise is how
    the next one is ignored.

    Deliberately not resent to whoever already missed it: the owner is told,
    because the point of a missed deadline is that the first line did not act.
    """
    from app.messaging.outbox import queue
    from app.messaging.templates import render

    breached = session.execute(
        text(
            """
            SELECT e.escalation_id, e.kind::text AS kind, e.raised_at,
                   e.respond_by, e.owner_staff_id
              FROM escalations e
             WHERE e.status = 'open'
               AND e.acknowledged_at IS NULL
               AND e.respond_by < now()
               AND e.breach_alerted_at IS NULL
             ORDER BY e.respond_by
             FOR UPDATE SKIP LOCKED
            """
        )
    ).mappings().all()

    alerted, unroutable = 0, 0
    for row in breached:
        recipient = _breach_recipient(session, row["owner_staff_id"])
        if recipient is None:
            # Left unmarked on purpose, so it is picked up again once somebody
            # is available. An alert nobody can receive is not "handled".
            unroutable += 1
            continue

        queue(
            session,
            template_key="escalation_breach",
            body=render(
                "escalation_breach",
                kind=row["kind"],
                raised=row["raised_at"].strftime("%d %b %H:%M"),
                respond_by=row["respond_by"].strftime("%d %b %H:%M"),
                link=f"/ui/#escalation-{row['escalation_id']}",
            ),
            staff_id=recipient,
        )
        session.execute(
            text("UPDATE escalations SET breach_alerted_at = clock_timestamp() "
                 "WHERE escalation_id = :eid"),
            {"eid": str(row["escalation_id"])},
        )
        alerted += 1

    return {"alerted": alerted, "unroutable": unroutable,
            "breached": len(breached)}


def _breach_recipient(session: Session, owner_staff_id) -> UUID | None:
    """Somebody other than whoever already missed it, if there is anybody.

    An owner who missed their own deadline still gets told -- there is nobody
    above them, and silence would be worse than a redundant message.
    """
    above = session.execute(
        text(
            "SELECT staff_id FROM staff "
            "WHERE role IN ('owner', 'admin') AND is_active "
            # Cast before the IS NULL: an untyped NULL parameter leaves
            # PostgreSQL nothing to infer the type from.
            "  AND (CAST(:owner AS uuid) IS NULL "
            "       OR staff_id <> CAST(:owner AS uuid)) "
            "ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at "
            "LIMIT 1"
        ),
        {"owner": str(owner_staff_id) if owner_staff_id else None},
    ).scalar_one_or_none()
    if above:
        return above
    return session.execute(
        text("SELECT staff_id FROM staff WHERE is_active "
             "AND role IN ('owner', 'admin') ORDER BY created_at LIMIT 1")
    ).scalar_one_or_none()
