"""Finding same-day cover, which is a different question from matching.

The general matcher answers "who should do this job", and ranks on prior work
with the employer, retention, assessment score, then commute. That is right
for a shift starting next Tuesday.

It is the wrong question at 08:40 when the 08:00 cleaner has not arrived. Then
the binding constraint is physical: **can anyone get there while there is
still a shift left to work**. An excellent candidate 45 minutes away is worth
less than an adequate one 10 minutes away, and a perfect one who is already on
someone else's shift is worth nothing at all.

So cover runs its own filters and its own ranking. It is still a rules engine
with a stated reason -- the coordinator has to say "she can be with you by
09:05 and she has worked for you twice" out loud to an employer who is
currently short-staffed and unhappy.

This is the guarantee. Everything else in the system exists to make this
moment survivable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.clock import KIGALI
from app.matching.engine import (
    Candidate,
    WorkRequest,
    _safety,
    _transport_viability,
)

# How long from being telephoned to actually leaving. Somebody has to answer,
# agree, and get out of the door; treating travel time as the whole answer
# would have us promise arrivals that never happen, and this is the one
# promise the business is built on.
MOBILISATION_MINUTES = 20

# Below this, sending someone is theatre. They arrive, the employer has
# already covered it themselves or given up, and we have spent a worker's
# afternoon and their transport fare to look responsive.
MINIMUM_USEFUL_MINUTES = 60


@dataclass(frozen=True)
class CoverOption:
    candidate: Candidate
    arrives_at: time
    minutes_covered: int
    reason: str


@dataclass(frozen=True)
class CoverRejection:
    candidate: Candidate
    filter_name: str
    reason: str


@dataclass(frozen=True)
class CoverResult:
    options: list[CoverOption]
    rejections: list[CoverRejection]
    shift_ends_at: time | None
    minutes_remaining: int
    viable: bool
    note: str


def _now_kigali(now: datetime | None) -> datetime:
    return (now or datetime.now(KIGALI)).astimezone(KIGALI)


def _minutes_between(start: time, end: time) -> int:
    """Minutes from start to end, allowing a shift that runs past midnight."""
    delta = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return delta if delta >= 0 else delta + 24 * 60


def find_cover(
    candidates: list[Candidate],
    request: WorkRequest,
    *,
    now: datetime | None = None,
) -> CoverResult:
    """Who could still work this shift today, soonest arrival first."""
    moment = _now_kigali(now)
    clock = moment.time()

    if request.shift_end is None:
        return CoverResult(
            [], [], None, 0, False,
            "this request has no shift end, so there is no window to cover",
        )

    remaining = _minutes_between(clock, request.shift_end)
    # A shift that ended hours ago reads as nearly a full day remaining once
    # the clock wraps past midnight. Treat a window longer than a day as over.
    if remaining > 16 * 60:
        remaining = 0

    if remaining < MINIMUM_USEFUL_MINUTES:
        return CoverResult(
            [], [], request.shift_end, max(remaining, 0), False,
            f"only {max(remaining, 0)} minutes of this shift remain -- too "
            "little to send anyone. Agree a replacement day with the employer "
            "instead of covering today",
        )

    options: list[CoverOption] = []
    rejections: list[CoverRejection] = []

    for candidate in candidates:
        failure = _cover_exclusion(candidate, request, clock, remaining)
        if failure is not None:
            rejections.append(CoverRejection(candidate, *failure))
            continue

        travel = candidate.est_commute_min or 0
        arrival_in = MOBILISATION_MINUTES + travel
        arrives = (
            datetime.combine(moment.date(), clock) + timedelta(minutes=arrival_in)
        ).time()
        covered = _minutes_between(arrives, request.shift_end)

        options.append(
            CoverOption(
                candidate=candidate,
                arrives_at=arrives,
                minutes_covered=covered,
                reason=_cover_reason(candidate, arrives, covered),
            )
        )

    options.sort(key=lambda o: _cover_rank(o))
    return CoverResult(
        options=options,
        rejections=rejections,
        shift_ends_at=request.shift_end,
        minutes_remaining=remaining,
        viable=True,
        note=f"{remaining} minutes of the shift remain",
    )


def rights_exclusion(c: Candidate, r: WorkRequest) -> tuple[str, str] | None:
    """The filters that do not depend on what time it is.

    Separated from the physical ones because they answer a different question.
    Whether somebody can reach a shift before it ends is a fact about the
    clock; whether they are old enough, consented, safe to send after dark and
    not paying a third of the wage to get there is not, and stays true whether
    or not cover is still worth attempting.

    That distinction is load-bearing. find_cover returns early when the window
    has closed -- rightly, there is nothing to rank -- and produces no
    per-candidate rejections at all. A write path that asked find_cover
    "is this person excluded?" therefore got silence for every candidate on
    every shift that had already ended, which is most of the ones a coordinator
    is recording after the fact.
    """
    if not c.age_eligible and not c.meets_minimum_age(r.starts_on):
        return ("hard exclusion", "below the minimum working age")
    if not c.has_placement_consent:
        return ("hard exclusion", "no consent on record")

    transport = _transport_viability(c, r)
    if transport is not None:
        return ("transport viability", transport)

    safety = _safety(c, r)
    if safety is not None:
        return ("safety", safety)

    return None


def _cover_exclusion(
    c: Candidate, r: WorkRequest, clock: time, remaining: int
) -> tuple[str, str] | None:
    """Cover's own hard filters. Legal ones first, then physical ones."""
    rights = rights_exclusion(c, r)
    if rights is not None:
        return rights

    # Already working. The general matcher checks for a conflicting commitment
    # too, but here it is the most common reason and worth saying plainly.
    if c.has_conflicting_commitment:
        return ("already working", "already on a shift in this window")

    # The physical question, and the one that makes this different from
    # ordinary matching.
    if c.est_commute_min is None:
        return (
            "reachability",
            "no home location on file, so arrival time cannot be estimated",
        )

    arrival_in = MOBILISATION_MINUTES + c.est_commute_min
    if arrival_in >= remaining:
        return (
            "reachability",
            f"cannot arrive before the shift ends "
            f"({arrival_in} minutes away, {remaining} remaining)",
        )
    if remaining - arrival_in < MINIMUM_USEFUL_MINUTES:
        return (
            "reachability",
            f"would arrive with only {remaining - arrival_in} minutes left",
        )
    return None


def _cover_rank(option: CoverOption) -> tuple:
    """Soonest useful arrival, then who the employer already knows.

    Deliberately different from the general ranking. Assessment score barely
    matters for a shift someone has done before, and a higher-scoring worker
    who arrives an hour later covers less of the gap the employer is standing
    in right now.
    """
    c = option.candidate
    return (
        -option.minutes_covered,
        -c.prior_completed_with_employer,
        -c.retention_30day_rate,
        -c.assessment_score,
    )


def _cover_reason(c: Candidate, arrives: time, covered: int) -> str:
    parts = [f"can be there by {arrives.strftime('%H:%M')}",
             f"covers {covered} of the remaining minutes"]
    if c.prior_completed_with_employer:
        parts.append(
            f"has worked here {c.prior_completed_with_employer} time"
            f"{'s' if c.prior_completed_with_employer != 1 else ''}"
        )
    if c.est_commute_min is not None:
        parts.append(f"{c.est_commute_min}-min commute")
    if c.retention_30day_rate:
        parts.append(f"{c.retention_30day_rate:.0%} 30-day retention")
    return "cover: " + ", ".join(parts)
