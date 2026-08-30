"""Data subject rights: access and erasure (Law No. 058/2021).

The central test here is that erasure removes the person without destroying the
employment record. A literal DELETE would cascade through candidates into
placements, attendance and pay records -- taking an employer's confirmed
attendance and another candidate's replacement chain with it.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.operations.attendance import (
    log_attendance,
    record_replacement,
    start_placement,
)
from app.operations.data_rights import (
    DataRightsError,
    complete_erasure,
    erasure_blockers,
    export_candidate_data,
    open_erasure_requests,
    refuse_erasure,
    request_erasure,
)

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


# --- access ---------------------------------------------------------------

def test_export_covers_everything_held_about_a_person(
    session, make_placement, make_candidate, staff_id
):
    cid = make_candidate(consented=False, name="Aline")
    pid = make_placement(candidate_id=cid)
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, True, "employer", hours_worked=8)

    export = export_candidate_data(session, cid)

    assert export["identity"]["legal_first_name"] == "Test"
    assert export["profile"][0]["display_name"] == "Aline"
    assert export["consent_history"] == []  # fixture candidate has none
    assert len(export["placements"]) == 1
    assert export["placements"][0]["business_name"] == "Isuku Cooperative"
    assert len(export["attendance"]) == 1
    assert export["follow_ups"]  # scheduled at start
    assert export["identity_access_log"]


def test_producing_an_export_is_itself_audited(session, make_candidate):
    """Reading someone's whole file is an access. It has to appear in the log."""
    cid = make_candidate()
    before = session.execute(
        text(
            "SELECT count(*) FROM audit_log WHERE record_id = :cid "
            "AND action = 'read'"
        ),
        {"cid": cid},
    ).scalar_one()

    export_candidate_data(session, cid)

    after = session.execute(
        text(
            "SELECT count(*) FROM audit_log WHERE record_id = :cid "
            "AND action = 'read'"
        ),
        {"cid": cid},
    ).scalar_one()
    assert after == before + 1


def test_export_of_an_unknown_candidate_fails_cleanly(session):
    import uuid

    with pytest.raises(DataRightsError, match="not found"):
        export_candidate_data(session, uuid.uuid4())


# --- erasure --------------------------------------------------------------

def test_erasure_redacts_the_identity_record(session, make_candidate, staff_id):
    cid = make_candidate(name="Aline")
    erasure_id = request_erasure(session, cid, "whatsapp", staff_id)
    complete_erasure(session, erasure_id)

    row = session.execute(
        text(
            "SELECT legal_first_name, legal_last_name, national_id, "
            "       phone_alt, emergency_contact, erased_at "
            "FROM candidate_identity WHERE candidate_id = :cid"
        ),
        {"cid": cid},
    ).mappings().one()

    assert row["legal_first_name"] == "ERASED"
    assert row["legal_last_name"] == "ERASED"
    assert row["national_id"] is None
    assert row["phone_alt"] is None
    assert row["emergency_contact"] is None
    assert row["erased_at"] is not None


def test_erasure_clears_home_location_and_display_name(
    session, make_candidate, staff_id
):
    """A home location plus shift times re-identifies someone with no name at all."""
    cid = make_candidate()
    session.execute(
        text(
            "UPDATE candidates SET home_lat = -1.95, home_lng = 30.11, "
            "cell = 'Kabeza' WHERE candidate_id = :cid"
        ),
        {"cid": cid},
    )
    erasure_id = request_erasure(session, cid, "paper", staff_id)
    complete_erasure(session, erasure_id)

    row = session.execute(
        text(
            "SELECT home_lat, home_lng, cell, display_name, status::text "
            "FROM candidates WHERE candidate_id = :cid"
        ),
        {"cid": cid},
    ).mappings().one()
    assert row["home_lat"] is None
    assert row["home_lng"] is None
    assert row["cell"] is None
    assert row["display_name"] == "Erased candidate"
    assert row["status"] == "withdrawn"


def test_erasure_preserves_the_employment_record(
    session, make_placement, make_candidate, staff_id
):
    """The whole reason erasure is redaction and not deletion.

    A DELETE on candidate_identity cascades to candidates, and from there to
    placements, attendance and pay records -- destroying an employer's
    confirmed attendance and our own evidence that we met our obligations.
    """
    cid = make_candidate()
    pid = make_placement(candidate_id=cid)
    start_placement(session, pid, TODAY - timedelta(days=2))
    log_attendance(session, pid, TODAY - timedelta(days=2), True, "employer",
                   hours_worked=8)
    session.execute(
        text(
            """
            INSERT INTO pay_records (placement_id, period_start, period_end,
                                     gross_rwf, due_on, paid_on, method)
            VALUES (:pid, :start, :end, 5000, :due, :due, 'momo')
            """
        ),
        {"pid": pid, "start": TODAY - timedelta(days=2), "end": TODAY,
         "due": TODAY},
    )

    erasure_id = request_erasure(session, cid, "phone", staff_id)
    complete_erasure(session, erasure_id)

    assert session.execute(
        text("SELECT count(*) FROM placements WHERE candidate_id = :cid"),
        {"cid": cid},
    ).scalar_one() == 1
    assert session.execute(
        text(
            "SELECT count(*) FROM attendance a JOIN placements p "
            "ON p.placement_id = a.placement_id WHERE p.candidate_id = :cid"
        ),
        {"cid": cid},
    ).scalar_one() == 1
    assert session.execute(
        text(
            "SELECT count(*) FROM pay_records pr JOIN placements p "
            "ON p.placement_id = pr.placement_id WHERE p.candidate_id = :cid"
        ),
        {"cid": cid},
    ).scalar_one() == 1


def test_erasure_does_not_break_another_candidates_replacement_chain(
    session, make_placement, make_candidate, staff_id
):
    """Erasing a no-show must not erase the evidence that we covered the shift."""
    no_show = make_candidate(name="Did not arrive")
    pid = make_placement(candidate_id=no_show)
    start_placement(session, pid, TODAY)
    log_attendance(session, pid, TODAY, False, "employer",
                   absence_reason="did not arrive")
    cover = make_candidate(name="Cover")
    replacement_id = record_replacement(session, pid, cover, "matched on: x")

    erasure_id = request_erasure(session, no_show, "app", staff_id)
    complete_erasure(session, erasure_id)

    chain = session.execute(
        text(
            "SELECT replaces_placement FROM placements WHERE placement_id = :pid"
        ),
        {"pid": replacement_id},
    ).scalar_one()
    assert chain == pid
    assert session.execute(
        text("SELECT count(*) FROM v_guarantee_invocations")
    ).scalar_one() == 1


def test_consent_history_survives_erasure(session, make_candidate, staff_id):
    """Erasing the proof we had a lawful basis is the opposite of compliance."""
    from app.operations.registry import record_consent

    cid = make_candidate(consented=False)
    record_consent(session, cid, "placement", True, "paper", staff_id)

    erasure_id = request_erasure(session, cid, "paper", staff_id)
    complete_erasure(session, erasure_id)

    assert session.execute(
        text("SELECT count(*) FROM consent_records WHERE candidate_id = :cid"),
        {"cid": cid},
    ).scalar_one() == 1


def test_the_erasure_itself_is_audited(session, make_candidate, staff_id):
    cid = make_candidate()
    erasure_id = request_erasure(session, cid, "email", staff_id)
    complete_erasure(session, erasure_id)

    row = session.execute(
        text(
            "SELECT action, detail, staff_id FROM audit_log "
            "WHERE record_id = :cid AND action = 'delete'"
        ),
        {"cid": cid},
    ).mappings().one()
    assert row["detail"]["method"] == "redaction_in_place"
    assert row["staff_id"] is not None


def test_a_candidate_cannot_be_erased_twice(session, make_candidate, staff_id):
    cid = make_candidate()
    first = request_erasure(session, cid, "paper", staff_id)
    complete_erasure(session, first)

    with pytest.raises(DataRightsError, match="already been erased"):
        request_erasure(session, cid, "paper", staff_id)


def test_completing_the_same_request_twice_is_refused(
    session, make_candidate, staff_id
):
    cid = make_candidate()
    erasure_id = request_erasure(session, cid, "paper", staff_id)
    complete_erasure(session, erasure_id)
    with pytest.raises(DataRightsError, match="already completed"):
        complete_erasure(session, erasure_id)


def test_erasure_refuses_to_run_unattributed(session, make_candidate, staff_id):
    """The one operation where "who did this" can never be reconstructed later."""
    cid = make_candidate()
    erasure_id = request_erasure(session, cid, "paper", staff_id)
    session.execute(text("SELECT set_config('app.staff_id', '', true)"))

    with pytest.raises(Exception, match="requires an acting staff member"):
        complete_erasure(session, erasure_id)


# --- blockers and refusal --------------------------------------------------

def test_a_live_placement_is_flagged_as_a_blocker(
    session, make_placement, make_candidate, staff_id
):
    cid = make_candidate()
    make_placement(candidate_id=cid)
    blockers = erasure_blockers(session, cid)
    assert [b.reason for b in blockers] == ["live_placement"]


def test_an_unpaid_wage_is_flagged_as_a_blocker(
    session, make_placement, make_candidate, staff_id
):
    """Deleting someone's phone number while owing them money is not compliance."""
    cid = make_candidate()
    pid = make_placement(candidate_id=cid)
    start_placement(session, pid, TODAY)
    session.execute(
        text(
            """
            INSERT INTO pay_records (placement_id, period_start, period_end,
                                     gross_rwf, due_on)
            VALUES (:pid, :d, :d, 5000, :d)
            """
        ),
        {"pid": pid, "d": TODAY},
    )
    reasons = [b.reason for b in erasure_blockers(session, cid)]
    assert "unpaid_wages" in reasons


def test_blockers_are_advisory_not_a_veto(
    session, make_placement, make_candidate, staff_id
):
    """The right is the person's. Blockers inform the decision, they don't override it."""
    cid = make_candidate()
    make_placement(candidate_id=cid)
    assert erasure_blockers(session, cid)

    erasure_id = request_erasure(session, cid, "paper", staff_id)
    complete_erasure(session, erasure_id)  # must not raise

    assert session.execute(
        text("SELECT erased_at FROM candidate_identity WHERE candidate_id = :cid"),
        {"cid": cid},
    ).scalar_one() is not None


def test_a_refusal_must_state_a_reason(session, make_candidate, staff_id):
    cid = make_candidate()
    erasure_id = request_erasure(session, cid, "paper", staff_id)
    with pytest.raises(DataRightsError, match="must state its reason"):
        refuse_erasure(session, erasure_id, "   ")


def test_a_refused_request_leaves_the_queue(session, make_candidate, staff_id):
    cid = make_candidate()
    erasure_id = request_erasure(session, cid, "paper", staff_id)
    assert len(open_erasure_requests(session)) == 1

    refuse_erasure(session, erasure_id, "identity could not be verified")
    assert open_erasure_requests(session) == []


def test_a_refused_request_cannot_then_be_completed(
    session, make_candidate, staff_id
):
    cid = make_candidate()
    erasure_id = request_erasure(session, cid, "paper", staff_id)
    refuse_erasure(session, erasure_id, "not the data subject")
    with pytest.raises(DataRightsError, match="already refused"):
        complete_erasure(session, erasure_id)


# --- over HTTP -------------------------------------------------------------

def test_erasure_endpoints_require_the_identity_grant(
    api, session, staff_login, make_candidate
):
    from app.auth import login

    cid = make_candidate()
    session.execute(
        text("UPDATE staff SET can_view_identity = false WHERE staff_id = :sid"),
        {"sid": staff_login["staff_id"]},
    )
    api.headers["Authorization"] = (
        f"Bearer {login(session, staff_login['phone'], staff_login['password'])}"
    )

    assert api.get(f"/candidates/{cid}/data-export").status_code == 403
    assert api.post(
        f"/candidates/{cid}/erasure-requests", json={"requested_via": "paper"}
    ).status_code == 403
    assert api.get("/erasure-requests").status_code == 403


def test_the_erasure_flow_over_http(client, make_candidate, make_placement):
    cid = make_candidate(name="Requester")
    make_placement(candidate_id=cid)

    created = client.post(
        f"/candidates/{cid}/erasure-requests", json={"requested_via": "whatsapp"}
    )
    assert created.status_code == 201
    body = created.json()
    # Blockers come back with the request, not behind a second call.
    assert [b["reason"] for b in body["blockers"]] == ["live_placement"]

    assert len(client.get("/erasure-requests").json()["open"]) == 1

    done = client.post(f"/erasure-requests/{body['erasure_id']}/complete")
    assert done.status_code == 200
    assert client.get("/erasure-requests").json()["open"] == []

    erased = client.get(f"/candidates/{cid}/identity?purpose=data_request").json()
    assert erased["legal_first_name"] == "ERASED"
