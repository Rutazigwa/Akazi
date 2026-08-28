"""What workers say about an employer, and who is allowed to read it.

The employer rates the worker and that rating is shown to them. The worker
rates the employer at follow-up and, until now, nothing ever read it back.
That asymmetry is the power imbalance the blueprint asks this business to
correct, and it matters most for the group it names: the blueprint lists
"employer safety ratings written by women who worked there" as a product
requirement rather than a reporting line.

A woman weighing a shift that finishes after dark at an employer she has never
worked for is making a safety judgement with no information. Somebody else
already has that information.

**Coordinator-facing only.** An employer must never see this, in aggregate or
otherwise: told that one of the two women who worked there did not feel safe,
they know exactly who said it, and the consequence lands on her. There is no
threshold that makes it safe to show an employer their own safety reports, so
this module offers no way to.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

CONCERNS = ("harassment", "unsafe_equipment", "unsafe_hours",
            "transport_after_dark", "pressure_to_work_unpaid", "pay", "other")

# A concern at or above this share of women reporting turns the employer's
# entry into a warning rather than a note. Deliberately not a block: refusing
# to place anyone with an employer is a commercial decision for the owner, and
# a threshold invented here would make it silently, on evidence a coordinator
# never saw. The warning puts it in front of a person instead.
WARN_AT_UNSAFE_SHARE = 0.34


class SafetyReportError(Exception):
    """A safety report that cannot be recorded as given."""


def record_safety_report(
    session: Session,
    *,
    placement_id: UUID,
    felt_safe: bool,
    would_return: bool | None = None,
    concern: str | None = None,
    note: str | None = None,
    recorded_by: UUID | None = None,
) -> UUID:
    """Record what a worker says about the employer she worked for.

    Asked again at week 1 and day 30; the later answer replaces the earlier,
    because what she thinks now is the thing worth knowing.
    """
    if concern is not None and concern not in CONCERNS:
        raise SafetyReportError(f"concern must be one of {CONCERNS}")
    if not felt_safe and concern is None:
        raise SafetyReportError(
            "a worker who did not feel safe should say what the concern was -- "
            "an unexplained flag cannot be acted on"
        )

    row = session.execute(
        text(
            """
            SELECT p.candidate_id, wr.employer_id
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
             WHERE p.placement_id = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()
    if row is None:
        raise SafetyReportError(f"no such placement {placement_id}")

    report_id = session.execute(
        text(
            """
            INSERT INTO employer_safety_reports
                (employer_id, candidate_id, placement_id, felt_safe,
                 would_return, concern, note, recorded_by)
            VALUES (:eid, :cid, :pid, :safe, :ret, :concern, :note, :by)
            ON CONFLICT (employer_id, candidate_id) DO UPDATE
                SET felt_safe    = EXCLUDED.felt_safe,
                    would_return = EXCLUDED.would_return,
                    concern      = EXCLUDED.concern,
                    note         = EXCLUDED.note,
                    placement_id = EXCLUDED.placement_id,
                    recorded_by  = EXCLUDED.recorded_by,
                    reported_at  = clock_timestamp()
            RETURNING report_id
            """
        ),
        {
            "eid": str(row["employer_id"]), "cid": str(row["candidate_id"]),
            "pid": str(placement_id), "safe": felt_safe, "ret": would_return,
            "concern": concern, "note": note,
            "by": str(recorded_by) if recorded_by else None,
        },
    ).scalar_one()

    # A worker reporting harassment is not only a data point. The escalation
    # path exists precisely so this does not sit in a table waiting to be
    # noticed by whoever next reads a report.
    if concern == "harassment":
        from app.operations.escalations import raise_escalation

        raise_escalation(
            session, "harassment",
            candidate_id=row["candidate_id"],
            placement_id=placement_id,
            detail=(
                "reported at a follow-up check-in about this employer"
                + (f": {note}" if note else "")
            ),
        )

    return report_id


def employer_safety(session: Session, employer_id: UUID) -> dict | None:
    """What has been said about one employer. Coordinator-facing."""
    row = session.execute(
        text("SELECT * FROM v_employer_safety WHERE employer_id = :eid"),
        {"eid": str(employer_id)},
    ).mappings().first()
    if row is None:
        return None

    record = dict(row)
    record["warn"] = (
        record["reports_women"] > 0
        and record["felt_unsafe_women"] / record["reports_women"]
        >= WARN_AT_UNSAFE_SHARE
    )
    record["summary"] = _summarise(record)
    return record


def _summarise(record: dict) -> str:
    """A sentence a coordinator can act on, not a score."""
    if record["reports_women"]:
        safe = record["felt_safe_women"]
        total = record["reports_women"]
        line = f"{safe} of {total} women who worked here felt safe"
    else:
        line = (
            f"{record['felt_safe']} of {record['reports']} workers felt safe; "
            "no woman has worked here yet"
        )
    if record["concerns"]:
        line += " — raised: " + ", ".join(
            c.replace("_", " ") for c in sorted(record["concerns"])
        )
    return line


def employers_of_concern(session: Session) -> list[dict]:
    """Employers whose own workers have flagged them, worst first.

    For the owner rather than the coordinator: deciding whether to keep
    trading with someone is not a shift-by-shift call.
    """
    rows = session.execute(
        text(
            """
            SELECT * FROM v_employer_safety
             WHERE felt_unsafe > 0
             ORDER BY felt_unsafe_women DESC, felt_unsafe DESC
            """
        )
    ).mappings()
    out = []
    for row in rows:
        record = dict(row)
        record["warn"] = (
            record["reports_women"] > 0
            and record["felt_unsafe_women"] / record["reports_women"]
            >= WARN_AT_UNSAFE_SHARE
        )
        record["summary"] = _summarise(record)
        out.append(record)
    return out
