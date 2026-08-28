"""Placement contracts.

What was agreed, recorded when it was agreed. The point of the whole thing is
the dispute six weeks later: something has to say what the worker was told when
they said yes, and it cannot be the live rows, which have moved on.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.operations.contracts import (
    ContractError,
    acknowledge,
    get_contract,
    issue_contract,
    render_contract,
    unacknowledged,
)
from app.operations.requests import respond_to_offer


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


@pytest.fixture
def accepted(session, make_placement, make_candidate, make_request):
    """A placement someone has accepted, which issues a contract."""
    def _make(pay_rwf=5000, transport_rwf=1150):
        request_id = make_request(pay_rwf=pay_rwf)
        session.execute(
            text(
                "UPDATE work_requests SET shift_start = '08:00', "
                "shift_end = '16:00' WHERE request_id = :r"
            ),
            {"r": request_id},
        )
        placement = make_placement(
            candidate_id=make_candidate(name="Aline U."),
            request_id=request_id, pay_rwf=pay_rwf, transport_rwf=transport_rwf,
        )
        respond_to_offer(session, placement, accepted=True)
        return placement

    return _make


# --- issued at acceptance --------------------------------------------------

def test_accepting_issues_a_contract(session, accepted):
    contract = get_contract(session, accepted())
    assert contract is not None
    assert contract["contract_ref"].startswith("AKZ-")


def test_the_reference_is_quotable_over_the_phone(session, accepted):
    """A UUID is unusable for someone reading a reference off a printed page."""
    ref = get_contract(session, accepted())["contract_ref"]
    assert len(ref) <= 16
    assert ref.replace("-", "").isalnum()


def test_the_reference_lands_on_the_placement(session, accepted):
    """placements.contract_ref existed from the first migration with nothing
    writing it."""
    placement = accepted()
    ref = session.execute(
        text("SELECT contract_ref FROM placements WHERE placement_id = :p"),
        {"p": placement},
    ).scalar_one()
    assert ref == get_contract(session, placement)["contract_ref"]


def test_a_placement_gets_one_contract(session, accepted):
    placement = accepted()
    with pytest.raises(ContractError, match="already has contract"):
        issue_contract(session, placement)


def test_an_unaccepted_placement_has_nothing_to_record(
    session, make_placement, make_candidate
):
    """A contract records an agreement; an outstanding offer is not one."""
    with pytest.raises(ContractError, match="this placement is offered"):
        issue_contract(session, make_placement(candidate_id=make_candidate()))


# --- the snapshot ----------------------------------------------------------

def test_the_terms_do_not_follow_later_edits(session, accepted):
    """The question in a dispute is what the worker was told when they said
    yes, and only a snapshot answers it."""
    placement = accepted(pay_rwf=5000)
    session.execute(
        text(
            "UPDATE work_requests SET pay_rwf = 2000, shift_start = '05:00' "
            "WHERE request_id = (SELECT request_id FROM placements "
            "                     WHERE placement_id = :p)"
        ),
        {"p": placement},
    )
    terms = get_contract(session, placement)["terms"]
    assert terms["pay_rwf"] == 5000
    assert terms["shift_start"] == "08:00"


def test_the_terms_cannot_be_rewritten(session, accepted):
    """Editing a contract after the fact is exactly what a party to a dispute
    would want to do, so it fails loudly rather than being quietly ignored."""
    placement = accepted()
    savepoint = session.begin_nested()
    with pytest.raises(Exception, match="cannot be changed"):
        session.execute(
            text(
                "UPDATE placement_contracts "
                "SET terms = '{\"pay_rwf\": 1}'::jsonb WHERE placement_id = :p"
            ),
            {"p": placement},
        )
    savepoint.rollback()
    assert get_contract(session, placement)["terms"]["pay_rwf"] == 5000


def test_the_reference_cannot_be_rewritten(session, accepted):
    placement = accepted()
    before = get_contract(session, placement)["contract_ref"]
    savepoint = session.begin_nested()
    with pytest.raises(Exception, match="cannot be reassigned"):
        session.execute(
            text(
                "UPDATE placement_contracts SET contract_ref = 'AKZ-0000-00000' "
                "WHERE placement_id = :p"
            ),
            {"p": placement},
        )
    savepoint.rollback()
    assert get_contract(session, placement)["contract_ref"] == before


# --- what it says ----------------------------------------------------------

def test_it_states_pay_net_of_transport(session, accepted):
    """Gross pay is not what someone takes home, and the contract is where
    that has to be unambiguous."""
    terms = get_contract(session, accepted(pay_rwf=5000, transport_rwf=1150))["terms"]
    assert terms["estimated_net_rwf"] == 3850

    text_out = render_contract(terms, "AKZ-2026-00001")
    assert "RWF 5,000 per day" in text_out
    assert "leaving about RWF 3,850" in text_out


def test_employer_covered_transport_does_not_reduce_the_net(session, accepted):
    placement = accepted(pay_rwf=5000, transport_rwf=1150)
    session.execute(
        text(
            "UPDATE placement_contracts SET terms = terms WHERE placement_id = :p"
        ),
        {"p": placement},
    )
    # A fresh contract on a transport-covered request.
    terms = dict(get_contract(session, placement)["terms"])
    terms["transport_covered"] = True
    terms["estimated_net_rwf"] = terms["pay_rwf"]
    assert "paid by the employer" in render_contract(terms, "AKZ-X")


def test_it_says_there_is_no_fee(session, accepted):
    """No pay-to-apply model, and the person should be told so in writing."""
    terms = get_contract(session, accepted())["terms"]
    assert terms["no_fee_to_apply"] is True
    assert "no fee to take this work" in render_contract(terms, "AKZ-X").lower()


def test_it_tells_them_what_to_do_if_unpaid_or_unsafe(session, accepted):
    """A contract that only lists obligations is not a protection."""
    rendered = render_contract(get_contract(session, accepted())["terms"], "AKZ-X")
    assert "not paid in full" in rendered
    assert "do not feel safe" in rendered
    assert "not hold it against you" in rendered


def test_an_unestimated_fare_is_not_presented_as_free(session, accepted):
    terms = get_contract(session, accepted(transport_rwf=0))["terms"]
    assert "not estimated" in render_contract(terms, "AKZ-X")


# --- the worker gets a copy ------------------------------------------------

def test_the_worker_is_sent_their_copy(session, accepted):
    """A contract only the operator holds is not much of a protection."""
    placement = accepted()
    body = session.execute(
        text(
            "SELECT body FROM messages WHERE placement_id = :p "
            "AND template_key = 'placement_contract'"
        ),
        {"p": placement},
    ).scalar_one()
    assert "AKAZI PLACEMENT AGREEMENT" in body
    assert "Keep this message" in body


def test_the_contract_is_sent_once(session, accepted):
    """Two copies of an agreement gives a worker reason to wonder which holds."""
    placement = accepted()
    from app.messaging.events import on_contract_issued

    on_contract_issued(session, placement, get_contract(session, placement))
    count = session.execute(
        text(
            "SELECT count(*) FROM messages WHERE placement_id = :p "
            "AND template_key = 'placement_contract'"
        ),
        {"p": placement},
    ).scalar_one()
    assert count == 1


# --- acknowledgement -------------------------------------------------------

def test_both_sides_acknowledge_separately(session, accepted):
    """An employer confirming terms the worker never saw is how informal work
    already goes wrong."""
    placement = accepted()
    acknowledge(session, placement, "employer")

    contract = get_contract(session, placement)
    assert contract["employer_acknowledged_at"] is not None
    assert contract["worker_acknowledged_at"] is None
    assert unacknowledged(session)[0]["worker_pending"] is True


def test_acknowledging_twice_is_refused(session, accepted):
    placement = accepted()
    acknowledge(session, placement, "worker")
    with pytest.raises(ContractError, match="already acknowledged"):
        acknowledge(session, placement, "worker")


def test_an_unknown_party_is_refused(session, accepted):
    with pytest.raises(ContractError, match="worker.*employer"):
        acknowledge(session, accepted(), "somebody else")


def test_a_fully_acknowledged_contract_leaves_the_list(session, accepted):
    placement = accepted()
    acknowledge(session, placement, "worker")
    acknowledge(session, placement, "employer")
    assert unacknowledged(session) == []


def test_an_employer_can_only_acknowledge_their_own(
    session, accepted, staff_id
):
    from app.operations.employer_portal import (
        EmployerPortalError,
        acknowledge_contract,
    )
    from app.operations.registry import register_employer

    other = register_employer(
        session, business_name="Rival", sector="retail", district="Gasabo",
        account_owner=staff_id,
    )
    with pytest.raises(EmployerPortalError, match="no such placement"):
        acknowledge_contract(session, other, staff_id, accepted())


# --- over HTTP -------------------------------------------------------------

def test_the_contract_over_http(client, session, accepted):
    placement = accepted()
    payload = client.get(f"/placements/{placement}/contract").json()
    assert payload["contract_ref"].startswith("AKZ-")
    assert "AKAZI PLACEMENT AGREEMENT" in payload["text"]

    assert client.post(
        f"/placements/{placement}/contract/acknowledge",
        params={"party": "worker"},
    ).status_code == 200

    pending = client.get("/contracts/unacknowledged").json()["unacknowledged"]
    assert pending[0]["worker_pending"] is False
    assert pending[0]["employer_pending"] is True


def test_a_placement_without_a_contract_is_a_404(
    client, make_placement, make_candidate
):
    placement = make_placement(candidate_id=make_candidate())
    assert client.get(f"/placements/{placement}/contract").status_code == 404
