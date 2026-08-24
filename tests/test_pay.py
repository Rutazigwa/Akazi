"""Recording pay: the write path behind the pay-accuracy metric.

No money moves through the system -- these are records of claims. What is
protected here is that the metric cannot be flattered: a record is opened when
pay falls due, not when it lands, so a payment that never happened stays
visible as a hole.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.operations.attendance import log_attendance, start_placement
from app.operations.pay import (
    PayError,
    confirm_with_worker,
    mark_paid,
    overdue_pay,
    record_pay_period,
    suggest_pay_period,
)

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = date.today()
WEEK_AGO = TODAY - timedelta(days=7)


@pytest.fixture
def worked_placement(session, make_placement, make_candidate):
    """A placement with three confirmed days of attendance behind it."""
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, WEEK_AGO)
    for offset in range(3):
        log_attendance(
            session, pid, WEEK_AGO + timedelta(days=offset), True, "employer",
            hours_worked=8,
        )
    return pid


# --- recording -------------------------------------------------------------

def test_a_pay_period_records_terms_before_payment(session, worked_placement):
    pay_id = record_pay_period(
        session, worked_placement, WEEK_AGO, TODAY, 15_000, due_on=TODAY,
    )
    row = session.execute(
        text("SELECT net_rwf, paid_on, worker_confirmed FROM pay_records "
             "WHERE pay_id = :p"),
        {"p": pay_id},
    ).mappings().one()
    assert row["net_rwf"] == 15_000
    assert row["paid_on"] is None
    assert row["worker_confirmed"] is False


def test_the_suggestion_comes_from_attendance(session, worked_placement):
    """3 confirmed days x the agreed daily rate. The coordinator corrects a
    suggestion instead of typing sums."""
    suggestion = suggest_pay_period(session, worked_placement)
    assert suggestion["days_present"] == 3
    assert suggestion["gross_rwf"] == 3 * 5000
    assert suggestion["period_start"] == WEEK_AGO


def test_overlapping_periods_are_refused(session, worked_placement):
    """Two records covering the same days would double-count the wage."""
    record_pay_period(session, worked_placement, WEEK_AGO, TODAY, 15_000, TODAY)
    with pytest.raises(PayError, match="overlapping"):
        record_pay_period(
            session, worked_placement,
            TODAY - timedelta(days=1), TODAY + timedelta(days=6), 15_000,
            TODAY + timedelta(days=7),
        )


def test_deductions_cannot_exceed_gross(session, worked_placement):
    with pytest.raises(PayError, match="deductions"):
        record_pay_period(
            session, worked_placement, WEEK_AGO, TODAY, 5000, TODAY,
            deductions_rwf=6000,
        )


def test_pay_cannot_fall_due_before_the_period(session, worked_placement):
    with pytest.raises(PayError, match="due before the period"):
        record_pay_period(
            session, worked_placement, WEEK_AGO, TODAY, 5000,
            due_on=WEEK_AGO - timedelta(days=1),
        )


# --- the chase list --------------------------------------------------------

def test_unpaid_past_due_appears_on_the_chase_list(session, worked_placement):
    record_pay_period(
        session, worked_placement, WEEK_AGO, TODAY - timedelta(days=3),
        15_000, due_on=TODAY - timedelta(days=2),
    )
    chase = overdue_pay(session, as_of=TODAY)
    assert len(chase) == 1
    assert chase[0]["days_overdue"] == 2
    assert chase[0]["business_name"] == "Isuku Cooperative"


def test_marking_paid_clears_the_chase_list(session, worked_placement):
    pay_id = record_pay_period(
        session, worked_placement, WEEK_AGO, TODAY - timedelta(days=3),
        15_000, due_on=TODAY - timedelta(days=2),
    )
    mark_paid(session, pay_id, TODAY, method="momo")
    assert overdue_pay(session, as_of=TODAY) == []


def test_paying_twice_is_refused(session, worked_placement):
    pay_id = record_pay_period(
        session, worked_placement, WEEK_AGO, TODAY, 15_000, TODAY
    )
    mark_paid(session, pay_id, TODAY)
    with pytest.raises(PayError, match="already marked paid"):
        mark_paid(session, pay_id, TODAY)


# --- the worker's word -----------------------------------------------------

def test_worker_confirmation_completes_the_accuracy_metric(
    session, worked_placement
):
    """In full, on time, and the worker agrees: only then does it count."""
    pay_id = record_pay_period(
        session, worked_placement, WEEK_AGO, TODAY, 15_000, due_on=TODAY
    )
    mark_paid(session, pay_id, TODAY, method="momo")
    assert confirm_with_worker(session, pay_id, received_in_full=True) is None

    accurate = session.execute(
        text("SELECT paid_in_full_on_time FROM v_pay_accuracy WHERE pay_id = :p"),
        {"p": pay_id},
    ).scalar_one()
    assert accurate is True


def test_a_shortfall_raises_a_pay_escalation_in_the_same_call(
    session, worked_placement, staff_id
):
    """The coordinator on the phone should not have to remember a second step."""
    pay_id = record_pay_period(
        session, worked_placement, WEEK_AGO, TODAY, 15_000, due_on=TODAY
    )
    mark_paid(session, pay_id, TODAY)
    escalation_id = confirm_with_worker(
        session, pay_id, received_in_full=False, note="says RWF 5,000 short"
    )
    assert escalation_id is not None

    row = session.execute(
        text("SELECT kind::text, detail FROM escalations WHERE escalation_id = :e"),
        {"e": escalation_id},
    ).mappings().one()
    assert row["kind"] == "pay"
    assert "did not arrive in full" in row["detail"]
    assert "RWF 5,000 short" in row["detail"]


def test_an_employer_claim_alone_is_not_accuracy(session, worked_placement):
    """Paid on time by the employer's word, unconfirmed by the worker: not yet
    accurate. The gap between the claim and the confirmation is the point."""
    pay_id = record_pay_period(
        session, worked_placement, WEEK_AGO, TODAY, 15_000, due_on=TODAY
    )
    mark_paid(session, pay_id, TODAY)
    accurate = session.execute(
        text("SELECT paid_in_full_on_time FROM v_pay_accuracy WHERE pay_id = :p"),
        {"p": pay_id},
    ).scalar_one()
    assert accurate is False


# --- over HTTP -------------------------------------------------------------

def test_the_pay_flow_over_http(client, session, worked_placement):
    listed = client.get(f"/placements/{worked_placement}/pay").json()
    assert listed["suggestion"]["gross_rwf"] == 15_000

    created = client.post(
        f"/placements/{worked_placement}/pay",
        json={
            "period_start": str(WEEK_AGO), "period_end": str(TODAY),
            "gross_rwf": 15_000, "due_on": str(TODAY),
        },
    )
    assert created.status_code == 201
    pay_id = created.json()["pay_id"]

    assert client.post(
        f"/pay/{pay_id}/paid", json={"paid_on": str(TODAY), "method": "momo"}
    ).status_code == 200

    confirmed = client.post(
        f"/pay/{pay_id}/worker-confirmation",
        json={"received_in_full": False, "note": "short by a day"},
    ).json()
    assert confirmed["escalation_raised"] is not None

    kinds = [e["kind"] for e in client.get("/escalations").json()["open"]]
    assert "pay" in kinds


def test_the_overdue_list_requires_auth(api):
    assert api.get("/pay/overdue").status_code == 401
