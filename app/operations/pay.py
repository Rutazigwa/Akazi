"""Recording pay. Gap 3 of the four: whether the money moves correctly.

No money moves through this system in the pilot -- that is a hard constraint,
and this module honours it by being a ledger of claims, not a payment rail. The
employer pays the worker directly; we record what was agreed, what was due,
what landed, and whether the worker says so.

Three timestamps carry the metric:

    due_on            when the employer agreed to pay
    paid_on           when the employer says it was paid
    worker_confirmed  whether the worker agrees it arrived, in full

Pay accuracy (>= 95% in full, on the agreed date) needs all three. An employer
saying "paid" is a claim; the worker confirming is the fact. The gap between
the two is precisely where informal-work pay disputes live, and being the party
that notices the gap is part of what the placement fee buys.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from app.clock import kigali_today
from sqlalchemy import text
from sqlalchemy.orm import Session


class PayError(Exception):
    pass


def record_pay_period(
    session: Session,
    placement_id: UUID,
    period_start: date,
    period_end: date,
    gross_rwf: int,
    due_on: date,
    deductions_rwf: int = 0,
    method: str | None = None,
    deductions: list[dict] | None = None,
) -> UUID:
    """Open a pay period: what is owed, and when it falls due.

    Created when the terms are known -- usually when the period ends -- not
    when the money lands. A record created only at payment can never show a
    payment that failed to happen, and the missing ones are the point.
    """
    if period_end < period_start:
        raise PayError("the period cannot end before it starts")
    if gross_rwf <= 0:
        raise PayError("gross pay must be positive")
    if deductions_rwf < 0 or deductions_rwf > gross_rwf:
        raise PayError("deductions must be between zero and gross pay")

    # Money off a wage needs a stated reason. The database enforces this at
    # commit too, because a bulk import of paper payslips skips this code --
    # but refusing here gives the coordinator a sentence they can act on
    # rather than a constraint violation. See migration 040.
    lines = list(deductions or [])
    if deductions_rwf and not lines:
        raise PayError(
            f"RWF {deductions_rwf:,} is being deducted with no reason given. "
            "Itemise it: a worker with no payslip cannot query what they "
            "cannot see"
        )
    if lines and sum(int(d["amount_rwf"]) for d in lines) != deductions_rwf:
        raise PayError(
            "the itemised deductions do not add up to the total deducted"
        )
    if due_on < period_start:
        raise PayError("pay cannot fall due before the period it covers")
    if method is not None and method not in ("momo", "cash", "bank"):
        raise PayError("method must be momo, cash or bank")

    exists = session.execute(
        text("SELECT 1 FROM placements WHERE placement_id = :pid"),
        {"pid": str(placement_id)},
    ).first()
    if exists is None:
        raise PayError("no such placement")

    overlap = session.execute(
        text(
            """
            SELECT 1 FROM pay_records
             WHERE placement_id = :pid
               AND period_start <= :end AND period_end >= :start
            """
        ),
        {"pid": str(placement_id), "start": period_start, "end": period_end},
    ).first()
    if overlap:
        raise PayError(
            "this placement already has a pay record overlapping that period"
        )

    # The overlap check above is a read and this is the write; two coordinators
    # recording the same week both passed it before migration 028 added the
    # database guard. A savepoint so a refusal leaves the caller usable.
    savepoint = session.begin_nested()
    try:
        pay_id = session.execute(
            text(
                """
                INSERT INTO pay_records (placement_id, period_start, period_end,
                                         gross_rwf, deductions_rwf, due_on,
                                         method)
                VALUES (:pid, :start, :end, :gross, :deductions, :due, :method)
                RETURNING pay_id
                """
            ),
            {
                "pid": str(placement_id), "start": period_start,
                "end": period_end, "gross": gross_rwf,
                "deductions": deductions_rwf, "due": due_on, "method": method,
            },
        ).scalar_one()

        for line in lines:
            _add_deduction_line(session, pay_id, line)

        savepoint.commit()
    except Exception as exc:  # noqa: BLE001 -- re-raised as a domain error
        savepoint.rollback()
        if "already covers part of this period" in str(exc):
            raise PayError(
                "another pay record was created for an overlapping period "
                "while this one was being entered"
            ) from exc
        raise
    return pay_id


DEDUCTION_KINDS = ("advance", "uniform", "equipment", "transport",
                   "absence", "statutory", "damage", "other")

# The two most open to abuse. A deduction for "damage" with no written
# account of what was damaged is exactly what itemising is meant to prevent.
NEEDS_EXPLANATION = ("damage", "other")


def _add_deduction_line(session: Session, pay_id: UUID, line: dict) -> None:
    kind = str(line.get("kind", "")).strip()
    amount = int(line.get("amount_rwf", 0))
    note = (line.get("note") or "").strip() or None

    if kind not in DEDUCTION_KINDS:
        raise PayError(f"deduction kind must be one of {DEDUCTION_KINDS}")
    if amount <= 0:
        raise PayError("a deduction line must be a positive amount")
    if kind in NEEDS_EXPLANATION and (note is None or len(note) < 10):
        raise PayError(
            f"a '{kind}' deduction needs a written reason -- say what it was "
            "for, in enough words that the worker could dispute it"
        )

    session.execute(
        text(
            """
            INSERT INTO pay_deductions (pay_id, kind, amount_rwf, note,
                                        recorded_by)
            VALUES (:pid, :kind, :amount, :note, current_staff_id())
            """
        ),
        {"pid": str(pay_id), "kind": kind, "amount": amount, "note": note},
    )


def deduction_lines(session: Session, pay_id: UUID) -> list[dict]:
    """What was taken off, and why."""
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT pd.kind, pd.amount_rwf, pd.note, pd.recorded_at,
                       s.full_name AS recorded_by_name
                  FROM pay_deductions pd
                  LEFT JOIN staff s ON s.staff_id = pd.recorded_by
                 WHERE pd.pay_id = :pid
                 ORDER BY pd.amount_rwf DESC
                """
            ),
            {"pid": str(pay_id)},
        ).mappings()
    ]


def pay_variances(session: Session, placement_id: UUID | None = None) -> list[dict]:
    """Pay recorded below what confirmed attendance implies.

    Not proof of anything on its own -- rates change, half days happen -- but
    it is the question worth asking before the money moves rather than after.
    """
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT * FROM v_pay_expected
                 WHERE variance_rwf IS NOT NULL AND variance_rwf < 0
                   AND (CAST(:pid AS uuid) IS NULL
                        OR placement_id = CAST(:pid AS uuid))
                 ORDER BY variance_rwf
                """
            ),
            {"pid": str(placement_id) if placement_id else None},
        ).mappings()
    ]


def suggest_pay_period(session: Session, placement_id: UUID) -> dict | None:
    """Pre-fill a pay record from what the system already knows.

    Days present come from confirmed attendance and the rate from the agreed
    terms, so the coordinator corrects a suggestion instead of typing sums --
    and the suggestion never silently disagrees with the attendance record.
    Only meaningful for daily-rate work; other units return the window alone.
    """
    row = session.execute(
        text(
            """
            SELECT p.agreed_pay_rwf, p.pay_unit,
                   min(a.work_date) AS first_day,
                   max(a.work_date) AS last_day,
                   count(*) FILTER (WHERE a.present) AS days_present
              FROM placements p
              LEFT JOIN attendance a ON a.placement_id = p.placement_id
             WHERE p.placement_id = :pid
             GROUP BY p.placement_id, p.agreed_pay_rwf, p.pay_unit
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()
    if row is None or row["first_day"] is None:
        return None

    gross = (
        row["agreed_pay_rwf"] * row["days_present"]
        if row["pay_unit"] == "day"
        else None
    )
    return {
        "period_start": row["first_day"],
        "period_end": row["last_day"],
        "days_present": row["days_present"],
        "gross_rwf": gross,
        "pay_unit": row["pay_unit"],
        "rate_rwf": row["agreed_pay_rwf"],
    }


def mark_paid(
    session: Session, pay_id: UUID, paid_on: date, method: str | None = None
) -> None:
    """The employer's claim that the money moved."""
    updated = session.execute(
        text(
            """
            UPDATE pay_records
               SET paid_on = :paid_on, method = COALESCE(:method, method)
             WHERE pay_id = :pay_id AND paid_on IS NULL
            RETURNING pay_id
            """
        ),
        {"pay_id": str(pay_id), "paid_on": paid_on, "method": method},
    ).scalar_one_or_none()
    if updated is None:
        raise PayError("no such pay record, or it is already marked paid")


def confirm_with_worker(
    session: Session, pay_id: UUID, received_in_full: bool,
    note: str | None = None,
) -> UUID | None:
    """The worker's answer, taken on the follow-up call.

    A worker saying the money did not arrive in full raises a pay escalation
    on the spot -- the coordinator on the phone should not have to remember a
    second step, and this record is the moment the dispute is known to us.
    Returns the escalation id when one is raised.
    """
    row = session.execute(
        text(
            """
            UPDATE pay_records SET worker_confirmed = :confirmed
             WHERE pay_id = :pay_id
            RETURNING placement_id, net_rwf, period_start, period_end
            """
        ),
        {"pay_id": str(pay_id), "confirmed": received_in_full},
    ).mappings().first()
    if row is None:
        raise PayError("no such pay record")

    if received_in_full:
        return None

    from app.operations.escalations import raise_escalation

    candidate_id = session.execute(
        text("SELECT candidate_id FROM placements WHERE placement_id = :pid"),
        {"pid": row["placement_id"]},
    ).scalar_one()
    detail = (
        f"worker says pay for {row['period_start']}–{row['period_end']} "
        f"(net RWF {row['net_rwf']:,}) did not arrive in full"
    )
    if note:
        detail += f" — {note}"
    return raise_escalation(
        session, "pay",
        candidate_id=candidate_id,
        placement_id=row["placement_id"],
        detail=detail,
    )


def overdue_pay(
    session: Session, as_of: date | None = None, limit: int | None = None
) -> list[dict]:
    """Pay past its agreed date with no payment recorded. The chase list.

    Ordered by how much and how late: the biggest oldest debt is the
    relationship most at risk, on both sides. Each row carries total_rows so a
    capped screen can say how many it is not showing.
    """
    rows = session.execute(
        text(
            """
            SELECT pr.pay_id, pr.placement_id, pr.period_start, pr.period_end,
                   pr.net_rwf, pr.due_on,
                   (CAST(:as_of AS date) - pr.due_on) AS days_overdue,
                   c.display_name, e.business_name,
                   count(*) OVER () AS total_rows
              FROM pay_records pr
              JOIN placements p     ON p.placement_id = pr.placement_id
              JOIN candidates c     ON c.candidate_id = p.candidate_id
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
             WHERE pr.paid_on IS NULL AND pr.due_on < CAST(:as_of AS date)
             ORDER BY pr.due_on ASC, pr.net_rwf DESC
             LIMIT :limit
            """
        ),
        {"as_of": as_of or kigali_today(), "limit": limit},
    ).mappings()
    return [dict(r) for r in rows]


def pay_records_for_placement(session: Session, placement_id: UUID) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT pay_id, period_start, period_end, gross_rwf, deductions_rwf,
                   net_rwf, due_on, paid_on, method, worker_confirmed
              FROM pay_records
             WHERE placement_id = :pid
             ORDER BY period_start
            """
        ),
        {"pid": str(placement_id)},
    ).mappings()
    return [dict(r) for r in rows]
