"""Residency-sensitive identity data.

Every route here is gated on the per-account identity grant and leaves an
audit_log row behind. The read goes through read_candidate_identity(), the
SECURITY DEFINER function, because direct SELECT on candidate_identity is
revoked -- that is what makes the trail complete rather than best-effort.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.deps import IdentityStaffDep, SessionDep

router = APIRouter(prefix="/candidates", tags=["identity"])


@router.get("/{candidate_id}/identity")
def read_identity(
    candidate_id: UUID, session: SessionDep, staff: IdentityStaffDep
):
    row = session.execute(
        text("SELECT * FROM read_candidate_identity(:cid)"),
        {"cid": str(candidate_id)},
    ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return dict(row)


@router.get("/{candidate_id}/access-log")
def access_log(
    candidate_id: UUID, session: SessionDep, staff: IdentityStaffDep
):
    """Who has looked at this person's identity record, and when.

    This is the answer to the NCSA's question, and it is also what a candidate
    is entitled to ask under Law No. 058/2021.
    """
    rows = session.execute(
        text(
            """
            SELECT a.action, a.occurred_at, s.full_name AS staff_name
              FROM audit_log a
              LEFT JOIN staff s ON s.staff_id = a.staff_id
             WHERE a.table_name = 'candidate_identity'
               AND a.record_id = :cid
             ORDER BY a.occurred_at DESC
            """
        ),
        {"cid": str(candidate_id)},
    ).mappings()
    return {"access_log": [dict(r) for r in rows]}
