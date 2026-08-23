"""Coordinator-facing operations endpoints.

Internal admin only -- coordinators and the owner. There is no authentication
here yet, so this must not be exposed beyond localhost or a trusted network
until it exists. The X-Staff-Id header is an attribution mechanism, not an
authentication one: it stamps the audit trail, and it is trivially forgeable.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import session_scope
from app.operations.attendance import (
    AttendanceError,
    log_attendance,
    open_guarantees,
    record_replacement,
    start_placement,
)
from app.operations.follow_ups import complete_follow_up, due_follow_ups

router = APIRouter(tags=["operations"])

StaffId = Annotated[UUID, Header(alias="X-Staff-Id")]


def get_session(staff_id: StaffId):
    with session_scope(staff_id=staff_id) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


class StartPlacement(BaseModel):
    started_on: date


class LogAttendance(BaseModel):
    work_date: date
    present: bool
    confirmed_by: str = Field(pattern="^(employer|coordinator|worker)$")
    hours_worked: float | None = None
    absence_reason: str | None = None


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
def start(placement_id: UUID, body: StartPlacement, session: SessionDep):
    try:
        schedule = start_placement(session, placement_id, body.started_on)
    except AttendanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"placement_id": placement_id, "follow_ups": schedule}


@router.post("/placements/{placement_id}/attendance")
def attendance(placement_id: UUID, body: LogAttendance, session: SessionDep):
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
def replacement(placement_id: UUID, body: RecordReplacement, session: SessionDep):
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


@router.get("/guarantees/open")
def guarantees(session: SessionDep):
    return {"open": open_guarantees(session)}


@router.get("/follow-ups/due")
def follow_ups(session: SessionDep, as_of: date | None = None):
    return {"due": due_follow_ups(session, as_of or date.today())}


@router.post("/follow-ups/{follow_up_id}/complete")
def complete(follow_up_id: UUID, body: CompleteFollowUp, session: SessionDep):
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
def scorecard(session: SessionDep):
    row = session.execute(text("SELECT * FROM v_pilot_scorecard")).mappings().one()
    return dict(row)
