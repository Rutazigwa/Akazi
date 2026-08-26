"""The application's real writes, run as the role production connects with.

Every one of these covers a defect that shipped green: grants were asserted
from the migration source, which proves what was granted, not that what the
application does is permitted. The difference is not academic --

  * creating a staff account had no INSERT grant at all;
  * registering a candidate failed because RETURNING candidate_id needs
    SELECT on that column, and SELECT had been revoked wholesale;
  * an inbound reply was matched against candidate_identity directly, so
    every worker reply was dropped -- and the lookup left no audit trail;
  * requesting erasure read erased_at, also revoked.

None of that is visible to a test connected as the owner, because the owner
bypasses grants. So these connect as akazi_app and do the real thing.
"""
from __future__ import annotations

import uuid
from datetime import date, time

import pytest
from sqlalchemy import text

from app.messaging.inbound import record_inbound
from app.operations.data_rights import request_erasure, export_candidate_data
from app.operations.registry import AvailabilitySlot, register_candidate


pytestmark = pytest.mark.usefixtures("restricted_session")


@pytest.fixture(scope="module")
def owner_staff(restricted_session):
    """A staff row created through the restricted role -- gap (1)."""
    staff_id = restricted_session.execute(
        text(
            """
            INSERT INTO staff (full_name, phone, role, can_view_identity,
                               password_hash, must_change_password)
            VALUES ('Restricted Owner', '+250780009911', 'owner', TRUE,
                    'x', FALSE)
            RETURNING staff_id
            """
        )
    ).scalar_one()
    restricted_session.commit()
    return staff_id


def _register(session, staff_id, phone: str):
    return register_candidate(
        session,
        legal_first_name="Aline",
        legal_last_name="Uwase",
        date_of_birth=date(2002, 3, 4),
        phone_primary=phone,
        display_name="Aline U.",
        district="Gasabo",
        sector="Remera",
        gender="F",
        home_lat=-1.9480,
        home_lng=30.1050,
        max_commute_rwf=2000,
        consent_captured_via="paper",
        registered_by=staff_id,
        availability=[
            AvailabilitySlot(d, time(6, 0), time(20, 0))
            for d in range(7)
        ],
    )


def test_the_app_role_can_create_a_staff_account(owner_staff):
    """Gap (1): without INSERT on staff, nobody could be onboarded at all."""
    assert owner_staff is not None


def test_the_app_role_can_register_a_candidate(restricted_session, owner_staff):
    """Gap (2): INSERT ... RETURNING needs SELECT on the returned column."""
    candidate_id = _register(restricted_session, owner_staff, "+250788770001")
    restricted_session.commit()
    assert candidate_id is not None


def test_an_inbound_reply_resolves_to_the_candidate_who_sent_it(
    restricted_session, owner_staff
):
    """Gap (3): the lookup read candidate_identity directly and was refused."""
    phone = "+250788770002"
    candidate_id = _register(restricted_session, owner_staff, phone)
    restricted_session.commit()

    record_inbound(
        restricted_session, from_phone=phone, channel="whatsapp",
        body="I am running late", provider_ref=f"wa-{uuid.uuid4().hex[:8]}",
    )
    restricted_session.commit()

    matched = restricted_session.execute(
        text("SELECT candidate_id FROM inbound_messages "
             "WHERE from_phone = :p ORDER BY inbound_id DESC LIMIT 1"),
        {"p": phone},
    ).scalar_one()
    assert matched == candidate_id


def test_resolving_an_inbound_sender_is_recorded_as_an_identity_read(
    restricted_session, owner_staff
):
    """Matching a phone number to a person is a read, and must leave a trace."""
    phone = "+250788770003"
    candidate_id = _register(restricted_session, owner_staff, phone)
    restricted_session.commit()

    record_inbound(
        restricted_session, from_phone=phone, channel="whatsapp",
        body="ok", provider_ref=f"wa-{uuid.uuid4().hex[:8]}",
    )
    restricted_session.commit()

    purposes = restricted_session.execute(
        text("SELECT detail ->> 'purpose' FROM audit_log "
             "WHERE table_name = 'candidate_identity' AND record_id = :cid "
             "AND action = 'read'"),
        {"cid": str(candidate_id)},
    ).scalars().all()
    assert "inbound_message" in purposes


def test_an_unknown_number_resolves_to_nobody_and_audits_nothing(
    restricted_session, owner_staff
):
    """A number matching no one has no record to attach a read to."""
    before = restricted_session.execute(
        text("SELECT count(*) FROM audit_log "
             "WHERE table_name = 'candidate_identity' AND action = 'read'")
    ).scalar_one()

    record_inbound(
        restricted_session, from_phone="+250788000000", channel="sms",
        body="wrong number", provider_ref=f"sms-{uuid.uuid4().hex[:8]}",
    )
    restricted_session.commit()

    after = restricted_session.execute(
        text("SELECT count(*) FROM audit_log "
             "WHERE table_name = 'candidate_identity' AND action = 'read'")
    ).scalar_one()
    assert after == before


def test_the_app_role_can_accept_an_erasure_request(
    restricted_session, owner_staff
):
    """Gap (4): the already-erased check reads erased_at, which was revoked."""
    candidate_id = _register(restricted_session, owner_staff, "+250788770004")
    restricted_session.commit()

    erasure_id = request_erasure(
        restricted_session, candidate_id=candidate_id,
        requested_via="phone", received_by=owner_staff,
    )
    restricted_session.commit()
    assert erasure_id is not None


def test_a_subject_access_export_records_why_identity_was_read(
    restricted_session, owner_staff
):
    """The access log has to answer 'why', not only 'who' and 'when'."""
    candidate_id = _register(restricted_session, owner_staff, "+250788770005")
    restricted_session.commit()

    export = export_candidate_data(restricted_session, candidate_id)
    restricted_session.commit()

    purposes = {row["purpose"] for row in export["identity_access_log"]}
    assert "data_request" in purposes


def test_the_app_role_still_cannot_read_identity_columns_directly(
    restricted_session
):
    """The grants added above are narrow. The identifying columns stay shut."""
    for column in ("legal_first_name", "national_id", "phone_primary",
                   "date_of_birth", "*"):
        restricted_session.rollback()
        with pytest.raises(Exception) as caught:
            restricted_session.execute(
                text(f"SELECT {column} FROM candidate_identity LIMIT 1")
            )
        assert "permission denied" in str(caught.value), column
    restricted_session.rollback()


def test_the_readable_columns_are_only_the_two_that_were_granted(
    restricted_session
):
    """A regression guard: if this list grows, it grew on purpose."""
    restricted_session.rollback()
    readable = restricted_session.execute(
        text(
            """
            SELECT column_name FROM information_schema.column_privileges
             WHERE table_name = 'candidate_identity'
               AND grantee = 'app_identity' AND privilege_type = 'SELECT'
             ORDER BY column_name
            """
        )
    ).scalars().all()
    assert readable == ["candidate_id", "erased_at"]
