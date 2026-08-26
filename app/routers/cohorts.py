"""Cohort endpoints."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.deps import SessionDep, StaffDep
from app.operations.cohorts import (
    CohortError,
    add_member,
    cohort_members,
    create_cohort,
    list_cohorts,
    record_outcome,
    set_status,
    training_effect,
)

router = APIRouter(prefix="/cohorts", tags=["cohorts"])


class NewCohort(BaseModel):
    name: str
    starts_on: date
    sector: str | None = None
    ends_on: date | None = None
    women_only: bool = False
    capacity: int | None = Field(default=None, ge=1)
    location: str | None = None


class Enrolment(BaseModel):
    candidate_id: UUID


class Outcome(BaseModel):
    candidate_id: UUID
    outcome: str = Field(pattern="^(completed|withdrew|did_not_finish)$")
    notes: str | None = None


class CohortStatus(BaseModel):
    status: str = Field(pattern="^(planned|running|completed|cancelled)$")


@router.get("")
def index(session: SessionDep, staff: StaffDep, include_finished: bool = False):
    return {"cohorts": list_cohorts(session, include_finished)}


@router.post("", status_code=201)
def create(body: NewCohort, session: SessionDep, staff: StaffDep):
    try:
        cohort_id = create_cohort(
            session,
            name=body.name,
            starts_on=body.starts_on,
            facilitator=staff.staff_id,
            sector=body.sector,
            ends_on=body.ends_on,
            women_only=body.women_only,
            capacity=body.capacity,
            location=body.location,
        )
    except CohortError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"cohort_id": cohort_id}


@router.get("/{cohort_id}/members")
def members(cohort_id: UUID, session: SessionDep, staff: StaffDep):
    return {"members": cohort_members(session, cohort_id)}


@router.post("/{cohort_id}/members", status_code=201)
def enrol(
    cohort_id: UUID, body: Enrolment, session: SessionDep, staff: StaffDep
):
    try:
        add_member(session, cohort_id, body.candidate_id)
    except CohortError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"cohort_id": cohort_id, "candidate_id": body.candidate_id}


@router.post("/{cohort_id}/outcomes")
def outcome(cohort_id: UUID, body: Outcome, session: SessionDep, staff: StaffDep):
    try:
        record_outcome(
            session, cohort_id, body.candidate_id, body.outcome, body.notes
        )
    except CohortError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"cohort_id": cohort_id, "outcome": body.outcome}


@router.patch("/{cohort_id}")
def update_status(
    cohort_id: UUID, body: CohortStatus, session: SessionDep, staff: StaffDep
):
    try:
        set_status(session, cohort_id, body.status)
    except CohortError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"cohort_id": cohort_id, "status": body.status}


@router.get("/training-effect")
def effect(session: SessionDep, staff: StaffDep):
    """Placement rates for people who finished a cohort against those who
    never sat one. An association, not a causal claim."""
    return training_effect(session)
