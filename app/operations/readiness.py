"""Tomorrow's shifts, and what could stop each one happening.

Every other view on the dashboard reports something that has already gone
wrong: an escalation raised, pay overdue, a guarantee clock running. Each
matters, and each arrives too late to prevent the thing it describes.

The guarantee is priced into the placement fee, so an invocation costs real
money and the cheapest one is the one that never happens. What makes a no-show
predictable the evening before is not a mystery and does not need a model:
the worker never accepted, the reminder never reached them, or the shift has
nobody assigned to it at all.

No score, no weighting, no ranking. A list of concrete flags a coordinator can
act on, each one a fact with an obvious remedy -- the same reason matching
uses sequential filters and surfaces its reason. "Risk 0.72" tells a
coordinator nothing about what to do next.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.clock import kigali_today

# Above this share of daily pay, transport is the blueprint's stated cause of
# week-two dropout. It is a standing fact about the placement rather than
# something that went wrong today, so it is worth seeing before the first day.
TRANSPORT_HEAVY_PCT = 30


def shifts_on(session: Session, day: date | None = None) -> list[dict]:
    """Placements due to start on a given day, with what is unresolved.

    Ordered by how much is unresolved, so the shift most likely to fail is
    read first rather than found by scrolling.
    """
    day = day or kigali_today() + timedelta(days=1)

    rows = session.execute(
        text(
            """
            SELECT p.placement_id,
                   p.status::text                      AS status,
                   c.candidate_id,
                   c.display_name,
                   c.has_smartphone,
                   e.business_name,
                   wr.request_id,
                   wr.title,
                   wr.shift_start,
                   wr.shift_end,
                   wr.transport_covered,
                   np.transport_pct,
                   np.net_daily_rwf,
                   -- Has this person worked for this employer before? A first
                   -- day is where the arrival risk actually sits.
                   (SELECT count(*) FROM placements prior
                      JOIN work_requests pwr
                        ON pwr.request_id = prior.request_id
                     WHERE prior.candidate_id = p.candidate_id
                       AND pwr.employer_id = wr.employer_id
                       AND prior.status = 'completed')  AS prior_completed,
                   -- The reminder for this placement, if one was queued.
                   rem.status                           AS reminder_status,
                   rem.delivered_at                     AS reminder_delivered_at
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
              JOIN candidates c     ON c.candidate_id = p.candidate_id
              LEFT JOIN v_placement_net_pay np
                     ON np.placement_id = p.placement_id
              LEFT JOIN LATERAL (
                    SELECT m.status::text AS status, m.delivered_at
                      FROM messages m
                     WHERE m.placement_id = p.placement_id
                       AND m.template_key = 'shift_reminder'
                     ORDER BY m.created_at DESC
                     LIMIT 1
              ) rem ON TRUE
             WHERE COALESCE(p.started_on, wr.starts_on) = :day
               AND p.status IN ('offered', 'accepted', 'active')
             ORDER BY wr.shift_start NULLS LAST, e.business_name
            """
        ),
        {"day": day},
    ).mappings()

    return [_with_flags(dict(row)) for row in rows]


def _with_flags(row: dict) -> dict:
    """Attach the concerns, each phrased as what a coordinator would do."""
    flags: list[str] = []

    if row["status"] == "offered":
        flags.append("has not accepted yet — confirm they are coming")

    reminder = row["reminder_status"]
    if reminder is None:
        flags.append("no reminder queued")
    elif reminder == "failed":
        flags.append("reminder failed to send — call them")
    elif reminder in ("queued", "sending"):
        flags.append("reminder not sent yet")
    elif reminder == "sent" and row["reminder_delivered_at"] is None:
        flags.append("reminder sent but not confirmed delivered")

    if not row["has_smartphone"]:
        flags.append("no smartphone — WhatsApp will not reach them")

    pct = row["transport_pct"]
    if (
        pct is not None
        and pct > TRANSPORT_HEAVY_PCT
        and not row["transport_covered"]
    ):
        flags.append(f"transport is {pct:.0f}% of pay")

    if row["prior_completed"] == 0:
        flags.append("first shift with this employer")

    row["flags"] = flags
    return row


def unstaffed_shifts_on(session: Session, day: date | None = None) -> list[dict]:
    """Requests starting on a day with fewer people assigned than asked for.

    The worst case, and the one no other view shows: a shift nobody is going
    to turn up to, because nobody was ever assigned. The guarantee does not
    cover a slot that was never filled -- that is simply a shift we failed to
    staff, and the employer finds out on the day.
    """
    day = day or kigali_today() + timedelta(days=1)

    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT wr.request_id, wr.title, wr.headcount,
                       wr.shift_start, wr.shift_end, e.business_name,
                       count(p.placement_id) FILTER (
                           WHERE p.status IN ('offered','accepted','active')
                       ) AS assigned,
                       wr.headcount - count(p.placement_id) FILTER (
                           WHERE p.status IN ('offered','accepted','active')
                       ) AS short_by
                  FROM work_requests wr
                  JOIN employers e ON e.employer_id = wr.employer_id
                  LEFT JOIN placements p ON p.request_id = wr.request_id
                 WHERE wr.starts_on = :day
                   AND wr.status IN ('open', 'filling')
                 GROUP BY wr.request_id, wr.title, wr.headcount,
                          wr.shift_start, wr.shift_end, e.business_name
                HAVING wr.headcount > count(p.placement_id) FILTER (
                           WHERE p.status IN ('offered','accepted','active')
                       )
                 ORDER BY short_by DESC, wr.shift_start NULLS LAST
                """
            ),
            {"day": day},
        ).mappings()
    ]
