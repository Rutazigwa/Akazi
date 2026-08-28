"""Which employers are worth having, on the evidence.

Every fact needed was already recorded and none of it was grouped by employer:
retention per worker, guarantee invocations per placement, pay accuracy per
pay record. The question the operation turns on -- is this client worth having
-- could not be asked.

It is a question only an operator carrying the guarantee can ask. The fee
includes covering a shift when somebody does not arrive, so an employer whose
shifts repeatedly go uncovered is subsidised by the ones whose do not.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text

from app.operations.employer_health import (
    DISTINCT_NO_SHOWS_BEFORE_CONCERN,
    ENOUGH_TO_JUDGE,
    employer_health,
    employers_needing_a_conversation,
)


def no_show(session, make_placement, make_candidate, request_id):
    placement_id = make_placement(candidate_id=make_candidate(),
                                  request_id=request_id)
    session.execute(
        text("UPDATE placements SET status = 'no_show' WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    return placement_id


def checked_at_30_days(session, make_placement, make_candidate, request_id,
                       still_working):
    placement_id = make_placement(candidate_id=make_candidate(),
                                  request_id=request_id)
    session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on, "
             "completed_at, still_working) VALUES (:p, 'day_30', CURRENT_DATE, "
             "now(), :w)"),
        {"p": str(placement_id), "w": still_working},
    )


def only(session, employer_id):
    return employer_health(session, employer_id)[0]


# --- attribution, carefully ------------------------------------------------

def test_one_worker_not_arriving_says_nothing_about_the_employer(
    session, make_placement, make_candidate, make_request, employer_id
):
    """People have bad days. That is not evidence about a place."""
    request_id = make_request()
    no_show(session, make_placement, make_candidate, request_id)
    assert only(session, employer_id)["findings"] == []


def test_several_different_workers_not_arriving_is_a_pattern(
    session, make_placement, make_candidate, make_request, employer_id
):
    """Unpaid, unreachable, or unpleasant on arrival -- worth asking about."""
    request_id = make_request()
    for _ in range(DISTINCT_NO_SHOWS_BEFORE_CONCERN):
        no_show(session, make_placement, make_candidate, request_id)
    findings = only(session, employer_id)["findings"]
    assert any("did not arrive here" in f for f in findings)


def test_the_same_worker_twice_is_not_a_pattern(
    session, make_placement, make_candidate, make_request, employer_id
):
    """Distinct people, not events -- otherwise one person's bad month
    condemns an employer."""
    candidate_id = make_candidate()
    for _ in range(3):
        request_id = make_request()
        placement_id = make_placement(candidate_id=candidate_id,
                                      request_id=request_id)
        session.execute(
            text("UPDATE placements SET status = 'no_show' "
                 "WHERE placement_id = :p"),
            {"p": str(placement_id)},
        )
    assert only(session, employer_id)["workers_who_did_not_arrive"] == 1
    assert only(session, employer_id)["findings"] == []


# --- the promise we charge for --------------------------------------------

def test_an_uncovered_guarantee_is_reported(session, make_placement,
                                            make_candidate, make_request,
                                            employer_id):
    request_id = make_request()
    placement_id = no_show(session, make_placement, make_candidate, request_id)
    session.execute(
        text("INSERT INTO attendance (placement_id, work_date, present, "
             "confirmed_by, confirmed_at, absence_reason) VALUES "
             "(:p, CURRENT_DATE, FALSE, 'employer', now(), 'no show')"),
        {"p": str(placement_id)},
    )
    findings = only(session, employer_id)["findings"]
    assert any("not covered inside 24 hours" in f for f in findings)


# --- whose workers stay ----------------------------------------------------

def test_workers_leaving_inside_a_month_is_reported(
    session, make_placement, make_candidate, make_request, employer_id
):
    request_id = make_request()
    for still in (True, False, False, False):
        checked_at_30_days(session, make_placement, make_candidate,
                           request_id, still)
    findings = only(session, employer_id)["findings"]
    assert any("still there at 30 days" in f for f in findings)


def test_too_few_check_ins_draws_no_conclusion(
    session, make_placement, make_candidate, make_request, employer_id
):
    """A proportion of two means nothing, and saying it does is worse than
    saying nothing."""
    request_id = make_request()
    for still in (False, False):
        checked_at_30_days(session, make_placement, make_candidate,
                           request_id, still)
    assert only(session, employer_id)["findings"] == []
    assert only(session, employer_id)["checked_at_30_days"] == 2


def test_an_employer_people_stay_with_has_nothing_against_them(
    session, make_placement, make_candidate, make_request, employer_id
):
    request_id = make_request()
    for _ in range(ENOUGH_TO_JUDGE):
        checked_at_30_days(session, make_placement, make_candidate,
                           request_id, True)
    assert only(session, employer_id)["findings"] == []


# --- the list an owner reads ----------------------------------------------

def test_only_employers_with_something_to_answer_are_listed(
    session, make_placement, make_candidate, make_request, employer_id
):
    request_id = make_request()
    for _ in range(ENOUGH_TO_JUDGE):
        checked_at_30_days(session, make_placement, make_candidate,
                           request_id, True)
    assert employers_needing_a_conversation(session) == []


def test_a_cooperative_with_a_problem_is_ranked_higher(
    session, make_employer, make_request, make_placement, make_candidate
):
    """One cooperative relationship is worth fifteen SME ones, so a problem
    there deserves more attention, not less."""
    from app.operations.requests import create_work_request

    def spoil(employer_id):
        request_id = create_work_request(
            session, employer_id=employer_id, title="Shift", work_type="shift",
            headcount=1, starts_on=date.today() + timedelta(days=1),
            pay_rwf=5000, pay_unit="day",
        )
        for _ in range(DISTINCT_NO_SHOWS_BEFORE_CONCERN):
            no_show(session, make_placement, make_candidate, request_id)

    sme = make_employer(name="An SME")
    coop = make_employer(name="A Cooperative", is_cooperative=True)
    spoil(sme)
    spoil(coop)

    listed = employers_needing_a_conversation(session)
    assert listed[0]["business_name"] == "A Cooperative"


def test_the_employers_page_shows_the_findings(
    web, session, make_placement, make_candidate, make_request, employer_id
):
    request_id = make_request()
    for _ in range(DISTINCT_NO_SHOWS_BEFORE_CONCERN):
        no_show(session, make_placement, make_candidate, request_id)
    page = web.get("/ui/employers").text
    assert "did not arrive here" in page


def test_there_is_no_score(session, employer_id):
    """A number cannot be read to anybody, and a finding can."""
    record = only(session, employer_id)
    assert "score" not in record
    assert isinstance(record["findings"], list)
