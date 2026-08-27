"""The catalogue and the candidate page in the browser.

Both were missing entirely. The build order puts coordinators in the admin web
app, and there they could not define a skill, could not see a rubric, and had
no page for one person at all -- so scoring, which is a weeks 1-6 deliverable,
was reachable only through the API.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.operations.catalogue import create_assessment, create_skill


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the rendered form"
    return match.group(1)


@pytest.fixture
def web(api, staff_login):
    from tests.test_web_ui import totp_now

    api.post(
        "/ui/login",
        data={"phone": staff_login["phone"], "password": staff_login["password"]},
        follow_redirects=True,
    )
    page = api.get("/ui/mfa").text
    api.post(
        "/ui/mfa",
        data={"csrf_token": csrf(page),
              "code": totp_now(staff_login["totp_secret"], 1)},
        follow_redirects=True,
    )
    return api


@pytest.fixture
def greeting(session):
    skill_id = create_skill(
        session, skill_code="retail_greeting",
        skill_name="Greeting customers", category="retail",
    )
    assessment_id = create_assessment(
        session, skill_id=skill_id, title="Counter greeting, observed",
        method="observed", pass_score=3, max_score=5,
        rubric="1 no greeting; 3 greets and offers help; 5 also closes",
    )
    return {"skill_id": skill_id, "assessment_id": assessment_id}


# --- the catalogue ---------------------------------------------------------

def test_an_empty_catalogue_says_why_that_matters(web):
    """Not just "no rows": until a skill exists nothing can be required."""
    page = web.get("/ui/catalogue")
    assert page.status_code == 200
    assert "Nothing defined yet" in page.text
    assert "no work request can require" in " ".join(page.text.split())


def test_a_skill_can_be_defined_from_the_browser(web):
    page = web.get("/ui/catalogue").text
    web.post("/ui/catalogue/skills",
             data={"csrf_token": csrf(page), "skill_code": "Kitchen Prep",
                   "skill_name": "Kitchen preparation",
                   "category": "hospitality"},
             follow_redirects=True)
    assert "kitchen_prep" in web.get("/ui/catalogue").text


def test_a_duplicate_skill_reports_the_reason(web):
    page = web.get("/ui/catalogue").text
    fields = {"csrf_token": csrf(page), "skill_code": "kitchen_prep",
              "skill_name": "Kitchen preparation", "category": "hospitality"}
    web.post("/ui/catalogue/skills", data=fields, follow_redirects=True)
    again = web.post("/ui/catalogue/skills", data=fields, follow_redirects=True)
    assert "already exists" in again.text


def test_the_catalogue_shows_a_skill_that_cannot_be_scored(web, session):
    """A skill with no assessment filters on nothing when required."""
    create_skill(session, skill_code="forklift", skill_name="Forklift",
                 category="logistics")
    assert "cannot be scored" in web.get("/ui/catalogue").text


def test_the_rubric_is_visible_in_the_catalogue(web, greeting):
    assert "greets and offers help" in web.get("/ui/catalogue").text


def test_a_coordinator_is_not_offered_the_authoring_forms(web, session):
    """Defining a pass mark is policy. Do not show a form that will refuse."""
    session.execute(text("UPDATE staff SET role = 'coordinator'"))
    page = web.get("/ui/catalogue").text
    assert "Define a skill" not in page


def test_a_coordinator_posting_the_form_anyway_is_refused(web, session):
    """Hiding a form is presentation. The refusal is the control."""
    page = web.get("/ui/catalogue").text
    token = csrf(page)
    session.execute(text("UPDATE staff SET role = 'coordinator'"))
    refused = web.post("/ui/catalogue/skills",
                       data={"csrf_token": token, "skill_code": "sneaky",
                             "skill_name": "Sneaky", "category": "other"},
                       follow_redirects=True)
    assert "Only an administrator" in refused.text
    assert session.execute(
        text("SELECT count(*) FROM skills WHERE skill_code = 'sneaky'")
    ).scalar_one() == 0


# --- one candidate ---------------------------------------------------------

def test_the_candidate_list_links_to_each_person(web, make_candidate):
    make_candidate()
    assert re.search(r'/ui/candidates/[0-9a-f-]{36}', web.get("/ui/candidates").text)


def test_a_candidate_page_shows_availability_and_consent(web, make_candidate):
    page = web.get(f"/ui/candidates/{make_candidate()}")
    assert page.status_code == 200
    assert "Availability" in page.text
    assert "Consent" in page.text


def test_an_unknown_candidate_does_not_error(web):
    import uuid
    page = web.get(f"/ui/candidates/{uuid.uuid4()}", follow_redirects=True)
    assert page.status_code == 200
    assert "No such candidate" in page.text


def test_a_score_can_be_recorded_from_the_candidate_page(
    web, greeting, make_candidate, session
):
    candidate_id = make_candidate()
    page = web.get(f"/ui/candidates/{candidate_id}").text
    recorded = web.post(
        f"/ui/candidates/{candidate_id}/assessments",
        data={"csrf_token": csrf(page),
              "assessment_id": str(greeting["assessment_id"]),
              "score": 4, "notes": "greeted and offered help"},
        follow_redirects=True,
    )
    assert "retail_greeting 4/5" in recorded.text
    assert "passed" in recorded.text


def test_a_score_below_the_pass_mark_says_so(web, greeting, make_candidate):
    candidate_id = make_candidate()
    page = web.get(f"/ui/candidates/{candidate_id}").text
    recorded = web.post(
        f"/ui/candidates/{candidate_id}/assessments",
        data={"csrf_token": csrf(page),
              "assessment_id": str(greeting["assessment_id"]), "score": 2},
        follow_redirects=True,
    )
    assert "below the pass mark" in recorded.text


def test_a_score_above_the_maximum_is_refused_by_the_database(
    web, greeting, make_candidate, session
):
    """Refused by trigger, not by the form -- a paper-sheet import is held to
    the same rule, and the page reports what the database said."""
    candidate_id = make_candidate()
    page = web.get(f"/ui/candidates/{candidate_id}").text
    refused = web.post(
        f"/ui/candidates/{candidate_id}/assessments",
        data={"csrf_token": csrf(page),
              "assessment_id": str(greeting["assessment_id"]), "score": 9},
        follow_redirects=True,
    )
    assert "9" in refused.text and "5" in refused.text
    assert session.execute(
        text("SELECT count(*) FROM assessment_results")
    ).scalar_one() == 0


def test_the_rubric_is_in_front_of_whoever_scores(web, greeting, make_candidate):
    """It was stored and shown nowhere, which is how two coordinators
    score the same performance differently."""
    page = web.get(f"/ui/candidates/{make_candidate()}").text
    assert "greets and offers help" in page


def test_a_candidate_page_with_no_assessments_points_at_the_catalogue(
    web, make_candidate
):
    page = web.get(f"/ui/candidates/{make_candidate()}").text
    assert "/ui/catalogue" in page


def test_the_candidate_page_shows_no_identity_data(web, make_candidate, session):
    """Legal names and national ID stay behind the audited read.

    The legal name is set to something that cannot collide with the display
    name, so the assertion is about the page and not about the fixture.
    """
    candidate_id = make_candidate()
    session.execute(
        text("UPDATE candidate_identity "
             "SET legal_first_name = 'Nyiraneza', national_id = '1199988887777666' "
             "WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    )
    page = web.get(f"/ui/candidates/{candidate_id}").text
    assert "Nyiraneza" not in page
    assert "1199988887777666" not in page
