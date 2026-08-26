"""Coordinator-facing operations endpoints.

Internal admin only -- coordinators and the owner. Every route requires a valid
bearer session; the acting staff member is resolved from it and stamped on the
transaction so the audit triggers can attribute the work.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.deps import SessionDep, StaffDep
from app.operations.contracts import (
    ContractError,
    acknowledge,
    get_contract,
    issue_contract,
    render_contract,
    unacknowledged,
)
from app.operations.attendance import (
    AttendanceError,
    complete_placement,
    log_attendance,
    open_guarantees,
    record_replacement,
    start_placement,
    terminate_placement,
)
from app.operations.follow_ups import complete_follow_up, due_follow_ups

router = APIRouter(tags=["operations"])


class StartPlacement(BaseModel):
    started_on: date


class LogAttendance(BaseModel):
    work_date: date
    present: bool
    confirmed_by: str = Field(pattern="^(employer|coordinator|worker)$")
    hours_worked: float | None = None
    absence_reason: str | None = None


class EndPlacement(BaseModel):
    ended_on: date | None = None
    # Required to terminate, ignored on completion.
    reason: str | None = None


class RecordReplacement(BaseModel):
    candidate_id: UUID
    match_reason: str
    agreed_pay_rwf: int | None = None
    est_transport_rwf: int = 0
    est_commute_min: int | None = None


class CompleteFollowUp(BaseModel):
    still_working: bool
    worker_rating: int | None = Field(default=None, ge=1, le=5)
    employer_rating: int | None = Field(default=None, ge=1, le=5)
    issue_flag: str | None = None
    notes: str | None = None


@router.post("/placements/{placement_id}/start")
def start(placement_id: UUID, body: StartPlacement, session: SessionDep, staff: StaffDep):
    try:
        schedule = start_placement(session, placement_id, body.started_on)
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": placement_id, "follow_ups": schedule}


@router.post("/placements/{placement_id}/attendance")
def attendance(placement_id: UUID, body: LogAttendance, session: SessionDep, staff: StaffDep):
    try:
        invocation = log_attendance(
            session,
            placement_id,
            body.work_date,
            body.present,
            body.confirmed_by,
            body.hours_worked,
            body.absence_reason,
        )
    except AttendanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if invocation is None:
        return {"recorded": True, "guarantee_invoked": False}

    # The response is the alert: a no-show starts a 24-hour clock, and the
    # coordinator needs the deadline in front of them immediately.
    return {
        "recorded": True,
        "guarantee_invoked": True,
        "invoked_at": invocation.invoked_at,
        "fill_by": invocation.due_by,
        "request_id": invocation.request_id,
    }


@router.post("/placements/{placement_id}/replacement", status_code=201)
def replacement(placement_id: UUID, body: RecordReplacement, session: SessionDep, staff: StaffDep):
    try:
        new_id = record_replacement(
            session,
            placement_id,
            body.candidate_id,
            body.match_reason,
            body.agreed_pay_rwf,
            body.est_transport_rwf,
            body.est_commute_min,
        )
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": new_id, "replaces": placement_id}


@router.post("/placements/{placement_id}/complete")
def complete_the_placement(
    placement_id: UUID, body: EndPlacement, session: SessionDep,
    staff: StaffDep,
):
    """The work finished as agreed. Frees the worker for new matches."""
    try:
        complete_placement(session, placement_id, body.ended_on)
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": placement_id, "status": "completed"}


@router.post("/placements/{placement_id}/terminate")
def terminate_the_placement(
    placement_id: UUID, body: EndPlacement, session: SessionDep,
    staff: StaffDep,
):
    """The work ended early. A reason is required."""
    try:
        terminate_placement(
            session, placement_id, body.reason or "", body.ended_on
        )
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": placement_id, "status": "terminated"}


@router.get("/placements/{placement_id}/contract")
def contract(placement_id: UUID, session: SessionDep, staff: StaffDep):
    """The agreed terms, and the text a worker can be given."""
    found = get_contract(session, placement_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no contract for that placement")
    return {
        **found,
        "text": render_contract(found["terms"], found["contract_ref"]),
    }


@router.post("/placements/{placement_id}/contract", status_code=201)
def create_contract(
    placement_id: UUID, session: SessionDep, staff: StaffDep,
    supervisor_name: str | None = None,
):
    """Issue one by hand, for a placement accepted before contracts existed."""
    try:
        return issue_contract(
            session, placement_id, staff.staff_id, supervisor_name
        )
    except ContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/placements/{placement_id}/contract/acknowledge")
def acknowledge_contract(
    placement_id: UUID, session: SessionDep, staff: StaffDep,
    party: str = "worker",
):
    try:
        acknowledge(session, placement_id, party)
    except ContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": placement_id, "acknowledged_by": party}


@router.get("/contracts/unacknowledged")
def pending_contracts(session: SessionDep, staff: StaffDep):
    """Contracts one side has not confirmed seeing."""
    return {"unacknowledged": unacknowledged(session)}


@router.get("/guarantees/open")
def guarantees(session: SessionDep, staff: StaffDep):
    return {"open": open_guarantees(session)}


@router.get("/follow-ups/due")
def follow_ups(session: SessionDep, staff: StaffDep,
               as_of: date | None = None):
    return {"due": due_follow_ups(session, as_of or date.today())}


@router.post("/follow-ups/{follow_up_id}/complete")
def complete(follow_up_id: UUID, body: CompleteFollowUp, session: SessionDep, staff: StaffDep):
    complete_follow_up(
        session,
        follow_up_id,
        body.still_working,
        body.worker_rating,
        body.employer_rating,
        body.issue_flag,
        body.notes,
    )
    return {"follow_up_id": follow_up_id, "completed": True}


@router.get("/metrics/scorecard")
def scorecard(session: SessionDep, staff: StaffDep):
    row = session.execute(text("SELECT * FROM v_pilot_scorecard")).mappings().one()
    return dict(row)
