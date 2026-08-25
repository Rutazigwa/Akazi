"""LMIS outcome reporting endpoints.

Restricted to owner and admin. Not because the numbers are sensitive -- they
are aggregated and suppressed -- but because handing a dataset to a national
system is an act with consequences, and it should be a decision someone with
standing makes rather than anyone with a login.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Response

from app.deps import AdminDep, SessionDep
from app.operations.lmis import (
    LMISError,
    ReportWindow,
    build_report,
    reporting_consent_counts,
    to_csv,
)

router = APIRouter(prefix="/lmis", tags=["lmis"])


def _window(starts_on: date, ends_on: date) -> ReportWindow:
    try:
        return ReportWindow(starts_on=starts_on, ends_on=ends_on)
    except LMISError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/report")
def report(
    starts_on: date, ends_on: date, session: SessionDep, admin: AdminDep
):
    """The submission: summary plus grouped outcomes, disclosure-controlled."""
    return build_report(session, _window(starts_on, ends_on))


@router.get("/report.csv")
def report_csv(
    starts_on: date, ends_on: date, session: SessionDep, admin: AdminDep
):
    """The grouped rows as CSV, which is what statistical offices ask for."""
    payload = build_report(session, _window(starts_on, ends_on))
    return Response(
        content=to_csv(payload["outcomes"]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="akazi-outcomes-'
                f'{starts_on}-{ends_on}.csv"'
            )
        },
    )


@router.get("/reporting-consent")
def consent(session: SessionDep, admin: AdminDep):
    """How many candidates could lawfully appear in a record-level dataset."""
    return reporting_consent_counts(session)
