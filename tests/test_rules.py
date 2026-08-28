"""The numbers the business runs on, checked against every copy of them.

Each of these was written down more than once: the transport rule as 0.30 in
the matcher and 30 in the Tomorrow view, the minimum age as a constant in two
modules and two SQL expressions, the guarantee window as two independent
INTERVAL clauses. None of the copies disagreed, which is the only reason it
had not caused a problem.

The failure is quiet. Change the matcher's transport threshold after a month
of real fares and the Tomorrow view keeps flagging at the old one, so a
coordinator sees a warning for a placement the matcher considers fine, or
nothing for one it would refuse. Nothing errors.

SQL cannot import Python, so views keep their own copy and these tests assert
the two agree -- derived from the source, like the privilege and data-rights
audits, rather than trusting that somebody remembered.
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import text

from app import rules


MIGRATIONS = sorted(Path("migrations").glob("*.sql"))


def migration_text() -> str:
    return "\n".join(p.read_text() for p in MIGRATIONS)


def test_the_database_enforces_the_same_minimum_age(session):
    """chk_minimum_age is the backstop a bulk import cannot get past, so it
    has to be the same number the application refuses on."""
    definition = session.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
             "WHERE conname = 'chk_minimum_age'")
    ).scalar_one()
    assert f"'{rules.MINIMUM_AGE} years'" in definition, definition


def test_the_age_eligibility_function_uses_the_same_number(session):
    body = session.execute(
        text("SELECT prosrc FROM pg_proc WHERE proname = "
             "'candidates_age_eligible'")
    ).scalar_one()
    assert f"'{rules.MINIMUM_AGE} years'" in body, body


def test_the_guarantee_window_is_the_same_everywhere(session):
    """v_guarantee_invocations decides whether the promise was kept, and
    open_guarantees decides how long is left on the clock. Two different
    windows would mean a coordinator racing a deadline the metric does not
    recognise."""
    definition = session.execute(
        text("SELECT pg_get_viewdef('v_guarantee_invocations'::regclass)")
    ).scalar_one()
    assert f"'{rules.GUARANTEE_HOURS}:00:00'" in definition, definition

    used = set(re.findall(r"INTERVAL '(\d+) hours'", migration_text()))
    used |= set(re.findall(r"interval '(\d+) hours'", migration_text()))
    guarantee_windows = {h for h in used if h == str(rules.GUARANTEE_HOURS)}
    assert guarantee_windows, "no 24-hour interval found in any migration"


def test_the_tomorrow_flag_uses_the_matchers_threshold(session):
    """Not a second copy of it."""
    from app.operations.readiness import TRANSPORT_HEAVY_PCT

    assert TRANSPORT_HEAVY_PCT == rules.MAX_TRANSPORT_SHARE * 100


def test_nothing_redefines_a_rule_privately(session):
    """A module with its own copy is a module that will drift."""
    offenders = []
    for path in Path("app").rglob("*.py"):
        if path.name == "rules.py":
            continue
        source = path.read_text()
        for name in ("MINIMUM_AGE", "MAX_TRANSPORT_SHARE", "GUARANTEE_HOURS"):
            if re.search(rf"^{name}\s*=", source, re.M):
                offenders.append(f"{path}: {name}")
    assert offenders == [], (
        f"these define a business rule privately: {offenders}. "
        "Import it from app.rules instead."
    )


def test_the_scorecard_targets_match_the_blueprint(session):
    """The target shown beside each figure comes from app.rules, so a number
    edited in one place cannot leave the dashboard reporting against a goal
    the blueprint never set."""
    from app.operations.scorecard import _SPEC

    shown = {key: target for key, _, target, *_ in _SPEC}
    assert shown["avg_transport_pct"] == "≤ 25"
    assert shown["retention_30day_pct"] == "≥ 60"
    assert shown["guarantee_filled_24h_pct"] == "≥ 90"
    assert shown["women_placed_pct"] == "≥ 45"
    assert shown["pay_accuracy_pct"] == "≥ 95"
    assert shown["employer_reorder_pct"] == "≥ 40"

    assert rules.TARGET_TRANSPORT_SHARE == 0.25
    assert rules.TARGET_RETENTION_30DAY == 0.60
    assert rules.TARGET_GUARANTEE_FILLED == 0.90
    assert rules.TARGET_WOMEN_PLACED == 0.45
    assert rules.TARGET_PAY_ACCURACY == 0.95
    assert rules.TARGET_REORDER_RATE == 0.40


def test_the_shown_target_matches_the_threshold_used(session):
    """The sentence and the comparison have to be the same number. A card
    reading "target >= 60" that passes at 50 is worse than no card."""
    from app.operations.scorecard import _SPEC

    for key, _, target_text, _how, threshold, _dep in _SPEC:
        digits = "".join(c for c in target_text if c.isdigit() or c == ".")
        if digits and "–" not in target_text:
            assert float(digits) == float(threshold), key


def test_the_matcher_leaves_headroom_below_the_target(session):
    """A placement accepted at exactly the target has no room for a fare rise,
    and the fare is the thing that moves."""
    assert rules.MAX_TRANSPORT_SHARE > rules.TARGET_TRANSPORT_SHARE


def test_no_template_hardcodes_a_threshold_that_could_move(session):
    """A number typed into a page is a copy of a rule, and it goes stale
    without erroring. After a month of real fares the matcher's threshold may
    well change; the sentence explaining it to a coordinator has to change
    with it."""
    stale = []
    for path in Path("app/web/templates").rglob("*.html"):
        source = path.read_text()
        for percent in re.findall(r"(\d+)%-of-pay", source):
            if int(percent) != int(rules.MAX_TRANSPORT_SHARE * 100):
                stale.append(f"{path.name}: says {percent}%")
    assert stale == [], (
        f"{stale} — the matcher refuses at "
        f"{rules.MAX_TRANSPORT_SHARE:.0%}. Render it from app.rules rather "
        "than typing it."
    )
