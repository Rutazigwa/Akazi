"""The message outbox: queueing, guards, dispatch and retry."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text

from app.messaging.events import on_placement_offered
from app.messaging.outbox import (
    KIGALI,
    MAX_ATTEMPTS,
    MessagingError,
    dispatch,
    in_quiet_hours,
    next_send_window,
    queue,
)
from app.messaging.providers import FailingProvider, RecordingProvider
from app.messaging.templates import render, transport_line
from app.operations.attendance import (
    log_attendance,
    record_replacement,
    start_placement,
)
from app.operations.requests import respond_to_offer

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = date.today()


def sending_moment() -> datetime:
    """A moment that is after 'now' and outside quiet hours.

    Computed rather than hard-coded: a fixed hour is in the past whenever the
    suite happens to run later than it, and messages queued at clock_timestamp()
    then look not-yet-due. Tests should not depend on the wall clock.
    """
    now = datetime.now(timezone.utc)
    noon_kigali = datetime.combine(
        now.astimezone(KIGALI).date(), time(12, 0), tzinfo=KIGALI
    ).astimezone(timezone.utc)
    if noon_kigali <= now:
        noon_kigali += timedelta(days=1)
    return noon_kigali


MIDDAY = sending_moment()


def bodies(session) -> list[str]:
    return session.execute(
        text("SELECT body FROM messages ORDER BY created_at")
    ).scalars().all()


def statuses(session) -> list[str]:
    return session.execute(
        text("SELECT status::text FROM messages ORDER BY created_at")
    ).scalars().all()


def grant_consent(session, candidate_id, staff_id):
    from app.operations.registry import record_consent

    record_consent(session, candidate_id, "placement", True, "paper", staff_id)


# --- templates -------------------------------------------------------------

def test_the_offer_states_net_pay_after_transport():
    """Gross pay is not what someone takes home, and the gap is what kills a
    placement in week two."""
    line = transport_line(5000, 1150, covered=False)
    assert "RWF 1150 per day" in line
    assert "leaving about RWF 3850" in line


def test_employer_covered_transport_is_said_plainly():
    assert transport_line(3000, 1600, covered=True) == (
        "Transport: paid by the employer."
    )


def test_an_unknown_fare_is_not_presented_as_free(session):
    """Silence would read as 'no transport cost', which is the wrong default."""
    line = transport_line(5000, 0, covered=False)
    assert "no fare estimated" in line


def test_the_offer_says_there_is_no_fee_to_apply():
    """No pay-to-apply model — and the person should be told so."""
    body = render(
        "placement_offer", business_name="X", title="Y", starts_on="1 Sep",
        shift="", pay_rwf="5,000", pay_unit="day",
        transport_line=transport_line(5000, 500, False),
    )
    assert "No fee to apply" in body


# --- queueing --------------------------------------------------------------

def test_a_message_needs_exactly_one_recipient(session):
    with pytest.raises(MessagingError, match="exactly one recipient"):
        queue(session, template_key="x", body="y")


def test_offering_a_placement_tells_the_candidate_the_terms(
    session, make_placement, make_candidate, staff_id
):
    cid = make_candidate()
    pid = make_placement(candidate_id=cid, transport_rwf=1150)
    on_placement_offered(session, pid)

    text_sent = bodies(session)[0]
    assert "Isuku Cooperative" in text_sent
    assert "RWF 5,000 per day" in text_sent
    assert "leaving about RWF 3850" in text_sent


def test_the_same_reminder_is_not_queued_twice(
    session, make_placement, make_candidate
):
    """A coordinator clicking twice should not produce two messages."""
    pid = make_placement(candidate_id=make_candidate())
    on_placement_offered(session, pid)
    on_placement_offered(session, pid)

    count = session.execute(
        text(
            "SELECT count(*) FROM messages "
            "WHERE placement_id = :pid AND template_key = 'placement_offer'"
        ),
        {"pid": pid},
    ).scalar_one()
    assert count == 1


def test_starting_a_placement_queues_the_checkin_nudges(
    session, make_placement, make_candidate
):
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, TODAY)

    keys = session.execute(
        text(
            "SELECT template_key FROM messages WHERE placement_id = :pid "
            "AND template_key LIKE 'followup%' ORDER BY scheduled_for"
        ),
        {"pid": pid},
    ).scalars().all()
    assert keys == [
        "followup_day_1", "followup_week_1", "followup_day_30", "followup_day_90",
    ]


def test_no_reminder_for_a_shift_that_starts_today(
    session, make_placement, make_candidate
):
    """A reminder for something happening now is noise."""
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, TODAY)
    keys = session.execute(
        text("SELECT template_key FROM messages WHERE placement_id = :pid"),
        {"pid": pid},
    ).scalars().all()
    assert "shift_reminder" not in keys


def test_a_reminder_is_queued_for_the_evening_before(
    session, make_placement, make_candidate
):
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, TODAY + timedelta(days=3))

    when = session.execute(
        text(
            "SELECT scheduled_for FROM messages "
            "WHERE placement_id = :pid AND template_key = 'shift_reminder'"
        ),
        {"pid": pid},
    ).scalar_one()
    local = when.astimezone(KIGALI)
    assert local.date() == TODAY + timedelta(days=2)
    assert local.time() == time(18, 0)


# --- cancellation ----------------------------------------------------------

def test_declining_an_offer_cancels_queued_messages(
    session, make_placement, make_candidate
):
    """A reminder for a job someone declined sends them to the wrong place."""
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, TODAY + timedelta(days=3))
    session.execute(
        text("UPDATE placements SET status = 'offered' WHERE placement_id = :p"),
        {"p": pid},
    )
    respond_to_offer(session, pid, accepted=False)

    remaining = session.execute(
        text(
            "SELECT count(*) FROM messages "
            "WHERE placement_id = :pid AND status = 'queued'"
        ),
        {"pid": pid},
    ).scalar_one()
    assert remaining == 0


def test_a_no_show_cancels_the_rest_of_that_placements_messages(
    session, make_placement, make_candidate
):
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, TODAY + timedelta(days=2))
    log_attendance(
        session, pid, TODAY, False, "employer", absence_reason="did not arrive"
    )
    assert "cancelled" in statuses(session)


def test_covering_a_no_show_tells_the_employer(
    session, make_placement, make_candidate, employer_id
):
    session.execute(
        text(
            "INSERT INTO employer_contacts (employer_id, full_name, phone, "
            "is_primary) VALUES (:eid, 'Chantal', '+250788000999', true)"
        ),
        {"eid": employer_id},
    )
    pid = make_placement(candidate_id=make_candidate(name="Aline"))
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer", absence_reason="no-show")
    record_replacement(session, pid, make_candidate(name="Claudine"), "matched")

    cover = [b for b in bodies(session) if "as cover at no charge" in b]
    assert cover and "Aline" in cover[0] and "Claudine" in cover[0]


# --- dispatch guards -------------------------------------------------------

def test_nothing_is_sent_during_quiet_hours(session, make_placement, make_candidate):
    """Nobody's phone should light up at 03:00 because a cron job woke up."""
    pid = make_placement(candidate_id=make_candidate())
    on_placement_offered(session, pid)

    night = MIDDAY.astimezone(KIGALI).replace(hour=1, minute=0).astimezone(
        timezone.utc
    )
    provider = RecordingProvider()
    report = dispatch(session, provider, now=night)

    assert provider.sent == []
    assert report.deferred >= 1
    assert statuses(session) == ["queued"] * len(statuses(session))


def test_deferred_messages_are_rescheduled_to_the_morning(
    session, make_placement, make_candidate
):
    pid = make_placement(candidate_id=make_candidate())
    on_placement_offered(session, pid)
    night = MIDDAY.astimezone(KIGALI).replace(hour=1, minute=0).astimezone(
        timezone.utc
    )
    dispatch(session, RecordingProvider(), now=night)

    when = session.execute(
        text("SELECT min(scheduled_for) FROM messages")
    ).scalar_one()
    assert when.astimezone(KIGALI).time() == time(7, 0)


def test_a_candidate_without_consent_is_not_messaged(
    session, make_placement, make_candidate
):
    """The fixture candidate has no consent record, so the guard applies."""
    pid = make_placement(candidate_id=make_candidate())
    on_placement_offered(session, pid)

    provider = RecordingProvider()
    report = dispatch(session, provider, now=MIDDAY)
    assert provider.sent == []
    assert report.suppressed >= 1
    assert "suppressed" in statuses(session)


def test_a_consenting_candidate_is_messaged(
    session, make_placement, make_candidate, staff_id
):
    cid = make_candidate()
    grant_consent(session, cid, staff_id)
    pid = make_placement(candidate_id=cid)
    on_placement_offered(session, pid)

    provider = RecordingProvider()
    report = dispatch(session, provider, now=MIDDAY)
    assert report.sent == 1
    phone, body, channel = provider.sent[0]
    assert phone.startswith("+250")
    assert "work offer" in body
    assert channel == "whatsapp"


def test_sending_a_message_is_audited_as_an_identity_read(
    session, make_placement, make_candidate, staff_id
):
    """Resolving a recipient to a phone number reads identity data."""
    cid = make_candidate()
    grant_consent(session, cid, staff_id)
    on_placement_offered(session, make_placement(candidate_id=cid))
    dispatch(session, RecordingProvider(), now=MIDDAY)

    purposes = session.execute(
        text(
            "SELECT detail->>'purpose' FROM audit_log "
            "WHERE record_id = :cid AND action = 'read'"
        ),
        {"cid": cid},
    ).scalars().all()
    assert "messaging" in purposes


def test_an_erased_candidate_is_not_messaged(
    session, make_placement, make_candidate, staff_id
):
    from app.operations.data_rights import complete_erasure, request_erasure

    cid = make_candidate()
    grant_consent(session, cid, staff_id)
    on_placement_offered(session, make_placement(candidate_id=cid))
    complete_erasure(session, request_erasure(session, cid, "paper", staff_id))

    provider = RecordingProvider()
    report = dispatch(session, provider, now=MIDDAY)
    assert provider.sent == []
    assert report.suppressed == 1


# --- retry -----------------------------------------------------------------

def test_a_retryable_failure_backs_off_and_stays_queued(
    session, make_placement, make_candidate, staff_id
):
    cid = make_candidate()
    grant_consent(session, cid, staff_id)
    on_placement_offered(session, make_placement(candidate_id=cid))

    dispatch(session, FailingProvider(retryable=True), now=MIDDAY)
    row = session.execute(
        text(
            "SELECT status::text, attempts, scheduled_for FROM messages LIMIT 1"
        )
    ).mappings().one()
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["scheduled_for"] > MIDDAY


def test_a_permanent_failure_is_not_retried(
    session, make_placement, make_candidate, staff_id
):
    """'This number is not on WhatsApp' will not come right on the fifth try."""
    cid = make_candidate()
    grant_consent(session, cid, staff_id)
    on_placement_offered(session, make_placement(candidate_id=cid))

    dispatch(
        session,
        FailingProvider(retryable=False, error="not a WhatsApp number"),
        now=MIDDAY,
    )
    row = session.execute(
        text("SELECT status::text, attempts, last_error FROM messages LIMIT 1")
    ).mappings().one()
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "not a WhatsApp number" in row["last_error"]


def test_retries_give_up_after_the_limit(
    session, make_placement, make_candidate, staff_id
):
    cid = make_candidate()
    grant_consent(session, cid, staff_id)
    on_placement_offered(session, make_placement(candidate_id=cid))

    provider = FailingProvider(retryable=True)
    moment = MIDDAY
    for _ in range(MAX_ATTEMPTS):
        session.execute(text("UPDATE messages SET scheduled_for = :now"),
                        {"now": moment})
        dispatch(session, provider, now=moment)

    status = session.execute(
        text("SELECT status::text FROM messages LIMIT 1")
    ).scalar_one()
    assert status == "failed"
    assert provider.attempts == MAX_ATTEMPTS


def test_a_sent_message_is_not_sent_again(
    session, make_placement, make_candidate, staff_id
):
    cid = make_candidate()
    grant_consent(session, cid, staff_id)
    on_placement_offered(session, make_placement(candidate_id=cid))

    provider = RecordingProvider()
    dispatch(session, provider, now=MIDDAY)
    dispatch(session, provider, now=MIDDAY)
    assert len(provider.sent) == 1


def test_quiet_hours_boundaries():
    def kigali(hour):
        return datetime.combine(TODAY, time(hour, 0), tzinfo=KIGALI)

    assert in_quiet_hours(kigali(21)) is True
    assert in_quiet_hours(kigali(3)) is True
    assert in_quiet_hours(kigali(6)) is True
    assert in_quiet_hours(kigali(7)) is False
    assert in_quiet_hours(kigali(20)) is False
    assert next_send_window(kigali(23)).astimezone(KIGALI).time() == time(7, 0)
