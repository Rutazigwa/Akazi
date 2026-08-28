"""Choosing a channel, falling back, and knowing whether it arrived.

The offer message is how a worker learns the work exists. Sending it somewhere
they cannot receive it is the same as not telling them -- so the channel is
chosen from what we know about their handset, and a permanent WhatsApp failure
is not the end of the attempt.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.messaging.events import on_placement_offered
from app.messaging.outbox import (
    dispatch,
    preferred_channel,
    queue,
    record_delivery,
    undelivered,
)
from app.messaging.providers import FailingProvider, RecordingProvider, SendResult
from app.operations.registry import record_consent
from tests.test_messaging import MIDDAY


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


@pytest.fixture
def consenting(session, make_candidate, staff_id):
    def _make(has_smartphone=False, name="Aline"):
        cid = make_candidate(name=name)
        record_consent(session, cid, "placement", True, "paper", staff_id)
        session.execute(
            text(
                "UPDATE candidates SET has_smartphone = :s WHERE candidate_id = :c"
            ),
            {"s": has_smartphone, "c": cid},
        )
        return cid

    return _make


def channel_of(session, message_id=None) -> str:
    return session.execute(
        text("SELECT channel::text FROM messages ORDER BY created_at LIMIT 1")
    ).scalar_one()


# --- choosing ---------------------------------------------------------------

def test_a_candidate_without_a_smartphone_gets_sms(session, consenting):
    """A meaningful share of this cohort is on low-storage handsets where
    WhatsApp is not installed."""
    assert preferred_channel(session, consenting(has_smartphone=False)) == "sms"


def test_a_candidate_with_a_smartphone_gets_whatsapp(session, consenting):
    assert preferred_channel(session, consenting(has_smartphone=True)) == "whatsapp"


def test_employer_contacts_get_whatsapp(session):
    assert preferred_channel(session, None) == "whatsapp"


def test_the_offer_is_queued_on_the_right_channel(
    session, consenting, make_placement
):
    on_placement_offered(
        session, make_placement(candidate_id=consenting(has_smartphone=True))
    )
    assert channel_of(session) == "whatsapp"


def test_an_explicit_channel_still_wins(session, consenting):
    """Callers that know better are not overridden."""
    queue(
        session, template_key="placement_offer", body="x",
        candidate_id=consenting(has_smartphone=True), channel="sms",
    )
    assert channel_of(session) == "sms"


# --- falling back -----------------------------------------------------------

def test_a_permanent_whatsapp_failure_falls_back_to_sms(
    session, consenting, make_placement
):
    """Usually it means the number is not on WhatsApp, which SMS does not
    care about."""
    on_placement_offered(
        session, make_placement(candidate_id=consenting(has_smartphone=True))
    )
    report = dispatch(
        session,
        FailingProvider(retryable=False, error="not a WhatsApp number"),
        now=MIDDAY,
    )
    assert report.fell_back == 1
    assert report.failed == 0

    row = session.execute(
        text("SELECT channel::text, status::text, attempts, last_error "
             "FROM messages LIMIT 1")
    ).mappings().one()
    assert row["channel"] == "sms"
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert "falling back to SMS" in row["last_error"]


def test_the_fallback_then_sends(session, consenting, make_placement):
    on_placement_offered(
        session, make_placement(candidate_id=consenting(has_smartphone=True))
    )
    dispatch(session, FailingProvider(retryable=False), now=MIDDAY)

    provider = RecordingProvider()
    report = dispatch(session, provider, now=MIDDAY)
    assert report.sent == 1
    assert provider.sent[0][2] == "sms"


def test_sms_does_not_fall_back_again(session, consenting, make_placement):
    """A message already on SMS has nowhere left to go, and retrying forever
    would bury the real failures."""
    on_placement_offered(
        session, make_placement(candidate_id=consenting(has_smartphone=False))
    )
    report = dispatch(
        session, FailingProvider(retryable=False, error="invalid number"),
        now=MIDDAY,
    )
    assert report.failed == 1
    assert report.fell_back == 0
    assert session.execute(
        text("SELECT status::text FROM messages LIMIT 1")
    ).scalar_one() == "failed"


def test_a_retryable_failure_does_not_trigger_the_fallback(
    session, consenting, make_placement
):
    """A network blip is not evidence the number is wrong."""
    on_placement_offered(
        session, make_placement(candidate_id=consenting(has_smartphone=True))
    )
    report = dispatch(session, FailingProvider(retryable=True), now=MIDDAY)
    assert report.fell_back == 0
    assert channel_of(session) == "whatsapp"


# --- knowing whether it arrived --------------------------------------------

def test_a_delivery_receipt_marks_it_delivered(
    session, consenting, make_placement
):
    """'Sent' means the provider accepted it; 'delivered' means it reached the
    handset, and a shift reminder that never arrived looks identical to one
    that did without this."""
    on_placement_offered(session, make_placement(candidate_id=consenting()))
    dispatch(session, RecordingProvider(), now=MIDDAY)

    ref = session.execute(
        text("SELECT provider_ref FROM messages LIMIT 1")
    ).scalar_one()
    assert record_delivery(session, ref, delivered=True) is True

    row = session.execute(
        text("SELECT status::text, delivered_at FROM messages LIMIT 1")
    ).mappings().one()
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None


def test_a_failed_receipt_marks_it_failed(session, consenting, make_placement):
    on_placement_offered(session, make_placement(candidate_id=consenting()))
    dispatch(session, RecordingProvider(), now=MIDDAY)
    ref = session.execute(
        text("SELECT provider_ref FROM messages LIMIT 1")
    ).scalar_one()

    record_delivery(session, ref, delivered=False, error="handset unreachable")
    row = session.execute(
        text("SELECT status::text, last_error FROM messages LIMIT 1")
    ).mappings().one()
    assert row["status"] == "failed"
    assert "unreachable" in row["last_error"]


def test_an_unknown_reference_is_reported_not_guessed(session):
    assert record_delivery(session, "not-ours", delivered=True) is False


def test_accepted_but_unconfirmed_messages_are_listed(
    session, consenting, make_placement
):
    on_placement_offered(session, make_placement(candidate_id=consenting()))
    dispatch(session, RecordingProvider(), now=MIDDAY)
    session.execute(text("UPDATE messages SET sent_at = now() - INTERVAL '2 hours'"))

    waiting = undelivered(session, older_than_minutes=60)
    assert len(waiting) == 1
    assert waiting[0]["template_key"] == "placement_offer"


def test_a_delivered_message_leaves_that_list(
    session, consenting, make_placement
):
    on_placement_offered(session, make_placement(candidate_id=consenting()))
    dispatch(session, RecordingProvider(), now=MIDDAY)
    session.execute(text("UPDATE messages SET sent_at = now() - INTERVAL '2 hours'"))
    ref = session.execute(
        text("SELECT provider_ref FROM messages LIMIT 1")
    ).scalar_one()

    record_delivery(session, ref, delivered=True)
    assert undelivered(session, older_than_minutes=60) == []


def test_send_result_carries_the_retryable_flag():
    """The whole fallback decision rests on this distinction."""
    assert SendResult(ok=False, retryable=True).retryable is True
    assert SendResult(ok=False).retryable is False


# --- over HTTP -------------------------------------------------------------

def test_the_delivery_webhook_requires_its_secret(api):
    r = api.post(
        "/webhooks/delivery",
        json={"provider_ref": "abc", "delivered": True},
    )
    assert r.status_code in (401, 503)


def test_the_undelivered_list_requires_auth(api):
    assert api.get("/messages/undelivered").status_code == 401


def test_a_coordinator_can_see_what_never_arrived(
    client, session, consenting, make_placement
):
    on_placement_offered(session, make_placement(candidate_id=consenting()))
    dispatch(session, RecordingProvider(), now=MIDDAY)
    session.execute(text("UPDATE messages SET sent_at = now() - INTERVAL '2 hours'"))

    listed = client.get("/messages/undelivered").json()["undelivered"]
    assert len(listed) == 1
    assert listed[0]["channel"] in ("sms", "whatsapp")
