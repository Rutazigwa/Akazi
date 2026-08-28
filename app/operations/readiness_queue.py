"""Who is in the registry and not working, and what would fix it.

The matcher explains why each candidate was excluded from each request. It
cannot show the person excluded from *every* request, always for the same
reason, whom nobody has looked at. Individually each rejection is explained;
in aggregate the registry is silent.

At pilot volume that is a handful of people. Against 928,426 NEET youth it is
the whole question -- and the failure is invisible by construction, because
somebody who never matches never appears on a match page.

**Every blocker here is fixable in a phone call.** Capture a location, record
availability, score one assessment, take consent. That is why they are listed
in the order a coordinator should work through them, rather than counted.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

# Registered this long ago with no offer is not bad luck. It is either a
# blocker nobody noticed or a district where we have found no employers, and
# the two need different answers.
STALE_AFTER_DAYS = 21


def _blockers(row: dict, today: date) -> list[str]:
    """What is stopping this person, most disqualifying first."""
    found: list[str] = []

    if not row["age_eligible"]:
        # A row for someone under sixteen cannot exist -- chk_minimum_age
        # refuses it -- and erased records are already filtered out by status.
        # So this is not "too young", it is "we could not establish an age",
        # which is a data problem to investigate rather than a call to make.
        # The engine takes the same position: unknown means excluded, because
        # the failure mode of guessing is placing a child.
        found.append(
            "age could not be established from the identity record — "
            "matching excludes them until it is"
        )
        return found

    if not row["has_consent"]:
        found.append("no consent on record — matching excludes them entirely")
    if not row["availability_windows"]:
        found.append("no availability recorded — no shift can be shown to fit")
    if not row["has_home_location"]:
        found.append(
            "no home location — transport cannot be estimated, so they can "
            "never clear the transport filter or be offered as cover"
        )
    if not row["skills_scored"]:
        found.append(
            "no assessment scored — cannot be matched to any request that "
            "requires a skill"
        )

    if not found and not row["offers"]:
        waiting = (today - row["created_at"].date()).days
        if waiting >= STALE_AFTER_DAYS:
            found.append(
                f"ready for {waiting} days and never offered anything — the "
                "gap is on our side, not theirs"
            )
    return found


def registry_queue(session: Session, today: date | None = None) -> list[dict]:
    """Everyone with something fixable, in the order to work through them."""
    from app.clock import kigali_today

    today = today or kigali_today()
    rows = session.execute(text("SELECT * FROM v_candidate_readiness")).mappings()

    queue = []
    for row in rows:
        record = dict(row)
        record["blockers"] = _blockers(record, today)
        if record["blockers"]:
            queue.append(record)

    # Most blocked first, then longest waiting: somebody with three missing
    # things has had three chances to be noticed and was missed each time.
    queue.sort(key=lambda r: (-len(r["blockers"]), r["created_at"]))
    return queue


def registry_summary(session: Session, today: date | None = None) -> dict:
    """The shape of the problem, for whoever decides where the week goes.

    Broken out by gender because the blueprint makes women's participation a
    product requirement: if women are disproportionately stuck behind a
    fixable blocker, that is the finding, and a single total would hide it.
    """
    from app.clock import kigali_today

    today = today or kigali_today()
    rows = [dict(r) for r in session.execute(
        text("SELECT * FROM v_candidate_readiness")).mappings()]

    counts: dict[str, int] = {}
    women_counts: dict[str, int] = {}
    for row in rows:
        for blocker in _blockers(row, today):
            key = blocker.split(" —")[0]
            counts[key] = counts.get(key, 0) + 1
            if row["gender"] == "F":
                women_counts[key] = women_counts.get(key, 0) + 1

    working = sum(1 for r in rows if r["placements_worked"])
    return {
        "in_registry": len(rows),
        "have_worked": working,
        "never_offered": sum(1 for r in rows if not r["offers"]),
        "blocked": sum(1 for r in rows if _blockers(r, today)),
        "by_blocker": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "women_by_blocker": women_counts,
    }
