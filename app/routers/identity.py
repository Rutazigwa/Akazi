"""Residency-sensitive identity data.

Every route here is gated on the per-account identity grant and leaves an
audit_log row behind. The read goes through read_candidate_identity(), the
SECURITY DEFINER function, because direct SELECT on candidate_identity is
revoked -- that is what makes the trail complete rather than best-effort.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.deps import IdentityStaffDep, SessionDep

router = APIRouter(prefix="/candidates", tags=["identity"])


@router.get("/{candidate_id}/identity")
def read_identity(
    candidate_id: UUID,
    session: SessionDep,
    staff: IdentityStaffDep,
    purpose: Annotated[str, Query(
        description="Why this record is being opened. Recorded in the audit "
                    "log and answerable to the candidate and the NCSA.",
    )],
):
    """Open somebody's identity record, and say why.

    purpose is required rather than defaulted. It used to default to
    'operations', which meant every read recorded the same word whatever it was
    for -- a coordinator taking a support call and a staff member assembling a
    subject access request were indistinguishable in the log. A reason nobody
    has to give is not a reason, and this column is what gets produced when the
    NCSA asks why staff opened a person's record.

    The valid list lives in assert_identity_read_purpose(); an unknown one is
    refused here rather than recorded.
    """
    try:
        # A savepoint, because the refusal is an exception raised inside
        # PostgreSQL and that aborts the surrounding transaction. Without it a
        # rejected purpose would answer 400 and leave the connection unusable
        # for anything else in the same request.
        with session.begin_nested():
            row = session.execute(
                text("SELECT * FROM read_candidate_identity(:cid, :purpose)"),
                {"cid": str(candidate_id), "purpose": purpose},
            ).mappings().first()
    except DBAPIError as exc:
        if "unknown identity read purpose" not in str(exc.orig):
            raise
        raise HTTPException(
            status_code=400,
            detail=f"unknown identity read purpose {purpose!r}",
        ) from exc

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
            SELECT a.action, a.occurred_at, s.full_name AS staff_name,
                   -- The column this endpoint existed without. "Somebody read
                   -- your record" is not an answer to "why did somebody read
                   -- my record", and the subject access export has returned
                   -- purpose all along -- so the two answers to the same
                   -- question disagreed, and the thinner one was the one a
                   -- coordinator would produce.
                   COALESCE(a.detail ->> 'purpose', 'unrecorded') AS purpose
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
