"""Cohort management, and the all-female cohort option.

The last of the seven things the blueprint lists for weeks 1-6. Most of it is
ordinary grouping; the part that carries weight is `women_only`.

The blueprint asks for all-female cohort options as a concrete measure for
women's participation, because female unemployment runs 15.5% against 11.6%
male and tracking the gap is not a plan. A woman who will not attend a mixed
session is not served by a system that offers her one anyway -- so the rule is
enforced in the database, not only in the code path that happens to be in front
of it today.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.operations.cohorts import (
    CohortError,
    add_member,
    cohort_members,
    create_cohort,
    list_cohorts,
    record_outcome,
    set_status,
    training_effect,
)

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


@pytest.fixture
def cohort(session, staff_id):
    def _make(women_only=False, capacity=None, name="Cleaning orientation"):
        return create_cohort(
            session, name=name, starts_on=TODAY, facilitator=staff_id,
            sector="cleaning", women_only=women_only, capacity=capacity,
        )

    return _make


def status_of(session, candidate_id) -> str:
    return session.execute(
        text("SELECT status::text FROM candidates WHERE candidate_id = :c"),
        {"c": candidate_id},
    ).scalar_one()


# --- the women-only promise ------------------------------------------------

def test_a_women_only_cohort_admits_women(session, cohort, make_candidate):
    cohort_id = cohort(women_only=True)
    add_member(session, cohort_id, make_candidate(gender="F"))
    assert len(cohort_members(session, cohort_id)) == 1


def test_a_women_only_cohort_refuses_a_man(session, cohort, make_candidate):
    cohort_id = cohort(women_only=True)
    with pytest.raises(CohortError, match="women-only"):
        add_member(session, cohort_id, make_candidate(gender="M"))


def test_a_women_only_cohort_refuses_an_unrecorded_gender(
    session, cohort, make_candidate
):
    """Unknown is not a pass. The promise is specific, and 'we did not ask'
    does not satisfy it."""
    cohort_id = cohort(women_only=True)
    cid = make_candidate(gender="F")
    session.execute(
        text("UPDATE candidates SET gender = NULL WHERE candidate_id = :c"),
        {"c": cid},
    )
    with pytest.raises(CohortError, match="women-only"):
        add_member(session, cohort_id, cid)


def test_the_rule_holds_against_a_direct_insert(
    session, cohort, make_candidate
):
    """Enforced in the database, so it does not depend on every future code
    path remembering it."""
    cohort_id = cohort(women_only=True)
    with pytest.raises(Exception, match="women-only"):
        session.execute(
            text(
                "INSERT INTO cohort_members (cohort_id, candidate_id) "
                "VALUES (:co, :ca)"
            ),
            {"co": cohort_id, "ca": make_candidate(gender="M")},
        )


def test_a_mixed_cohort_admits_anyone(session, cohort, make_candidate):
    cohort_id = cohort(women_only=False)
    add_member(session, cohort_id, make_candidate(gender="M"))
    add_member(session, cohort_id, make_candidate(gender="F", name="Aline"))
    assert len(cohort_members(session, cohort_id)) == 2


# --- capacity --------------------------------------------------------------

def test_a_full_cohort_refuses_another_place(session, cohort, make_candidate):
    """A room has a size, and turning someone away at the door after telling
    them to come is worse than not enrolling them."""
    cohort_id = cohort(capacity=2)
    add_member(session, cohort_id, make_candidate(name="One"))
    add_member(session, cohort_id, make_candidate(name="Two"))
    with pytest.raises(CohortError, match="full"):
        add_member(session, cohort_id, make_candidate(name="Three"))


def test_capacity_must_be_at_least_one(session, staff_id):
    with pytest.raises(CohortError, match="at least one place"):
        create_cohort(
            session, name="Empty", starts_on=TODAY, facilitator=staff_id,
            capacity=0,
        )


def test_enrolling_the_same_person_twice_is_harmless(
    session, cohort, make_candidate
):
    cohort_id = cohort()
    cid = make_candidate()
    add_member(session, cohort_id, cid)
    add_member(session, cohort_id, cid)
    assert len(cohort_members(session, cohort_id)) == 1


# --- outcomes and status ---------------------------------------------------

def test_finishing_a_cohort_makes_someone_trained(
    session, cohort, make_candidate
):
    """What candidate_status.trained has been reserved for since migration 002."""
    cohort_id = cohort()
    cid = make_candidate()
    add_member(session, cohort_id, cid)
    assert status_of(session, cid) == "registered"

    record_outcome(session, cohort_id, cid, "completed")
    assert status_of(session, cid) == "trained"


def test_withdrawing_does_not_make_someone_trained(
    session, cohort, make_candidate
):
    """Recording otherwise would flatter both the cohort numbers and the
    candidate's readiness."""
    cohort_id = cohort()
    cid = make_candidate()
    add_member(session, cohort_id, cid)
    record_outcome(session, cohort_id, cid, "withdrew", notes="found other work")
    assert status_of(session, cid) == "registered"


def test_being_placed_outranks_being_trained(
    session, cohort, make_candidate, make_placement, make_request
):
    cohort_id = cohort()
    cid = make_candidate()
    add_member(session, cohort_id, cid)
    record_outcome(session, cohort_id, cid, "completed")
    assert status_of(session, cid) == "trained"

    from app.operations.attendance import start_placement

    start_placement(
        session, make_placement(candidate_id=cid, request_id=make_request()),
        TODAY,
    )
    assert status_of(session, cid) == "placed"


def test_finishing_a_placement_returns_them_to_trained(
    session, cohort, make_candidate, make_placement, make_request
):
    """Not to 'registered' -- the training still happened."""
    from app.operations.attendance import complete_placement, start_placement

    cohort_id = cohort()
    cid = make_candidate()
    add_member(session, cohort_id, cid)
    record_outcome(session, cohort_id, cid, "completed")

    placement = make_placement(candidate_id=cid, request_id=make_request())
    start_placement(session, placement, TODAY)
    complete_placement(session, placement)
    assert status_of(session, cid) == "trained"


def test_an_outcome_for_a_non_member_is_refused(
    session, cohort, make_candidate
):
    with pytest.raises(CohortError, match="not in this cohort"):
        record_outcome(session, cohort(), make_candidate(), "completed")


def test_an_unknown_outcome_is_refused(session, cohort, make_candidate):
    cohort_id = cohort()
    cid = make_candidate()
    add_member(session, cohort_id, cid)
    with pytest.raises(CohortError, match="outcome must be one of"):
        record_outcome(session, cohort_id, cid, "sort of finished")


# --- lifecycle -------------------------------------------------------------

def test_a_finished_cohort_takes_no_new_members(
    session, cohort, make_candidate
):
    cohort_id = cohort()
    set_status(session, cohort_id, "completed")
    with pytest.raises(CohortError, match="completed"):
        add_member(session, cohort_id, make_candidate())


def test_a_cohort_cannot_end_before_it_starts(session, staff_id):
    with pytest.raises(CohortError, match="cannot end before it starts"):
        create_cohort(
            session, name="Backwards", starts_on=TODAY, facilitator=staff_id,
            ends_on=TODAY - timedelta(days=1),
        )


def test_finished_cohorts_are_hidden_by_default(session, cohort):
    cohort_id = cohort(name="Old cohort")
    assert len(list_cohorts(session)) == 1
    set_status(session, cohort_id, "completed")
    assert list_cohorts(session) == []
    assert len(list_cohorts(session, include_finished=True)) == 1


def test_the_facilitator_is_a_named_person(session, cohort, staff_id):
    """"Who ran this cohort" needs an answer months later, and rotas change."""
    cohort(name="Named")
    listed = list_cohorts(session)[0]
    assert listed["facilitator_name"] == "Coordinator"


# --- the register does not leak identity -----------------------------------

def test_the_register_carries_no_identity_data(
    session, cohort, make_candidate
):
    cohort_id = cohort()
    cid = make_candidate(name="Aline U.")
    add_member(session, cohort_id, cid)

    identity = session.execute(
        text(
            "SELECT legal_first_name, legal_last_name, phone_primary "
            "FROM candidate_identity WHERE candidate_id = :c"
        ),
        {"c": cid},
    ).mappings().one()

    blob = str(cohort_members(session, cohort_id))
    assert "Aline U." in blob
    for value in identity.values():
        assert str(value) not in blob


# --- does training help? ---------------------------------------------------

def test_the_training_effect_compares_both_groups(
    session, cohort, make_candidate, make_placement, make_request
):
    from app.operations.attendance import start_placement

    cohort_id = cohort()
    trained = make_candidate(name="Trained")
    add_member(session, cohort_id, trained)
    record_outcome(session, cohort_id, trained, "completed")
    start_placement(
        session,
        make_placement(candidate_id=trained, request_id=make_request()),
        TODAY,
    )

    make_candidate(name="Untrained")

    effect = training_effect(session)
    assert effect["trained"] == 1
    assert effect["trained_placed"] == 1
    assert effect["untrained"] >= 1
    assert effect["untrained_placed"] == 0


# --- over HTTP -------------------------------------------------------------

def test_the_cohort_flow_over_http(client, session, make_candidate):
    created = client.post(
        "/cohorts",
        json={
            "name": "Cleaning orientation", "starts_on": str(TODAY),
            "sector": "cleaning", "women_only": True, "capacity": 12,
        },
    )
    assert created.status_code == 201
    cohort_id = created.json()["cohort_id"]

    woman = make_candidate(gender="F", name="Aline")
    assert client.post(
        f"/cohorts/{cohort_id}/members", json={"candidate_id": str(woman)}
    ).status_code == 201

    man = make_candidate(gender="M", name="Eric")
    refused = client.post(
        f"/cohorts/{cohort_id}/members", json={"candidate_id": str(man)}
    )
    assert refused.status_code == 409
    assert "women-only" in refused.json()["detail"]

    assert client.post(
        f"/cohorts/{cohort_id}/outcomes",
        json={"candidate_id": str(woman), "outcome": "completed"},
    ).status_code == 200
    assert status_of(session, woman) == "trained"

    listed = client.get("/cohorts").json()["cohorts"]
    assert listed[0]["women_only"] is True
    assert listed[0]["finished"] == 1


def test_cohorts_require_auth(api):
    assert api.get("/cohorts").status_code == 401


def test_a_refusal_leaves_the_transaction_usable(
    session, cohort, make_candidate
):
    """A coordinator enrolling a list must not lose everyone after the first
    person who is turned away."""
    cohort_id = cohort(women_only=True)
    add_member(session, cohort_id, make_candidate(gender="F", name="First"))

    with pytest.raises(CohortError):
        add_member(session, cohort_id, make_candidate(gender="M", name="Man"))

    add_member(session, cohort_id, make_candidate(gender="F", name="Third"))
    assert len(cohort_members(session, cohort_id)) == 2
