"""Assessment scores, and what they are allowed to mean.

Two things were wrong. A score could exceed its assessment's maximum, because
the maximum lives on another table and a plain CHECK cannot see it -- so "9/5"
was storable, and that is the number matching ranks on and a coordinator reads
aloud to an employer. And the assessment's own pass mark was never consulted,
so a demonstrably failed attempt counted as a low score that could still clear
a low employer bar.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.matching.repository import find_matches
from app.operations.registry import record_consent
from app.operations.requests import require_skill


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


@pytest.fixture
def skill(session):
    """A skill assessed out of 5, passing at 3."""
    def _make(code="retail_greeting", max_score=5, pass_score=3):
        skill_id = session.execute(
            text(
                "INSERT INTO skills (skill_code, skill_name, category) "
                "VALUES (:c, :c, 'retail') RETURNING skill_id"
            ),
            {"c": code},
        ).scalar_one()
        assessment_id = session.execute(
            text(
                """
                INSERT INTO assessments (skill_id, title, method, max_score,
                                         pass_score)
                VALUES (:s, 'Observed', 'observed', :max, :pass)
                RETURNING assessment_id
                """
            ),
            {"s": skill_id, "max": max_score, "pass": pass_score},
        ).scalar_one()
        return code, assessment_id

    return _make


@pytest.fixture
def scored(session, make_candidate, staff_id):
    def _make(assessment_id, score, name="Aline"):
        cid = make_candidate(name=name)
        record_consent(session, cid, "placement", True, "paper", staff_id)
        session.execute(
            text(
                "INSERT INTO availability (candidate_id, day_of_week, "
                "start_time, end_time) "
                "SELECT :c, d, '06:00', '22:00' FROM generate_series(0,6) d"
            ),
            {"c": cid},
        )
        session.execute(
            text(
                "INSERT INTO assessment_results (candidate_id, assessment_id, "
                "score, assessed_by) VALUES (:c, :a, :s, :by)"
            ),
            {"c": cid, "a": assessment_id, "s": score, "by": staff_id},
        )
        return cid

    return _make


# --- the maximum -----------------------------------------------------------

def test_a_score_above_the_maximum_is_refused(session, skill, make_candidate,
                                              staff_id):
    """9 out of 5 was accepted, and would have been read aloud as '9/5'."""
    _, assessment_id = skill()
    cid = make_candidate()
    with pytest.raises(Exception, match="exceeds the maximum of 5"):
        session.execute(
            text(
                "INSERT INTO assessment_results (candidate_id, assessment_id, "
                "score, assessed_by) VALUES (:c, :a, 9, :by)"
            ),
            {"c": cid, "a": assessment_id, "by": staff_id},
        )


def test_a_score_at_the_maximum_is_fine(session, skill, scored):
    _, assessment_id = skill()
    assert scored(assessment_id, 5)


def test_an_update_cannot_push_a_score_over(session, skill, scored):
    """The trigger covers UPDATE, not only INSERT."""
    _, assessment_id = skill()
    cid = scored(assessment_id, 4)
    with pytest.raises(Exception, match="exceeds the maximum"):
        session.execute(
            text(
                "UPDATE assessment_results SET score = 8 WHERE candidate_id = :c"
            ),
            {"c": cid},
        )


def test_a_result_for_a_missing_assessment_is_refused(session, make_candidate,
                                                      staff_id):
    with pytest.raises(Exception):
        session.execute(
            text(
                "INSERT INTO assessment_results (candidate_id, assessment_id, "
                "score, assessed_by) VALUES (:c, :a, 3, :by)"
            ),
            {"c": make_candidate(), "a": str(uuid.uuid4()), "by": staff_id},
        )


# --- the pass mark ---------------------------------------------------------

def test_a_failed_attempt_is_not_evidence_of_the_skill(
    session, skill, scored, make_request
):
    """Scored 2 against a pass mark of 3, for a request asking only for 1.

    The employer's bar is lower than the assessment's, but the candidate
    demonstrably did not do the thing.
    """
    code, assessment_id = skill()
    scored(assessment_id, 2)
    request_id = make_request()
    require_skill(session, request_id, code, min_score=1)

    result = find_matches(session, request_id)
    assert [m.candidate.display_name for m in result.matches] == []
    reason = result.rejections[0].reason
    assert "did not reach the assessment's pass mark of 3" in reason


def test_a_passing_attempt_counts(session, skill, scored, make_request):
    code, assessment_id = skill()
    scored(assessment_id, 3)
    request_id = make_request()
    require_skill(session, request_id, code, min_score=3)
    assert [
        m.candidate.display_name for m in find_matches(session, request_id).matches
    ] == ["Aline"]


def test_never_assessed_reads_differently_from_failed(
    session, skill, make_candidate, make_request, staff_id
):
    """A coordinator needs to tell 'has not sat it' from 'sat it and failed'."""
    code, _ = skill()
    cid = make_candidate(name="Unassessed")
    record_consent(session, cid, "placement", True, "paper", staff_id)
    request_id = make_request()
    require_skill(session, request_id, code, min_score=3)

    reason = find_matches(session, request_id).rejections[0].reason
    assert "no assessment on record" in reason


def test_a_retake_supersedes_a_failed_attempt(
    session, skill, scored, make_request, staff_id
):
    code, assessment_id = skill()
    cid = scored(assessment_id, 2)
    session.execute(
        text(
            "INSERT INTO assessment_results (candidate_id, assessment_id, "
            "score, assessed_by, assessed_at) "
            "VALUES (:c, :a, 4, :by, now() + INTERVAL '1 hour')"
        ),
        {"c": cid, "a": assessment_id, "by": staff_id},
    )
    request_id = make_request()
    require_skill(session, request_id, code, min_score=3)
    assert [
        m.candidate.display_name for m in find_matches(session, request_id).matches
    ] == ["Aline"]


# --- the denominator -------------------------------------------------------

def test_the_reason_uses_the_real_maximum(session, skill, scored, make_request):
    """It was hardcoded as /5, so an assessment out of ten read as '8/5'."""
    code, assessment_id = skill(code="forklift", max_score=10, pass_score=6)
    scored(assessment_id, 8)
    request_id = make_request()
    require_skill(session, request_id, code, min_score=6)

    reason = find_matches(session, request_id).matches[0].reason
    assert "forklift 8/10 (needs 6)" in reason
    assert "8/5" not in reason


def test_a_below_bar_score_shows_its_maximum_too(
    session, skill, scored, make_request
):
    code, assessment_id = skill(code="forklift", max_score=10, pass_score=3)
    scored(assessment_id, 4)
    request_id = make_request()
    require_skill(session, request_id, code, min_score=8)

    reason = find_matches(session, request_id).rejections[0].reason
    assert "forklift 4/10 is below the required 8" in reason
