"""Reporting harassment from a number we cannot place.

The blueprint promises "an in-app harassment report with a named escalation
path and a defined response time". The path and the time both existed, and both
were conditional on our being able to work out who sent the message.

Demonstrated on one candidate and the message "the supervisor keeps touching me
and I feel unsafe", which interpret() classified as harassment every time:

    her primary number      resolved=yes  escalations=1
    her recorded alternate  resolved=NO   escalations=0
    a borrowed phone        resolved=NO   escalations=0
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.messaging.inbound import handle, interpret, record_inbound

REPORT = "the supervisor keeps touching me and I feel unsafe"


def send(session, phone, body=REPORT):
    inbound_id = record_inbound(session, from_phone=phone, body=body,
                                provider_ref=str(uuid.uuid4()))
    outcome = handle(session, inbound_id)
    raised = session.execute(
        text("SELECT kind::text, candidate_id, respond_by FROM escalations "
             "WHERE inbound_id = :i"),
        {"i": str(inbound_id)},
    ).mappings().first()
    return outcome, raised


@pytest.fixture
def reporter(session, make_candidate):
    cid = make_candidate(name="Reporter", gender="F")
    session.execute(
        text("UPDATE candidate_identity SET phone_alt = :a WHERE candidate_id = :c"),
        {"a": "+250788999111", "c": str(cid)},
    )
    primary = session.execute(
        text("SELECT phone_primary FROM candidate_identity WHERE candidate_id = :c"),
        {"c": str(cid)},
    ).scalar_one()
    return {"candidate_id": cid, "primary": primary, "alt": "+250788999111"}


def test_the_message_was_never_the_problem():
    """Guards the guard. interpret() read it correctly all along -- the report
    was lost after classification, not during it."""
    reading = interpret(REPORT)
    assert reading.intent == "issue_reported"
    assert reading.issue_kind == "harassment"


def test_her_own_alternate_number_is_recognised(session, reporter):
    """She gave us that number at registration. It sits in the same row as the
    one we do match, and was simply never consulted."""
    _, raised = send(session, reporter["alt"])
    assert raised is not None
    assert raised["kind"] == "harassment"
    assert raised["candidate_id"] == reporter["candidate_id"], (
        "matched, but attributed to nobody"
    )


def test_the_primary_number_still_wins(session, reporter, make_candidate):
    """Two people can share a phone. Whoever put it down as their main number
    is the better guess."""
    other = make_candidate(name="Housemate")
    session.execute(
        text("UPDATE candidate_identity SET phone_alt = :a WHERE candidate_id = :c"),
        {"a": reporter["primary"], "c": str(other)},
    )
    _, raised = send(session, reporter["primary"])
    assert raised["candidate_id"] == reporter["candidate_id"]


def test_a_borrowed_phone_still_starts_the_clock(session, staff_id):
    """The one that matters most.

    She may be using somebody else's phone precisely because of what she is
    reporting. Parking it made the promised response time conditional on our
    being able to identify her, and put the report in the same queue as the
    messages that read "???".
    """
    outcome, raised = send(session, "+250788000777")
    assert raised is not None, outcome
    assert raised["kind"] == "harassment"
    assert raised["candidate_id"] is None, "nobody is attached to it yet"
    assert raised["respond_by"] is not None, "the clock has to be running"


def test_an_unreadable_message_is_still_just_queued(session, staff_id):
    """Guards the guard, and keeps the exception narrow.

    Escalating everything from an unknown number would bury the reports that
    matter under wrong numbers and gibberish -- which is the same failure as
    the hundred SMS, from the other direction.
    """
    outcome, raised = send(session, "+250788000778", body="???")
    assert raised is None
    assert "do not recognise" in outcome


def test_an_ordinary_reply_from_an_unknown_number_is_not_escalated(session,
                                                                   staff_id):
    """You cannot cancel somebody's shift on an unauthenticated message."""
    outcome, raised = send(session, "+250788000779", body="yes I can work")
    assert raised is None
    assert "do not recognise" in outcome


def test_a_report_nobody_can_own_stays_visible(session):
    """An escalation with no owner is refused on purpose -- "a record that
    looks like a safeguard and is not one". The report must not vanish with the
    exception, so it is parked, unhandled, and says why.
    """
    outcome, raised = send(session, "+250788000780")
    assert raised is None
    assert "could not raise escalation" in outcome
    unhandled = session.execute(
        text("SELECT count(*) FROM inbound_messages WHERE handled_at IS NULL")
    ).scalar_one()
    assert unhandled >= 1, "it has to stay in somebody's queue"
