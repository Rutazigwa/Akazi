"""LMIS outcome reporting.

The blueprint's position is to supply the national system rather than compete
with it. The tests that matter here are the disclosure-control ones: an export
that leaks an individual, or that publishes a cell of one, turns a contract into
an incident.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.operations.attendance import start_placement
from app.operations.follow_ups import complete_follow_up
from app.operations.lmis import (
    MIN_CELL,
    SUPPRESSED,
    LMISError,
    ReportWindow,
    build_report,
    whole_months_ending_before,
    placement_outcomes,
    reporting_consent_counts,
    summary,
    to_csv,
)

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()

# The last complete calendar month. Not "90 days back from today": a window
# that moves every day, or that covers a month still filling up, is what let
# two exports a day apart be subtracted from each other. See ReportWindow.
WINDOW = whole_months_ending_before(TODAY, 1)
# Somewhere inside it, for placements that must fall in the window.
IN_WINDOW = WINDOW.starts_on + timedelta(days=3)


def place(session, make_placement, make_candidate, n=1, gender="F", started=True):
    """Placements inside the reporting window.

    offered_at is moved into the window explicitly: reports cover complete
    months, so work offered today is in a month that has not ended and is
    correctly invisible to them.
    """
    ids = []
    for _ in range(n):
        pid = make_placement(candidate_id=make_candidate(gender=gender))
        session.execute(
            text("UPDATE placements SET offered_at = :d WHERE placement_id = :p"),
            {"d": f"{IN_WINDOW} 09:00+02", "p": str(pid)},
        )
        if started:
            start_placement(session, pid, IN_WINDOW)
        ids.append(pid)
    return ids


# --- disclosure control ----------------------------------------------------

def test_small_cells_are_suppressed(session, make_placement, make_candidate):
    """One woman placed in one sector of one district is not anonymous to
    anyone who works there."""
    place(session, make_placement, make_candidate, n=2)
    rows = placement_outcomes(session, WINDOW)
    assert rows
    assert rows[0]["placements"] == SUPPRESSED
    assert rows[0]["women"] == SUPPRESSED


def test_cells_at_the_threshold_are_reported(
    session, make_placement, make_candidate
):
    place(session, make_placement, make_candidate, n=MIN_CELL)
    rows = placement_outcomes(session, WINDOW)
    assert rows[0]["placements"] == MIN_CELL


def test_suppression_happens_before_the_numbers_leave(
    session, make_placement, make_candidate
):
    """Not left to whoever receives the file."""
    place(session, make_placement, make_candidate, n=1)
    report = build_report(session, WINDOW)
    assert report["disclosure_control"]["min_cell_size"] == MIN_CELL
    assert all(
        row["placements"] == SUPPRESSED for row in report["outcomes"]
    )


def test_no_identifier_of_any_kind_reaches_the_report(
    session, make_placement, make_candidate
):
    """Names, IDs, phones -- and the surrogate key too.

    candidate_id is stable across exports, so publishing it would let anyone
    holding two reports track an individual between them.
    """
    cid = make_candidate(name="Aline Uwase")
    pid = make_placement(candidate_id=cid)
    start_placement(session, pid, TODAY)

    identity = session.execute(
        text(
            "SELECT legal_first_name, legal_last_name, phone_primary, "
            "       date_of_birth FROM candidate_identity WHERE candidate_id = :c"
        ),
        {"c": cid},
    ).mappings().one()

    blob = str(build_report(session, WINDOW))
    for value in identity.values():
        assert str(value) not in blob
    assert str(cid) not in blob
    assert str(pid) not in blob
    assert "Aline" not in blob


def test_the_csv_carries_only_grouped_columns(
    session, make_placement, make_candidate
):
    place(session, make_placement, make_candidate, n=MIN_CELL)
    csv_text = to_csv(placement_outcomes(session, WINDOW))
    header = csv_text.splitlines()[0]
    assert header == (
        "sector,district,work_type,placements,women,completed,"
        "day_30_answered,day_30_retained"
    )
    assert "candidate" not in csv_text
    assert "phone" not in csv_text


# --- the numbers -----------------------------------------------------------

def test_totals_are_not_suppressed(session, make_placement, make_candidate):
    """A national figure covering every district identifies nobody, and
    suppressing it would make the report useless."""
    place(session, make_placement, make_candidate, n=2)
    totals = summary(session, WINDOW)
    assert totals["placements"] == 2
    assert isinstance(totals["placements"], int)


def test_net_earnings_after_transport_is_reported(
    session, make_placement, make_candidate
):
    """The figure no competitor publishes, and the LMIS has no other source for."""
    pid = make_placement(
        candidate_id=make_candidate(), pay_rwf=5000, transport_rwf=1150
    )
    session.execute(
        text("UPDATE placements SET offered_at = :d WHERE placement_id = :p"),
        {"d": f"{IN_WINDOW} 09:00+02", "p": str(pid)},
    )
    start_placement(session, pid, IN_WINDOW)
    totals = summary(session, WINDOW)
    assert totals["mean_daily_pay_rwf"] == 5000
    assert totals["mean_daily_transport_rwf"] == 1150
    assert totals["mean_net_daily_rwf"] == 3850


def test_retention_counts_only_answered_checkins(
    session, make_placement, make_candidate
):
    """Reporting an unanswered check-in as a failure would understate the sector."""
    placements = place(session, make_placement, make_candidate, n=MIN_CELL)
    for pid in placements[:3]:
        follow_up_id = session.execute(
            text(
                "SELECT follow_up_id FROM follow_ups WHERE placement_id = :p "
                "AND checkpoint = 'day_30'"
            ),
            {"p": pid},
        ).scalar_one()
        complete_follow_up(session, follow_up_id, still_working=True)

    rows = placement_outcomes(session, WINDOW)
    assert rows[0]["placements"] == MIN_CELL
    # Three answered, of five placed. The other two are missing data.
    assert rows[0]["day_30_answered"] == SUPPRESSED
    assert rows[0]["day_30_retained"] == SUPPRESSED


def test_the_window_is_respected(session, make_placement, make_candidate):
    place(session, make_placement, make_candidate, n=2)
    old = whole_months_ending_before(TODAY, 14)
    old = ReportWindow(starts_on=old.starts_on,
                       ends_on=whole_months_ending_before(TODAY, 13).starts_on
                       - timedelta(days=1))
    assert summary(session, old)["placements"] == 0


def test_a_backwards_window_is_refused():
    with pytest.raises(LMISError, match="cannot end before it starts"):
        ReportWindow(starts_on=WINDOW.ends_on, ends_on=WINDOW.starts_on)


# --- consent ---------------------------------------------------------------

def test_reporting_consent_is_tracked_separately_from_placement(
    session, make_candidate, staff_id
):
    """Agreeing to be placed is not agreeing to appear in a national dataset."""
    from app.operations.registry import record_consent

    cid = make_candidate()
    record_consent(session, cid, "placement", True, "paper", staff_id)
    assert reporting_consent_counts(session) == {"granted": 0, "refused": 0}

    record_consent(session, cid, "reporting", True, "paper", staff_id)
    assert reporting_consent_counts(session)["granted"] == 1


# --- access ----------------------------------------------------------------

def test_the_report_requires_owner_or_admin(api, session, staff_login):
    """Handing a dataset to a national system should be a decision with standing."""
    from app.auth import login

    session.execute(
        text("UPDATE staff SET role = 'coordinator' WHERE staff_id = :s"),
        {"s": staff_login["staff_id"]},
    )
    api.headers["Authorization"] = (
        f"Bearer {login(session, staff_login['phone'], staff_login['password'])}"
    )
    r = api.get(
        "/lmis/report",
        params={"starts_on": str(WINDOW.starts_on), "ends_on": str(WINDOW.ends_on)},
    )
    assert r.status_code == 403


def test_the_report_over_http(client, session, make_placement, make_candidate):
    place(session, make_placement, make_candidate, n=MIN_CELL)
    payload = client.get(
        "/lmis/report",
        params={"starts_on": str(WINDOW.starts_on), "ends_on": str(WINDOW.ends_on)},
    ).json()
    assert payload["source"] == "Akazi"
    assert payload["summary"]["placements"] == MIN_CELL
    assert payload["disclosure_control"]["suppressed_as"] == SUPPRESSED

    csv_response = client.get(
        "/lmis/report.csv",
        params={"starts_on": str(WINDOW.starts_on), "ends_on": str(WINDOW.ends_on)},
    )
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers["content-disposition"]


# --- suppression against a caller who chooses the window -------------------

def test_two_windows_a_day_apart_cannot_be_subtracted(session, make_placement,
                                                      make_candidate):
    """The attack this alignment exists to stop.

    With arbitrary dates, nine placements in one sector, district and work type
    published like this:

        end date    placements
        day  4          <5
        day  5           5      -> exactly one placement on day 5
        day  6           6      -> exactly one placement on day 6

    Suppression protected the first four and nothing after: once the cumulative
    count crosses MIN_CELL every later day is recoverable by subtracting
    consecutive windows, giving an exact per-day count at a granularity this
    module says is not anonymous.
    """
    base = WINDOW.starts_on
    for offset in range(9):
        pid = make_placement(candidate_id=make_candidate())
        session.execute(
            text("UPDATE placements SET offered_at = :d WHERE placement_id = :p"),
            {"d": f"{base + timedelta(days=offset)} 09:00+02", "p": str(pid)},
        )

    for days in (4, 5, 6):
        with pytest.raises(LMISError, match="last day of a month"):
            ReportWindow(starts_on=base, ends_on=base + timedelta(days=days))


def test_a_month_still_in_progress_is_refused(session):
    """A window nominally ending on the 31st, requested on the 12th, covers a
    month that is still filling up -- so the same export run a day later
    differs by one day's placements. The attack again, with the dates hidden.
    """
    first_of_this_month = date(TODAY.year, TODAY.month, 1)
    last_of_this_month = (
        date(TODAY.year + 1, 1, 1) if TODAY.month == 12
        else date(TODAY.year, TODAY.month + 1, 1)
    ) - timedelta(days=1)

    with pytest.raises(LMISError, match="not over yet"):
        ReportWindow(starts_on=first_of_this_month, ends_on=last_of_this_month)


def test_whole_months_are_still_reportable(session, make_placement,
                                           make_candidate):
    """Guards the guard.

    A validation that refused everything would pass both tests above while
    making the export useless -- and the export is a contract and political
    cover, not an optional extra.
    """
    place(session, make_placement, make_candidate, n=6)
    rows = placement_outcomes(session, WINDOW)
    assert rows and rows[0]["placements"] == 6

    quarter = whole_months_ending_before(TODAY, 3)
    assert quarter.starts_on.day == 1
    assert placement_outcomes(session, quarter)[0]["placements"] == 6


def test_the_default_window_the_page_offers_is_acceptable(web):
    """The form's own defaults must satisfy the rule it explains.

    Its previous default -- ninety days back from today -- is now refused, and
    a page offering a window its own endpoint rejects is worse than no default.
    """
    page = " ".join(web.get("/ui/reports").text.split())
    import re

    dates = re.findall(r'name="(?:starts_on|ends_on)" type="date" value="([\d-]+)"', page)
    assert len(dates) == 2, page[:300]
    starts, ends = (date.fromisoformat(d) for d in dates)
    ReportWindow(starts_on=starts, ends_on=ends)  # must not raise
