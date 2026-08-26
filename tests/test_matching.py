"""Tests for the v1 matching filters.

Each test names the rule it protects. These filters are the product logic, so a
failure here is a business failure, not a unit-test failure.
"""

from datetime import date, time
from uuid import uuid4

from app.matching.engine import (
    AvailabilityWindow,
    Candidate,
    WorkRequest,
    match_candidates,
)

MONDAY = date(2026, 9, 7)  # a Monday
EMPLOYER = uuid4()

def make_request(**overrides) -> WorkRequest:
    base = dict(
        request_id=uuid4(),
        employer_id=EMPLOYER,
        starts_on=MONDAY,
        pay_rwf=5000,
        pay_unit="day",
        shift_start=time(8, 0),
        shift_end=time(16, 0),
        required_skills={},
    )
    base.update(overrides)
    return WorkRequest(**base)

def make_candidate(**overrides) -> Candidate:
    base = dict(
        candidate_id=uuid4(),
        display_name="Test Candidate",
        gender="M",
        date_of_birth=date(2002, 1, 1),
        availability=[AvailabilityWindow(0, time(6, 0), time(20, 0))],
        skill_scores={},
        skill_max={},
        max_commute_rwf=2000,
        max_commute_min=90,
        has_placement_consent=True,
        est_transport_rwf=500,
        est_commute_min=20,
    )
    base.update(overrides)
    return Candidate(**base)

def only_rejection(result):
    assert not result.matches
    assert len(result.rejections) == 1
    return result.rejections[0]

# --- Filter 1: hard exclusions -------------------------------------------

def test_under_16_is_excluded():
    young = make_candidate(date_of_birth=date(2011, 1, 1))
    rejection = only_rejection(match_candidates(make_request(), [young]))
    assert rejection.filter_name == "hard_exclusion"
    assert "minimum working age" in rejection.reason

def test_age_is_measured_on_the_start_date_not_today():
    """Someone who turns 16 the day before the shift is eligible."""
    turns_16_just_in_time = make_candidate(
        date_of_birth=date(MONDAY.year - 16, MONDAY.month, MONDAY.day - 1)
    )
    result = match_candidates(make_request(), [turns_16_just_in_time])
    assert len(result.matches) == 1

def test_missing_consent_is_excluded():
    no_consent = make_candidate(has_placement_consent=False)
    rejection = only_rejection(match_candidates(make_request(), [no_consent]))
    assert "consent" in rejection.reason

def test_availability_must_cover_the_whole_shift():
    partial = make_candidate(
        availability=[AvailabilityWindow(0, time(8, 0), time(12, 0))]
    )
    rejection = only_rejection(match_candidates(make_request(), [partial]))
    assert "availability does not cover" in rejection.reason

def test_availability_on_the_wrong_day_is_excluded():
    wrong_day = make_candidate(
        availability=[AvailabilityWindow(2, time(6, 0), time(20, 0))]
    )
    rejection = only_rejection(match_candidates(make_request(), [wrong_day]))
    assert rejection.filter_name == "hard_exclusion"

def test_skill_below_min_score_is_excluded():
    request = make_request(required_skills={"retail_greeting": 4})
    weak = make_candidate(
        skill_scores={"retail_greeting": 2}, skill_max={"retail_greeting": 5}
    )
    rejection = only_rejection(match_candidates(request, [weak]))
    assert "below the required 4" in rejection.reason

def test_unassessed_required_skill_is_excluded():
    request = make_request(required_skills={"food_safety": 3})
    unassessed = make_candidate(skill_scores={})
    rejection = only_rejection(match_candidates(request, [unassessed]))
    assert "no assessment on record" in rejection.reason

# --- Filter 2: transport viability ---------------------------------------

def test_the_blueprint_example_is_rejected():
    """RWF 3,000/day against RWF 1,600 moto fare: 53% of pay.

    This is the placement that dies in week two. It must never be offered.
    """
    request = make_request(pay_rwf=3000)
    candidate = make_candidate(est_transport_rwf=1600, max_commute_rwf=2000)
    rejection = only_rejection(match_candidates(request, [candidate]))
    assert rejection.filter_name == "transport_viability"
    assert "53% of daily pay" in rejection.reason

def test_transport_over_the_candidate_ceiling_is_rejected():
    candidate = make_candidate(est_transport_rwf=2500, max_commute_rwf=2000)
    rejection = only_rejection(match_candidates(make_request(), [candidate]))
    assert "exceeds the candidate's ceiling" in rejection.reason

def test_employer_covered_transport_waives_the_whole_filter():
    request = make_request(pay_rwf=3000, transport_covered=True)
    candidate = make_candidate(est_transport_rwf=1600)
    result = match_candidates(request, [candidate])
    assert len(result.matches) == 1
    assert "employer covers transport" in result.matches[0].reason

def test_transport_exactly_at_the_limit_is_rejected():
    """30% is the limit, not the last acceptable value."""
    request = make_request(pay_rwf=1000)
    candidate = make_candidate(est_transport_rwf=300, max_commute_rwf=1000)
    assert only_rejection(match_candidates(request, [candidate]))

def test_commute_time_ceiling_is_enforced():
    candidate = make_candidate(est_commute_min=120, max_commute_min=90)
    rejection = only_rejection(match_candidates(make_request(), [candidate]))
    assert "exceeds the candidate's ceiling of 90 min" in rejection.reason

def test_monthly_pay_is_normalised_to_a_daily_rate():
    """RWF 66,000/month is RWF 3,000/day, so the same fare is still too high."""
    request = make_request(pay_rwf=66_000, pay_unit="month")
    candidate = make_candidate(est_transport_rwf=1600, max_commute_rwf=2000)
    rejection = only_rejection(match_candidates(request, [candidate]))
    assert rejection.filter_name == "transport_viability"

def test_task_rate_work_skips_the_share_test_but_keeps_the_ceiling():
    request = make_request(pay_rwf=500, pay_unit="task")
    ok = make_candidate(est_transport_rwf=1500, max_commute_rwf=2000)
    assert len(match_candidates(request, [ok]).matches) == 1

    over = make_candidate(est_transport_rwf=2500, max_commute_rwf=2000)
    assert only_rejection(match_candidates(request, [over]))

# --- Filter 3: safety ------------------------------------------------------

def test_after_dark_shift_needs_transport_or_opt_in_for_women():
    request = make_request(shift_start=time(12, 0), shift_end=time(21, 0))
    woman = make_candidate(
        gender="F",
        availability=[AvailabilityWindow(0, time(6, 0), time(23, 0))],
    )
    rejection = only_rejection(match_candidates(request, [woman]))
    assert rejection.filter_name == "safety"
    assert "after dark" in rejection.reason

def test_after_dark_opt_in_is_honoured():
    request = make_request(shift_start=time(12, 0), shift_end=time(21, 0))
    woman = make_candidate(
        gender="F",
        accepts_after_dark=True,
        availability=[AvailabilityWindow(0, time(6, 0), time(23, 0))],
    )
    assert len(match_candidates(request, [woman]).matches) == 1

def test_after_dark_employer_transport_is_honoured():
    request = make_request(
        shift_start=time(12, 0), shift_end=time(21, 0), transport_covered=True
    )
    woman = make_candidate(
        gender="F",
        availability=[AvailabilityWindow(0, time(6, 0), time(23, 0))],
    )
    assert len(match_candidates(request, [woman]).matches) == 1

def test_daytime_shift_is_unaffected_by_the_safety_filter():
    woman = make_candidate(gender="F")
    assert len(match_candidates(make_request(), [woman]).matches) == 1

# --- Filter 4: ranking -----------------------------------------------------

def test_prior_placements_with_this_employer_outrank_a_better_score():
    reliable = make_candidate(
        display_name="Reliable", prior_completed_with_employer=3,
        assessment_score=3,
    )
    talented = make_candidate(
        display_name="Talented", prior_completed_with_employer=0,
        assessment_score=5,
    )
    result = match_candidates(make_request(), [talented, reliable])
    assert [m.candidate.display_name for m in result.matches] == [
        "Reliable",
        "Talented",
    ]

def test_retention_breaks_ties_before_assessment_score():
    sticky = make_candidate(
        display_name="Sticky", retention_30day_rate=0.9, assessment_score=3
    )
    scorer = make_candidate(
        display_name="Scorer", retention_30day_rate=0.2, assessment_score=5
    )
    result = match_candidates(make_request(), [scorer, sticky])
    assert result.matches[0].candidate.display_name == "Sticky"

def test_commute_time_is_the_final_tiebreak():
    near = make_candidate(display_name="Near", est_commute_min=10)
    far = make_candidate(display_name="Far", est_commute_min=50)
    result = match_candidates(make_request(), [far, near])
    assert result.matches[0].candidate.display_name == "Near"

# --- Filter 5: explainability ---------------------------------------------

def test_every_match_carries_a_defensible_reason():
    request = make_request(pay_rwf=5000, required_skills={"retail_greeting": 3})
    candidate = make_candidate(
        skill_scores={"retail_greeting": 4},
        skill_max={"retail_greeting": 5},
        est_commute_min=12,
        est_transport_rwf=500,
    )
    reason = match_candidates(request, [candidate]).matches[0].reason
    assert reason.startswith("matched on: ")
    assert "retail_greeting 4/5 (needs 3)" in reason
    assert "12-min commute" in reason
    assert "net RWF 4500/day after transport" in reason

def test_rejections_are_returned_as_a_demand_signal():
    """A request that fills nobody must say why, per candidate."""
    request = make_request(pay_rwf=3000)
    candidates = [
        make_candidate(display_name="A", est_transport_rwf=1600),
        make_candidate(display_name="B", has_placement_consent=False),
        make_candidate(display_name="C", date_of_birth=date(2013, 1, 1)),
    ]
    result = match_candidates(request, candidates)
    assert not result.matches
    assert {r.filter_name for r in result.rejections} == {
        "transport_viability",
        "hard_exclusion",
    }
    assert all(r.reason for r in result.rejections)

def test_filters_short_circuit_in_order():
    """A candidate failing several filters is reported against the first."""
    request = make_request(shift_start=time(12, 0), shift_end=time(21, 0))
    both = make_candidate(
        gender="F",
        has_placement_consent=False,
        est_transport_rwf=9999,
        availability=[AvailabilityWindow(0, time(6, 0), time(23, 0))],
    )
    assert only_rejection(match_candidates(request, [both])).filter_name == (
        "hard_exclusion"
    )
