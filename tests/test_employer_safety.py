"""What workers say about an employer, and who may read it.

The employer rates the worker, and that rating is shown to them. The worker
rated the employer at follow-up and nothing ever read it back -- it appeared
in a subject access export and nowhere else. That asymmetry is the power
imbalance the blueprint asks this business to correct, and it names the case
that matters most: "employer safety ratings written by women who worked
there", listed as a product requirement rather than a reporting line.
"""
from __future__ import annotations


import pytest
from sqlalchemy import text

from app.operations.safety import (
    SafetyReportError,
    employer_safety,
    employers_of_concern,
    record_safety_report,
)
from tests.conftest import csrf


@pytest.fixture
def woman(session, make_candidate):
    def _make():
        candidate_id = make_candidate()
        session.execute(
            text("UPDATE candidates SET gender = 'F' WHERE candidate_id = :c"),
            {"c": str(candidate_id)},
        )
        return candidate_id
    return _make


def placement_for(session, make_placement, candidate_id, request_id=None):
    return make_placement(candidate_id=candidate_id, request_id=request_id)


# --- recording -------------------------------------------------------------

def test_a_worker_can_say_she_did_not_feel_safe(session, make_placement, woman):
    placement_id = placement_for(session, make_placement, woman())
    record_safety_report(session, placement_id=placement_id, felt_safe=False,
                         concern="unsafe_hours")
    employer_id = session.execute(
        text("SELECT employer_id FROM employer_safety_reports")
    ).scalar_one()
    assert employer_safety(session, employer_id)["felt_unsafe_women"] == 1


def test_not_feeling_safe_needs_a_concern(session, make_placement, woman):
    """An unexplained flag cannot be acted on."""
    placement_id = placement_for(session, make_placement, woman())
    with pytest.raises(SafetyReportError, match="what the concern was"):
        record_safety_report(session, placement_id=placement_id,
                             felt_safe=False)


def test_feeling_safe_needs_no_concern(session, make_placement, woman):
    placement_id = placement_for(session, make_placement, woman())
    assert record_safety_report(session, placement_id=placement_id,
                                felt_safe=True) is not None


def test_an_invented_concern_is_refused(session, make_placement, woman):
    placement_id = placement_for(session, make_placement, woman())
    with pytest.raises(SafetyReportError, match="concern must be one of"):
        record_safety_report(session, placement_id=placement_id,
                             felt_safe=False, concern="vibes")


def test_asked_again_later_the_newer_answer_replaces_the_older(
    session, make_placement, woman
):
    """What she thinks now is the thing worth knowing."""
    candidate_id = woman()
    placement_id = placement_for(session, make_placement, candidate_id)
    record_safety_report(session, placement_id=placement_id, felt_safe=True)
    record_safety_report(session, placement_id=placement_id, felt_safe=False,
                         concern="pressure_to_work_unpaid")

    employer_id = session.execute(
        text("SELECT employer_id FROM employer_safety_reports")
    ).scalar_one()
    safety = employer_safety(session, employer_id)
    assert safety["reports"] == 1
    assert safety["felt_unsafe"] == 1


def test_reporting_harassment_raises_the_escalation(session, make_placement,
                                                    woman, staff_id):
    """It must not sit in a table waiting for someone to read a report."""
    placement_id = placement_for(session, make_placement, woman())
    record_safety_report(session, placement_id=placement_id, felt_safe=False,
                         concern="harassment", note="the supervisor")
    kinds = session.execute(
        text("SELECT kind::text FROM escalations")
    ).scalars().all()
    assert "harassment" in kinds


# --- what a coordinator is told -------------------------------------------

def test_the_summary_speaks_about_women_when_women_have_worked_there(
    session, make_placement, woman, make_request
):
    request_id = make_request()
    for felt_safe in (True, True, False):
        record_safety_report(
            session,
            placement_id=placement_for(session, make_placement, woman(),
                                       request_id=request_id),
            felt_safe=felt_safe,
            concern=None if felt_safe else "transport_after_dark",
        )
    employer_id = session.execute(
        text("SELECT employer_id FROM employer_safety_reports LIMIT 1")
    ).scalar_one()
    safety = employer_safety(session, employer_id)
    assert "2 of 3 women who worked here felt safe" in safety["summary"]
    assert "transport after dark" in safety["summary"]


def test_it_says_so_when_no_woman_has_worked_there(session, make_placement,
                                                   make_candidate):
    placement_id = placement_for(session, make_placement, make_candidate())
    session.execute(text("UPDATE candidates SET gender = 'M'"))
    record_safety_report(session, placement_id=placement_id, felt_safe=True)
    employer_id = session.execute(
        text("SELECT employer_id FROM employer_safety_reports")
    ).scalar_one()
    assert "no woman has worked here yet" in employer_safety(
        session, employer_id)["summary"]


def test_enough_reports_turn_it_into_a_warning(session, make_placement, woman,
                                               make_request):
    request_id = make_request()
    for felt_safe in (True, False, False):
        record_safety_report(
            session,
            placement_id=placement_for(session, make_placement, woman(),
                                       request_id=request_id),
            felt_safe=felt_safe,
            concern=None if felt_safe else "harassment",
        )
    employer_id = session.execute(
        text("SELECT employer_id FROM employer_safety_reports LIMIT 1")
    ).scalar_one()
    assert employer_safety(session, employer_id)["warn"] is True


def test_one_good_report_is_not_a_warning(session, make_placement, woman):
    placement_id = placement_for(session, make_placement, woman())
    record_safety_report(session, placement_id=placement_id, felt_safe=True)
    employer_id = session.execute(
        text("SELECT employer_id FROM employer_safety_reports")
    ).scalar_one()
    assert employer_safety(session, employer_id)["warn"] is False


def test_nobody_is_blocked_automatically(session, make_placement, woman,
                                         make_request):
    """Refusing to trade with an employer is the owner's decision, and a
    threshold invented in code would make it silently."""
    from app.matching.repository import find_matches

    request_id = make_request()
    placement_id = placement_for(session, make_placement, woman(),
                                 request_id=request_id)
    record_safety_report(session, placement_id=placement_id, felt_safe=False,
                         concern="harassment")
    # The matcher is unchanged by safety reports; the warning goes to a person.
    assert find_matches(session, request_id) is not None


def test_employers_of_concern_lists_the_worst_first(session, make_placement,
                                                     woman, make_request):
    request_id = make_request()
    record_safety_report(
        session,
        placement_id=placement_for(session, make_placement, woman(),
                                   request_id=request_id),
        felt_safe=False, concern="unsafe_equipment",
    )
    listed = employers_of_concern(session)
    assert len(listed) == 1
    assert listed[0]["felt_unsafe_women"] == 1


# --- who may read it -------------------------------------------------------

def test_no_employer_facing_route_touches_the_safety_view():
    """An employer told one of two women did not feel safe knows who said it,
    and the consequence lands on her. There is no threshold that makes it safe
    to show, so nothing employer-facing may read it."""
    from pathlib import Path

    employer_facing = [
        Path("app/web/employer_router.py"),
        Path("app/operations/employer_portal.py"),
        Path("app/web/employer_deps.py"),
    ]
    for path in employer_facing:
        source = path.read_text()
        for forbidden in ("v_employer_safety", "employer_safety_reports",
                          "employers_of_concern", "record_safety_report"):
            assert forbidden not in source, f"{path.name} reads {forbidden}"


def test_the_employer_templates_do_not_render_it():
    from pathlib import Path

    for path in Path("app/web/templates/employer").rglob("*.html"):
        source = path.read_text()
        assert "felt_safe" not in source, path.name
        assert "safety." not in source, path.name


def test_the_check_actually_examined_employer_facing_files():
    """Guards the guard: a moved file would make the test above vacuous."""
    from pathlib import Path

    assert Path("app/web/employer_router.py").exists()
    assert len(list(Path("app/web/templates/employer").rglob("*.html"))) >= 4


def test_a_coordinator_sees_it_on_the_matches_page(web, session, make_request,
                                                   make_placement, woman):
    request_id = make_request()
    record_safety_report(
        session,
        placement_id=placement_for(session, make_placement, woman(),
                                   request_id=request_id),
        felt_safe=False, concern="unsafe_hours",
    )
    page = web.get(f"/ui/requests/{request_id}").text
    assert "What their workers say" in page
    assert "unsafe hours" in page


def test_it_is_collected_at_the_check_in(web, session, make_placement, woman):
    placement_id = placement_for(session, make_placement, woman())
    session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', CURRENT_DATE)"),
        {"p": str(placement_id)},
    )
    follow_up_id = session.execute(
        text("SELECT follow_up_id FROM follow_ups WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one()

    page = web.get(f"/ui/placements/{placement_id}").text
    assert 'name="felt_safe"' in page

    web.post(f"/ui/follow-ups/{follow_up_id}/complete",
             data={"csrf_token": csrf(page), "still_working": "true",
                   "felt_safe": "false", "safety_concern": "unsafe_hours"},
             follow_redirects=True)
    assert session.execute(
        text("SELECT count(*) FROM employer_safety_reports")
    ).scalar_one() == 1


def test_a_safety_report_can_be_recorded_through_the_api(client,
                                                          make_placement,
                                                          session):
    placement_id = make_placement()
    recorded = client.post(f"/placements/{placement_id}/safety",
                           json={"felt_safe": False, "concern": "unsafe_hours",
                                 "note": "asked to stay late"})
    assert recorded.status_code == 201, recorded.text

    employer_id = session.execute(
        text("SELECT employer_id FROM employer_safety_reports")
    ).scalar_one()
    summary = client.get(f"/employers/{employer_id}/safety").json()
    assert summary["felt_unsafe"] == 1


def test_the_api_still_requires_a_concern_when_unsafe(client, make_placement):
    refused = client.post(f"/placements/{make_placement()}/safety",
                          json={"felt_safe": False})
    assert refused.status_code == 422
    assert "what the concern was" in refused.json()["detail"]
