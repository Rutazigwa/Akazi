"""Data subject rights under Law No. 058/2021.

Two obligations, both stated requirements and neither of them optional:

**Access.** A person can ask what we hold about them. `export_candidate_data`
assembles it -- identity, profile, consent history, assessments, placements,
attendance, pay records, follow-ups, and the log of who has looked at their
identity record. The export itself is an access to identity data, so it is
audited like any other.

**Erasure.** A person can ask us to delete their data. This is handled by
redaction rather than deletion -- see migration 012 for why a literal DELETE
would cascade through and destroy an employer's confirmed attendance, the pay
records proving someone was paid, and the replacement chain of an unrelated
candidate.

Erasure is a two-step flow on purpose: a request is recorded when it arrives,
and carried out as a separate, deliberate action. The gap is where the operator
checks whether anything genuinely blocks it -- an open placement, an unpaid
wage. Deleting someone's contact details while they are owed money is not
compliance, it is losing the ability to pay them.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

REQUEST_CHANNELS = ("paper", "whatsapp", "app", "phone", "email")


class DataRightsError(Exception):
    pass


@dataclass(frozen=True)
class ErasureBlocker:
    reason: str
    detail: str


def request_erasure(
    session: Session,
    candidate_id: UUID,
    requested_via: str,
    received_by: UUID,
) -> UUID:
    if requested_via not in REQUEST_CHANNELS:
        raise DataRightsError(
            f"requested_via must be one of {REQUEST_CHANNELS}"
        )

    # Select the key alongside erased_at: a bare scalar cannot distinguish
    # "no such candidate" from "candidate exists and has not been erased",
    # since both come back as None.
    existing = session.execute(
        text(
            "SELECT candidate_id, erased_at FROM candidate_identity "
            "WHERE candidate_id = :cid"
        ),
        {"cid": str(candidate_id)},
    ).first()

    if existing is None:
        raise DataRightsError(f"candidate {candidate_id} not found")
    if existing.erased_at is not None:
        raise DataRightsError("this candidate's data has already been erased")

    return session.execute(
        text(
            """
            INSERT INTO erasure_requests (candidate_id, requested_via,
                                          received_by)
            VALUES (:cid, :via, :by)
            RETURNING erasure_id
            """
        ),
        {"cid": str(candidate_id), "via": requested_via, "by": str(received_by)},
    ).scalar_one()


def erasure_blockers(session: Session, candidate_id: UUID) -> list[ErasureBlocker]:
    """Reasons to pause before erasing. Advisory -- the operator decides.

    None of these override the right. They exist so that the decision is made
    knowingly: erasing the contact details of someone who is owed a wage, or who
    is due on a shift tomorrow, creates a different problem than it solves.
    """
    blockers: list[ErasureBlocker] = []

    live = session.execute(
        text(
            """
            SELECT count(*) FROM placements
             WHERE candidate_id = :cid
               AND status IN ('offered','accepted','active')
            """
        ),
        {"cid": str(candidate_id)},
    ).scalar_one()
    if live:
        blockers.append(
            ErasureBlocker(
                "live_placement",
                f"{live} placement(s) offered, accepted or active",
            )
        )

    unpaid = session.execute(
        text(
            """
            SELECT count(*) FROM pay_records pr
              JOIN placements p ON p.placement_id = pr.placement_id
             WHERE p.candidate_id = :cid AND pr.paid_on IS NULL
            """
        ),
        {"cid": str(candidate_id)},
    ).scalar_one()
    if unpaid:
        blockers.append(
            ErasureBlocker(
                "unpaid_wages",
                f"{unpaid} pay record(s) with no payment date -- erasing "
                f"contact details would remove the means to pay",
            )
        )

    return blockers


def complete_erasure(session: Session, erasure_id: UUID) -> None:
    """Redact the identity record. Irreversible."""
    row = session.execute(
        text(
            "SELECT candidate_id, status::text AS status FROM erasure_requests "
            "WHERE erasure_id = :eid"
        ),
        {"eid": str(erasure_id)},
    ).mappings().first()

    if row is None:
        raise DataRightsError(f"erasure request {erasure_id} not found")
    if row["status"] in ("completed", "refused"):
        raise DataRightsError(
            f"erasure request {erasure_id} is already {row['status']}"
        )

    session.execute(
        text("SELECT erase_candidate_identity(:cid, :eid)"),
        {"cid": row["candidate_id"], "eid": str(erasure_id)},
    )


def refuse_erasure(session: Session, erasure_id: UUID, note: str) -> None:
    """Refuse a request, on the record. A refusal without a reason is not defensible."""
    if not note.strip():
        raise DataRightsError("a refusal must state its reason")

    updated = session.execute(
        text(
            """
            UPDATE erasure_requests
               SET status = 'refused', decision_note = :note
             WHERE erasure_id = :eid AND status IN ('requested','in_review')
            RETURNING erasure_id
            """
        ),
        {"eid": str(erasure_id), "note": note},
    ).scalar_one_or_none()

    if updated is None:
        raise DataRightsError(
            f"erasure request {erasure_id} is not open"
        )


def open_erasure_requests(session: Session) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT e.erasure_id, e.candidate_id, e.requested_at,
                   e.requested_via, e.status::text AS status,
                   c.display_name
              FROM erasure_requests e
              JOIN candidates c ON c.candidate_id = e.candidate_id
             WHERE e.status IN ('requested','in_review')
             ORDER BY e.requested_at ASC
            """
        )
    ).mappings()
    return [dict(r) for r in rows]


def export_candidate_data(session: Session, candidate_id: UUID) -> dict:
    """Everything held about one person, for a subject access request.

    The identity read goes through read_candidate_identity(), so producing an
    export is itself recorded in the access log -- as it should be.
    """
    identity = session.execute(
        text("SELECT * FROM read_candidate_identity(:cid, 'data_request')"),
        {"cid": str(candidate_id)},
    ).mappings().first()

    if identity is None:
        raise DataRightsError(f"candidate {candidate_id} not found")

    def rows(sql: str) -> list[dict]:
        return [
            dict(r)
            for r in session.execute(
                text(sql), {"cid": str(candidate_id)}
            ).mappings()
        ]

    return {
        "identity": dict(identity),
        "profile": rows(
            "SELECT * FROM candidates WHERE candidate_id = :cid"
        ),
        "availability": rows(
            "SELECT day_of_week, start_time, end_time FROM availability "
            "WHERE candidate_id = :cid ORDER BY day_of_week"
        ),
        "consent_history": rows(
            "SELECT policy_version, purpose, granted, captured_via, captured_at "
            "FROM consent_records WHERE candidate_id = :cid "
            "ORDER BY recorded_seq"
        ),
        "assessments": rows(
            """
            SELECT s.skill_name, a.title, ar.score, ar.assessed_at, ar.notes
              FROM assessment_results ar
              JOIN assessments a ON a.assessment_id = ar.assessment_id
              JOIN skills s      ON s.skill_id = a.skill_id
             WHERE ar.candidate_id = :cid ORDER BY ar.assessed_at
            """
        ),
        "placements": rows(
            """
            SELECT p.placement_id, e.business_name, wr.title,
                   p.status::text AS status, p.agreed_pay_rwf, p.pay_unit,
                   p.est_transport_rwf, p.match_reason,
                   p.offered_at, p.started_on, p.ended_on
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
             WHERE p.candidate_id = :cid ORDER BY p.offered_at
            """
        ),
        "attendance": rows(
            """
            SELECT a.work_date, a.present, a.hours_worked, a.confirmed_by,
                   a.absence_reason
              FROM attendance a
              JOIN placements p ON p.placement_id = a.placement_id
             WHERE p.candidate_id = :cid ORDER BY a.work_date
            """
        ),
        "pay_records": rows(
            """
            SELECT pr.period_start, pr.period_end, pr.gross_rwf,
                   pr.deductions_rwf, pr.net_rwf, pr.due_on, pr.paid_on,
                   pr.method, pr.worker_confirmed
              FROM pay_records pr
              JOIN placements p ON p.placement_id = pr.placement_id
             WHERE p.candidate_id = :cid ORDER BY pr.period_start
            """
        ),
        "follow_ups": rows(
            """
            SELECT f.checkpoint::text AS checkpoint, f.due_on, f.completed_at,
                   f.still_working, f.worker_rating, f.employer_rating,
                   f.issue_flag, f.notes
              FROM follow_ups f
              JOIN placements p ON p.placement_id = f.placement_id
             WHERE p.candidate_id = :cid ORDER BY f.due_on
            """
        ),
        "identity_access_log": rows(
            """
            SELECT a.action, a.occurred_at, s.full_name AS staff_name,
                   COALESCE(a.detail ->> 'purpose', 'unrecorded') AS purpose
              FROM audit_log a
              LEFT JOIN staff s ON s.staff_id = a.staff_id
             WHERE a.table_name = 'candidate_identity' AND a.record_id = :cid
             ORDER BY a.occurred_at
            """
        ),
    }
