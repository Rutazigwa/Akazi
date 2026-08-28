"""What a follow-up call collects beyond "still working".

Both of these were reachable only through the browser form. The API could not
record either, which matters for the same reason the database holds the rules:
the web UI is not the only thing that will ever write here. A bulk import of
paper follow-up sheets, or anything that comes after the candidate app, needs
a way in -- and a capability that exists in one surface and not the other is
a divergence that grows.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.deps import SessionDep, StaffDep
from app.operations.safety import (
    CONCERNS,
    SafetyReportError,
    employer_safety,
    record_safety_report,
)
from app.operations.transport import (
    TransportReportError,
    calibration,
    record_transport_report,
    route_history,
)

router = APIRouter(tags=["follow-up reports"])


class TransportReport(BaseModel):
    reported_rwf: int = Field(ge=0)
    reported_min: int | None = Field(default=None, ge=0)
    work_date: date | None = None
    note: str | None = None
    source: str = Field(default="follow_up",
                        pattern="^(follow_up|coordinator|inbound)$")


class SafetyReport(BaseModel):
    felt_safe: bool
    would_return: bool | None = None
    concern: str | None = Field(default=None,
                                pattern="^(" + "|".join(CONCERNS) + ")$")
    note: str | None = None


@router.post("/placements/{placement_id}/transport", status_code=201)
def add_transport_report(
    placement_id: UUID, body: TransportReport, session: SessionDep,
    staff: StaffDep,
):
    """What the day's travel actually cost, both legs."""
    try:
        report_id = record_transport_report(
            session, placement_id=placement_id,
            reported_rwf=body.reported_rwf, reported_min=body.reported_min,
            work_date=body.work_date, note=body.note, source=body.source,
            recorded_by=staff.staff_id,
        )
    except TransportReportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"report_id": report_id}


@router.get("/placements/{placement_id}/transport")
def get_transport_reports(
    placement_id: UUID, session: SessionDep, staff: StaffDep
):
    return {
        "reports": route_history(session, placement_id),
        "calibration": calibration(session),
    }


@router.post("/placements/{placement_id}/safety", status_code=201)
def add_safety_report(
    placement_id: UUID, body: SafetyReport, session: SessionDep,
    staff: StaffDep,
):
    """What a worker says about the employer she worked for."""
    try:
        report_id = record_safety_report(
            session, placement_id=placement_id, felt_safe=body.felt_safe,
            would_return=body.would_return, concern=body.concern,
            note=body.note, recorded_by=staff.staff_id,
        )
    except SafetyReportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"report_id": report_id}


@router.get("/employers/{employer_id}/safety")
def get_employer_safety(
    employer_id: UUID, session: SessionDep, staff: StaffDep
):
    """Coordinator-facing only.

    There is no employer-facing counterpart and there must not be: an employer
    told that one of two women did not feel safe knows exactly who said it.
    See migration 041.
    """
    record = employer_safety(session, employer_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="nobody has reported on this employer yet",
        )
    return record
