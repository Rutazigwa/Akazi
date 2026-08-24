"""Pay recording endpoints. Records of claims -- no money moves here."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.deps import SessionDep, StaffDep
from app.operations.pay import (
    PayError,
    confirm_with_worker,
    mark_paid,
    overdue_pay,
    pay_records_for_placement,
    record_pay_period,
    suggest_pay_period,
)

router = APIRouter(tags=["pay"])


class NewPayPeriod(BaseModel):
    period_start: date
    period_end: date
    gross_rwf: int = Field(gt=0)
    due_on: date
    deductions_rwf: int = Field(default=0, ge=0)
    method: str | None = Field(default=None, pattern="^(momo|cash|bank)$")


class MarkPaid(BaseModel):
    paid_on: date
    method: str | None = Field(default=None, pattern="^(momo|cash|bank)$")


class WorkerConfirmation(BaseModel):
    received_in_full: bool
    note: str | None = None


@router.get("/placements/{placement_id}/pay")
def list_pay(placement_id: UUID, session: SessionDep, staff: StaffDep):
    return {
        "pay_records": pay_records_for_placement(session, placement_id),
        "suggestion": suggest_pay_period(session, placement_id),
    }


@router.post("/placements/{placement_id}/pay", status_code=201)
def create_pay_period(
    placement_id: UUID, body: NewPayPeriod, session: SessionDep, staff: StaffDep
):
    try:
        pay_id = record_pay_period(
            session, placement_id,
            body.period_start, body.period_end, body.gross_rwf, body.due_on,
            body.deductions_rwf, body.method,
        )
    except PayError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"pay_id": pay_id}


@router.post("/pay/{pay_id}/paid")
def paid(pay_id: UUID, body: MarkPaid, session: SessionDep, staff: StaffDep):
    try:
        mark_paid(session, pay_id, body.paid_on, body.method)
    except PayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"pay_id": pay_id, "paid_on": body.paid_on}


@router.post("/pay/{pay_id}/worker-confirmation")
def worker_confirmation(
    pay_id: UUID, body: WorkerConfirmation, session: SessionDep, staff: StaffDep
):
    """The worker's answer from the follow-up call.

    A shortfall raises a pay escalation in the same call -- the response says
    so, so the coordinator on the phone knows the clock has started.
    """
    try:
        escalation_id = confirm_with_worker(
            session, pay_id, body.received_in_full, body.note
        )
    except PayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "pay_id": pay_id,
        "received_in_full": body.received_in_full,
        "escalation_raised": escalation_id,
    }


@router.get("/pay/overdue")
def overdue(session: SessionDep, staff: StaffDep, as_of: date | None = None):
    """The chase list: pay past its agreed date with nothing recorded."""
    return {"overdue": overdue_pay(session, as_of)}
