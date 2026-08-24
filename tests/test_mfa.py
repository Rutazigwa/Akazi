"""Two-factor authentication and the identity-access gate.

The policy under test: a password is enough for operational work, but reaching
a national ID number needs a second factor on the current session.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.auth import login
from app.mfa import (
    MFAError,
    begin_enrolment,
    confirm_enrolment,
    elevate_session,
    reset_enrolment,
)
from tests.conftest import totp_now

os.environ.setdefault("DATA_RESIDENCY", "local_dev")


# --- enrolment -------------------------------------------------------------

def test_enrolment_returns_a_secret_and_a_provisioning_uri(session, staff_id):
    enrolment = begin_enrolment(session, staff_id)
    assert len(enrolment.secret) >= 16
    assert enrolment.otpauth_uri.startswith("otpauth://totp/")
    assert "issuer=Akazi" in enrolment.otpauth_uri


def test_enrolment_is_not_complete_until_a_code_is_confirmed(session, staff_id):
    """A mistyped setup must not lock the account out of identity data."""
    begin_enrolment(session, staff_id)
    enrolled = session.execute(
        text("SELECT totp_enrolled_at FROM staff WHERE staff_id = :sid"),
        {"sid": staff_id},
    ).scalar_one()
    assert enrolled is None


def test_a_wrong_code_does_not_complete_enrolment(session, staff_id):
    begin_enrolment(session, staff_id)
    with pytest.raises(MFAError, match="invalid code"):
        confirm_enrolment(session, staff_id, "000000")


def test_enrolling_twice_is_refused(session, staff_login):
    with pytest.raises(MFAError, match="already enrolled"):
        begin_enrolment(session, staff_login["staff_id"])


# --- replay ----------------------------------------------------------------

def test_a_code_cannot_be_used_twice(session, staff_login):
    """A TOTP code stays valid for its whole step. Once used, it is spent."""
    token = login(session, staff_login["phone"], staff_login["password"])
    session_id = session.execute(
        text("SELECT session_id FROM staff_sessions ORDER BY issued_at DESC LIMIT 1")
    ).scalar_one()

    code = totp_now(staff_login["totp_secret"], 1)
    elevate_session(session, staff_login["staff_id"], session_id, code)

    with pytest.raises(MFAError, match="already been used"):
        elevate_session(session, staff_login["staff_id"], session_id, code)
    assert token


def test_an_older_code_is_refused_after_a_newer_one(session, staff_login):
    session_id = session.execute(
        text("SELECT session_id FROM staff_sessions LIMIT 1")
    ).scalar_one_or_none()
    login(session, staff_login["phone"], staff_login["password"])
    session_id = session.execute(
        text("SELECT session_id FROM staff_sessions ORDER BY issued_at DESC LIMIT 1")
    ).scalar_one()

    elevate_session(
        session, staff_login["staff_id"], session_id,
        totp_now(staff_login["totp_secret"], 1),
    )
    with pytest.raises(MFAError):
        elevate_session(
            session, staff_login["staff_id"], session_id,
            totp_now(staff_login["totp_secret"], 0),
        )


# --- the gate --------------------------------------------------------------

def test_identity_access_needs_mfa_on_this_session(api, session, staff_login,
                                                   make_candidate):
    """Password alone reaches operational data, not a national ID number."""
    cid = make_candidate()
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"

    # Operational work is fine without elevating.
    assert api.get("/metrics/scorecard").status_code == 200
    assert api.get("/follow-ups/due").status_code == 200

    # Identity data is not.
    blocked = api.get(f"/candidates/{cid}/identity")
    assert blocked.status_code == 403
    assert "second factor on this session" in blocked.json()["detail"]

    api.post("/auth/mfa", json={"code": totp_now(staff_login["totp_secret"], 1)})
    assert api.get(f"/candidates/{cid}/identity").status_code == 200


def test_an_account_with_no_second_factor_cannot_reach_identity_data(
    api, session, staff_login, make_candidate
):
    cid = make_candidate()
    reset_enrolment(session, staff_login["staff_id"])
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"

    blocked = api.get(f"/candidates/{cid}/identity")
    assert blocked.status_code == 403
    assert "enrol one at" in blocked.json()["detail"]


def test_elevation_is_per_session_not_per_account(
    api, session, staff_login, make_candidate
):
    """A code presented on one device must not elevate someone else's token."""
    cid = make_candidate()
    laptop = login(session, staff_login["phone"], staff_login["password"])
    phone = login(session, staff_login["phone"], staff_login["password"])

    api.headers["Authorization"] = f"Bearer {laptop}"
    api.post("/auth/mfa", json={"code": totp_now(staff_login["totp_secret"], 1)})
    assert api.get(f"/candidates/{cid}/identity").status_code == 200

    api.headers["Authorization"] = f"Bearer {phone}"
    assert api.get(f"/candidates/{cid}/identity").status_code == 403


def test_resetting_mfa_cuts_live_sessions(api, session, staff_login):
    """A session elevated with the old factor must not survive its removal."""
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"
    assert api.get("/auth/me").status_code == 200

    reset_enrolment(session, staff_login["staff_id"])
    assert api.get("/auth/me").status_code == 401


def test_login_reports_that_a_second_factor_is_outstanding(api, staff_login):
    r = api.post(
        "/auth/login",
        json={"phone": staff_login["phone"], "password": staff_login["password"]},
    ).json()
    assert r["mfa_required"] is True


def test_the_enrolment_flow_over_http(api, session, staff_login, make_candidate):
    cid = make_candidate()
    reset_enrolment(session, staff_login["staff_id"])
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"

    enrol = api.post("/auth/totp/enrol")
    assert enrol.status_code == 200
    secret = enrol.json()["secret"]
    assert enrol.json()["otpauth_uri"].startswith("otpauth://")

    assert api.post(
        "/auth/totp/confirm", json={"code": totp_now(secret)}
    ).status_code == 200
    assert api.get("/auth/me").json()["mfa_enrolled"] is True

    assert api.post(
        "/auth/mfa", json={"code": totp_now(secret, 1)}
    ).status_code == 200
    assert api.get(f"/candidates/{cid}/identity").status_code == 200
