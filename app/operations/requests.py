"""Work requests and offers.

A work request is an employer saying "I need this shift covered." An offer is us
saying "this person will cover it, and here is why." The reason is stored on the
placement at offer time and never recomputed -- an employer may ask months later
why a particular person was sent, and "the algorithm would pick them again
today" is not an answer.
"""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.matching.repository import find_matches, load_request
from app.matching.transport import estimate_transport

PAY_UNITS = ("day", "hour", "month", "task")
WORK_TYPES = ("shift", "internship", "apprenticeship", "fixed_term", "project")


class RequestError(Exception):
    pass


def create_work_request(
    session: Session,
    *,
    employer_id: UUID,
    title: str,
    work_type: str,
    headcount: int,
    starts_on: date,
    pay_rwf: int,
    pay_unit: str,
    ends_on: date | None = None,
    shift_start: time | None = None,
    shift_end: time | None = None,
    transport_covered: bool = False,
    meals_provided: bool = False,
    safety_notes: str | None = None,
    reorders_request: UUID | None = None,
) -> UUID:
    if work_type not in WORK_TYPES:
        raise RequestError(f"work_type must be one of {WORK_TYPES}")
    if pay_unit not in PAY_UNITS:
        raise RequestError(f"pay_unit must be one of {PAY_UNITS}")

    return session.execute(
        text(
            """
            INSERT INTO work_requests
                (employer_id, title, work_type, headcount, starts_on, ends_on,
                 shift_start, shift_end, pay_rwf, pay_unit, transport_covered,
                 meals_provided, safety_notes, reorders_request)
            VALUES (:eid, :title, CAST(:wtype AS work_type), :headcount,
                    :starts, :ends, :shift_start, :shift_end, :pay,
                    :unit, :transport, :meals, :safety, :reorders)
            RETURNING request_id
            """
        ),
        {
            "eid": str(employer_id),
            "title": title,
            "wtype": work_type,
            "headcount": headcount,
            "starts": starts_on,
            "ends": ends_on,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "pay": pay_rwf,
            "unit": pay_unit,
            "transport": transport_covered,
            "meals": meals_provided,
            "safety": safety_notes,
            "reorders": str(reorders_request) if reorders_request else None,
        },
    ).scalar_one()


def require_skill(
    session: Session, request_id: UUID, skill_code: str, min_score: int = 3
) -> None:
    skill_id = session.execute(
        text("SELECT skill_id FROM skills WHERE skill_code = :code"),
        {"code": skill_code},
    ).scalar_one_or_none()
    if skill_id is None:
        raise RequestError(f"unknown skill {skill_code!r}")

    session.execute(
        text(
            """
            INSERT INTO request_skills (request_id, skill_id, min_score)
            VALUES (:rid, :sid, :min_score)
            ON CONFLICT (request_id, skill_id) DO UPDATE
                SET min_score = EXCLUDED.min_score
            """
        ),
        {"rid": str(request_id), "sid": skill_id, "min_score": min_score},
    )


def offer_placement(
    session: Session, request_id: UUID, candidate_id: UUID
) -> UUID:
    """Offer a specific candidate the work, recording why they were chosen.

    Re-runs the matching filters rather than trusting the caller: a coordinator
    working from a list rendered ten minutes ago must not be able to offer work
    to someone who has since failed the transport, safety or consent filters.
    """
    result = find_matches(session, request_id)

    match = next(
        (m for m in result.matches if m.candidate.candidate_id == candidate_id),
        None,
    )
    if match is None:
        rejection = next(
            (r for r in result.rejections
             if r.candidate.candidate_id == candidate_id),
            None,
        )
        if rejection is not None:
            raise RequestError(
                f"{rejection.candidate.display_name} is excluded by the "
                f"{rejection.filter_name} filter: {rejection.reason}"
            )
        raise RequestError(
            f"candidate {candidate_id} is not in the pool for this request"
        )

    context = load_request(session, request_id)
    estimate = estimate_transport(
        *_home_coords(session, candidate_id), context.site_lat, context.site_lng
    )

    # The overlap check above is a read; the insert below is the write. Between
    # them another transaction can place the same person, so the database has
    # the last word (migration 027). Inside a savepoint, so a refusal leaves the
    # caller's transaction usable.
    savepoint = session.begin_nested()
    try:
        placement_id = _insert_offer(
            session, request_id, candidate_id, context, estimate, match.reason
        )
        savepoint.commit()
    except Exception as exc:  # noqa: BLE001 -- re-raised as a domain error
        savepoint.rollback()
        if "already committed to overlapping work" in str(exc):
            raise RequestError(
                f"{match.candidate.display_name} was committed to overlapping "
                f"work while this offer was being made"
            ) from exc
        raise

    _refresh_request_status(session, request_id)

    from app.messaging.events import on_placement_offered

    on_placement_offered(session, placement_id)
    return placement_id


def _insert_offer(
    session: Session, request_id, candidate_id, context, estimate, reason
):
    return session.execute(
        text(
            """
            INSERT INTO placements
                (request_id, candidate_id, status, agreed_pay_rwf, pay_unit,
                 est_transport_rwf, est_commute_min, match_reason)
            VALUES (:rid, :cid, 'offered', :pay, :unit, :transport, :commute,
                    :reason)
            RETURNING placement_id
            """
        ),
        {
            "rid": str(request_id),
            "cid": str(candidate_id),
            "pay": context.request.pay_rwf,
            "unit": context.request.pay_unit,
            "transport": estimate.daily_rwf if estimate else 0,
            "commute": estimate.commute_min if estimate else None,
            "reason": reason,
        },
    ).scalar_one()


def respond_to_offer(
    session: Session, placement_id: UUID, accepted: bool
) -> None:
    """Record the candidate's answer. Pay was visible before this point."""
    updated = session.execute(
        text(
            """
            UPDATE placements
               SET status = CASE WHEN :accepted THEN 'accepted'::placement_status
                                 ELSE 'declined'::placement_status END
             WHERE placement_id = :pid AND status = 'offered'
            RETURNING request_id
            """
        ),
        {"pid": str(placement_id), "accepted": accepted},
    ).scalar_one_or_none()

    if updated is None:
        raise RequestError(
            f"placement {placement_id} is not awaiting a response"
        )
    _refresh_request_status(session, updated)

    from app.operations.attendance import refresh_candidate_status

    candidate_id = session.execute(
        text("SELECT candidate_id FROM placements WHERE placement_id = :pid"),
        {"pid": str(placement_id)},
    ).scalar_one()
    refresh_candidate_status(session, candidate_id)

    if accepted:
        # Issued here because acceptance is the moment the terms are settled,
        # and the moment the transparent-pay requirement was met: they saw the
        # pay net of transport before saying yes.
        from app.messaging.events import on_contract_issued
        from app.operations.contracts import ContractError, issue_contract

        try:
            contract = issue_contract(session, placement_id)
        except ContractError:
            contract = None
        if contract:
            on_contract_issued(session, placement_id, contract)

    if not accepted:
        from app.messaging.events import on_placement_ended

        on_placement_ended(session, placement_id, "offer declined")


def _home_coords(session: Session, candidate_id: UUID):
    row = session.execute(
        text(
            "SELECT home_lat, home_lng FROM candidates WHERE candidate_id = :cid"
        ),
        {"cid": str(candidate_id)},
    ).first()
    if row is None or row[0] is None or row[1] is None:
        return None, None
    return float(row[0]), float(row[1])


def _refresh_request_status(session: Session, request_id: UUID) -> None:
    """Move a request between open / filling / filled as offers land.

    Counts placements that are live -- a declined offer or a no-show frees the
    slot again, which is the whole point of tracking headcount rather than a
    boolean.
    """
    session.execute(
        text(
            """
            WITH live AS (
                SELECT count(*) AS n
                  FROM placements
                 WHERE request_id = :rid
                   AND status IN ('offered','accepted','active','completed')
            )
            UPDATE work_requests wr
               SET status = CASE
                       WHEN live.n = 0                 THEN 'open'
                       WHEN live.n >= wr.headcount     THEN 'filled'
                       ELSE 'filling' END::request_status,
                   filled_at = CASE
                       WHEN live.n >= wr.headcount THEN COALESCE(wr.filled_at, now())
                       ELSE NULL END
              FROM live
             WHERE wr.request_id = :rid
               AND wr.status NOT IN ('cancelled','expired')
            """
        ),
        {"rid": str(request_id)},
    )


def cancel_work_request(
    session: Session, request_id: UUID, reason: str, employer_id: UUID | None = None
) -> dict:
    """Withdraw a shift that is no longer happening.

    Refused once anybody has actually started: work that has begun cannot be
    un-happened, and the honest record of it ending early is a termination with
    a reason, one placement at a time.

    Placements that had not started become 'cancelled' rather than 'declined'.
    Declined means the candidate turned it down, and putting the employer's
    decision on a worker's record would be a small unfairness that compounds --
    prior behaviour feeds the ranking.
    """
    if not reason.strip():
        raise RequestError("a cancellation must say why")

    row = session.execute(
        text(
            """
            SELECT status::text AS status, employer_id
              FROM work_requests WHERE request_id = :rid
            """
        ),
        {"rid": str(request_id)},
    ).mappings().first()
    if row is None or (
        employer_id is not None and row["employer_id"] != employer_id
    ):
        raise RequestError("no such work request")
    if row["status"] == "cancelled":
        raise RequestError("this request is already cancelled")

    started = session.execute(
        text(
            "SELECT count(*) FROM placements "
            "WHERE request_id = :rid AND status IN ('active','completed')"
        ),
        {"rid": str(request_id)},
    ).scalar_one()
    if started:
        raise RequestError(
            f"{started} worker(s) have already started -- end those placements "
            f"individually with a reason instead of cancelling the request"
        )

    affected = session.execute(
        text(
            """
            UPDATE placements SET status = 'cancelled'
             WHERE request_id = :rid AND status IN ('offered','accepted')
            RETURNING placement_id, candidate_id
            """
        ),
        {"rid": str(request_id)},
    ).mappings().all()

    session.execute(
        text(
            "UPDATE work_requests "
            "   SET status = 'cancelled', safety_notes = "
            "       COALESCE(safety_notes || ' | ', '') || :reason "
            " WHERE request_id = :rid"
        ),
        {"rid": str(request_id), "reason": f"cancelled: {reason}"},
    )

    from app.messaging.events import on_placement_cancelled, on_placement_ended
    from app.operations.attendance import refresh_candidate_status

    for placement in affected:
        # Stop anything still queued before telling them it is off, so the
        # cancellation is not followed by a shift reminder.
        on_placement_ended(session, placement["placement_id"], "request cancelled")
        on_placement_cancelled(session, placement["placement_id"], reason)
        refresh_candidate_status(session, placement["candidate_id"])

    return {"request_id": request_id, "placements_cancelled": len(affected)}


def open_requests(
    session: Session, statuses: tuple[str, ...] = ("open", "filling")
) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT wr.request_id, wr.title, wr.status::text AS status,
                   wr.headcount, wr.starts_on, wr.pay_rwf, wr.pay_unit,
                   wr.transport_covered, e.business_name,
                   (SELECT count(*) FROM placements p
                     WHERE p.request_id = wr.request_id
                       AND p.status IN ('offered','accepted','active','completed')
                   ) AS filled
              FROM work_requests wr
              JOIN employers e ON e.employer_id = wr.employer_id
             WHERE wr.status::text = ANY(:statuses)
             ORDER BY wr.starts_on ASC
            """
        ),
        {"statuses": list(statuses)},
    ).mappings()
    return [dict(r) for r in rows]
