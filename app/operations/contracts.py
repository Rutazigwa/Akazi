"""Placement contracts.

What was agreed, recorded at the moment it was agreed. The blueprint lists
placement contracts among the seven things for weeks 1-6, and there is a
concrete need underneath: when a pay dispute reaches the escalation path,
something has to say what the worker was told when they said yes.

The terms are a snapshot. A work request can be edited after acceptance -- the
shift moves, the rate changes -- and a contract that quietly follows those edits
records the current intention rather than an agreement. The database refuses to
let the terms be rewritten at all, because editing one after the fact is exactly
what a party to a dispute would want to do.

Both sides acknowledge separately. An employer confirming terms the worker never
saw is how informal work already goes wrong.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


class ContractError(Exception):
    pass


def issue_contract(
    session: Session,
    placement_id: UUID,
    issued_by: UUID | None = None,
    supervisor_name: str | None = None,
) -> dict:
    """Record the agreed terms. One per placement, issued once.

    Called when the candidate accepts: that is the moment the terms are settled
    and the moment the transparent-pay requirement is met -- they saw the pay,
    net of transport, before saying yes.
    """
    existing = session.execute(
        text(
            "SELECT contract_ref FROM placement_contracts "
            "WHERE placement_id = :pid"
        ),
        {"pid": str(placement_id)},
    ).scalar_one_or_none()
    if existing:
        raise ContractError(f"this placement already has contract {existing}")

    row = session.execute(
        text(
            """
            SELECT p.status::text AS status, p.agreed_pay_rwf, p.pay_unit,
                   p.est_transport_rwf, p.est_commute_min, p.match_reason,
                   c.display_name, c.district AS candidate_district,
                   wr.title, wr.work_type::text AS work_type,
                   wr.starts_on, wr.ends_on, wr.shift_start, wr.shift_end,
                   wr.transport_covered, wr.meals_provided, wr.safety_notes,
                   e.business_name, e.district AS site_district
              FROM placements p
              JOIN candidates c     ON c.candidate_id = p.candidate_id
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
             WHERE p.placement_id = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()

    if row is None:
        raise ContractError("no such placement")
    if row["status"] not in ("accepted", "active"):
        raise ContractError(
            f"a contract records an agreement; this placement is "
            f"{row['status']}"
        )

    transport = row["est_transport_rwf"] or 0
    terms = {
        "worker": row["display_name"],
        "employer": row["business_name"],
        "role": row["title"],
        "work_type": row["work_type"],
        "starts_on": row["starts_on"].isoformat(),
        "ends_on": row["ends_on"].isoformat() if row["ends_on"] else None,
        "shift_start": (
            row["shift_start"].strftime("%H:%M") if row["shift_start"] else None
        ),
        "shift_end": (
            row["shift_end"].strftime("%H:%M") if row["shift_end"] else None
        ),
        "pay_rwf": row["agreed_pay_rwf"],
        "pay_unit": row["pay_unit"],
        "transport_covered": row["transport_covered"],
        "estimated_transport_rwf": transport,
        # Stated explicitly rather than left to be worked out. Gross pay is not
        # what someone takes home, and the contract is where that has to be
        # unambiguous.
        "estimated_net_rwf": (
            row["agreed_pay_rwf"] if row["transport_covered"]
            else row["agreed_pay_rwf"] - transport
        ),
        "estimated_commute_min": row["est_commute_min"],
        "meals_provided": row["meals_provided"],
        "site_district": row["site_district"],
        "supervisor": supervisor_name,
        "safety_notes": row["safety_notes"],
        "no_fee_to_apply": True,
    }

    contract_ref = session.execute(text("SELECT next_contract_ref()")).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO placement_contracts (placement_id, contract_ref,
                                             issued_by, terms)
            VALUES (:pid, :ref, :by, CAST(:terms AS jsonb))
            """
        ),
        {
            "pid": str(placement_id), "ref": contract_ref,
            "by": str(issued_by) if issued_by else None,
            "terms": json.dumps(terms),
        },
    )
    session.execute(
        text(
            "UPDATE placements SET contract_ref = :ref, "
            "       supervisor_name = COALESCE(:sup, supervisor_name) "
            " WHERE placement_id = :pid"
        ),
        {"ref": contract_ref, "sup": supervisor_name, "pid": str(placement_id)},
    )
    return {"contract_ref": contract_ref, "terms": terms}


def get_contract(session: Session, placement_id: UUID) -> dict | None:
    row = session.execute(
        text(
            """
            SELECT contract_ref, issued_at, terms,
                   worker_acknowledged_at, employer_acknowledged_at
              FROM placement_contracts WHERE placement_id = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()
    return dict(row) if row else None


def acknowledge(
    session: Session,
    placement_id: UUID,
    party: str,
    contact_id: UUID | None = None,
) -> None:
    """Record that one side has seen the terms.

    Separately, because an employer confirming terms the worker never saw is
    how informal work already goes wrong.
    """
    if party not in ("worker", "employer"):
        raise ContractError("party must be 'worker' or 'employer'")

    column = (
        "worker_acknowledged_at" if party == "worker"
        else "employer_acknowledged_at"
    )
    updated = session.execute(
        text(
            f"""
            UPDATE placement_contracts
               SET {column} = clock_timestamp(),
                   employer_acknowledged_by =
                       CASE WHEN :party = 'employer' THEN CAST(:contact AS uuid)
                            ELSE employer_acknowledged_by END
             WHERE placement_id = :pid AND {column} IS NULL
            RETURNING contract_ref
            """
        ),
        {
            "pid": str(placement_id), "party": party,
            "contact": str(contact_id) if contact_id else None,
        },
    ).scalar_one_or_none()
    if updated is None:
        raise ContractError(
            "no contract for that placement, or it is already acknowledged"
        )


def render_contract(terms: dict, contract_ref: str) -> str:
    """The contract as plain text -- printable, and sendable over WhatsApp.

    Short on purpose. A worker reading this on a lock screen needs the pay, the
    hours and where to go; anything longer will not be read, and an unread
    contract protects nobody.
    """
    shift = ""
    if terms.get("shift_start") and terms.get("shift_end"):
        shift = f" ({terms['shift_start']}–{terms['shift_end']})"

    dates = terms["starts_on"]
    if terms.get("ends_on"):
        dates += f" to {terms['ends_on']}"

    if terms["transport_covered"]:
        transport = "Transport: paid by the employer."
    elif terms["estimated_transport_rwf"]:
        transport = (
            f"Transport: about RWF {terms['estimated_transport_rwf']:,} per day, "
            f"leaving about RWF {terms['estimated_net_rwf']:,}."
        )
    else:
        transport = "Transport: not estimated — check your route before starting."

    lines = [
        f"AKAZI PLACEMENT AGREEMENT  {contract_ref}",
        "",
        f"Worker:   {terms['worker']}",
        f"Employer: {terms['employer']} ({terms['site_district']})",
        f"Role:     {terms['role']}{shift}",
        f"Dates:    {dates}",
        f"Pay:      RWF {terms['pay_rwf']:,} per {terms['pay_unit']}",
        transport,
    ]
    if terms.get("supervisor"):
        lines.append(f"Supervisor: {terms['supervisor']}")
    if terms.get("meals_provided"):
        lines.append("Meals provided on site.")
    if terms.get("safety_notes"):
        lines.append(f"Site notes: {terms['safety_notes']}")

    lines += [
        "",
        "There is no fee to take this work.",
        "If you are not paid in full on the agreed date, tell Akazi and we "
        "will take it up.",
        "If you cannot get there or do not feel safe, tell us — we will not "
        "hold it against you.",
    ]
    return "\n".join(lines)


def unacknowledged(session: Session) -> list[dict]:
    """Contracts one side has not confirmed seeing."""
    rows = session.execute(
        text(
            """
            SELECT pc.placement_id, pc.contract_ref, pc.issued_at,
                   (pc.worker_acknowledged_at IS NULL)   AS worker_pending,
                   (pc.employer_acknowledged_at IS NULL) AS employer_pending,
                   pc.terms->>'worker'   AS worker,
                   pc.terms->>'employer' AS employer
              FROM placement_contracts pc
              JOIN placements p ON p.placement_id = pc.placement_id
             WHERE (pc.worker_acknowledged_at IS NULL
                    OR pc.employer_acknowledged_at IS NULL)
               AND p.status IN ('accepted','active')
             ORDER BY pc.issued_at
            """
        )
    ).mappings()
    return [dict(r) for r in rows]
