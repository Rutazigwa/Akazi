"""What happens when a response time is missed.

The blueprint promises an in-app harassment report with a named escalation
path and a defined response time. The path was named and the time was stored
-- and when it passed, the only thing that happened was that a pill turned red
on a page. If nobody had that page open, nothing happened at all.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.operations.escalations import (
    alert_on_missed_response_times,
    raise_escalation,
)


@pytest.fixture
def coordinator(session, staff_id):
    """Someone junior to own an escalation, and an owner above them."""
    session.execute(
        text("UPDATE staff SET role = 'owner' WHERE staff_id = :s"),
        {"s": staff_id},
    )
    return session.execute(
        text("INSERT INTO staff (full_name, phone, role, password_hash) "
             "VALUES ('Chantal', '+250780007777', 'coordinator', 'x') "
             "RETURNING staff_id")
    ).scalar_one()


def breach(session, escalation_id):
    session.execute(
        text("UPDATE escalations SET respond_by = now() - interval '10 minutes' "
             "WHERE escalation_id = :e"),
        {"e": str(escalation_id)},
    )


def open_escalation(session, make_candidate, kind="harassment"):
    return raise_escalation(session, kind, candidate_id=make_candidate(),
                            detail="the supervisor keeps shouting at me")


def queued_alerts(session):
    return session.execute(
        text("SELECT message_id, staff_id, channel::text AS channel, body "
             "FROM messages WHERE template_key = 'escalation_breach'")
    ).mappings().all()


def test_a_missed_response_time_reaches_somebody(session, make_candidate,
                                                 coordinator):
    """Until now it turned a pill red on a page nobody had open."""
    escalation_id = open_escalation(session, make_candidate)
    breach(session, escalation_id)

    report = alert_on_missed_response_times(session)
    assert report["alerted"] == 1
    assert len(queued_alerts(session)) == 1


def test_an_escalation_still_inside_its_time_is_left_alone(session,
                                                           make_candidate,
                                                           coordinator):
    open_escalation(session, make_candidate)
    assert alert_on_missed_response_times(session)["alerted"] == 0


def test_an_acknowledged_escalation_is_not_chased(session, make_candidate,
                                                  coordinator, staff_id):
    from app.operations.escalations import acknowledge

    escalation_id = open_escalation(session, make_candidate)
    breach(session, escalation_id)
    acknowledge(session, escalation_id, staff_id)
    assert alert_on_missed_response_times(session)["alerted"] == 0


def test_the_alert_is_raised_once_not_every_five_minutes(session,
                                                         make_candidate,
                                                         coordinator):
    """Re-alerting until someone acts is how an alert becomes noise, and
    noise is how the next one gets ignored."""
    escalation_id = open_escalation(session, make_candidate)
    breach(session, escalation_id)

    assert alert_on_missed_response_times(session)["alerted"] == 1
    assert alert_on_missed_response_times(session)["alerted"] == 0
    assert len(queued_alerts(session)) == 1


def test_the_alert_carries_no_report_detail(session, make_candidate,
                                            coordinator):
    """A missed deadline is a prompt to open the escalation, not a channel
    for what was reported. Names and detail stay in the system."""
    escalation_id = open_escalation(session, make_candidate)
    breach(session, escalation_id)
    alert_on_missed_response_times(session)

    body = queued_alerts(session)[0]["body"]
    assert "shouting" not in body
    assert "harassment" in body


def test_the_alert_goes_by_sms(session, make_candidate, coordinator):
    """It must not depend on someone having WhatsApp open, least of all here."""
    escalation_id = open_escalation(session, make_candidate)
    breach(session, escalation_id)
    alert_on_missed_response_times(session)
    assert queued_alerts(session)[0]["channel"] == "sms"


def test_it_is_not_sent_to_whoever_already_missed_it(session, make_candidate,
                                                     coordinator, staff_id):
    """The point of a missed deadline is that the first line did not act."""
    escalation_id = raise_escalation(
        session, "pay", candidate_id=make_candidate(),
        detail="not paid", owner_staff_id=coordinator,
    )
    breach(session, escalation_id)
    alert_on_missed_response_times(session)

    recipient = queued_alerts(session)[0]["staff_id"]
    assert recipient != coordinator
    assert recipient == staff_id


def test_an_owner_who_missed_their_own_deadline_is_still_told(
    session, make_candidate, staff_id
):
    """There is nobody above them, and silence would be worse."""
    session.execute(
        text("UPDATE staff SET role = 'owner' WHERE staff_id = :s"),
        {"s": staff_id},
    )
    escalation_id = raise_escalation(
        session, "harassment", candidate_id=make_candidate(),
        detail="unsafe", owner_staff_id=staff_id,
    )
    breach(session, escalation_id)
    assert alert_on_missed_response_times(session)["alerted"] == 1
    assert queued_alerts(session)[0]["staff_id"] == staff_id


def test_a_breach_nobody_can_be_told_about_is_retried_not_swallowed(
    session, make_candidate, coordinator
):
    """An alert nobody can receive is not "handled"."""
    escalation_id = open_escalation(session, make_candidate)
    breach(session, escalation_id)
    # deactivated_at as well: chk_deactivation refuses an inactive account
    # with no record of when it was switched off.
    session.execute(
        text("UPDATE staff SET is_active = FALSE, deactivated_at = now()")
    )

    report = alert_on_missed_response_times(session)
    assert report["unroutable"] == 1
    assert report["alerted"] == 0
    assert session.execute(
        text("SELECT breach_alerted_at FROM escalations WHERE escalation_id = :e"),
        {"e": str(escalation_id)},
    ).scalar_one() is None


# --- quiet hours must not silence an internal alert ------------------------

def test_an_internal_alert_is_sent_during_quiet_hours(session, make_candidate,
                                                      coordinator):
    """Quiet hours stop us waking a worker at 23:00 about a shift.

    They are not a reason to sit on a harassment escalation that missed its
    response time at 22:00.
    """
    from datetime import datetime, timedelta, timezone

    from app.messaging.outbox import dispatch
    from app.messaging.providers import RecordingProvider

    escalation_id = open_escalation(session, make_candidate)
    breach(session, escalation_id)
    alert_on_missed_response_times(session)

    # 22:30 Kigali, squarely inside quiet hours -- and tomorrow's, so it is
    # always after the message was queued. Building it from today's date puts
    # it in the past whenever the suite runs after 20:30 UTC, and then nothing
    # is due and the test fails for a reason that has nothing to do with
    # quiet hours.
    midnight_kigali = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=20, minute=30, second=0, microsecond=0
    )
    report = dispatch(session, RecordingProvider(), now=midnight_kigali)
    assert report.sent == 1

    status = session.execute(
        text("SELECT status::text FROM messages "
             "WHERE template_key = 'escalation_breach'")
    ).scalar_one()
    assert status == "sent"


def test_a_worker_message_is_still_held_during_quiet_hours(session,
                                                           make_candidate):
    from datetime import datetime, timedelta, timezone

    from app.messaging.outbox import dispatch, queue
    from app.messaging.providers import RecordingProvider

    queue(session, template_key="shift_reminder", body="see you tomorrow",
          candidate_id=make_candidate())

    # Tomorrow's 22:30 Kigali, for the same reason as the test above: built
    # from today's date it lands before the message was queued whenever the
    # suite runs after 20:30 UTC, and a message that is not yet due is not
    # evidence about quiet hours.
    midnight_kigali = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=20, minute=30, second=0, microsecond=0
    )
    report = dispatch(session, RecordingProvider(), now=midnight_kigali)
    assert report.sent == 0
    assert report.deferred == 1


# --- a hundred alerts is not a hundred prompts ----------------------------

def one_breached(session, coordinator, kind="pay"):
    """An escalation that has already missed its response time."""
    from app.operations.escalations import raise_escalation

    eid = raise_escalation(session, kind, detail="waiting",
                           owner_staff_id=coordinator)
    session.execute(
        text("UPDATE escalations SET raised_at = now() - INTERVAL '3 days', "
             "respond_by = now() - INTERVAL '2 days' WHERE escalation_id = :e"),
        {"e": str(eid)},
    )
    return eid


def alerts(session):
    return session.execute(
        text("SELECT template_key, body FROM messages WHERE staff_id IS NOT NULL "
             "ORDER BY created_at")
    ).mappings().all()


def test_a_handful_of_breaches_are_alerted_one_by_one(session, coordinator):
    """Below the limit nothing changes: each names its own escalation."""
    from app.operations.escalations import alert_on_missed_response_times
    from app.rules import ALERT_BURST_LIMIT

    for _ in range(ALERT_BURST_LIMIT):
        one_breached(session, coordinator)

    result = alert_on_missed_response_times(session)
    assert result["alerted"] == ALERT_BURST_LIMIT
    assert result["summarised"] == 0
    assert len(alerts(session)) == ALERT_BURST_LIMIT


def test_a_backlog_becomes_one_message_not_a_hundred(session, coordinator):
    """Measured at scale: 100 breached escalations produced 100 text messages
    to one staff member in a single five-minute run.

    That is not a hundred prompts, it is a phone nobody can use -- and among a
    hundred texts the harassment ones are indistinguishable.
    """
    from app.operations.escalations import alert_on_missed_response_times
    from app.rules import ALERT_BURST_LIMIT

    for _ in range(ALERT_BURST_LIMIT + 20):
        one_breached(session, coordinator)

    result = alert_on_missed_response_times(session)
    sent = alerts(session)
    assert len(sent) == 1, f"{len(sent)} messages for one backlog"
    assert sent[0]["template_key"] == "escalation_breach_many"
    assert result["summarised"] == 1
    # Still counted as handled, so the next run does not repeat them.
    assert result["alerted"] == ALERT_BURST_LIMIT + 20


def test_the_summary_says_how_many_are_harassment(session, coordinator):
    """The whole reason a burst is dangerous.

    A summary that hid the kinds would be worse than the hundred texts: at
    least those said "harassment" somewhere.
    """
    from app.operations.escalations import alert_on_missed_response_times
    from app.rules import ALERT_BURST_LIMIT

    for _ in range(ALERT_BURST_LIMIT + 4):
        one_breached(session, coordinator, kind="pay")
    for _ in range(3):
        one_breached(session, coordinator, kind="harassment")

    alert_on_missed_response_times(session)
    body = alerts(session)[0]["body"]
    assert "3 harassment" in body, body
    # Gravest first: harassment must be read before the pay count.
    assert body.index("harassment") < body.index("pay"), body


def test_breaches_owned_by_different_people_still_land_on_one_phone(
    session, staff_id, coordinator
):
    """Which is why the bound is per recipient rather than per owner.

    _breach_recipient deliberately routes to somebody other than whoever missed
    the deadline -- the first active owner or admin. So a dozen escalations
    spread across a dozen coordinators do not become a dozen people each
    receiving one; they all converge on the same person. Grouping by owner
    would have missed the burst entirely.
    """
    from app.operations.escalations import alert_on_missed_response_times
    from app.rules import ALERT_BURST_LIMIT

    owners = [
        session.execute(
            text("INSERT INTO staff (full_name, phone, role, password_hash) "
                 "VALUES (:n, :p, 'coordinator', 'x') RETURNING staff_id"),
            {"n": f"Owner {i}", "p": f"+25078000{7000 + i}"},
        ).scalar_one()
        for i in range(ALERT_BURST_LIMIT + 2)
    ]
    for owner in owners:
        one_breached(session, owner)

    result = alert_on_missed_response_times(session)
    recipients = session.execute(
        text("SELECT DISTINCT staff_id FROM messages WHERE staff_id IS NOT NULL")
    ).scalars().all()
    assert len(recipients) == 1, "the routing sends these to one person"
    assert result["summarised"] == 1
    assert len(alerts(session)) == 1


def test_a_summarised_backlog_is_not_alerted_again(session, coordinator):
    """Marking them is what stops the next run repeating the whole thing."""
    from app.operations.escalations import alert_on_missed_response_times
    from app.rules import ALERT_BURST_LIMIT

    for _ in range(ALERT_BURST_LIMIT + 10):
        one_breached(session, coordinator)

    alert_on_missed_response_times(session)
    again = alert_on_missed_response_times(session)
    assert again["breached"] == 0
    assert len(alerts(session)) == 1
