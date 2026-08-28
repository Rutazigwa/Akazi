"""Which employers are worth having, on the evidence.

Every fact needed was already recorded and none of it was grouped by employer.
Retention is measured per worker, guarantee invocations per placement, pay
accuracy per pay record -- so the question the whole operation turns on could
not be asked at all.

It is a question only an operator carrying the guarantee can ask. The fee
includes covering a shift when somebody does not arrive, so an employer whose
shifts repeatedly go uncovered is being subsidised by the ones whose do not.
A competitor that ends its responsibility at the introduction never pays that
cost and never needs the number.

**Findings, not a score.** "Employer quality 62/100" cannot be read to anyone
or acted on. "Three different workers did not arrive here, and two of the four
we asked had left within a month" tells an owner what conversation to have,
and gives the employer something they can answer.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# One worker failing to arrive says nothing about the employer -- people have
# bad days. Several different ones at the same site is a pattern about the
# place: unpaid, unreachable, or unpleasant on arrival.
DISTINCT_NO_SHOWS_BEFORE_CONCERN = 3

# Below this, people are leaving faster than the business can place them, and
# the placements are costing more to make than they return.
POOR_RETENTION = 0.5

# Enough placements that a proportion means anything. Under this, the counts
# are shown and no finding is drawn from them.
ENOUGH_TO_JUDGE = 4


def employer_health(session: Session, employer_id: UUID | None = None) -> list[dict]:
    """Per-employer evidence with plain findings attached."""
    rows = session.execute(
        text(
            """
            SELECT * FROM v_employer_reliability
             WHERE (CAST(:eid AS uuid) IS NULL
                    OR employer_id = CAST(:eid AS uuid))
             ORDER BY placements DESC, business_name
            """
        ),
        {"eid": str(employer_id) if employer_id else None},
    ).mappings()
    return [_with_findings(dict(row)) for row in rows]


def _with_findings(row: dict) -> dict:
    findings: list[str] = []

    if row["workers_who_did_not_arrive"] >= DISTINCT_NO_SHOWS_BEFORE_CONCERN:
        findings.append(
            f"{row['workers_who_did_not_arrive']} different workers did not "
            "arrive here — worth asking what happens on the first morning"
        )

    unfilled = row["guarantee_invocations"] - row["guarantee_filled_in_24h"]
    if unfilled > 0:
        findings.append(
            f"{unfilled} of {row['guarantee_invocations']} guarantee "
            "invocation(s) were not covered inside 24 hours — the promise we "
            "charge for"
        )

    if row["checked_at_30_days"] >= ENOUGH_TO_JUDGE:
        stayed = row["still_there_at_30_days"] / row["checked_at_30_days"]
        if stayed < POOR_RETENTION:
            findings.append(
                f"only {row['still_there_at_30_days']} of "
                f"{row['checked_at_30_days']} were still there at 30 days"
            )

    if row["pay_records"] >= ENOUGH_TO_JUDGE:
        correct = row["paid_correctly"] / row["pay_records"]
        if correct < 0.95:
            findings.append(
                f"{row['pay_records'] - row['paid_correctly']} of "
                f"{row['pay_records']} pay periods were not paid in full on "
                "the agreed date"
            )

    if row["felt_unsafe"] > 0:
        findings.append(
            f"{row['felt_unsafe']} worker(s) said they did not feel safe here"
        )

    row["findings"] = findings
    # A cooperative is pre-aggregated demand -- one relationship worth fifteen
    # SME ones -- so a problem there is worth more attention, not less.
    row["priority"] = len(findings) + (1 if row["is_cooperative"] and findings else 0)
    return row


def employers_needing_a_conversation(session: Session) -> list[dict]:
    """Only those with something to answer for, most first."""
    return sorted(
        (e for e in employer_health(session) if e["findings"]),
        key=lambda e: (-e["priority"], -e["placements"]),
    )
