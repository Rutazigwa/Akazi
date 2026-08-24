"""Data subject rights endpoints (Law No. 058/2021).

All of these touch identity data, so all of them require the identity grant and
all of them are audited.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.deps import IdentityStaffDep, SessionDep
from app.operations.data_rights import (
    DataRightsError,
    complete_erasure,
    erasure_blockers,
    export_candidate_data,
    open_erasure_requests,
    refuse_erasure,
    request_erasure,
)

router = APIRouter(tags=["data-rights"])


class NewErasureRequest(BaseModel):
    requested_via: str = Field(pattern="^(paper|whatsapp|app|phone|email)$")


class Refusal(BaseModel):
    decision_note: str = Field(min_length=1)


@router.get("/candidates/{candidate_id}/data-export")
def data_export(
    candidate_id: UUID, session: SessionDep, staff: IdentityStaffDep
):
    """Everything held about one person, for a subject access request."""
    try:
        return export_candidate_data(session, candidate_id)
    except DataRightsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/candidates/{candidate_id}/erasure-requests", status_code=201)
def create_erasure_request(
    candidate_id: UUID,
    body: NewErasureRequest,
    session: SessionDep,
    staff: IdentityStaffDep,
):
    try:
        erasure_id = request_erasure(
            session, candidate_id, body.requested_via, staff.staff_id
        )
    except DataRightsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Surfaced with the request, not hidden behind a second call: whoever
    # takes the request should see immediately if acting on it would strand
    # an unpaid wage or an active placement.
    return {
        "erasure_id": erasure_id,
        "blockers": [
            {"reason": b.reason, "detail": b.detail}
            for b in erasure_blockers(session, candidate_id)
        ],
    }


@router.get("/erasure-requests")
def list_erasure_requests(session: SessionDep, staff: IdentityStaffDep):
    return {"open": open_erasure_requests(session)}


@router.post("/erasure-requests/{erasure_id}/complete")
def complete(
    erasure_id: UUID, session: SessionDep, staff: IdentityStaffDep
):
    """Redact the identity record. Irreversible."""
    try:
        complete_erasure(session, erasure_id)
    except DataRightsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"erasure_id": erasure_id, "status": "completed"}


@router.post("/erasure-requests/{erasure_id}/refuse")
def refuse(
    erasure_id: UUID,
    body: Refusal,
    session: SessionDep,
    staff: IdentityStaffDep,
):
    try:
        refuse_erasure(session, erasure_id, body.decision_note)
    except DataRightsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"erasure_id": erasure_id, "status": "refused"}
