"""Dates in the timezone the users live in.

date.today() is the server's date, and servers run on UTC. Kigali is UTC+2, so
between 22:00 and midnight UTC every date this system offered was a day behind
what the coordinator's own calendar said — and those two hours are 00:00 to
02:00 local, exactly when a late shift ends and attendance gets logged.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.clock import KIGALI, kigali_now, kigali_today

os.environ.setdefault("DATA_RESIDENCY", "local_dev")


# --- the offset ------------------------------------------------------------

def test_kigali_is_two_hours_ahead_of_utc():
    assert KIGALI.utcoffset(None) == timedelta(hours=2)


def test_kigali_today_matches_a_kigali_clock():
    assert kigali_today() == datetime.now(timezone.utc).astimezone(KIGALI).date()


def test_kigali_now_carries_its_zone():
    """A naive datetime would be indistinguishable from a UTC one downstream."""
    assert kigali_now().tzinfo is not None


# --- the window that was wrong ---------------------------------------------

@pytest.mark.parametrize(
    "utc_moment,expected_local",
    [
        (datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc), date(2026, 9, 7)),
        # The two hours that were a day behind.
        (datetime(2026, 9, 7, 22, 30, tzinfo=timezone.utc), date(2026, 9, 8)),
        (datetime(2026, 9, 7, 23, 59, tzinfo=timezone.utc), date(2026, 9, 8)),
        (datetime(2026, 9, 8, 0, 1, tzinfo=timezone.utc), date(2026, 9, 8)),
        # Year boundary, where the contract reference year would have been wrong.
        (datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc), date(2027, 1, 1)),
    ],
)
def test_the_local_date_is_what_a_coordinator_would_say(
    utc_moment, expected_local
):
    assert utc_moment.astimezone(KIGALI).date() == expected_local


def test_the_server_date_and_the_local_date_genuinely_differ(monkeypatch):
    """Not a theoretical concern: this is a real two-hour window every night."""
    late = datetime(2026, 9, 7, 22, 30, tzinfo=timezone.utc)
    assert late.date() != late.astimezone(KIGALI).date()


# --- the database agrees ---------------------------------------------------

def test_the_database_has_a_matching_helper(session):
    """Python and SQL must not disagree about what day it is."""
    assert session.execute(text("SELECT kigali_today()")).scalar_one() == (
        kigali_today()
    )


def test_the_database_helper_is_not_current_date(session):
    """It is computed from the Kigali zone, not the server's."""
    definition = session.execute(
        text("SELECT pg_get_functiondef('kigali_today'::regproc)")
    ).scalar_one()
    assert "Africa/Kigali" in definition


# --- the call sites --------------------------------------------------------

def test_nothing_user_facing_calls_date_today():
    """The regression guard. A new date.today() in these files reintroduces
    a bug that only appears for two hours a night."""
    import pathlib
    import re

    user_facing = [
        "app/web/router.py",
        "app/web/employer_router.py",
        "app/routers/operations.py",
        "app/operations/registry.py",
        "app/operations/pay.py",
    ]
    offenders = []
    for path in user_facing:
        source = pathlib.Path(path).read_text()
        for number, line in enumerate(source.splitlines(), 1):
            if re.search(r"\bdate\.today\(\)", line):
                offenders.append(f"{path}:{number}")
    assert offenders == [], (
        f"use kigali_today() instead of date.today() at: {offenders}"
    )


def test_no_user_facing_sql_uses_current_date():
    """CURRENT_DATE is the server's date. Same bug, different language."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bCURRENT_DATE\b", line):
                offenders.append(f"{path}:{number}")
    assert offenders == [], (
        f"use kigali_today() instead of CURRENT_DATE at: {offenders}"
    )


def test_one_definition_of_the_offset():
    """Two modules disagreeing about the offset would be worse than either
    being wrong consistently."""
    from app.messaging.outbox import KIGALI as messaging_kigali

    assert messaging_kigali is KIGALI
