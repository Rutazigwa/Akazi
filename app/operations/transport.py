"""What the commute actually cost, asked of the worker.

The fare model has always described itself as a placeholder awaiting real
receipts. Nothing collected any, and two load-bearing things rested on the
guess: matching filter 2, which refuses a placement when transport exceeds 30%
of daily pay, and "net earnings after transport", a headline pilot metric that
was being derived from straight-line distance and reported as though measured.

Asked at the day-1 follow-up, where a coordinator is already on the telephone
and the journey is fresh.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


class TransportReportError(Exception):
    """A reported fare that cannot be recorded as given."""


# A day's round trip above this is not a commute, it is a mistake in the
# units -- almost always a monthly figure, or a one-off airport run. Refused
# rather than silently absorbed, because the median it would feed decides who
# gets offered work.
IMPLAUSIBLE_DAILY_RWF = 20_000


def record_transport_report(
    session: Session,
    *,
    placement_id: UUID,
    reported_rwf: int,
    work_date: date | None = None,
    reported_min: int | None = None,
    source: str = "follow_up",
    note: str | None = None,
    recorded_by: UUID | None = None,
) -> UUID:
    """Record what a worker says the day's travel cost, both legs.

    The estimate we made for this placement is stored alongside it, so the
    calibration ratio can be computed later without re-deriving geometry that
    may have changed in the meantime.
    """
    if reported_rwf < 0:
        raise TransportReportError("a fare cannot be negative")
    if reported_rwf > IMPLAUSIBLE_DAILY_RWF:
        raise TransportReportError(
            f"RWF {reported_rwf:,} for one day's travel is implausible -- "
            "check whether this is a monthly or one-way figure"
        )

    placement = session.execute(
        text(
            """
            SELECT p.candidate_id, wr.employer_id, p.est_transport_rwf,
                   COALESCE(p.started_on, wr.starts_on) AS default_date
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
             WHERE p.placement_id = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()
    if placement is None:
        raise TransportReportError(f"no such placement {placement_id}")

    return session.execute(
        text(
            """
            INSERT INTO transport_reports
                (candidate_id, employer_id, placement_id, work_date,
                 reported_rwf, reported_min, estimated_rwf, source, note,
                 recorded_by)
            VALUES (:cid, :eid, :pid, :work_date, :rwf, :minutes, :estimated,
                    :source, :note, :by)
            ON CONFLICT (candidate_id, employer_id, work_date) DO UPDATE
                SET reported_rwf = EXCLUDED.reported_rwf,
                    reported_min = EXCLUDED.reported_min,
                    note         = EXCLUDED.note,
                    recorded_by  = EXCLUDED.recorded_by,
                    recorded_at  = clock_timestamp()
            RETURNING report_id
            """
        ),
        {
            "cid": str(placement["candidate_id"]),
            "eid": str(placement["employer_id"]),
            "pid": str(placement_id),
            "work_date": work_date or placement["default_date"],
            "rwf": reported_rwf,
            "minutes": reported_min,
            "estimated": placement["est_transport_rwf"],
            "source": source,
            "note": note,
            "by": str(recorded_by) if recorded_by else None,
        },
    ).scalar_one()


def calibration(session: Session) -> dict:
    """How wrong the straight-line model is, on the evidence so far."""
    row = session.execute(
        text("SELECT reports, factor, raw_factor FROM v_transport_calibration")
    ).mappings().first()
    return {
        "reports": row["reports"],
        "factor": float(row["factor"]),
        "raw_factor": float(row["raw_factor"]),
    }


def route_history(session: Session, placement_id: UUID) -> list[dict]:
    """Every fare reported for this worker and employer, newest first."""
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT tr.work_date, tr.reported_rwf, tr.reported_min,
                       tr.estimated_rwf, tr.source, tr.note,
                       s.full_name AS recorded_by_name
                  FROM transport_reports tr
                  LEFT JOIN staff s ON s.staff_id = tr.recorded_by
                 WHERE (tr.candidate_id, tr.employer_id) = (
                        SELECT p.candidate_id, wr.employer_id
                          FROM placements p
                          JOIN work_requests wr ON wr.request_id = p.request_id
                         WHERE p.placement_id = :pid)
                 ORDER BY tr.work_date DESC
                """
            ),
            {"pid": str(placement_id)},
        ).mappings()
    ]
