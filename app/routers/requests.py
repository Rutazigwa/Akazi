"""Work requests, matching, and offers."""

from __future__ import annotations

from datetime import date, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.deps import SessionDep, StaffDep
from app.matching.repository import find_matches
from app.operations.requests import (
    RequestError,
    cancel_work_request,
    create_work_request,
    drop_skill_requirement,
    offer_placement,
    open_requests,
    request_requirements,
    require_skill,
    respond_to_offer,
)

router = APIRouter(tags=["requests"])


class NewWorkRequest(BaseModel):
    employer_id: UUID
    title: str
    work_type: str = Field(
        pattern="^(shift|internship|apprenticeship|fixed_term|project)$"
    )
    headcount: int = Field(ge=1)
    starts_on: date
    pay_rwf: int = Field(gt=0)
    pay_unit: str = Field(pattern="^(day|hour|month|task)$")
    ends_on: date | None = None
    shift_start: time | None = None
    shift_end: time | None = None
    transport_covered: bool = False
    meals_provided: bool = False
    safety_notes: str | None = None


class RequiredSkill(BaseModel):
    skill_code: str
    min_score: int = Field(default=3, ge=0)


class Cancellation(BaseModel):
    reason: str = Field(min_length=1)


class Offer(BaseModel):
    candidate_id: UUID


class OfferResponse(BaseModel):
    accepted: bool


@router.post("/work-requests", status_code=201)
def create(body: NewWorkRequest, session: SessionDep, staff: StaffDep):
    try:
        request_id = create_work_request(
            session,
            employer_id=body.employer_id,
            title=body.title,
            work_type=body.work_type,
            headcount=body.headcount,
            starts_on=body.starts_on,
            pay_rwf=body.pay_rwf,
            pay_unit=body.pay_unit,
            ends_on=body.ends_on,
            shift_start=body.shift_start,
            shift_end=body.shift_end,
            transport_covered=body.transport_covered,
            meals_provided=body.meals_provided,
            safety_notes=body.safety_notes,
        )
    except RequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request_id": request_id}


@router.get("/work-requests")
def list_open(
    session: SessionDep,
    staff: StaffDep,
    status: Annotated[list[str] | None, Query()] = None,
):
    """Open and filling requests by default -- the coordinator's actual queue.

    Pass ?status=filled (repeatable) to see the rest.
    """
    return {
        "requests": open_requests(
            session, tuple(status) if status else ("open", "filling")
        )
    }


@router.post("/work-requests/{request_id}/skills", status_code=201)
def add_skill(
    request_id: UUID, body: RequiredSkill, session: SessionDep, staff: StaffDep
):
    try:
        require_skill(session, request_id, body.skill_code, body.min_score)
    except RequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request_id": request_id, "skill_code": body.skill_code}


@router.get("/work-requests/{request_id}/skills")
def list_required_skills(
    request_id: UUID, session: SessionDep, staff: StaffDep
):
    return {"required_skills": request_requirements(session, request_id)}


@router.delete("/work-requests/{request_id}/skills/{skill_id}")
def remove_skill(
    request_id: UUID, skill_id: UUID, session: SessionDep, staff: StaffDep
):
    """Nothing could remove a requirement, so one attached in error stayed for
    the life of the request, filtering candidates out with no way back."""
    try:
        drop_skill_requirement(session, request_id, skill_id)
    except RequestError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"request_id": request_id, "skill_id": skill_id, "removed": True}


@router.post("/work-requests/{request_id}/cancel")
def cancel(
    request_id: UUID, body: Cancellation, session: SessionDep, staff: StaffDep
):
    """Withdraw a shift. Refused once anyone has started work."""
    try:
        return cancel_work_request(session, request_id, body.reason)
    except RequestError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/work-requests/{request_id}/matches")
def matches(request_id: UUID, session: SessionDep, staff: StaffDep):
    """Ranked candidates, each with the reason, plus why everyone else was out.

    The rejections are not debug output. A request that matches nobody is a
    demand signal, and the breakdown says whether the blocker is pay, transport,
    skills or supply.
    """
    try:
        result = find_matches(session, request_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "matches": [
            {
                "candidate_id": m.candidate.candidate_id,
                "display_name": m.candidate.display_name,
                "reason": m.reason,
                "est_transport_rwf": m.candidate.est_transport_rwf,
                "est_commute_min": m.candidate.est_commute_min,
            }
            for m in result.matches
        ],
        "rejections": [
            {
                "candidate_id": r.candidate.candidate_id,
                "display_name": r.candidate.display_name,
                "filter": r.filter_name,
                "reason": r.reason,
            }
            for r in result.rejections
        ],
    }


@router.post("/work-requests/{request_id}/offers", status_code=201)
def offer(
    request_id: UUID, body: Offer, session: SessionDep, staff: StaffDep
):
    try:
        placement_id = offer_placement(session, request_id, body.candidate_id)
    except RequestError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": placement_id}


@router.post("/placements/{placement_id}/response")
def respond(
    placement_id: UUID,
    body: OfferResponse,
    session: SessionDep,
    staff: StaffDep,
):
    try:
        respond_to_offer(session, placement_id, body.accepted)
    except RequestError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": placement_id, "accepted": body.accepted}
