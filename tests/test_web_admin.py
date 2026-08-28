"""The staff console and reports pages.

These features already existed over the API. What is tested here is that they
are reachable by the people who need them and refused to everyone else -- a
nav link hidden by a template is an affordance, never a control.
"""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.auth import AuthError, login
from tests.conftest import totp_now


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the rendered form"
    return match.group(1)


@pytest.fixture
def web(api, staff_login):
    r = api.post(
        "/ui/login",
        data={"phone": staff_login["phone"], "password": staff_login["password"]},
        follow_redirects=True,
    )
    assert r.status_code == 200
    page = api.get("/ui/mfa").text
    api.post(
        "/ui/mfa",
        data={"csrf_token": csrf(page),
              "code": totp_now(staff_login["totp_secret"], 1)},
        follow_redirects=True,
    )
    return api


def as_coordinator(api, session, staff_login):
    session.execute(
        text("UPDATE staff SET role = 'coordinator' WHERE staff_id = :s"),
        {"s": staff_login["staff_id"]},
    )
    api.post(
        "/ui/login",
        data={"phone": staff_login["phone"], "password": staff_login["password"]},
        follow_redirects=True,
    )
    return api


# --- authorization ---------------------------------------------------------

def test_a_coordinator_cannot_reach_the_staff_console(api, session, staff_login):
    page = as_coordinator(api, session, staff_login).get(
        "/ui/staff", follow_redirects=True
    )
    assert "owners and admins" in page.text
    assert "Add someone" not in page.text


def test_a_coordinator_cannot_create_staff_by_posting_directly(
    api, session, staff_login
):
    """The hidden form is a UI affordance; the server still has to refuse."""
    client = as_coordinator(api, session, staff_login)
    page = client.get("/ui/", follow_redirects=True).text
    client.post(
        "/ui/staff",
        data={
            "csrf_token": csrf(page), "full_name": "Sneaky",
            "phone": "+250780007777", "role": "owner",
            "can_view_identity": "true",
        },
        follow_redirects=True,
    )
    assert session.execute(
        text("SELECT count(*) FROM staff WHERE full_name = 'Sneaky'")
    ).scalar_one() == 0


def test_the_nav_hides_admin_pages_from_a_coordinator(api, session, staff_login):
    page = as_coordinator(api, session, staff_login).get("/ui/").text
    assert 'href="/ui/staff"' not in page
    assert 'href="/ui/reports"' not in page


def test_an_owner_sees_them(web):
    page = web.get("/ui/").text
    assert 'href="/ui/staff"' in page
    assert 'href="/ui/reports"' in page


# --- creating staff --------------------------------------------------------

def test_creating_a_coordinator_shows_the_password_once(web, session):
    page = web.get("/ui/staff").text
    result = web.post(
        "/ui/staff",
        data={
            "csrf_token": csrf(page), "full_name": "New Coordinator",
            "phone": "+250780008001", "role": "coordinator",
            "can_view_identity": "false",
        },
        follow_redirects=True,
    )
    assert "Created New Coordinator" in result.text
    assert "Shown once" in result.text

    match = re.search(r"Temporary password: <code[^>]*>([^<]+)</code>", result.text)
    assert match, "the password must be displayed"
    assert login(session, "+250780008001", match.group(1))

    # Not shown again on a plain reload.
    assert "Temporary password" not in web.get("/ui/staff").text


def test_a_duplicate_phone_is_refused(web):
    page = web.get("/ui/staff").text
    payload = {
        "csrf_token": csrf(page), "full_name": "First",
        "phone": "+250780008002", "role": "coordinator",
        "can_view_identity": "false",
    }
    web.post("/ui/staff", data=payload, follow_redirects=True)
    again = web.post("/ui/staff", data=payload, follow_redirects=True)
    assert "already has that phone number" in again.text


def test_identity_access_defaults_off_in_the_form(web, session):
    page = web.get("/ui/staff").text
    web.post(
        "/ui/staff",
        data={
            "csrf_token": csrf(page), "full_name": "Default Access",
            "phone": "+250780008003", "role": "admin",
            "can_view_identity": "false",
        },
        follow_redirects=True,
    )
    granted = session.execute(
        text("SELECT can_view_identity FROM staff WHERE phone = '+250780008003'")
    ).scalar_one()
    assert granted is False


# --- changing access -------------------------------------------------------

def test_revoking_identity_access_ends_their_sessions(web, session):
    page = web.get("/ui/staff").text
    result = web.post(
        "/ui/staff",
        data={
            "csrf_token": csrf(page), "full_name": "Has Access",
            "phone": "+250780008004", "role": "coordinator",
            "can_view_identity": "true",
        },
        follow_redirects=True,
    )
    password = re.search(
        r"Temporary password: <code[^>]*>([^<]+)</code>", result.text
    ).group(1)
    login(session, "+250780008004", password)

    target = session.execute(
        text("SELECT staff_id FROM staff WHERE phone = '+250780008004'")
    ).scalar_one()
    page = web.get("/ui/staff").text
    revoked = web.post(
        f"/ui/staff/{target}/identity",
        data={"csrf_token": csrf(page), "can_view_identity": "false"},
        follow_redirects=True,
    )
    assert "revoked" in revoked.text
    assert "1 session(s) ended" in revoked.text


def test_resetting_a_password_invalidates_the_old_one(web, session):
    page = web.get("/ui/staff").text
    first = web.post(
        "/ui/staff",
        data={
            "csrf_token": csrf(page), "full_name": "Forgetful",
            "phone": "+250780008005", "role": "coordinator",
            "can_view_identity": "false",
        },
        follow_redirects=True,
    )
    old = re.search(
        r"Temporary password: <code[^>]*>([^<]+)</code>", first.text
    ).group(1)

    target = session.execute(
        text("SELECT staff_id FROM staff WHERE phone = '+250780008005'")
    ).scalar_one()
    page = web.get("/ui/staff").text
    reset = web.post(
        f"/ui/staff/{target}/reset-password",
        data={"csrf_token": csrf(page)}, follow_redirects=True,
    )
    new = re.search(
        r"Temporary password: <code[^>]*>([^<]+)</code>", reset.text
    ).group(1)

    assert new != old
    with pytest.raises(AuthError):
        login(session, "+250780008005", old)
    assert login(session, "+250780008005", new)


def test_you_cannot_deactivate_yourself(web, staff_login):
    page = web.get("/ui/staff").text
    result = web.post(
        f"/ui/staff/{staff_login['staff_id']}/deactivate",
        data={"csrf_token": csrf(page)}, follow_redirects=True,
    )
    assert "cannot deactivate your own account" in result.text


def test_the_audit_chain_is_reported_on_the_staff_page(web, make_candidate):
    make_candidate()
    page = web.get("/ui/staff").text
    assert "Audit log integrity" in page
    assert "intact" in page


def test_staff_changes_are_audited(web, session):
    page = web.get("/ui/staff").text
    web.post(
        "/ui/staff",
        data={
            "csrf_token": csrf(page), "full_name": "Audited Person",
            "phone": "+250780008006", "role": "coordinator",
            "can_view_identity": "false",
        },
        follow_redirects=True,
    )
    target = session.execute(
        text("SELECT staff_id FROM staff WHERE phone = '+250780008006'")
    ).scalar_one()
    events = session.execute(
        text(
            "SELECT detail->>'event' FROM audit_log "
            "WHERE table_name = 'staff' AND record_id = :s ORDER BY audit_id"
        ),
        {"s": target},
    ).scalars().all()
    assert "staff_created" in events


# --- employer contact logins -----------------------------------------------

def test_a_coordinator_can_give_an_employer_a_login(web, session, employer_id):
    page = web.get("/ui/employers").text
    result = web.post(
        f"/ui/employers/{employer_id}/contacts",
        data={
            "csrf_token": csrf(page), "full_name": "Chantal",
            "phone": "+250788009001", "role_title": "Manager",
            "is_primary": "true", "give_login": "true",
        },
        follow_redirects=True,
    )
    assert "Login for Chantal" in result.text
    password = re.search(
        r"Temporary password: <code[^>]*>([^<]+)</code>", result.text
    ).group(1)

    from app.employer_auth import employer_login

    assert employer_login(session, "+250788009001", password)


def test_a_contact_added_without_a_login_cannot_sign_in(
    web, session, employer_id
):
    from app.employer_auth import EmployerAuthError, employer_login

    page = web.get("/ui/employers").text
    web.post(
        f"/ui/employers/{employer_id}/contacts",
        data={
            "csrf_token": csrf(page), "full_name": "No Login",
            "phone": "+250788009002", "give_login": "false",
        },
        follow_redirects=True,
    )
    with pytest.raises(EmployerAuthError):
        employer_login(session, "+250788009002", "anything")


def test_contacts_are_listed_against_their_employer(web, employer_id):
    page = web.get("/ui/employers").text
    web.post(
        f"/ui/employers/{employer_id}/contacts",
        data={
            "csrf_token": csrf(page), "full_name": "Listed Contact",
            "phone": "+250788009003", "is_primary": "true",
            "give_login": "true",
        },
        follow_redirects=True,
    )
    listing = web.get("/ui/employers").text
    assert "Listed Contact" in listing
    assert "has login" in listing


# --- reports ---------------------------------------------------------------

def test_the_reports_page_shows_escalation_performance(
    web, session, make_candidate
):
    from app.operations.escalations import raise_escalation

    raise_escalation(session, "harassment", candidate_id=make_candidate())
    page = web.get("/ui/reports").text
    assert "Escalation response times" in page
    assert "harassment" in page
    assert "within 2 hours" in page


def test_the_reports_page_offers_the_lmis_export(web):
    page = web.get("/ui/reports").text
    assert 'action="/lmis/report.csv"' in page
    assert "no identifier of any kind" in page


def test_reporting_consent_is_shown_separately(web, session, make_candidate,
                                               staff_id):
    from app.operations.registry import record_consent

    cid = make_candidate()
    record_consent(session, cid, "placement", True, "paper", staff_id)
    record_consent(session, cid, "reporting", True, "paper", staff_id)

    page = web.get("/ui/reports").text
    assert "Reporting consent" in page
    # Normalised: the template wraps this sentence across a line break.
    assert "agreeing to appear in a national dataset" in " ".join(page.split())
    assert "Tracked separately from placement consent" in " ".join(page.split())


def test_a_coordinator_cannot_reach_reports(api, session, staff_login):
    page = as_coordinator(api, session, staff_login).get(
        "/ui/reports", follow_redirects=True
    )
    assert "owners and admins" in page.text
