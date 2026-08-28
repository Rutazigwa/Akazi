"""Temporary passwords must be spent on choosing a real one.

The flag existed from the start and was displayed on the staff console, but
nothing enforced it. That is worse than not having it: an administrator
generates a password, hands it over, and both of them can use it indefinitely
while the console reports "must change password" as though it were a control.
"""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.auth import login


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()
TEMPORARY = "a-temporary-issued-password"
CHOSEN = "the-password-they-picked"


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the rendered form"
    return match.group(1)


@pytest.fixture
def temporary_account(session, staff_id):
    """A staff account whose password was issued by someone else."""
    from app.auth import set_password

    phone = "+250780005555"
    new_id = session.execute(
        text(
            "INSERT INTO staff (full_name, phone, role) "
            "VALUES ('Issued Account', :phone, 'coordinator') RETURNING staff_id"
        ),
        {"phone": phone},
    ).scalar_one()
    set_password(session, new_id, TEMPORARY, must_change=True)
    return {"staff_id": new_id, "phone": phone, "password": TEMPORARY}


# --- the API ---------------------------------------------------------------

def test_the_api_refuses_everything_until_it_is_changed(
    api, session, temporary_account
):
    token = login(session, temporary_account["phone"], temporary_account["password"])
    api.headers["Authorization"] = f"Bearer {token}"

    blocked = api.get("/metrics/scorecard")
    assert blocked.status_code == 403
    assert "temporary" in blocked.json()["detail"]
    assert api.get("/follow-ups/due").status_code == 403


def test_the_change_endpoint_itself_stays_reachable(
    api, session, temporary_account
):
    """Otherwise the account is simply bricked."""
    token = login(session, temporary_account["phone"], temporary_account["password"])
    api.headers["Authorization"] = f"Bearer {token}"

    assert api.get("/auth/me").status_code == 200
    changed = api.post(
        "/auth/password",
        json={"current_password": TEMPORARY, "new_password": CHOSEN},
    )
    assert changed.status_code == 200


def test_everything_opens_up_once_it_is_changed(api, session, temporary_account):
    token = login(session, temporary_account["phone"], temporary_account["password"])
    api.headers["Authorization"] = f"Bearer {token}"
    api.post(
        "/auth/password",
        json={"current_password": TEMPORARY, "new_password": CHOSEN},
    )
    assert api.get("/metrics/scorecard").status_code == 200


def test_the_flag_clears_on_change(api, session, temporary_account):
    token = login(session, temporary_account["phone"], temporary_account["password"])
    api.headers["Authorization"] = f"Bearer {token}"
    api.post(
        "/auth/password",
        json={"current_password": TEMPORARY, "new_password": CHOSEN},
    )
    still_flagged = session.execute(
        text("SELECT must_change_password FROM staff WHERE staff_id = :s"),
        {"s": temporary_account["staff_id"]},
    ).scalar_one()
    assert still_flagged is False


def test_the_issued_password_stops_working(api, session, temporary_account):
    """The administrator who generated it loses their way in."""
    from app.auth import AuthError

    token = login(session, temporary_account["phone"], temporary_account["password"])
    api.headers["Authorization"] = f"Bearer {token}"
    api.post(
        "/auth/password",
        json={"current_password": TEMPORARY, "new_password": CHOSEN},
    )
    with pytest.raises(AuthError):
        login(session, temporary_account["phone"], TEMPORARY)
    assert login(session, temporary_account["phone"], CHOSEN)


# --- the web UI ------------------------------------------------------------

def test_signing_in_lands_on_the_change_page(api, temporary_account):
    landed = api.post(
        "/ui/login",
        data={
            "phone": temporary_account["phone"],
            "password": temporary_account["password"],
        },
        follow_redirects=True,
    )
    assert "Choose a password" in landed.text


def test_other_pages_redirect_back_to_it(api, temporary_account):
    api.post(
        "/ui/login",
        data={
            "phone": temporary_account["phone"],
            "password": temporary_account["password"],
        },
        follow_redirects=True,
    )
    r = api.get("/ui/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/password"


def test_changing_it_in_the_ui_opens_the_dashboard(api, temporary_account):
    landed = api.post(
        "/ui/login",
        data={
            "phone": temporary_account["phone"],
            "password": temporary_account["password"],
        },
        follow_redirects=True,
    )
    done = api.post(
        "/ui/password",
        data={
            "csrf_token": csrf(landed.text),
            "current_password": TEMPORARY,
            "new_password": CHOSEN,
        },
        follow_redirects=True,
    )
    assert "Password changed" in done.text
    assert "Pilot scorecard" in api.get("/ui/").text


def test_a_wrong_current_password_is_refused(api, temporary_account):
    landed = api.post(
        "/ui/login",
        data={
            "phone": temporary_account["phone"],
            "password": temporary_account["password"],
        },
        follow_redirects=True,
    )
    r = api.post(
        "/ui/password",
        data={
            "csrf_token": csrf(landed.text),
            "current_password": "not it",
            "new_password": CHOSEN,
        },
        follow_redirects=True,
    )
    assert "invalid credentials" in r.text
    assert api.get("/ui/", follow_redirects=False).status_code == 303


def test_reusing_the_same_password_is_refused(api, temporary_account):
    """Otherwise 'changing' it changes nothing."""
    landed = api.post(
        "/ui/login",
        data={
            "phone": temporary_account["phone"],
            "password": temporary_account["password"],
        },
        follow_redirects=True,
    )
    r = api.post(
        "/ui/password",
        data={
            "csrf_token": csrf(landed.text),
            "current_password": TEMPORARY,
            "new_password": TEMPORARY,
        },
        follow_redirects=True,
    )
    assert "must differ" in r.text


def test_a_normal_account_is_unaffected(api, staff_login):
    """Someone whose password is their own goes straight where they asked.

    Uses the cookie session, not the bearer client -- the redirect being
    tested belongs to the web UI.
    """
    api.post(
        "/ui/login",
        data={"phone": staff_login["phone"], "password": staff_login["password"]},
        follow_redirects=True,
    )
    landing = api.get("/ui/", follow_redirects=False)
    assert landing.status_code == 200


# --- employers -------------------------------------------------------------

def test_an_invited_employer_must_replace_their_password(api, session,
                                                         employer_id):
    from app.employer_auth import employer_login, invite_contact

    contact_id = session.execute(
        text(
            "INSERT INTO employer_contacts (employer_id, full_name, phone) "
            "VALUES (:e, 'Invited', '+250788005555') RETURNING contact_id"
        ),
        {"e": employer_id},
    ).scalar_one()
    temporary = invite_contact(session, contact_id)
    assert employer_login(session, "+250788005555", temporary)

    landed = api.post(
        "/employer/login",
        data={"phone": "+250788005555", "password": temporary},
        follow_redirects=True,
    )
    assert "Choose a password" in landed.text

    blocked = api.get("/employer/", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/employer/password"

    done = api.post(
        "/employer/password",
        data={
            "csrf_token": csrf(landed.text),
            "current_password": temporary,
            "new_password": "an-employer-chosen-password",
        },
        follow_redirects=True,
    )
    assert done.status_code == 200
    assert "What we promised you" in api.get("/employer/").text


def test_the_employers_issued_password_stops_working(api, session, employer_id):
    from app.employer_auth import (
        EmployerAuthError,
        employer_login,
        invite_contact,
    )

    contact_id = session.execute(
        text(
            "INSERT INTO employer_contacts (employer_id, full_name, phone) "
            "VALUES (:e, 'Invited Two', '+250788005556') RETURNING contact_id"
        ),
        {"e": employer_id},
    ).scalar_one()
    temporary = invite_contact(session, contact_id)

    landed = api.post(
        "/employer/login",
        data={"phone": "+250788005556", "password": temporary},
        follow_redirects=True,
    )
    api.post(
        "/employer/password",
        data={
            "csrf_token": csrf(landed.text),
            "current_password": temporary,
            "new_password": "another-chosen-password",
        },
        follow_redirects=True,
    )
    with pytest.raises(EmployerAuthError):
        employer_login(session, "+250788005556", temporary)
    assert employer_login(session, "+250788005556", "another-chosen-password")
