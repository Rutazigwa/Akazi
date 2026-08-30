"""Outcome reporting for the national Labour Market Information System.

The Cabinet-approved LMIS consolidates placement, internship and apprenticeship
outcomes from government, the private sector and training providers. We generate
exactly that, structured and timestamped. Supplying it is a contract and it is
political cover -- the alternative is competing with a public utility.

**Nothing identifying leaves this module.** No names, no national IDs, no dates
of birth, no phone numbers, no home coordinates, and no candidate_id -- the
surrogate key is stable across exports, so publishing it would let anyone
holding two exports track an individual between them.

What goes out is counts, grouped. Small groups are suppressed: a single female
placement in one sector of one district is not anonymous to anyone who works
there, and "aggregate" is not a synonym for "safe". Suppression is applied
before the numbers leave, not left to whoever receives them.

Individual-level reporting -- if the LMIS ever asks for it -- needs consent for
the 'reporting' purpose, which is tracked separately from placement consent
precisely so that agreeing to be placed is not taken as agreeing to be
reported on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

# Groups smaller than this are reported as suppressed rather than counted. A
# cell of one or two re-identifies people in a small district, whatever the
# column headings say.
MIN_CELL = 5
SUPPRESSED = "<5"


class LMISError(Exception):
    pass


@dataclass(frozen=True)
class ReportWindow:
    """A whole number of calendar months, and nothing else.

    Cell suppression does not survive a caller who chooses the window. With
    arbitrary dates, two windows one day apart can be differenced:

        end date    placements published
        day  4          <5
        day  5           5      -> exactly one placement on day 5
        day  6           6      -> exactly one placement on day 6

    Demonstrated on nine placements in one sector, district and work type.
    Suppression protected the first four and nothing after: once the cumulative
    count crosses MIN_CELL, every subsequent day is recoverable by subtracting
    consecutive windows, giving an exact per-day count at a granularity this
    module's own docstring says is not anonymous -- "a single female placement
    in one sector of one district is not anonymous to anyone who works there".

    Aligning to whole months closes it. Any two aligned windows differ by whole
    months, and each of those is a period that could have been reported on its
    own -- so the difference tells an attacker nothing a legitimate request
    would not have. It is also the shape a national statistics feed wants:
    monthly and quarterly submissions, not arbitrary slices.
    """

    starts_on: date
    ends_on: date

    def __post_init__(self) -> None:
        if self.ends_on < self.starts_on:
            raise LMISError("the reporting window cannot end before it starts")

        if self.starts_on.day != 1:
            raise LMISError(
                f"a reporting window starts on the first of a month; "
                f"{self.starts_on} is not. Suppression only holds when windows "
                f"cannot be slid past each other a day at a time."
            )

        if self.ends_on != _last_day_of_month(self.ends_on):
            raise LMISError(
                f"a reporting window ends on the last day of a month; "
                f"{self.ends_on} is not. Suppression only holds when windows "
                f"cannot be slid past each other a day at a time."
            )

        # And the month has to be over. A window nominally ending on the 31st,
        # requested on the 12th, covers a month that is still filling up -- so
        # the same export run twice a day apart differs by exactly one day's
        # placements, which is the attack again with the dates hidden.
        from app.clock import kigali_today

        today = kigali_today()
        if self.ends_on >= date(today.year, today.month, 1):
            raise LMISError(
                f"the month ending {self.ends_on} is not over yet. A report "
                f"covering a month still in progress changes every day, and "
                f"two runs of it can be subtracted from each other."
            )


def _last_day_of_month(day: date) -> date:
    first_next = (
        date(day.year + 1, 1, 1) if day.month == 12
        else date(day.year, day.month + 1, 1)
    )
    return first_next - timedelta(days=1)


def whole_months_ending_before(today: date, months: int) -> ReportWindow:
    """The last `months` complete calendar months before today.

    The current month is deliberately excluded: a window ending today would
    move every day, which is the differencing attack with extra steps.
    """
    end = date(today.year, today.month, 1) - timedelta(days=1)
    start = end
    for _ in range(months - 1):
        start = date(start.year, start.month, 1) - timedelta(days=1)
    return ReportWindow(starts_on=date(start.year, start.month, 1), ends_on=end)


def _suppress(value: int | None) -> int | str | None:
    if value is None:
        return None
    return value if value >= MIN_CELL else SUPPRESSED


def placement_outcomes(session: Session, window: ReportWindow) -> list[dict]:
    """Placements by sector, district and work type, with retention.

    Grouped, never listed. The retention figure counts only answered day-30
    check-ins -- an unanswered one is missing data, not a failure, and reporting
    it as a failure to a national system would understate the sector.
    """
    rows = session.execute(
        text(
            """
            SELECT e.sector,
                   e.district,
                   wr.work_type::text AS work_type,
                   count(*)                                    AS placements,
                   count(*) FILTER (WHERE c.gender = 'F')       AS women,
                   count(*) FILTER (WHERE p.status = 'completed') AS completed,
                   count(r.still_working)                       AS day_30_answered,
                   count(*) FILTER (WHERE r.still_working)      AS day_30_retained
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
              JOIN candidates c     ON c.candidate_id = p.candidate_id
              LEFT JOIN LATERAL (
                  SELECT f.still_working FROM follow_ups f
                   WHERE f.placement_id = p.placement_id
                     AND f.checkpoint = 'day_30'
                     AND f.completed_at IS NOT NULL
              ) r ON true
             WHERE p.status IN ('active','completed','no_show','terminated')
               AND p.offered_at::date BETWEEN :starts AND :ends
             GROUP BY e.sector, e.district, wr.work_type
             ORDER BY e.sector, e.district, wr.work_type
            """
        ),
        {"starts": window.starts_on, "ends": window.ends_on},
    ).mappings()

    return [
        {
            "sector": r["sector"],
            "district": r["district"],
            "work_type": r["work_type"],
            "placements": _suppress(r["placements"]),
            "women": _suppress(r["women"]),
            "completed": _suppress(r["completed"]),
            "day_30_answered": _suppress(r["day_30_answered"]),
            "day_30_retained": _suppress(r["day_30_retained"]),
        }
        for r in rows
    ]


def summary(session: Session, window: ReportWindow) -> dict:
    """Headline totals for the window.

    Totals are not suppressed: a national figure covering every district does
    not identify anyone, and suppressing it would make the report useless.
    Suppression belongs on the cells that slice down to a handful of people.
    """
    row = session.execute(
        text(
            """
            SELECT count(*)                                       AS placements,
                   count(DISTINCT p.candidate_id)                 AS individuals,
                   count(DISTINCT wr.employer_id)                 AS employers,
                   count(*) FILTER (WHERE c.gender = 'F')         AS women,
                   count(*) FILTER (WHERE e.is_cooperative)       AS via_cooperatives,
                   ROUND(avg(p.agreed_pay_rwf) FILTER
                         (WHERE p.pay_unit = 'day'))              AS mean_daily_pay_rwf,
                   ROUND(avg(p.est_transport_rwf) FILTER
                         (WHERE p.pay_unit = 'day'))              AS mean_daily_transport_rwf
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
              JOIN candidates c     ON c.candidate_id = p.candidate_id
             WHERE p.status IN ('active','completed','no_show','terminated')
               AND p.offered_at::date BETWEEN :starts AND :ends
            """
        ),
        {"starts": window.starts_on, "ends": window.ends_on},
    ).mappings().one()

    result = dict(row)
    # Net earnings after transport is the figure no competitor publishes, and
    # the one the LMIS has no other source for.
    if result["mean_daily_pay_rwf"] and result["mean_daily_transport_rwf"] is not None:
        result["mean_net_daily_rwf"] = (
            result["mean_daily_pay_rwf"] - result["mean_daily_transport_rwf"]
        )
    else:
        result["mean_net_daily_rwf"] = None
    return result


def build_report(session: Session, window: ReportWindow) -> dict:
    """The whole submission, ready to hand over."""
    from datetime import datetime, timezone

    return {
        "source": "Akazi",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "starts_on": window.starts_on.isoformat(),
            "ends_on": window.ends_on.isoformat(),
        },
        "disclosure_control": {
            "min_cell_size": MIN_CELL,
            "suppressed_as": SUPPRESSED,
            "note": (
                "Counts below the minimum cell size are suppressed. No "
                "individual identifiers, including internal record ids, are "
                "included in this report."
            ),
        },
        "summary": summary(session, window),
        "outcomes": placement_outcomes(session, window),
    }


def reporting_consent_counts(session: Session) -> dict:
    """How many candidates have agreed to be reported on individually.

    Tracked separately from placement consent on purpose: agreeing to be placed
    is not agreeing to appear in a national dataset. If the LMIS ever asks for
    record-level data, this is the population that could lawfully be included --
    and it will be smaller than the placement count.
    """
    row = session.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE granted)     AS granted,
                   count(*) FILTER (WHERE NOT granted) AS refused
              FROM v_current_consent
             WHERE purpose = 'reporting'
            """
        )
    ).mappings().one()
    return dict(row)


def to_csv(outcomes: list[dict]) -> str:
    """The grouped rows as CSV, which is what statistical offices actually want."""
    import csv
    import io

    buffer = io.StringIO()
    fields = [
        "sector", "district", "work_type", "placements", "women",
        "completed", "day_30_answered", "day_30_retained",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in outcomes:
        writer.writerow(row)
    return buffer.getvalue()
