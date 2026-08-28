"""Matching engine, v1.

Sequential filters, not weights. Explainability matters more than optimality at
pilot volume: when an employer asks "why this person," the coordinator has to be
able to answer, and a model trained on 40 placements is noise.

The filter order is deliberate and load-bearing:

    1. Hard exclusions      -- legal and contractual disqualifiers
    2. Transport viability  -- the wage has to survive the commute
    3. Safety               -- after-dark shifts need transport or an opt-in
    4. Ranking              -- reliability history first, score later
    5. Reason               -- every match carries its own justification

Filter 2 is the one that matters most. A role paying RWF 3,000/day that costs
RWF 1,600 in moto fare is a placement that dies in week two and destroys the
retention metric, so transport cost is a hard constraint here rather than a
preference.
"""

from __future__ import annotations

from app.rules import MAX_TRANSPORT_SHARE, MINIMUM_AGE

from dataclasses import dataclass, field
from datetime import date, time
from typing import Iterable, Sequence
from uuid import UUID

# Kigali sits just south of the equator; sunset varies by only ~20 minutes
# across the year, so a fixed cutoff is honest here in a way it would not be at
# higher latitudes. Revisit only if the operation expands beyond Rwanda.
AFTER_DARK = time(18, 0)

# Standard working days per month, for normalising monthly pay to a daily rate.
WORKING_DAYS_PER_MONTH = 22


@dataclass(frozen=True)
class AvailabilityWindow:
    day_of_week: int  # 0 = Monday .. 6 = Sunday
    start: time
    end: time

    def covers(self, day_of_week: int, start: time, end: time) -> bool:
        return (
            self.day_of_week == day_of_week
            and self.start <= start
            and self.end >= end
        )


@dataclass(frozen=True)
class WorkRequest:
    request_id: UUID
    employer_id: UUID
    starts_on: date
    pay_rwf: int
    pay_unit: str  # day | hour | month | task
    ends_on: date | None = None
    shift_start: time | None = None
    shift_end: time | None = None
    transport_covered: bool = False
    required_skills: dict[str, int] = field(default_factory=dict)

    @property
    def day_of_week(self) -> int:
        return self.starts_on.weekday()

    def daily_pay_rwf(self) -> int | None:
        """Daily-equivalent pay, or None when it cannot be derived.

        Task-rate work has no meaningful daily equivalent without knowing how
        many tasks a day holds, so the transport-share test is skipped for it
        and only the candidate's own ceiling applies.
        """
        if self.pay_unit == "day":
            return self.pay_rwf
        if self.pay_unit == "month":
            return self.pay_rwf // WORKING_DAYS_PER_MONTH
        if self.pay_unit == "hour":
            if self.shift_start is None or self.shift_end is None:
                return None
            hours = (
                _minutes(self.shift_end) - _minutes(self.shift_start)
            ) / 60
            return int(self.pay_rwf * hours) if hours > 0 else None
        return None


@dataclass(frozen=True)
class Candidate:
    candidate_id: UUID
    display_name: str
    gender: str | None  # F | M | X | None
    # One of these must be set. The repository supplies age_eligible, derived
    # by the database, so that operational code never reads a date of birth --
    # see migration 018. Pure-domain callers may pass date_of_birth instead.
    date_of_birth: date | None = None
    age_eligible: bool | None = None
    availability: Sequence[AvailabilityWindow] = ()
    # Only scores that met the assessment's own pass mark. A failed attempt
    # is not a low score, it is no evidence of the skill.
    skill_scores: dict[str, int] = field(default_factory=dict)
    # skill_code -> the assessment's maximum, so a reason can say "4/5"
    # honestly. It was hardcoded as /5 once, which would have read "8/5" for
    # an assessment scored out of ten.
    skill_max: dict[str, int] = field(default_factory=dict)
    # skill_code -> (best score, the pass mark it fell short of). Carried so
    # an exclusion can be explained rather than reading as "never assessed".
    failed_skills: dict[str, tuple[int, int]] = field(default_factory=dict)
    max_commute_rwf: int | None = None
    max_commute_min: int | None = None
    accepts_after_dark: bool = False
    has_placement_consent: bool = False
    est_transport_rwf: int = 0
    est_commute_min: int | None = None
    # Already committed to work that overlaps this request. Computed by the
    # repository, because it is a fact about the database rather than about
    # the candidate.
    has_conflicting_commitment: bool = False
    # Reliability history -- the primary ranking signal.
    prior_completed_with_employer: int = 0
    retention_30day_rate: float = 0.0
    assessment_score: int = 0

    def age_on(self, when: date) -> int:
        if self.date_of_birth is None:
            raise ValueError("no date of birth on this candidate record")
        years = when.year - self.date_of_birth.year
        if (when.month, when.day) < (
            self.date_of_birth.month,
            self.date_of_birth.day,
        ):
            years -= 1
        return years

    def meets_minimum_age(self, when: date) -> bool:
        """Whether this candidate is old enough to be placed on `when`.

        Unknown means excluded. A candidate whose age we cannot establish is
        not someone to place at all -- the failure mode of guessing wrong here
        is placing a child.
        """
        if self.age_eligible is not None:
            return self.age_eligible
        if self.date_of_birth is not None:
            return self.age_on(when) >= MINIMUM_AGE
        return False


@dataclass(frozen=True)
class Match:
    candidate: Candidate
    reason: str


@dataclass(frozen=True)
class Rejection:
    candidate: Candidate
    filter_name: str
    reason: str


@dataclass(frozen=True)
class MatchResult:
    matches: list[Match]
    rejections: list[Rejection]


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _hard_exclusions(c: Candidate, r: WorkRequest) -> str | None:
    """Legal and contractual disqualifiers. Never overridable by ranking."""
    if not c.meets_minimum_age(r.starts_on):
        return (
            f"under minimum working age of {MINIMUM_AGE} on the start date, "
            f"or age could not be established"
        )
    if not c.has_placement_consent:
        return "no current consent record for the placement purpose"

    # Nobody can be in two places at once, and the whole promise to the
    # employer is that the person turns up. Checked against actual overlapping
    # placements rather than candidate status: status is a summary that drifts,
    # the overlap is the fact.
    if c.has_conflicting_commitment:
        return "already committed to overlapping work"

    if r.shift_start is not None and r.shift_end is not None:
        covered = any(
            w.covers(r.day_of_week, r.shift_start, r.shift_end)
            for w in c.availability
        )
        if not covered:
            return (
                f"availability does not cover "
                f"{r.shift_start:%H:%M}-{r.shift_end:%H:%M}"
            )

    for skill, min_score in r.required_skills.items():
        score = c.skill_scores.get(skill)
        if score is None:
            if skill in c.failed_skills:
                scored, pass_mark = c.failed_skills[skill]
                return (
                    f"{skill} {scored} did not reach the assessment's pass "
                    f"mark of {pass_mark}"
                )
            return f"no assessment on record for required skill '{skill}'"
        if score < min_score:
            out_of = c.skill_max.get(skill)
            shown = f"{score}/{out_of}" if out_of else str(score)
            return f"{skill} {shown} is below the required {min_score}"
    return None


def _transport_viability(c: Candidate, r: WorkRequest) -> str | None:
    """The filter that prevents most 30-day dropouts.

    An employer covering transport answers the money, and only the money. It
    does not make the journey shorter: somebody who says they cannot travel
    more than 45 minutes has told us something about their life -- childcare,
    a second job, getting home before dark -- not about their wallet. Placing
    them on a 90-minute commute because the fare is paid produces exactly the
    week-two departure this filter exists to prevent.
    """
    if (
        c.max_commute_min is not None
        and c.est_commute_min is not None
        and c.est_commute_min > c.max_commute_min
    ):
        return (
            f"commute {c.est_commute_min} min exceeds the candidate's "
            f"ceiling of {c.max_commute_min} min"
        )

    # Everything below is about cost, and cost is what the employer covers.
    if r.transport_covered:
        return None

    if c.max_commute_rwf is not None and c.est_transport_rwf > c.max_commute_rwf:
        return (
            f"transport RWF {c.est_transport_rwf}/day exceeds the candidate's "
            f"ceiling of RWF {c.max_commute_rwf}"
        )

    daily_pay = r.daily_pay_rwf()
    if daily_pay and daily_pay > 0:
        share = c.est_transport_rwf / daily_pay
        if share >= MAX_TRANSPORT_SHARE:
            return (
                f"transport is {share:.0%} of daily pay "
                f"(RWF {c.est_transport_rwf} of {daily_pay}), "
                f"over the {MAX_TRANSPORT_SHARE:.0%} limit"
            )
    return None


def _safety(c: Candidate, r: WorkRequest) -> str | None:
    """After-dark shifts are a placement risk we do not take by default."""
    if r.shift_end is None or r.shift_end <= AFTER_DARK:
        return None
    if c.gender != "F":
        return None
    if r.transport_covered or c.accepts_after_dark:
        return None
    return (
        f"shift ends {r.shift_end:%H:%M}, after dark, with no employer-covered "
        f"transport and no opt-in on record"
    )


def _rank_key(c: Candidate) -> tuple:
    return (
        -c.prior_completed_with_employer,
        -c.retention_30day_rate,
        -c.assessment_score,
        c.est_commute_min if c.est_commute_min is not None else 10**6,
    )


def _reason(c: Candidate, r: WorkRequest) -> str:
    """What the coordinator reads back to the employer."""
    parts: list[str] = []
    if c.prior_completed_with_employer:
        parts.append(
            f"{c.prior_completed_with_employer} prior completed "
            f"placement{'s' if c.prior_completed_with_employer != 1 else ''} "
            f"with this employer"
        )
    if c.retention_30day_rate:
        parts.append(f"{c.retention_30day_rate:.0%} 30-day retention")
    for skill, min_score in sorted(r.required_skills.items()):
        out_of = c.skill_max.get(skill)
        scored = (
            f"{c.skill_scores[skill]}/{out_of}" if out_of
            else str(c.skill_scores[skill])
        )
        parts.append(f"{skill} {scored} (needs {min_score})")
    if r.shift_start and r.shift_end:
        parts.append("availability")
    if c.est_commute_min is not None:
        parts.append(f"{c.est_commute_min}-min commute")
    daily_pay = r.daily_pay_rwf()
    if r.transport_covered:
        parts.append("employer covers transport")
    elif daily_pay and daily_pay > 0:
        net = daily_pay - c.est_transport_rwf
        parts.append(f"net RWF {net}/day after transport")
    return "matched on: " + ", ".join(parts)


def match_candidates(
    request: WorkRequest, candidates: Iterable[Candidate]
) -> MatchResult:
    """Run the sequential filters and rank the survivors.

    Returns both the matches and the rejections. The rejections are not
    debugging output: a request that fills nobody is a demand signal, and the
    reason it filled nobody is what tells us whether the problem is pay,
    transport, skills or supply.
    """
    filters = (
        ("hard_exclusion", _hard_exclusions),
        ("transport_viability", _transport_viability),
        ("safety", _safety),
    )

    survivors: list[Candidate] = []
    rejections: list[Rejection] = []

    for candidate in candidates:
        for name, check in filters:
            failure = check(candidate, request)
            if failure is not None:
                rejections.append(Rejection(candidate, name, failure))
                break
        else:
            survivors.append(candidate)

    survivors.sort(key=_rank_key)
    return MatchResult(
        matches=[Match(c, _reason(c, request)) for c in survivors],
        rejections=rejections,
    )


# What a coordinator can actually read before choosing somebody. Beyond this
# the list is not a shortlist, it is a directory -- and the ranking has
# already put the best ones at the top, so the tail is what you scroll past.
SHORTLIST = 25

# Enough of a reason to recognise it, not enough to hide the count. Seeing
# three names under "transport viability: 1,390" tells a coordinator what kind
# of person is being excluded; seeing all 1,390 tells them nothing.
EXAMPLES_PER_REASON = 3


def summarise(result: "MatchResult", shortlist: int = SHORTLIST) -> dict:
    """A match result as a person can read it.

    The full result is still the truth and the API returns all of it. This is
    for the screen, where 610 matched names and 1,390 rejections is not a
    shortlist -- it is a wall that hides the one fact worth knowing.

    Rejections are grouped rather than listed, and that is the important half.
    "1,390 excluded by transport viability" says the shift is underpaid or
    badly sited, which is a thing to go and fix. The same 1,390 as names says
    nothing at all, and takes a megabyte of page to say it.
    """
    by_reason: dict[str, list] = {}
    for rejection in result.rejections:
        by_reason.setdefault(rejection.filter_name, []).append(rejection)

    grouped = [
        {
            "filter_name": name,
            "count": len(rejections),
            "examples": rejections[:EXAMPLES_PER_REASON],
        }
        for name, rejections in sorted(
            by_reason.items(), key=lambda kv: -len(kv[1])
        )
    ]

    return {
        "matches": result.matches[:shortlist],
        "matched_total": len(result.matches),
        "matches_hidden": max(0, len(result.matches) - shortlist),
        "rejected_total": len(result.rejections),
        "rejection_groups": grouped,
    }
