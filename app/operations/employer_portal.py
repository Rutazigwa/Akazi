"""Everything an employer can do, scoped to their own employer.

Every function here takes `employer_id` and every query filters on it. That is
not defensive habit -- it is the only thing standing between one employer and
another's roster, pay rates and worker ratings. Ownership is re-checked on the
server for each action; a placement id in a URL proves nothing.

What an employer must never see, no matter what:

    legal names, national IDs, dates of birth, phone numbers, home locations

Those live in candidate_identity and are not joined here at all. Employers get
`display_name` and the operational facts about the shift they bought.
"""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


class EmployerPortalError(Exception):
    pass


def _own_placement(session: Session, employer_id: UUID, placement_id: UUID) -> dict:
    """Fetch a placement, or refuse if it is not this employer's.

    Refusing with the same message whether the placement belongs to someone else
    or does not exist: telling an employer that a given id is real but not
    theirs is itself a small leak.
    """
    row = session.execute(
        text(
            """
            SELECT p.placement_id, p.request_id, p.status::text AS status,
                   p.candidate_id, wr.employer_id
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
             WHERE p.placement_id = :pid AND wr.employer_id = :eid
            """
        ),
        {"pid": str(placement_id), "eid": str(employer_id)},
    ).mappings().first()

    if row is None:
        raise EmployerPortalError("no such placement")
    return dict(row)


def _own_request(session: Session, employer_id: UUID, request_id: UUID) -> dict:
    row = session.execute(
        text(
            """
            SELECT request_id, title, work_type, headcount, starts_on, ends_on,
                   shift_start, shift_end, pay_rwf, pay_unit, transport_covered,
                   meals_provided, safety_notes, status::text AS status,
                   opened_at
              FROM work_requests
             WHERE request_id = :rid AND employer_id = :eid
            """
        ),
        {"rid": str(request_id), "eid": str(employer_id)},
    ).mappings().first()
    if row is None:
        raise EmployerPortalError("no such work request")
    return dict(row)


def my_requests(session: Session, employer_id: UUID) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT wr.request_id, wr.title, wr.status::text AS status,
                   wr.headcount, wr.starts_on, wr.pay_rwf, wr.pay_unit,
                   wr.transport_covered, wr.shift_start, wr.shift_end,
                   (SELECT count(*) FROM placements p
                     WHERE p.request_id = wr.request_id
                       AND p.status IN ('offered','accepted','active','completed')
                   ) AS assigned
              FROM work_requests wr
             WHERE wr.employer_id = :eid
             ORDER BY wr.starts_on DESC, wr.opened_at DESC
            """
        ),
        {"eid": str(employer_id)},
    ).mappings()
    return [dict(r) for r in rows]


def assigned_workers(
    session: Session, employer_id: UUID, request_id: UUID | None = None
) -> list[dict]:
    """Who is coming, by display name only.

    No join to candidate_identity anywhere in this query. An employer buying a
    covered shift does not need the worker's national ID to receive them.
    """
    rows = session.execute(
        text(
            """
            SELECT p.placement_id, p.status::text AS status,
                   p.agreed_pay_rwf, p.pay_unit, p.started_on,
                   p.employer_rating, p.rated_at,
                   c.display_name,
                   wr.request_id, wr.title, wr.starts_on,
                   wr.shift_start, wr.shift_end,
                   (p.replaces_placement IS NOT NULL) AS is_replacement,
                   (SELECT count(*) FROM attendance a
                     WHERE a.placement_id = p.placement_id AND a.present
                   ) AS days_present
              FROM placements p
              JOIN candidates c     ON c.candidate_id = p.candidate_id
              JOIN work_requests wr ON wr.request_id = p.request_id
             WHERE wr.employer_id = :eid
               AND (CAST(:rid AS uuid) IS NULL OR wr.request_id = CAST(:rid AS uuid))
               AND p.status IN ('offered','accepted','active','completed','no_show')
             ORDER BY wr.starts_on DESC, c.display_name
            """
        ),
        {"eid": str(employer_id), "rid": str(request_id) if request_id else None},
    ).mappings()
    return [dict(r) for r in rows]


def confirm_attendance(
    session: Session,
    employer_id: UUID,
    contact_id: UUID,
    placement_id: UUID,
    work_date: date,
    present: bool,
    hours_worked: float | None = None,
    absence_reason: str | None = None,
):
    """The employer's own record of whether the worker arrived.

    This is the point of the whole portal. Until now every attendance row was
    typed by a coordinator, which makes it our word rather than the employer's --
    and the reliability guarantee is a claim we make to employers. Recording
    which contact pressed the button is what turns it into evidence.
    """
    from app.operations.attendance import log_attendance

    _own_placement(session, employer_id, placement_id)

    invocation = log_attendance(
        session, placement_id, work_date, present, "employer",
        hours_worked, absence_reason,
    )
    session.execute(
        text(
            "UPDATE attendance SET confirmed_by_contact = :contact "
            "WHERE placement_id = :pid AND work_date = :work_date"
        ),
        {
            "contact": str(contact_id),
            "pid": str(placement_id),
            "work_date": work_date,
        },
    )
    return invocation


def rate_worker(
    session: Session,
    employer_id: UUID,
    placement_id: UUID,
    rating: int,
    note: str | None = None,
) -> None:
    if not 1 <= rating <= 5:
        raise EmployerPortalError("rating must be between 1 and 5")

    placement = _own_placement(session, employer_id, placement_id)
    if placement["status"] not in ("active", "completed"):
        raise EmployerPortalError(
            "a worker can be rated once they have actually worked"
        )

    session.execute(
        text(
            """
            UPDATE placements
               SET employer_rating = :rating, employer_note = :note,
                   rated_at = now()
             WHERE placement_id = :pid
            """
        ),
        {"rating": rating, "note": note, "pid": str(placement_id)},
    )


def post_request(
    session: Session,
    employer_id: UUID,
    *,
    title: str,
    work_type: str,
    headcount: int,
    starts_on: date,
    pay_rwf: int,
    pay_unit: str,
    shift_start: time | None = None,
    shift_end: time | None = None,
    transport_covered: bool = False,
    meals_provided: bool = False,
    safety_notes: str | None = None,
) -> UUID:
    """An employer posting their own shift.

    employer_id comes from the session, never from the form -- otherwise an
    employer could post work in someone else's name.
    """
    from app.operations.requests import create_work_request

    return create_work_request(
        session,
        employer_id=employer_id,
        title=title,
        work_type=work_type,
        headcount=headcount,
        starts_on=starts_on,
        pay_rwf=pay_rwf,
        pay_unit=pay_unit,
        shift_start=shift_start,
        shift_end=shift_end,
        transport_covered=transport_covered,
        meals_provided=meals_provided,
        safety_notes=safety_notes,
    )


def reorder(
    session: Session, employer_id: UUID, request_id: UUID, starts_on: date
) -> UUID:
    """Repeat a previous request on a new date.

    Reorder rate is a pilot metric and the clearest signal that the guarantee is
    worth paying for -- so repeating an order is one action, not a form to fill
    in again.
    """
    original = _own_request(session, employer_id, request_id)
    return post_request(
        session,
        employer_id,
        title=original["title"],
        work_type=original["work_type"],
        headcount=original["headcount"],
        starts_on=starts_on,
        pay_rwf=original["pay_rwf"],
        pay_unit=original["pay_unit"],
        shift_start=original["shift_start"],
        shift_end=original["shift_end"],
        transport_covered=original["transport_covered"],
        meals_provided=original["meals_provided"],
        safety_notes=original["safety_notes"],
    )


def reliability_summary(session: Session, employer_id: UUID) -> dict:
    """What we promised this employer, and whether we delivered.

    Shown to the employer because the guarantee is the product. An operator who
    only reports this internally is asking to be taken on trust.
    """
    row = session.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM placements p
                 JOIN work_requests wr ON wr.request_id = p.request_id
                WHERE wr.employer_id = :eid
                  AND p.status IN ('active','completed')) AS shifts_covered,
              (SELECT count(*) FROM v_guarantee_invocations g
                 JOIN work_requests wr ON wr.request_id = g.request_id
                WHERE wr.employer_id = :eid) AS no_shows,
              (SELECT count(*) FROM v_guarantee_invocations g
                 JOIN work_requests wr ON wr.request_id = g.request_id
                WHERE wr.employer_id = :eid
                  AND g.filled_within_24h) AS covered_within_24h,
              (SELECT ROUND(avg(days_to_fill), 1) FROM v_time_to_fill t
                WHERE t.employer_id = :eid) AS avg_days_to_fill
            """
        ),
        {"eid": str(employer_id)},
    ).mappings().one()
    return dict(row)
