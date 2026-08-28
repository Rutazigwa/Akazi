"""Inbound replies and the escalation path.

The blueprint asks for a harassment report with a named escalation path and a
defined response time. Both halves are tested here, because a flag nobody is
accountable for answering is a reporting line, not a safeguard.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.messaging.inbound import handle, interpret, record_inbound
from app.operations.escalations import (
    ALWAYS_ESCALATE,
    RESPONSE_TIMES,
    EscalationError,
    acknowledge,
    open_escalations,
    raise_escalation,
    resolve,
    response_performance,
)
from app.operations.follow_ups import complete_follow_up
from app.operations.attendance import start_placement


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


def phone_of(session, candidate_id) -> str:
    return session.execute(
        text("SELECT phone_primary FROM candidate_identity WHERE candidate_id = :c"),
        {"c": candidate_id},
    ).scalar_one()


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize(
    "reply,intent",
    [
        ("YES", "affirmative"), ("yego", "affirmative"), ("ok", "affirmative"),
        ("sawa", "affirmative"), ("No thanks", "negative"), ("oya", "negative"),
        ("STOP", "opt_out"), ("hagarika", "opt_out"),
        ("what time tomorrow?", None),
    ],
)
def test_common_replies_are_understood(reply, intent):
    assert interpret(reply).intent == intent


@pytest.mark.parametrize(
    "reply",
    ["no problem", "no problems at all", "nta kibazo", "all good",
     "no issues here", "everything is fine"],
)
def test_no_problem_is_not_a_refusal(reply):
    """Reading this as a decline would cancel someone's work."""
    assert interpret(reply).intent == "affirmative"


@pytest.mark.parametrize(
    "reply,kind",
    [
        ("he harassed me", "harassment"),
        ("the supervisor touched me", "harassment"),
        # Tense and phrasing vary; one report written several ways. A live
        # run caught "keeps touching me" slipping through a literal match.
        ("the supervisor keeps touching me", "harassment"),
        ("he touches me", "harassment"),
        ("my boss shouts at me", "harassment"),
        ("he keeps following me", "harassment"),
        ("he wont leave me alone", "harassment"),
        ("it made me feel uncomfortable", "harassment"),
        ("inappropriate comments all day", "harassment"),
        ("yes but he shouted at me", "harassment"),
        ("ihohoterwa", "harassment"),
        ("I got injured", "safety"),
        ("there are no gloves", "safety"),
        ("they paid me less", "pay"),
        ("still waiting for my money", "pay"),
        ("the site is not safe", "safety"),
        ("I was not paid", "pay"),
        ("I have not been paid for last week", "pay"),
        ("sinahembwe", "pay"),
        ("I cannot afford the fare", "transport"),
        ("too many hours", "hours"),
    ],
)
def test_reported_problems_are_recognised(reply, kind):
    reading = interpret(reply)
    assert reading.intent == "issue_reported"
    assert reading.issue_kind == kind


@pytest.mark.parametrize(
    "reply", ["thanks!", "can I start at 9 instead?", "where is the site?"]
)
def test_ordinary_questions_are_not_read_as_reports(reply):
    """False positives cost a coordinator two minutes; the queue must stay usable."""
    assert interpret(reply).intent is None


def test_a_report_beats_a_yes(reply="yes but the supervisor touched me"):
    """Someone answering the question and reporting abuse has reported abuse."""
    assert interpret(reply).issue_kind == "harassment"


# --- escalation basics -----------------------------------------------------

def test_harassment_gets_the_shortest_response_time():
    assert RESPONSE_TIMES["harassment"] < RESPONSE_TIMES["safety"]
    assert RESPONSE_TIMES["safety"] < RESPONSE_TIMES["pay"]
    assert RESPONSE_TIMES["harassment"] <= timedelta(hours=2)


def test_an_escalation_has_a_named_owner_and_a_deadline(
    session, make_candidate, staff_id
):
    cid = make_candidate()
    escalation_id = raise_escalation(
        session, "harassment", candidate_id=cid, detail="reported by text"
    )
    row = session.execute(
        text(
            "SELECT owner_staff_id, respond_by, raised_at, status::text "
            "FROM escalations WHERE escalation_id = :eid"
        ),
        {"eid": escalation_id},
    ).mappings().one()

    assert row["owner_staff_id"] is not None
    assert row["status"] == "open"
    assert row["respond_by"] - row["raised_at"] == RESPONSE_TIMES["harassment"]


def test_harassment_is_owned_by_the_owner_not_a_coordinator(
    session, staff_id, make_candidate
):
    """The person receiving it may need to end a commercial relationship."""
    coordinator = session.execute(
        text(
            "INSERT INTO staff (full_name, phone, role) "
            "VALUES ('Coordinator Two', '+250780004444', 'coordinator') "
            "RETURNING staff_id"
        )
    ).scalar_one()
    escalation_id = raise_escalation(
        session, "harassment", candidate_id=make_candidate()
    )
    owner = session.execute(
        text("SELECT owner_staff_id FROM escalations WHERE escalation_id = :e"),
        {"e": escalation_id},
    ).scalar_one()
    assert owner != coordinator
    assert owner == staff_id  # the fixture staff member is the owner


def test_an_escalation_with_nobody_to_own_it_is_refused(session, make_candidate):
    """Better to fail loudly than record a safeguard nobody is accountable for."""
    cid = make_candidate()
    session.execute(
        text("UPDATE staff SET is_active = false, deactivated_at = now()")
    )
    with pytest.raises(EscalationError, match="nobody is accountable"):
        raise_escalation(session, "harassment", candidate_id=cid)


def test_resolving_requires_saying_what_was_done(session, make_candidate, staff_id):
    escalation_id = raise_escalation(
        session, "safety", candidate_id=make_candidate()
    )
    with pytest.raises(EscalationError, match="say what was done"):
        resolve(session, escalation_id, staff_id, "   ")


def test_acknowledging_stops_the_clock(session, make_candidate, staff_id):
    escalation_id = raise_escalation(
        session, "harassment", candidate_id=make_candidate()
    )
    acknowledge(session, escalation_id, staff_id)
    row = session.execute(
        text(
            "SELECT answered_in_time, status FROM v_escalation_response "
            "WHERE escalation_id = :e"
        ),
        {"e": escalation_id},
    ).mappings().one()
    assert row["answered_in_time"] is True
    assert row["status"] == "acknowledged"


def test_a_missed_deadline_shows_as_overdue(session, make_candidate, staff_id):
    escalation_id = raise_escalation(
        session, "harassment", candidate_id=make_candidate()
    )
    session.execute(
        text(
            "UPDATE escalations SET respond_by = now() - INTERVAL '1 hour' "
            "WHERE escalation_id = :e"
        ),
        {"e": escalation_id},
    )
    assert open_escalations(session)[0]["overdue"] is True
    assert response_performance(session)[0]["currently_overdue"] == 1


def test_closing_it_twice_is_refused(session, make_candidate, staff_id):
    escalation_id = raise_escalation(session, "pay", candidate_id=make_candidate())
    resolve(session, escalation_id, staff_id, "employer paid the same day")
    with pytest.raises(EscalationError, match="already closed"):
        resolve(session, escalation_id, staff_id, "again")


# --- follow-up calls escalate too ------------------------------------------

@pytest.mark.parametrize("flag", sorted(ALWAYS_ESCALATE))
def test_a_flag_raised_on_a_call_escalates(
    session, make_placement, make_candidate, staff_id, flag
):
    """The safeguard must not depend on how someone happened to tell us."""
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, TODAY)
    follow_up_id = session.execute(
        text(
            "SELECT follow_up_id FROM follow_ups WHERE placement_id = :p "
            "AND checkpoint = 'day_1'"
        ),
        {"p": pid},
    ).scalar_one()

    complete_follow_up(
        session, follow_up_id, still_working=True, issue_flag=flag,
        notes="said during the day-1 call",
    )
    kinds = [e["kind"] for e in open_escalations(session)]
    assert flag in kinds


def test_an_ordinary_followup_raises_nothing(
    session, make_placement, make_candidate
):
    pid = make_placement(candidate_id=make_candidate())
    start_placement(session, pid, TODAY)
    follow_up_id = session.execute(
        text(
            "SELECT follow_up_id FROM follow_ups WHERE placement_id = :p "
            "AND checkpoint = 'day_1'"
        ),
        {"p": pid},
    ).scalar_one()
    complete_follow_up(session, follow_up_id, still_working=True)
    assert open_escalations(session) == []


# --- inbound end to end ----------------------------------------------------

def test_a_texted_harassment_report_raises_an_escalation(
    session, make_candidate, make_placement, staff_id
):
    cid = make_candidate(name="Aline")
    make_placement(candidate_id=cid)
    inbound_id = record_inbound(
        session, phone_of(session, cid), "the supervisor touched me",
        provider_ref="wa-1",
    )
    outcome = handle(session, inbound_id)

    assert "harassment escalation raised" in outcome
    escalation = open_escalations(session)[0]
    assert escalation["kind"] == "harassment"
    assert escalation["display_name"] == "Aline"
    assert escalation["detail"] == "the supervisor touched me"


def test_yes_accepts_an_outstanding_offer(
    session, make_candidate, make_placement, staff_id
):
    cid = make_candidate()
    pid = make_placement(candidate_id=cid)
    inbound_id = record_inbound(session, phone_of(session, cid), "YES",
                                provider_ref="wa-2")
    handle(session, inbound_id)

    status = session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :p"),
        {"p": pid},
    ).scalar_one()
    assert status == "accepted"


def test_no_declines_an_outstanding_offer(
    session, make_candidate, make_placement
):
    cid = make_candidate()
    pid = make_placement(candidate_id=cid)
    handle(
        session,
        record_inbound(session, phone_of(session, cid), "oya", provider_ref="wa-3"),
    )
    status = session.execute(
        text("SELECT status::text FROM placements WHERE placement_id = :p"),
        {"p": pid},
    ).scalar_one()
    assert status == "declined"


def test_stop_withdraws_consent_and_stops_messages(
    session, make_candidate, make_placement, staff_id
):
    from app.messaging.events import on_placement_offered
    from app.operations.registry import record_consent

    cid = make_candidate()
    record_consent(session, cid, "placement", True, "paper", staff_id)
    pid = make_placement(candidate_id=cid)
    on_placement_offered(session, pid)

    handle(
        session,
        record_inbound(session, phone_of(session, cid), "STOP", provider_ref="wa-4"),
    )

    granted = session.execute(
        text(
            "SELECT granted FROM v_current_consent "
            "WHERE candidate_id = :c AND purpose = 'placement'"
        ),
        {"c": cid},
    ).scalar_one()
    assert granted is False

    queued = session.execute(
        text("SELECT count(*) FROM messages WHERE candidate_id = :c "
             "AND status = 'queued'"),
        {"c": cid},
    ).scalar_one()
    assert queued == 0


def test_a_duplicate_delivery_is_ignored(session, make_candidate, make_placement):
    """Providers retry webhooks; acting twice would decline an offer twice."""
    cid = make_candidate()
    make_placement(candidate_id=cid)
    phone = phone_of(session, cid)
    assert record_inbound(session, phone, "YES", provider_ref="wa-dup") is not None
    assert record_inbound(session, phone, "YES", provider_ref="wa-dup") is None


def test_an_unrecognised_number_is_kept_for_a_human(session):
    inbound_id = record_inbound(
        session, "+250780009999", "hello?", provider_ref="wa-5"
    )
    outcome = handle(session, inbound_id)
    assert "do not recognise" in outcome

    unhandled = session.execute(
        text("SELECT count(*) FROM inbound_messages WHERE handled_at IS NULL")
    ).scalar_one()
    assert unhandled == 1


def test_an_uninterpretable_reply_waits_rather_than_being_guessed(
    session, make_candidate, make_placement
):
    cid = make_candidate()
    make_placement(candidate_id=cid)
    inbound_id = record_inbound(
        session, phone_of(session, cid), "what time should I arrive?",
        provider_ref="wa-6",
    )
    assert "could not interpret" in handle(session, inbound_id)
    still_open = session.execute(
        text("SELECT status::text FROM placements WHERE candidate_id = :c"),
        {"c": cid},
    ).scalar_one()
    assert still_open == "offered"


def test_the_webhook_requires_its_secret(api, monkeypatch):
    from app.config import get_settings

    r = api.post(
        "/webhooks/inbound",
        json={"from_phone": "+250780000001", "body": "YES"},
    )
    # Unconfigured: 503 rather than accepting unauthenticated posts.
    assert r.status_code in (401, 503)
    assert get_settings().inbound_webhook_secret is None


# --- a flag raised on a call carries what was said -------------------------

def test_a_harassment_flag_without_a_note_is_refused(session, make_placement):
    """The escalation would be raised with no account of what happened -- so
    whoever picks it up, with a response clock running, has to telephone the
    coordinator back to find out. Same rule as a damage deduction: the flag is
    the category, the note is the thing somebody has to answer.
    """
    from app.operations.follow_ups import FollowUpError, complete_follow_up

    placement_id = make_placement()
    follow_up_id = session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', kigali_today()) RETURNING follow_up_id"),
        {"p": str(placement_id)},
    ).scalar_one()

    with pytest.raises(FollowUpError, match="needs a note"):
        complete_follow_up(session, follow_up_id, True, issue_flag="harassment")


def test_a_flagged_call_reaches_the_owner_with_its_detail(session,
                                                          make_placement,
                                                          staff_id):
    from app.operations.follow_ups import complete_follow_up

    placement_id = make_placement()
    follow_up_id = session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', kigali_today()) RETURNING follow_up_id"),
        {"p": str(placement_id)},
    ).scalar_one()

    complete_follow_up(session, follow_up_id, True, issue_flag="harassment",
                       notes="supervisor made comments about her appearance")

    detail = session.execute(
        text("SELECT detail FROM escalations WHERE kind = 'harassment'")
    ).scalar_one()
    assert "comments about her appearance" in detail


def test_an_ordinary_flag_still_needs_no_note(session, make_placement):
    """Only the ones that raise an escalation. A transport note is useful and
    a transport flag with none is not a safeguard failing."""
    from app.operations.follow_ups import complete_follow_up

    placement_id = make_placement()
    follow_up_id = session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', kigali_today()) RETURNING follow_up_id"),
        {"p": str(placement_id)},
    ).scalar_one()
    complete_follow_up(session, follow_up_id, True, issue_flag="transport")
    assert session.execute(
        text("SELECT count(*) FROM escalations")
    ).scalar_one() == 0


def test_the_form_offers_somewhere_to_write_it(web, session, make_placement):
    """A rule enforced in the operation and unreachable in the form is just a
    way to make the page fail."""
    placement_id = make_placement()
    session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', kigali_today())"),
        {"p": str(placement_id)},
    )
    page = web.get(f"/ui/placements/{placement_id}").text
    assert 'name="notes"' in page


def test_the_page_says_why_when_it_refuses(web, session, make_placement):
    from tests.conftest import csrf

    placement_id = make_placement()
    session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', kigali_today())"),
        {"p": str(placement_id)},
    )
    follow_up_id = session.execute(
        text("SELECT follow_up_id FROM follow_ups WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one()

    page = web.get(f"/ui/placements/{placement_id}").text
    refused = web.post(f"/ui/follow-ups/{follow_up_id}/complete",
                       data={"csrf_token": csrf(page), "still_working": "true",
                             "issue_flag": "harassment"},
                       follow_redirects=True)
    assert "needs a note" in refused.text
    assert session.execute(
        text("SELECT count(*) FROM escalations")
    ).scalar_one() == 0
