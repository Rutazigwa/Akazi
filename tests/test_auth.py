"""Authentication, session revocation, and the identity-access gate.

These protect access to national ID numbers. A failure here is a Law 058/2021
exposure, not a broken feature.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.auth import (
    MAX_FAILED_LOGINS,
    AuthError,
    authenticate,
    login,
    logout,
    revoke_all_sessions,
)

os.environ.setdefault("DATA_RESIDENCY", "local_dev")


# --- passwords and tokens --------------------------------------------------

def test_login_issues_a_working_token(session, staff_login):
    token = login(session, staff_login["phone"], staff_login["password"])
    staff = authenticate(session, token)
    assert staff.staff_id == staff_login["staff_id"]


def test_the_plaintext_token_is_never_stored(session, staff_login):
    token = login(session, staff_login["phone"], staff_login["password"])
    stored = session.execute(
        text("SELECT encode(token_sha256,'hex') FROM staff_sessions")
    ).scalar_one()
    assert token not in stored
    assert len(token) > 30


def test_the_password_is_not_stored_in_plaintext(session, staff_login):
    stored = session.execute(
        text("SELECT password_hash FROM staff WHERE staff_id = :sid"),
        {"sid": staff_login["staff_id"]},
    ).scalar_one()
    assert staff_login["password"] not in stored
    assert stored.startswith("$argon2")


def test_a_wrong_password_is_rejected(session, staff_login):
    with pytest.raises(AuthError):
        login(session, staff_login["phone"], "wrong")


def test_an_unknown_account_gives_the_same_error_as_a_wrong_password(
    session, staff_login
):
    """No user-enumeration oracle: both paths must look identical."""
    with pytest.raises(AuthError) as unknown:
        login(session, "+250780000000", "whatever")
    with pytest.raises(AuthError) as wrong:
        login(session, staff_login["phone"], "wrong")
    assert str(unknown.value) == str(wrong.value)


def test_a_garbage_token_is_rejected(session):
    with pytest.raises(AuthError):
        authenticate(session, "not-a-real-token")


# --- lockout ---------------------------------------------------------------

def test_repeated_failures_lock_the_account(session, staff_login):
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(AuthError):
            login(session, staff_login["phone"], "wrong")

    locked_until = session.execute(
        text("SELECT locked_until FROM staff WHERE staff_id = :sid"),
        {"sid": staff_login["staff_id"]},
    ).scalar_one()
    assert locked_until is not None

    # Even the correct password is refused while the lock holds.
    with pytest.raises(AuthError):
        login(session, staff_login["phone"], staff_login["password"])


def test_a_successful_login_clears_the_failure_count(session, staff_login):
    for _ in range(MAX_FAILED_LOGINS - 1):
        with pytest.raises(AuthError):
            login(session, staff_login["phone"], "wrong")
    login(session, staff_login["phone"], staff_login["password"])

    count = session.execute(
        text("SELECT failed_login_count FROM staff WHERE staff_id = :sid"),
        {"sid": staff_login["staff_id"]},
    ).scalar_one()
    assert count == 0


# --- revocation ------------------------------------------------------------

def test_logout_kills_the_token(session, staff_login):
    token = login(session, staff_login["phone"], staff_login["password"])
    logout(session, token)
    with pytest.raises(AuthError):
        authenticate(session, token)


def test_deactivating_staff_cuts_live_sessions_immediately(
    session, staff_login
):
    """Not at token expiry -- now. This is the whole reason tokens are stateful."""
    token = login(session, staff_login["phone"], staff_login["password"])
    assert authenticate(session, token)

    session.execute(
        text(
            "UPDATE staff SET is_active = false, deactivated_at = now() "
            "WHERE staff_id = :sid"
        ),
        {"sid": staff_login["staff_id"]},
    )
    with pytest.raises(AuthError):
        authenticate(session, token)


def test_revoke_all_sessions_cuts_every_device(session, staff_login):
    tokens = [
        login(session, staff_login["phone"], staff_login["password"])
        for _ in range(3)
    ]
    assert revoke_all_sessions(session, staff_login["staff_id"]) == 3
    for token in tokens:
        with pytest.raises(AuthError):
            authenticate(session, token)


def test_an_expired_session_is_rejected(session, staff_login):
    token = login(session, staff_login["phone"], staff_login["password"])
    # Move the whole window into the past: the constraint requires
    # expires_at > issued_at, and a real expired session satisfies that.
    session.execute(
        text(
            "UPDATE staff_sessions "
            "   SET issued_at  = now() - INTERVAL '13 hours',"
            "       expires_at = now() - INTERVAL '1 hour'"
        )
    )
    with pytest.raises(AuthError):
        authenticate(session, token)


# --- over HTTP -------------------------------------------------------------

def test_every_operations_route_requires_a_token(api):
    for method, path in [
        ("get", "/guarantees/open"),
        ("get", "/follow-ups/due"),
        ("get", "/metrics/scorecard"),
        ("get", "/auth/me"),
    ]:
        assert getattr(api, method)(path).status_code == 401, path


def test_a_bad_scheme_is_rejected(api):
    api.headers["Authorization"] = "Basic abc123"
    assert api.get("/auth/me").status_code == 401


def test_login_and_me_round_trip(client, staff_login):
    me = client.get("/auth/me").json()
    assert me["staff_id"] == str(staff_login["staff_id"])
    assert me["can_view_identity"] is True


def test_logout_over_http_invalidates_the_session(client):
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_login_over_http_does_not_leak_which_half_was_wrong(api, staff_login):
    bad_password = api.post(
        "/auth/login", json={"phone": staff_login["phone"], "password": "x"}
    )
    bad_account = api.post(
        "/auth/login", json={"phone": "+250780000000", "password": "x"}
    )
    assert bad_password.status_code == bad_account.status_code == 401
    assert bad_password.json() == bad_account.json()


# --- the identity gate -----------------------------------------------------

def test_identity_read_is_audited(client, session, make_candidate):
    cid = make_candidate(name="Aline")
    r = client.get(f"/candidates/{cid}/identity?purpose=support")
    assert r.status_code == 200
    assert r.json()["legal_first_name"] == "Test"

    log = client.get(f"/candidates/{cid}/access-log").json()["access_log"]
    actions = [entry["action"] for entry in log]
    assert "read" in actions
    assert "insert" in actions
    # The read is attributed, not anonymous.
    assert all(e["staff_name"] for e in log if e["action"] == "read")


def test_staff_without_the_grant_cannot_read_identity(
    api, session, staff_login, make_candidate
):
    cid = make_candidate()
    session.execute(
        text("UPDATE staff SET can_view_identity = false WHERE staff_id = :sid"),
        {"sid": staff_login["staff_id"]},
    )
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"

    r = api.get(f"/candidates/{cid}/identity?purpose=support")
    assert r.status_code == 403
    assert "identity data access" in r.json()["detail"]


def test_a_refused_read_leaves_no_audit_row(
    api, session, staff_login, make_candidate
):
    """The gate runs before the function that logs, so a 403 is not a 'read'."""
    cid = make_candidate()
    session.execute(
        text("UPDATE staff SET can_view_identity = false WHERE staff_id = :sid"),
        {"sid": staff_login["staff_id"]},
    )
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"
    api.get(f"/candidates/{cid}/identity?purpose=support")

    reads = session.execute(
        text(
            "SELECT count(*) FROM audit_log "
            "WHERE record_id = :cid AND action = 'read'"
        ),
        {"cid": cid},
    ).scalar_one()
    assert reads == 0


def test_direct_select_on_identity_is_revoked_for_the_operations_role(session):
    """The read trail is only complete because this path is closed."""
    granted = session.execute(
        text(
            """
            SELECT has_table_privilege('app_operations', 'candidate_identity',
                                       'SELECT')
            """
        )
    ).scalar_one()
    assert granted is False
