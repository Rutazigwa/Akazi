"""Who is in the registry and not working.

The matcher explains why each candidate was excluded from each request. It
cannot show the person excluded from every request, always for the same
reason, whom nobody has looked at. Somebody who never matches never appears on
a match page, so the failure is invisible by construction.
"""
from __future__ import annotations

from sqlalchemy import text

from app.operations.readiness_queue import (
    STALE_AFTER_DAYS,
    registry_queue,
    registry_summary,
)


def ready(session, candidate_id, staff_id):
    """Everything a person needs to be matchable."""
    session.execute(
        text("UPDATE candidates SET home_lat = -1.948, home_lng = 30.105 "
             "WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    )
    session.execute(
        text("INSERT INTO availability (candidate_id, day_of_week, start_time, "
             "end_time) SELECT :c, d, '06:00', '20:00' FROM generate_series(0,6) d"),
        {"c": str(candidate_id)},
    )
    session.execute(
        text("INSERT INTO consent_records (candidate_id, purpose, granted, "
             "policy_version, captured_via, captured_by) "
             "VALUES (:c, 'placement', TRUE, 'v1', 'paper', :s)"),
        {"c": str(candidate_id), "s": str(staff_id)},
    )


def score_one(session, candidate_id, staff_id):
    from app.operations.catalogue import create_assessment, create_skill
    from app.operations.registry import record_assessment_result

    skill_id = create_skill(session, skill_code=f"s{candidate_id.hex[:6]}",
                            skill_name="Something", category="other")
    assessment_id = create_assessment(session, skill_id=skill_id, title="Obs",
                                      method="observed", pass_score=3)
    record_assessment_result(session, candidate_id, assessment_id, 4, staff_id)


def blockers_for(session, candidate_id):
    return next(
        (c["blockers"] for c in registry_queue(session)
         if c["candidate_id"] == candidate_id),
        [],
    )


# --- what stops somebody ---------------------------------------------------

def test_no_consent_is_listed(session, make_candidate, staff_id):
    candidate_id = make_candidate()
    assert any("no consent" in b for b in blockers_for(session, candidate_id))


def test_no_availability_is_listed(session, make_candidate, staff_id):
    candidate_id = make_candidate()
    assert any("no availability" in b
               for b in blockers_for(session, candidate_id))


def test_no_home_location_is_listed_with_its_consequence(
    session, make_candidate, staff_id
):
    """Not "missing field" -- the reason it matters. Transport cannot be
    estimated, so they can never clear filter 2 or be offered as cover."""
    candidate_id = make_candidate()
    blockers = blockers_for(session, candidate_id)
    assert any("transport cannot be estimated" in b for b in blockers)


def test_no_assessment_is_listed(session, make_candidate, staff_id):
    candidate_id = make_candidate()
    ready(session, candidate_id, staff_id)
    assert any("no assessment scored" in b
               for b in blockers_for(session, candidate_id))


def test_somebody_ready_and_recently_registered_is_not_listed(
    session, make_candidate, staff_id
):
    """Being new is not a failing."""
    candidate_id = make_candidate()
    ready(session, candidate_id, staff_id)
    score_one(session, candidate_id, staff_id)
    assert blockers_for(session, candidate_id) == []


def test_ready_for_weeks_and_never_offered_says_the_gap_is_ours(
    session, make_candidate, staff_id
):
    """Either a blocker nobody noticed or a district with no employers, and
    the two need different answers."""
    candidate_id = make_candidate()
    ready(session, candidate_id, staff_id)
    score_one(session, candidate_id, staff_id)
    session.execute(
        text("UPDATE candidates SET created_at = now() - make_interval(days => :d) "
             "WHERE candidate_id = :c"),
        {"d": STALE_AFTER_DAYS + 5, "c": str(candidate_id)},
    )
    assert any("the gap is on our side" in b
               for b in blockers_for(session, candidate_id))


def test_somebody_who_has_been_offered_work_is_not_chased(
    session, make_candidate, make_placement, staff_id
):
    candidate_id = make_candidate()
    ready(session, candidate_id, staff_id)
    score_one(session, candidate_id, staff_id)
    session.execute(
        text("UPDATE candidates SET created_at = now() - make_interval(days => :d) "
             "WHERE candidate_id = :c"),
        {"d": STALE_AFTER_DAYS + 5, "c": str(candidate_id)},
    )
    make_placement(candidate_id=candidate_id)
    assert blockers_for(session, candidate_id) == []


def test_an_erased_candidate_is_not_in_the_queue(session, make_candidate,
                                                 staff_id):
    """Erasure redacts the identity record, which makes age unestablishable.

    Without the withdrawn filter they would appear here labelled as a data
    problem, which is both wrong and a reason to look them up again.
    """
    from app.operations.data_rights import complete_erasure, request_erasure

    candidate_id = make_candidate()
    erasure_id = request_erasure(session, candidate_id=candidate_id,
                                 requested_via="phone", received_by=staff_id)
    session.execute(
        text("SELECT set_config('app.staff_id', :s, true)"),
        {"s": str(staff_id)},
    )
    complete_erasure(session, erasure_id)
    assert blockers_for(session, candidate_id) == []


def test_a_withdrawn_candidate_is_not_chased(session, make_candidate):
    candidate_id = make_candidate()
    session.execute(
        text("UPDATE candidates SET status = 'withdrawn' WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    )
    assert blockers_for(session, candidate_id) == []


# --- the order to work through them ---------------------------------------

def test_the_most_blocked_come_first(session, make_candidate, staff_id):
    """Three missing things means three chances to be noticed, each missed."""
    stuck = make_candidate()
    nearly = make_candidate()
    ready(session, nearly, staff_id)

    queue = registry_queue(session)
    positions = {c["candidate_id"]: i for i, c in enumerate(queue)}
    assert positions[stuck] < positions[nearly]


# --- the shape of the problem ---------------------------------------------

def test_the_summary_counts_the_registry(session, make_candidate, staff_id):
    make_candidate()
    ready(session, make_candidate(), staff_id)
    summary = registry_summary(session)
    assert summary["in_registry"] == 2
    assert summary["blocked"] == 2
    assert summary["never_offered"] == 2


def test_women_are_broken_out_separately(session, make_candidate, staff_id):
    """Women's participation is a product requirement, so a total that hid a
    disproportionate blocker would be the wrong total."""
    woman = make_candidate()
    session.execute(
        text("UPDATE candidates SET gender = 'F' WHERE candidate_id = :c"),
        {"c": str(woman)},
    )
    man = make_candidate()
    session.execute(
        text("UPDATE candidates SET gender = 'M' WHERE candidate_id = :c"),
        {"c": str(man)},
    )
    summary = registry_summary(session)
    assert sum(summary["women_by_blocker"].values()) > 0
    assert sum(summary["women_by_blocker"].values()) < sum(
        summary["by_blocker"].values())


def test_the_page_lists_them(web, session, make_candidate):
    make_candidate()
    page = web.get("/ui/candidates").text
    assert "Not working, and fixable" in page
    assert "transport cannot be estimated" in page
