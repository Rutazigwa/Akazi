"""Why somebody opened a person's identity record.

Law No. 058/2021 makes this the answer to two questions: the candidate's, and
the NCSA's. The system had three ways of answering it and they disagreed.

  * read_candidate_identity() validated purpose against a list of six.
  * resolve_inbound_sender() and message_recipient_phone() wrote their own
    purposes straight into audit_log, past that check -- so the log held eight
    values and the validated list described none of the writers but one.
  * GET /candidates/{id}/identity took no purpose at all and recorded the
    default, so every read said "operations" whatever it was for.
  * GET /candidates/{id}/access-log did not return purpose, while the subject
    access export did -- two answers to the same question, and the thinner one
    was the one a coordinator would produce.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

MACHINE_PURPOSES = ("inbound_message", "messaging")
STATED_PURPOSES = ("operations", "placement", "support", "data_request",
                   "erasure", "reporting")


def purposes_for(session, candidate_id) -> list[str]:
    return session.execute(
        text("SELECT detail ->> 'purpose' FROM audit_log "
             "WHERE table_name = 'candidate_identity' AND record_id = :c "
             "AND action = 'read' ORDER BY occurred_at"),
        {"c": str(candidate_id)},
    ).scalars().all()


@pytest.mark.parametrize("purpose", STATED_PURPOSES + MACHINE_PURPOSES)
def test_every_purpose_in_use_is_a_purpose_the_list_accepts(session, purpose):
    """The list and the log must be the same vocabulary.

    Two writers used to insert purposes the validator would have rejected.
    """
    assert session.execute(
        text("SELECT assert_identity_read_purpose(:p)"), {"p": purpose}
    ).scalar_one() == purpose


def test_an_invented_purpose_is_refused(session):
    with pytest.raises(DBAPIError, match="unknown identity read purpose"):
        session.execute(text("SELECT assert_identity_read_purpose('curiosity')"))


def test_the_inbound_webhook_records_a_purpose_the_list_knows(
    session, make_candidate
):
    """resolve_inbound_sender wrote 'inbound_message' past the validator.

    Now it goes through it -- so if the list is ever narrowed without looking
    at its callers, this fails instead of the log quietly gaining a value
    nothing recognises.
    """
    candidate_id = make_candidate()
    phone = session.execute(
        text("SELECT phone_primary FROM candidate_identity WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    ).scalar_one()
    session.execute(text("SELECT * FROM resolve_inbound_sender(:p)"), {"p": phone})
    assert "inbound_message" in purposes_for(session, candidate_id)


def test_the_dispatcher_records_a_purpose_the_list_knows(session, make_candidate):
    candidate_id = make_candidate()
    message_id = session.execute(
        text("INSERT INTO messages (candidate_id, template_key, body) "
             "VALUES (:c, 'shift_reminder', 'tomorrow') RETURNING message_id"),
        {"c": str(candidate_id)},
    ).scalar_one()
    session.execute(text("SELECT * FROM message_recipient_phone(:m)"),
                    {"m": str(message_id)})
    assert "messaging" in purposes_for(session, candidate_id)


# --- over HTTP -------------------------------------------------------------

def test_a_read_must_say_why(client, session, make_candidate):
    """Not defaulted. A reason nobody has to give is not a reason."""
    candidate_id = make_candidate()
    session.commit()
    assert client.get(f"/candidates/{candidate_id}/identity").status_code == 422


def test_the_stated_reason_is_what_gets_recorded(client, session, make_candidate):
    """The defect: every read recorded 'operations' whatever it was for.

    A coordinator taking a support call and a staff member assembling a subject
    access request were indistinguishable in the log.
    """
    candidate_id = make_candidate()
    session.commit()
    assert client.get(
        f"/candidates/{candidate_id}/identity?purpose=support"
    ).status_code == 200
    assert purposes_for(session, candidate_id)[-1] == "support"


def test_an_unknown_reason_is_refused_rather_than_recorded(
    client, session, make_candidate
):
    candidate_id = make_candidate()
    session.commit()
    response = client.get(
        f"/candidates/{candidate_id}/identity?purpose=because_i_wanted_to"
    )
    assert response.status_code == 400
    assert "unknown identity read purpose" in response.json()["detail"]
    assert "because_i_wanted_to" not in purposes_for(session, candidate_id)


def test_the_access_log_says_why_not_only_who(client, session, make_candidate):
    """The endpoint whose docstring calls itself the answer to the NCSA's
    question returned action, time and staff name -- and not the reason."""
    candidate_id = make_candidate()
    session.commit()
    client.get(f"/candidates/{candidate_id}/identity?purpose=placement")

    entries = client.get(f"/candidates/{candidate_id}/access-log").json()["access_log"]
    reads = [e for e in entries if e["action"] == "read"]
    assert reads, entries
    assert "purpose" in reads[0], reads[0]
    assert reads[0]["purpose"] == "placement"


def test_the_access_log_and_the_export_give_the_same_answer(
    client, session, make_candidate, staff_id
):
    """Two queries answering one question. They disagreed for a long time."""
    from app.operations.data_rights import export_candidate_data

    candidate_id = make_candidate()
    session.commit()
    client.get(f"/candidates/{candidate_id}/identity?purpose=reporting")

    # The export performs an audited read of its own, so take it first and
    # read the endpoint afterwards -- otherwise the comparison is between two
    # different moments and would differ for an uninteresting reason.
    exported = export_candidate_data(session, candidate_id)["identity_access_log"]
    endpoint = client.get(
        f"/candidates/{candidate_id}/access-log"
    ).json()["access_log"]

    assert {e["purpose"] for e in endpoint if e["action"] == "read"} == {
        e["purpose"] for e in exported if e["action"] == "read"
    }
