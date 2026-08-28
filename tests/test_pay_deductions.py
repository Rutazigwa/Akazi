"""Money taken off a wage, and whether anyone said why.

"Whether the money moves correctly" is one of the four gaps this business
exists to close, and it is not only about pay arriving late -- it is about
arriving short. The people placed are in their first formal work, with no
payslip, no union and little bargaining power. An unexplained deduction is the
oldest way to quietly reduce a wage, and a system that records the amount but
not the reason is a system that helps.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.operations.pay import (
    PayError,
    deduction_lines,
    pay_variances,
    record_pay_period,
)


START = kigali_today() - timedelta(days=7)
END = kigali_today()
DUE = kigali_today() + timedelta(days=3)


def pay(session, placement_id, **over):
    fields = dict(period_start=START, period_end=END, gross_rwf=50_000,
                  due_on=DUE)
    fields.update(over)
    return record_pay_period(session, placement_id, **fields)


# --- a deduction needs a reason -------------------------------------------

def test_a_deduction_with_no_reason_is_refused(session, make_placement):
    with pytest.raises(PayError, match="no reason given"):
        pay(session, make_placement(), deductions_rwf=8000)


def test_an_itemised_deduction_is_recorded(session, make_placement):
    pay_id = pay(session, make_placement(), deductions_rwf=8000,
                 deductions=[{"kind": "advance", "amount_rwf": 8000}])
    lines = deduction_lines(session, pay_id)
    assert lines[0]["kind"] == "advance"
    assert lines[0]["amount_rwf"] == 8000


def test_lines_that_do_not_add_up_are_refused(session, make_placement):
    with pytest.raises(PayError, match="do not add up"):
        pay(session, make_placement(), deductions_rwf=8000,
            deductions=[{"kind": "advance", "amount_rwf": 5000}])


def test_several_reasons_can_be_given(session, make_placement):
    pay_id = pay(session, make_placement(), deductions_rwf=9500,
                 deductions=[{"kind": "advance", "amount_rwf": 8000},
                             {"kind": "uniform", "amount_rwf": 1500}])
    assert {l["kind"] for l in deduction_lines(session, pay_id)} == {
        "advance", "uniform"}


def test_a_damage_deduction_needs_it_written_down(session, make_placement):
    """The kind most open to abuse, and the hardest for a worker to dispute
    if nobody wrote down what was damaged."""
    with pytest.raises(PayError, match="needs a written reason"):
        pay(session, make_placement(), deductions_rwf=5000,
            deductions=[{"kind": "damage", "amount_rwf": 5000}])


def test_a_damage_deduction_with_an_account_is_allowed(session, make_placement):
    pay_id = pay(session, make_placement(), deductions_rwf=5000,
                 deductions=[{"kind": "damage", "amount_rwf": 5000,
                              "note": "broke a display cabinet on 3 August"}])
    assert "display cabinet" in deduction_lines(session, pay_id)[0]["note"]


def test_a_two_word_excuse_does_not_count_as_a_reason(session, make_placement):
    with pytest.raises(PayError, match="needs a written reason"):
        pay(session, make_placement(), deductions_rwf=5000,
            deductions=[{"kind": "other", "amount_rwf": 5000, "note": "stuff"}])


def test_an_invented_deduction_kind_is_refused(session, make_placement):
    """A closed list, so "what is being deducted across all our employers" is
    answerable without reading a thousand notes."""
    with pytest.raises(PayError, match="deduction kind must be"):
        pay(session, make_placement(), deductions_rwf=1000,
            deductions=[{"kind": "misc", "amount_rwf": 1000}])


def test_a_zero_line_is_refused(session, make_placement):
    with pytest.raises(PayError, match="positive amount"):
        pay(session, make_placement(), deductions_rwf=0,
            deductions=[{"kind": "advance", "amount_rwf": 0}])


def test_no_deduction_needs_no_lines(session, make_placement):
    """The ordinary case stays ordinary."""
    pay_id = pay(session, make_placement())
    assert deduction_lines(session, pay_id) == []


# --- the database enforces it too, for anything that skips the app ---------

def test_the_database_refuses_an_unitemised_deduction(session, make_placement):
    """A bulk import of paper payslips does not come through record_pay_period."""
    with pytest.raises(Exception, match="Every deduction needs a stated reason"):
        session.execute(
            text("INSERT INTO pay_records (placement_id, period_start, "
                 "period_end, gross_rwf, deductions_rwf, due_on) "
                 "VALUES (:p, :s, :e, 50000, 7000, :d)"),
            {"p": str(make_placement()), "s": START, "e": END, "d": DUE},
        )
        session.flush()
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    session.rollback()


def test_removing_a_line_afterwards_is_refused(session, make_placement):
    """Otherwise the reason could be deleted and the deduction kept."""
    pay_id = pay(session, make_placement(), deductions_rwf=8000,
                 deductions=[{"kind": "advance", "amount_rwf": 8000}])
    with pytest.raises(Exception, match="Every deduction needs a stated reason"):
        session.execute(
            text("DELETE FROM pay_deductions WHERE pay_id = :p"),
            {"p": str(pay_id)},
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    session.rollback()


# --- pay against what the attendance record implies -----------------------

def test_pay_below_what_the_days_worked_imply_is_flagged(
    session, make_placement
):
    """Not proof of anything -- but the question worth asking before the money
    moves rather than after."""
    placement_id = make_placement(pay_rwf=5000)
    for day in range(5):
        session.execute(
            text("INSERT INTO attendance (placement_id, work_date, present, "
                 "confirmed_by, confirmed_at) VALUES (:p, :d, TRUE, "
                 "'employer', now())"),
            {"p": str(placement_id), "d": START + timedelta(days=day)},
        )
    pay(session, placement_id, gross_rwf=18_000)   # five days at 5,000 is 25,000

    variance = pay_variances(session, placement_id)[0]
    assert variance["days_present"] == 5
    assert variance["expected_gross_rwf"] == 25_000
    assert variance["variance_rwf"] == -7_000


def test_pay_matching_the_days_worked_is_not_flagged(session, make_placement):
    placement_id = make_placement(pay_rwf=5000)
    for day in range(5):
        session.execute(
            text("INSERT INTO attendance (placement_id, work_date, present, "
                 "confirmed_by, confirmed_at) VALUES (:p, :d, TRUE, "
                 "'employer', now())"),
            {"p": str(placement_id), "d": START + timedelta(days=day)},
        )
    pay(session, placement_id, gross_rwf=25_000)
    assert pay_variances(session, placement_id) == []


def test_paying_more_than_expected_is_not_flagged(session, make_placement):
    """An employer paying a bonus is not a problem to solve."""
    placement_id = make_placement(pay_rwf=5000)
    session.execute(
        text("INSERT INTO attendance (placement_id, work_date, present, "
             "confirmed_by, confirmed_at) VALUES (:p, :d, TRUE, 'employer', now())"),
        {"p": str(placement_id), "d": START},
    )
    pay(session, placement_id, gross_rwf=8_000)
    assert pay_variances(session, placement_id) == []
