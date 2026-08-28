"""The pilot scorecard, and whether it says how the pilot is doing.

Every metric used to render identically: a guarantee fill rate of 0.0 against
"target >= 90" and a retention rate of 100.0 against "target >= 60" carried
the same weight on the panel an owner scans in seconds and a funder reads over
their shoulder.
"""
from __future__ import annotations

from app.operations.scorecard import Metric, read_scorecard, summary


def card(**over):
    base = {
        "active_employers": 10, "paid_placements": 30,
        "avg_days_to_fill": 4.0, "retention_30day_pct": 70.0,
        "avg_transport_pct": 20.0, "guarantee_filled_24h_pct": 95.0,
        "women_placed_pct": 50.0, "pay_accuracy_pct": 97.0,
        "employer_reorder_pct": 45.0, "guarantee_invocations": 3,
        "employers_served": 8,
    }
    base.update(over)
    return base


def verdict_for(metrics: list[Metric], key: str) -> str:
    return next(m.verdict for m in metrics if m.key == key)


def test_a_met_target_says_so(session=None):
    assert verdict_for(read_scorecard(card()), "retention_30day_pct") == "met"


def test_a_missed_target_says_so(session=None):
    metrics = read_scorecard(card(retention_30day_pct=40.0))
    assert verdict_for(metrics, "retention_30day_pct") == "missed"


def test_a_lower_is_better_target_is_read_the_right_way(session=None):
    """Transport at 20% of pay beats a 25% target; at 40% it does not. A
    comparison written the wrong way round would report the worst placements
    as the best."""
    assert verdict_for(read_scorecard(card(avg_transport_pct=20.0)),
                       "avg_transport_pct") == "met"
    assert verdict_for(read_scorecard(card(avg_transport_pct=40.0)),
                       "avg_transport_pct") == "missed"


def test_days_to_fill_is_read_the_right_way(session=None):
    assert verdict_for(read_scorecard(card(avg_days_to_fill=3.0)),
                       "avg_days_to_fill") == "met"
    assert verdict_for(read_scorecard(card(avg_days_to_fill=9.0)),
                       "avg_days_to_fill") == "missed"


# --- the honest part -------------------------------------------------------

def test_no_placements_means_no_verdict_on_time_to_fill(session=None):
    """Zero placements gives an average of nothing, which is not beating the
    target."""
    metrics = read_scorecard(card(paid_placements=0, avg_days_to_fill=0.0))
    assert verdict_for(metrics, "avg_days_to_fill") == "no data"


def test_no_guarantee_invocations_means_no_verdict_on_filling_them(
    session=None
):
    """A fill rate over zero invocations is not a failure. Nobody has needed
    covering yet."""
    metrics = read_scorecard(card(guarantee_invocations=0,
                                  guarantee_filled_24h_pct=0.0))
    assert verdict_for(metrics, "guarantee_filled_24h_pct") == "no data"


def test_one_unfilled_invocation_is_a_real_failure(session=None):
    """Once somebody has needed covering and did not get it, zero is a fact."""
    metrics = read_scorecard(card(guarantee_invocations=1,
                                  guarantee_filled_24h_pct=0.0))
    assert verdict_for(metrics, "guarantee_filled_24h_pct") == "missed"


def test_a_missing_figure_is_not_a_pass(session=None):
    metrics = read_scorecard(card(pay_accuracy_pct=None))
    assert verdict_for(metrics, "pay_accuracy_pct") == "no data"


def test_an_empty_pilot_claims_nothing(session=None):
    """Nine perfect scores on no data is the most flattering possible lie."""
    empty = {k: None for k in card()}
    metrics = read_scorecard(empty)
    assert all(m.verdict == "no data" for m in metrics)
    assert summary(metrics)["line"] == "no target has enough data yet"


def test_the_summary_counts_only_what_can_be_measured(session=None):
    metrics = read_scorecard(card(retention_30day_pct=40.0,
                                  guarantee_invocations=0,
                                  guarantee_filled_24h_pct=0.0))
    standing = summary(metrics)
    assert standing["missed"] == 1
    assert standing["no_data"] == 1
    assert "of 8 measurable targets met" in standing["line"]


# --- on the page -----------------------------------------------------------

def test_the_dashboard_marks_a_missed_target(web, session):
    from sqlalchemy import text

    session.execute(text("SELECT 1"))
    page = web.get("/ui/").text
    assert "measurable targets met" in page or "nothing to measure yet" in page


def test_the_dashboard_explains_why_a_blank_is_not_a_pass(web):
    page = web.get("/ui/").text
    assert "most flattering possible lie" in page
