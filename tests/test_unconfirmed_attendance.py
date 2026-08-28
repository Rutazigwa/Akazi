"""Shifts nobody said anything about.

Attendance is confirmed by the employer and it is the input the whole
guarantee rests on. Nothing noticed when it never arrived: a shift that ran on
Tuesday with nobody recording whether the worker turned up sat in the system
looking exactly like one that went perfectly well.

For a business whose promise is "the shift gets covered, and if it does not we
cover it", that is the most expensive silence there is.
"""
from __future__ import annotations

from sqlalchemy import text

from app.operations.attendance import (
    SILENT_AFTER_DAYS,
    SILENT_TOO_LONG_DAYS,
    chase_unconfirmed_attendance,
    unconfirmed_attendance,
)


def running_since(session, make_placement, days_ago, status="active",
                  candidate_id=None):
    placement_id = make_placement(candidate_id=candidate_id)
    session.execute(
        text("UPDATE placements SET status = CAST(:s AS placement_status), "
             "started_on = CURRENT_DATE - make_interval(days => :d) "
             "WHERE placement_id = :p"),
        {"s": status, "d": days_ago, "p": str(placement_id)},
    )
    return placement_id


def confirm(session, placement_id, days_ago, present=True):
    session.execute(
        text("INSERT INTO attendance (placement_id, work_date, present, "
             "confirmed_by, confirmed_at) VALUES (:p, CURRENT_DATE - "
             "make_interval(days => :d), :present, 'employer', now())"),
        {"p": str(placement_id), "d": days_ago, "present": present},
    )


def listed(session, placement_id):
    return next((u for u in unconfirmed_attendance(session)
                 if u["placement_id"] == placement_id), None)


# --- what counts as silence ------------------------------------------------

def test_a_shift_nobody_confirmed_is_listed(session, make_placement):
    placement_id = running_since(session, make_placement,
                                 SILENT_AFTER_DAYS + 1)
    row = listed(session, placement_id)
    assert row is not None
    assert row["never_confirmed"] is True
    assert "nothing recorded since it started" in row["summary"]


def test_yesterdays_shift_is_not_chased_yet(session, make_placement):
    """A shift finishing at 18:00 is not confirmable at 18:01. One clear day
    gives the employer a working morning to answer."""
    placement_id = running_since(session, make_placement, 1)
    assert listed(session, placement_id) is None


def test_a_confirmed_placement_falls_off_the_list(session, make_placement):
    placement_id = running_since(session, make_placement,
                                 SILENT_AFTER_DAYS + 1)
    confirm(session, placement_id, 0)
    assert listed(session, placement_id) is None


def test_a_placement_confirmed_long_ago_comes_back(session, make_placement):
    """Still running, and nobody has said anything for a week."""
    placement_id = running_since(session, make_placement, 20)
    confirm(session, placement_id, SILENT_TOO_LONG_DAYS + 2)
    row = listed(session, placement_id)
    assert row is not None
    assert row["never_confirmed"] is False
    assert "last confirmed" in row["summary"]


def test_long_silence_is_marked_urgent(session, make_placement):
    """The guarantee window has closed unnoticed: if the worker did not
    arrive, we owed cover and never knew."""
    placement_id = running_since(session, make_placement,
                                 SILENT_TOO_LONG_DAYS + 1)
    assert listed(session, placement_id)["urgent"] is True


def test_a_short_silence_is_not_urgent(session, make_placement):
    placement_id = running_since(session, make_placement,
                                 SILENT_AFTER_DAYS)
    assert listed(session, placement_id)["urgent"] is False


def test_a_placement_that_has_not_started_is_not_chased(session,
                                                        make_placement):
    """Nothing has happened yet. There is nothing to confirm."""
    placement_id = make_placement()
    session.execute(
        text("UPDATE placements SET status = 'accepted', started_on = NULL "
             "WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    assert listed(session, placement_id) is None


def test_a_cancelled_placement_is_not_chased(session, make_placement):
    placement_id = running_since(session, make_placement,
                                 SILENT_AFTER_DAYS + 1, status="cancelled")
    assert listed(session, placement_id) is None


def test_the_longest_silence_comes_first(session, make_placement,
                                         make_candidate):
    recent = running_since(session, make_placement, SILENT_AFTER_DAYS + 1,
                           candidate_id=make_candidate())
    ancient = running_since(session, make_placement, 30,
                            candidate_id=make_candidate())
    order = [u["placement_id"] for u in unconfirmed_attendance(session)]
    assert order.index(ancient) < order.index(recent)


# --- asking the employer ---------------------------------------------------

def test_the_employer_is_asked(session, make_placement, employer_id):
    """They are the only ones who know."""
    session.execute(
        text("INSERT INTO employer_contacts (employer_id, full_name, phone, "
             "is_primary) VALUES (:e, 'Site manager', '+250788112200', TRUE)"),
        {"e": employer_id},
    )
    running_since(session, make_placement, SILENT_AFTER_DAYS + 1)

    assert chase_unconfirmed_attendance(session)["asked"] == 1
    body = session.execute(
        text("SELECT body FROM messages WHERE template_key = "
             "'attendance_unconfirmed'")
    ).scalar_one()
    assert "did" in body and "work the" in body
    assert "free of charge" in body


def test_asking_twice_does_not_send_twice(session, make_placement,
                                          employer_id):
    """One message per placement. Asking again before lunch is how a useful
    message becomes one that gets ignored."""
    session.execute(
        text("INSERT INTO employer_contacts (employer_id, full_name, phone, "
             "is_primary) VALUES (:e, 'Site manager', '+250788112200', TRUE)"),
        {"e": employer_id},
    )
    running_since(session, make_placement, SILENT_AFTER_DAYS + 1)

    chase_unconfirmed_attendance(session)
    assert chase_unconfirmed_attendance(session)["asked"] == 0
    assert session.execute(
        text("SELECT count(*) FROM messages WHERE template_key = "
             "'attendance_unconfirmed'")
    ).scalar_one() == 1


def test_an_employer_with_no_contact_is_skipped_not_crashed(
    session, make_placement, employer_id
):
    """Left on the coordinator's list instead, which is where it belongs."""
    placement_id = running_since(session, make_placement,
                                 SILENT_AFTER_DAYS + 1)
    assert chase_unconfirmed_attendance(session)["asked"] == 0
    assert listed(session, placement_id) is not None


def test_the_dashboard_shows_them(web, session, make_placement):
    running_since(session, make_placement, SILENT_TOO_LONG_DAYS + 1)
    page = web.get("/ui/").text
    assert "Nobody said whether these happened" in page
    assert "guarantee window closed unnoticed" in page


def test_the_dashboard_stays_quiet_when_everything_is_confirmed(
    web, session, make_placement
):
    placement_id = running_since(session, make_placement,
                                 SILENT_AFTER_DAYS + 1)
    confirm(session, placement_id, 0)
    assert "Nobody said whether these happened" not in web.get("/ui/").text


# --- only while the answer still changes something ------------------------

def test_a_placement_that_finished_weeks_ago_is_not_chased(session,
                                                           make_placement):
    """The guarantee window shut long ago and the pay record is settled. An
    employer asked about a shift they have forgotten stops reading the
    messages, and the next one is the one that matters."""
    placement_id = running_since(session, make_placement, 40,
                                 status="completed")
    session.execute(
        text("UPDATE placements SET ended_on = CURRENT_DATE - 25 "
             "WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    assert listed(session, placement_id) is None


def test_a_placement_that_finished_this_week_is_still_chased(session,
                                                             make_placement):
    """The answer still changes the pay record."""
    placement_id = running_since(session, make_placement, 10,
                                 status="completed")
    session.execute(
        text("UPDATE placements SET ended_on = CURRENT_DATE - 2 "
             "WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    assert listed(session, placement_id) is not None


def test_an_old_active_placement_is_still_chased(session, make_placement):
    """Still running means the guarantee is live and a no-show today is
    still ours to cover, however long it has been going."""
    placement_id = running_since(session, make_placement, 60)
    assert listed(session, placement_id) is not None
