"""The pilot scorecard, with each figure judged against its target.

Every metric was rendered identically. A guarantee fill rate of 0.0 against
"target >= 90" and a retention rate of 100.0 against "target >= 60" carried
the same visual weight on the panel an owner scans in seconds and a funder
reads over their shoulder. The target was printed underneath; comparing the
two was left to the reader, every time, for nine figures.

The targets come from `app.rules` rather than the template, so the goal shown
beside a number cannot drift from the one the blueprint set.

**A metric with no data is not a metric that is failing.** Zero placements
gives an average time-to-fill of nothing, which is not "beating the target",
and a retention rate over no completed follow-ups is not 100%. Those read as
"nothing yet" and are the honest answer at week one of a pilot.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import rules


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    target_text: str
    value: float | None
    verdict: str          # "met" | "missed" | "no data"
    note: str = ""


# key, label, target as shown, comparison, threshold, and the count that has
# to be non-zero for the figure to mean anything. That last column is what
# keeps an empty pilot from reporting nine perfect scores.
_SPEC = [
    ("active_employers",         "Active employers",       "10",     "ge", 10,   None),
    ("paid_placements",          "Placements",             "30–50",  "ge", 30,   None),
    ("avg_days_to_fill",         "Days to fill",           "< 7",    "lt", rules.TARGET_DAYS_TO_FILL,          "paid_placements"),
    ("retention_30day_pct",      "30-day retention %",     "≥ 60",   "ge", rules.TARGET_RETENTION_30DAY * 100, None),
    ("avg_transport_pct",        "Transport % of pay",     "≤ 25",   "le", rules.TARGET_TRANSPORT_SHARE * 100, None),
    ("guarantee_filled_24h_pct", "Guarantee filled 24h %", "≥ 90",   "ge", rules.TARGET_GUARANTEE_FILLED * 100, "guarantee_invocations"),
    ("women_placed_pct",         "Women placed %",         "≥ 45",   "ge", rules.TARGET_WOMEN_PLACED * 100,    "paid_placements"),
    ("pay_accuracy_pct",         "Pay accuracy %",         "≥ 95",   "ge", rules.TARGET_PAY_ACCURACY * 100,    None),
    ("employer_reorder_pct",     "Employer reorder %",     "≥ 40",   "ge", rules.TARGET_REORDER_RATE * 100,    "employers_served"),
]

_COMPARE = {
    "ge": lambda value, target: value >= target,
    "le": lambda value, target: value <= target,
    "lt": lambda value, target: value < target,
}


def read_scorecard(card: dict) -> list[Metric]:
    """Turn one scorecard row into figures that say whether they are good."""
    metrics = []
    for key, label, target_text, how, threshold, depends_on in _SPEC:
        value = card.get(key)
        value = float(value) if value is not None else None

        # No data is not a pass and not a failure. Reporting an empty pilot as
        # nine met targets is the most flattering possible lie.
        if value is None or (depends_on and not card.get(depends_on)):
            metrics.append(Metric(key, label, target_text, value, "no data",
                                  "nothing to measure yet"))
            continue

        met = _COMPARE[how](value, threshold)
        metrics.append(Metric(key, label, target_text, value,
                              "met" if met else "missed"))
    return metrics


def summary(metrics: list[Metric]) -> dict:
    """How the pilot stands, in one line an owner can repeat."""
    met = sum(1 for m in metrics if m.verdict == "met")
    missed = sum(1 for m in metrics if m.verdict == "missed")
    return {
        "met": met,
        "missed": missed,
        "no_data": sum(1 for m in metrics if m.verdict == "no data"),
        "line": (
            f"{met} of {met + missed} measurable targets met"
            if met + missed else "no target has enough data yet"
        ),
    }
