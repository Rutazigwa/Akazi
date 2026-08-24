"""Staff administration and the tamper-evident audit log."""

from __future__ import annotations

import os

from sqlalchemy import text

from app.auth import login

os.environ.setdefault("DATA_RESIDENCY", "local_dev")


def demote(session, staff_id, role="coordinator"):
    session.execute(
        text("UPDATE staff SET role = CAST(:r AS staff_role) WHERE staff_id = :sid"),
        {"r": role, "sid": staff_id},
    )


# --- role gating -----------------------------------------------------------

def test_staff_administration_requires_owner_or_admin(
    api, session, staff_login
):
    demote(session, staff_login["staff_id"], "coordinator")
    api.headers["Authorization"] = (
        f"Bearer {login(session, staff_login['phone'], staff_login['password'])}"
    )

    assert api.get("/staff").status_code == 403
    assert api.post(
        "/staff",
        json={"full_name": "X", "phone": "+250780000055", "role": "coordinator"},
    ).status_code == 403
    assert api.get("/staff/audit/integrity").status_code == 403


def test_a_supervisor_is_not_an_admin(api, session, staff_login):
    demote(session, staff_login["staff_id"], "supervisor")
    api.headers["Authorization"] = (
        f"Bearer {login(session, staff_login['phone'], staff_login['password'])}"
    )
    assert api.get("/staff").status_code == 403


# --- account lifecycle -----------------------------------------------------

def test_creating_staff_returns_a_single_use_temporary_password(client):
    created = client.post(
        "/staff",
        json={
            "full_name": "New Coordinator",
            "phone": "+250780000060",
            "role": "coordinator",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert len(body["temporary_password"]) >= 16

    listed = {s["full_name"]: s for s in client.get("/staff").json()["staff"]}
    assert listed["New Coordinator"]["must_change_password"] is True
    assert listed["New Coordinator"]["can_view_identity"] is False


def test_identity_access_defaults_off_for_new_staff(client):
    client.post(
        "/staff",
        json={"full_name": "Default", "phone": "+250780000061", "role": "admin"},
    )
    listed = {s["full_name"]: s for s in client.get("/staff").json()["staff"]}
    # Even an admin does not get identity access unless it is asked for.
    assert listed["Default"]["can_view_identity"] is False


def test_a_duplicate_phone_is_refused(client):
    payload = {
        "full_name": "First", "phone": "+250780000062", "role": "coordinator",
    }
    assert client.post("/staff", json=payload).status_code == 201
    assert client.post("/staff", json=payload).status_code == 409


def test_the_temporary_password_actually_works(client, session):
    body = client.post(
        "/staff",
        json={"full_name": "Fresh", "phone": "+250780000063", "role": "coordinator"},
    ).json()
    token = login(session, "+250780000063", body["temporary_password"])
    assert token


def test_granting_identity_access_revokes_the_targets_sessions(
    client, session
):
    """A change to identity access takes effect now, not at token expiry."""
    body = client.post(
        "/staff",
        json={"full_name": "Target", "phone": "+250780000064", "role": "coordinator"},
    ).json()
    login(session, "+250780000064", body["temporary_password"])

    updated = client.patch(
        f"/staff/{body['staff_id']}", json={"can_view_identity": True}
    )
    assert updated.status_code == 200
    assert updated.json()["sessions_revoked"] == 1


def test_a_role_change_alone_does_not_cut_sessions(client, session):
    body = client.post(
        "/staff",
        json={"full_name": "Promoted", "phone": "+250780000065", "role": "coordinator"},
    ).json()
    login(session, "+250780000065", body["temporary_password"])

    updated = client.patch(f"/staff/{body['staff_id']}", json={"role": "supervisor"})
    assert updated.json()["sessions_revoked"] == 0


def test_resetting_a_password_cuts_every_session(client, session):
    body = client.post(
        "/staff",
        json={"full_name": "Locked Out", "phone": "+250780000066", "role": "coordinator"},
    ).json()
    old = body["temporary_password"]
    for _ in range(2):
        login(session, "+250780000066", old)

    reset = client.post(f"/staff/{body['staff_id']}/reset-password").json()
    assert reset["sessions_revoked"] == 2
    assert reset["temporary_password"] != old

    import pytest

    from app.auth import AuthError

    with pytest.raises(AuthError):
        login(session, "+250780000066", old)
    assert login(session, "+250780000066", reset["temporary_password"])


def test_deactivating_staff_cuts_sessions(client, session):
    body = client.post(
        "/staff",
        json={"full_name": "Departing", "phone": "+250780000067", "role": "coordinator"},
    ).json()
    login(session, "+250780000067", body["temporary_password"])

    result = client.post(f"/staff/{body['staff_id']}/deactivate")
    assert result.status_code == 200
    assert result.json()["sessions_revoked"] == 1
    assert client.post(f"/staff/{body['staff_id']}/deactivate").status_code == 404


def test_you_cannot_deactivate_yourself(client, staff_login):
    r = client.post(f"/staff/{staff_login['staff_id']}/deactivate")
    assert r.status_code == 409


# --- changing your own password -------------------------------------------

def test_changing_your_password_revokes_other_sessions(
    api, session, staff_login
):
    """If the old password leaked, an attacker's session must not survive."""
    from tests.conftest import totp_now

    login(session, staff_login["phone"], staff_login["password"])  # elsewhere
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"
    api.post("/auth/mfa", json={"code": totp_now(staff_login["totp_secret"], 1)})

    changed = api.post(
        "/auth/password",
        json={
            "current_password": staff_login["password"],
            "new_password": "a-brand-new-long-password",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["other_sessions_revoked"] >= 1
    # The session that made the change stays usable.
    assert api.get("/auth/me").status_code == 200


def test_the_wrong_current_password_is_refused(client, staff_login):
    r = client.post(
        "/auth/password",
        json={"current_password": "wrong", "new_password": "another-long-password"},
    )
    assert r.status_code == 422


def test_the_new_password_must_differ(client, staff_login):
    r = client.post(
        "/auth/password",
        json={
            "current_password": staff_login["password"],
            "new_password": staff_login["password"],
        },
    )
    assert r.status_code == 422


def test_a_short_password_is_refused(client, staff_login):
    r = client.post(
        "/auth/password",
        json={"current_password": staff_login["password"], "new_password": "short"},
    )
    assert r.status_code == 422


# --- the audit chain -------------------------------------------------------

def test_the_audit_log_reports_itself_intact(client, make_candidate):
    make_candidate()
    report = client.get("/staff/audit/integrity").json()
    assert report["intact"] is True
    assert report["entries"] > 0
    assert len(report["head_hash"]) == 64  # sha256, hex


def test_the_head_hash_advances_as_entries_are_added(client, make_candidate):
    first = client.get("/staff/audit/integrity").json()
    make_candidate()
    client.get("/staff/audit/integrity")
    second = client.get("/staff/audit/integrity").json()
    assert second["entries"] > first["entries"]
    assert second["head_hash"] != first["head_hash"]


def test_editing_an_audit_row_is_detected(client, session, make_candidate):
    """The rules block this; an attacker with database access can disable them."""
    make_candidate()
    assert client.get("/staff/audit/integrity").json()["intact"] is True

    target = session.execute(
        text("SELECT audit_id FROM audit_log ORDER BY audit_id LIMIT 1")
    ).scalar_one()
    session.execute(text("ALTER TABLE audit_log DISABLE RULE audit_no_update"))
    session.execute(
        text("UPDATE audit_log SET staff_id = NULL WHERE audit_id = :aid"),
        {"aid": target},
    )
    session.execute(text("ALTER TABLE audit_log ENABLE RULE audit_no_update"))

    report = client.get("/staff/audit/integrity").json()
    assert report["intact"] is False
    assert report["broken_at"]["broken_at_audit_id"] == target
    assert "modified" in report["broken_at"]["reason"]


def test_deleting_an_audit_row_is_detected(client, session, make_candidate):
    make_candidate()
    make_candidate()
    rows = session.execute(
        text("SELECT audit_id FROM audit_log ORDER BY audit_id")
    ).scalars().all()

    session.execute(text("ALTER TABLE audit_log DISABLE RULE audit_no_delete"))
    session.execute(
        text("DELETE FROM audit_log WHERE audit_id = :aid"), {"aid": rows[0]}
    )
    session.execute(text("ALTER TABLE audit_log ENABLE RULE audit_no_delete"))

    report = client.get("/staff/audit/integrity").json()
    assert report["intact"] is False
    assert "removed or reordered" in report["broken_at"]["reason"]


def test_ordinary_updates_and_deletes_are_refused_outright(
    session, make_candidate
):
    make_candidate()
    before = session.execute(text("SELECT count(*) FROM audit_log")).scalar_one()

    session.execute(text("UPDATE audit_log SET action = 'insert'"))
    session.execute(text("DELETE FROM audit_log"))

    after = session.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    assert after == before


def test_privileged_staff_changes_are_audited(client, session):
    created = client.post(
        "/staff",
        json={"full_name": "Audited", "phone": "+250780000070", "role": "coordinator"},
    ).json()
    client.patch(f"/staff/{created['staff_id']}", json={"can_view_identity": True})

    events = session.execute(
        text(
            "SELECT detail->>'event' FROM audit_log "
            "WHERE table_name = 'staff' AND record_id = :sid "
            "ORDER BY audit_id"
        ),
        {"sid": created["staff_id"]},
    ).scalars().all()
    assert events == ["staff_created", "staff_updated"]
