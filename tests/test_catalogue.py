"""Defining what is tested, which nothing could do before.

skills and assessments were empty on a fresh deployment and no application
code inserted into either, so require_skill() raised 'unknown skill' for every
code there was, record_assessment_result() could not be given a real
assessment_id, and matching filter 1 and rank criterion 3 never engaged.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.operations.catalogue import (
    CatalogueError,
    assessment_for_scoring,
    create_assessment,
    create_skill,
    list_assessments,
    list_skills,
    rename_skill,
    update_rubric,
)
from app.operations.registry import record_assessment_result
from app.operations.requests import RequestError, require_skill


@pytest.fixture
def greeting(session):
    return create_skill(
        session, skill_code="retail_greeting",
        skill_name="Greeting customers", category="retail",
    )


@pytest.fixture
def greeting_assessment(session, greeting):
    return create_assessment(
        session, skill_id=greeting, title="Counter greeting, observed",
        method="observed", pass_score=3, max_score=5,
        rubric="1 no greeting; 3 greets and offers help; 5 greets, offers "
               "help and closes the interaction",
    )


def test_a_work_request_can_finally_require_a_skill(
    session, greeting, make_request
):
    """The whole point: require_skill() resolves a code that now exists."""
    request_id = make_request()
    require_skill(session, request_id, "retail_greeting", min_score=3)

    stored = session.execute(
        text("SELECT min_score FROM request_skills WHERE request_id = :r"),
        {"r": str(request_id)},
    ).scalar_one()
    assert stored == 3


def test_requiring_an_undefined_skill_still_refuses(session, make_request):
    with pytest.raises(RequestError, match="unknown skill"):
        require_skill(session, make_request(), "no_such_skill")


def test_a_result_can_be_recorded_against_a_real_assessment(
    session, greeting_assessment, make_candidate, staff_id
):
    result_id = record_assessment_result(
        session, make_candidate(), greeting_assessment, 4, staff_id,
    )
    assert result_id is not None


# --- codes are normalised, because require_skill matches them exactly -------

@pytest.mark.parametrize("typed,stored", [
    ("Retail Greeting", "retail_greeting"),
    ("  retail-greeting  ", "retail_greeting"),
    ("RETAIL_GREETING", "retail_greeting"),
])
def test_skill_codes_are_normalised(session, typed, stored):
    skill_id = create_skill(
        session, skill_code=typed, skill_name="Greeting", category="retail"
    )
    assert session.execute(
        text("SELECT skill_code FROM skills WHERE skill_id = :s"),
        {"s": str(skill_id)},
    ).scalar_one() == stored


def test_a_duplicate_skill_code_is_refused(session, greeting):
    with pytest.raises(CatalogueError, match="already exists"):
        create_skill(session, skill_code="Retail Greeting",
                     skill_name="Something else", category="retail")


def test_a_skill_code_cannot_be_changed(session, greeting):
    """It is the handle require_skill resolves against, and it appears in
    operators' notes and import sheets."""
    with pytest.raises(Exception, match="stable handle"):
        session.execute(
            text("UPDATE skills SET skill_code = 'other' WHERE skill_id = :s"),
            {"s": str(greeting)},
        )
    session.rollback()


def test_a_skill_can_still_be_renamed(session, greeting):
    rename_skill(session, greeting, "Greeting customers at the till")
    assert session.execute(
        text("SELECT skill_name FROM skills WHERE skill_id = :s"),
        {"s": str(greeting)},
    ).scalar_one() == "Greeting customers at the till"


# --- bounds freeze once they have been used --------------------------------

def test_bounds_are_editable_before_any_result(session, greeting_assessment):
    """A typo during setup should be cheap to fix."""
    session.execute(
        text("UPDATE assessments SET pass_score = 4 WHERE assessment_id = :a"),
        {"a": str(greeting_assessment)},
    )
    assert session.execute(
        text("SELECT pass_score FROM assessments WHERE assessment_id = :a"),
        {"a": str(greeting_assessment)},
    ).scalar_one() == 4


def test_raising_the_pass_mark_after_results_is_refused(
    session, greeting_assessment, make_candidate, staff_id
):
    """Otherwise a candidate who passed at 3/5 has retroactively always
    failed -- including for placements already made on that score."""
    record_assessment_result(
        session, make_candidate(), greeting_assessment, 3, staff_id
    )
    with pytest.raises(Exception, match="retroactively change who passed"):
        session.execute(
            text("UPDATE assessments SET pass_score = 4 "
                 "WHERE assessment_id = :a"),
            {"a": str(greeting_assessment)},
        )
    session.rollback()


def test_the_rubric_stays_editable_after_results(
    session, greeting_assessment, make_candidate, staff_id
):
    """Sharpening the wording does not change who passed."""
    record_assessment_result(
        session, make_candidate(), greeting_assessment, 3, staff_id
    )
    update_rubric(session, greeting_assessment, "3 = greets and offers help")
    assert "offers help" in assessment_for_scoring(
        session, greeting_assessment
    )["rubric"]


# --- validation ------------------------------------------------------------

def test_a_pass_mark_above_the_maximum_is_refused(session, greeting):
    with pytest.raises(CatalogueError, match="between 0 and max_score"):
        create_assessment(session, skill_id=greeting, title="Impossible",
                          method="observed", pass_score=9, max_score=5)


def test_an_assessment_needs_a_skill_that_exists(session):
    import uuid
    with pytest.raises(CatalogueError, match="no such skill"):
        create_assessment(session, skill_id=uuid.uuid4(), title="Orphan",
                          method="observed", pass_score=3)


def test_an_unknown_category_is_refused(session):
    with pytest.raises(CatalogueError, match="category must be"):
        create_skill(session, skill_code="x", skill_name="X",
                     category="Retail")


# --- what the catalogue shows ----------------------------------------------

def test_a_skill_with_no_assessment_is_visible_as_unscoreable(
    session, greeting
):
    """It cannot be scored, so requiring it on a request filters on nothing."""
    listed = {s["skill_code"]: s for s in list_skills(session)}
    assert listed["retail_greeting"]["assessment_count"] == 0


def test_the_listing_says_why_bounds_are_locked(
    session, greeting_assessment, make_candidate, staff_id
):
    before = list_assessments(session)[0]
    assert before["bounds_editable"] is True

    record_assessment_result(
        session, make_candidate(), greeting_assessment, 3, staff_id
    )
    after = list_assessments(session)[0]
    assert after["results_recorded"] == 1
    assert after["bounds_editable"] is False


def test_an_assessor_gets_the_rubric_and_the_scale(
    session, greeting_assessment
):
    """The rubric was stored and shown nowhere, which is how two coordinators
    score the same performance differently."""
    scoring = assessment_for_scoring(session, greeting_assessment)
    assert scoring["max_score"] == 5
    assert scoring["pass_score"] == 3
    assert "greets and offers help" in scoring["rubric"]
    assert scoring["skill_code"] == "retail_greeting"


# --- authorisation ---------------------------------------------------------

def test_defining_a_pass_mark_requires_an_administrator(client, session):
    """It decides who is eligible for work, which is policy, not data entry."""
    session.execute(
        text("UPDATE staff SET role = 'coordinator'")
    )
    refused = client.post("/skills", json={
        "skill_code": "kitchen_prep", "skill_name": "Kitchen prep",
        "category": "hospitality",
    })
    assert refused.status_code == 403


def test_a_coordinator_can_still_read_the_catalogue(client, session, greeting):
    session.execute(text("UPDATE staff SET role = 'coordinator'"))
    listed = client.get("/skills")
    assert listed.status_code == 200
    assert any(s["skill_code"] == "retail_greeting"
               for s in listed.json()["skills"])


def test_recording_a_result_reports_the_scale_not_a_bare_number(
    client, greeting_assessment, make_candidate
):
    """"4" is meaningless alone, and it is what gets read aloud to an
    employer asking why this person."""
    recorded = client.post(
        f"/candidates/{make_candidate()}/assessments",
        json={"assessment_id": str(greeting_assessment), "score": 4},
    )
    assert recorded.status_code == 201, recorded.text
    body = recorded.json()
    assert (body["score"], body["max_score"], body["passed"]) == (4, 5, True)
    assert body["skill_code"] == "retail_greeting"
