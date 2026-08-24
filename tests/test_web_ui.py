"""The admin web UI.

The UI authenticates with a cookie rather than a bearer token, which means CSRF
applies to it in a way it does not to the JSON API. Most of what is tested here
is that boundary, plus the coordinator's actual path through a working day.
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.auth import login
from tests.conftest import totp_now

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = date.today()
SITE = (-1.9550, 30.1150)
NEARBY = (-1.9480, 30.1050)


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the rendered form"
    return match.group(1)


@pytest.fixture
def web(api, staff_login):
    """A browser session: signed in by cookie, second factor presented."""
    r = api.post(
        "/ui/login",
        data={"phone": staff_login["phone"], "password": staff_login["password"]},
        follow_redirects=True,
    )
    assert r.status_code == 200
    page = api.get("/ui/mfa").text
    api.post(
        "/ui/mfa",
        data={
            "csrf_token": csrf(page),
            "code": totp_now(staff_login["totp_secret"], 1),
        },
        follow_redirects=True,
    )
    return api


# --- authentication --------------------------------------------------------

def test_an_unauthenticated_browser_is_redirected_to_sign_in(api):
    r = api.get("/ui/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login?next=/ui/"


def test_bad_credentials_do_not_sign_you_in(api):
    r = api.post(
        "/ui/login", data={"phone": "+250780000000", "password": "wrong"},
        follow_redirects=True,
    )
    assert "Invalid credentials" in r.text
    assert api.get("/ui/", follow_redirects=False).status_code == 303


def test_signing_in_reaches_the_dashboard(web):
    r = web.get("/ui/")
    assert r.status_code == 200
    assert "Pilot scorecard" in r.text


def test_the_session_cookie_is_httponly_and_samesite_strict(api, staff_login):
    """A script must not be able to read the token, and the browser must not
    attach it cross-site."""
    r = api.post(
        "/ui/login",
        data={"phone": staff_login["phone"], "password": staff_login["password"]},
        follow_redirects=False,
    )
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_signing_out_invalidates_the_session(web):
    page = web.get("/ui/").text
    web.post("/ui/logout", data={"csrf_token": csrf(page)}, follow_redirects=True)
    assert web.get("/ui/", follow_redirects=False).status_code == 303


# --- CSRF ------------------------------------------------------------------

def test_a_form_post_without_a_token_is_refused(web):
    r = web.post(
        "/ui/employers",
        data={"business_name": "No Token", "sector": "x", "district": "y"},
    )
    assert r.status_code == 403


def test_a_forged_token_is_refused_and_nothing_is_written(web, session):
    r = web.post(
        "/ui/employers",
        data={
            "csrf_token": "forged",
            "business_name": "Cross Site",
            "sector": "x",
            "district": "y",
        },
    )
    assert r.status_code == 403
    assert session.execute(
        text("SELECT count(*) FROM employers WHERE business_name = 'Cross Site'")
    ).scalar_one() == 0


def test_another_sessions_token_does_not_work(web, session, staff_login):
    """CSRF tokens are bound to the session, not shared across them."""
    mine = session.execute(
        text(
            "SELECT csrf_token FROM staff_sessions WHERE csrf_token IS NOT NULL"
        )
    ).scalars().all()
    other = login(session, staff_login["phone"], staff_login["password"])
    stolen = session.execute(
        text(
            "SELECT csrf_token FROM staff_sessions "
            "WHERE csrf_token IS NOT NULL AND NOT (csrf_token = ANY(:mine))"
        ),
        {"mine": mine},
    ).scalars().first()
    assert stolen and other

    r = web.post(
        "/ui/employers",
        data={
            "csrf_token": stolen,
            "business_name": "Wrong Session",
            "sector": "x",
            "district": "y",
        },
    )
    assert r.status_code == 403
    assert other


# --- the coordinator's day -------------------------------------------------

def test_the_full_loop_through_the_ui(web, session):
    # Register an employer and promote it.
    page = web.get("/ui/employers").text
    web.post(
        "/ui/employers",
        data={
            "csrf_token": csrf(page), "business_name": "Isuku Cooperative",
            "sector": "cleaning", "district": "Gasabo",
            "site_lat": str(SITE[0]), "site_lng": str(SITE[1]),
            "is_cooperative": "true",
        },
        follow_redirects=True,
    )
    page = web.get("/ui/employers").text
    assert "Isuku Cooperative" in page
    employer_id = re.search(r"/ui/employers/([0-9a-f-]{36})/tier", page).group(1)
    web.post(
        f"/ui/employers/{employer_id}/tier",
        data={"csrf_token": csrf(page), "tier": "active"},
        follow_redirects=True,
    )

    # Register a candidate.
    page = web.get("/ui/candidates").text
    assert "legal_first_name" in page, "identity form should be visible with MFA"
    web.post(
        "/ui/candidates",
        data={
            "csrf_token": csrf(page),
            "legal_first_name": "Aline", "legal_last_name": "Uwase",
            "date_of_birth": str(TODAY - timedelta(days=365 * 24)),
            "phone_primary": "+250780009001", "display_name": "Aline U.",
            "district": "Gasabo", "sector": "Remera", "gender": "F",
            "home_lat": str(NEARBY[0]), "home_lng": str(NEARBY[1]),
            "max_commute_rwf": "2000", "consent_captured_via": "paper",
            # A browser sends the selected options of the multi-select; a test
            # client has to say so explicitly.
            "avail_days": ["0", "1", "2", "3", "4"],
            "avail_start": "06:00", "avail_end": "20:00",
        },
        follow_redirects=True,
    )
    assert "Aline U." in web.get("/ui/candidates").text

    # Post a shift.
    page = web.get("/ui/requests").text
    posted = web.post(
        "/ui/requests",
        data={
            "csrf_token": csrf(page), "employer_id": employer_id,
            "title": "Morning cleaner", "work_type": "shift", "headcount": "1",
            "starts_on": str(TODAY), "shift_start": "08:00", "shift_end": "16:00",
            "pay_rwf": "5000", "pay_unit": "day", "transport_covered": "false",
        },
        follow_redirects=True,
    )
    assert "Matched" in posted.text
    request_id = re.search(r"/ui/requests/([0-9a-f-]{36})", str(posted.url)).group(1)

    # The match screen shows a defensible reason.
    matches = web.get(f"/ui/requests/{request_id}").text
    assert "matched on:" in matches

    # Offer it.
    offered = web.post(
        f"/ui/requests/{request_id}/offer",
        data={
            "csrf_token": csrf(matches),
            "candidate_id": re.search(
                r'name="candidate_id" value="([0-9a-f-]{36})"', matches
            ).group(1),
        },
        follow_redirects=True,
    )
    placement_id = re.search(
        r"/ui/placements/([0-9a-f-]{36})", str(offered.url)
    ).group(1)

    # Accept, start, then the worker fails to arrive.
    page = web.get(f"/ui/placements/{placement_id}").text
    web.post(
        f"/ui/placements/{placement_id}/respond",
        data={"csrf_token": csrf(page), "accepted": "true"}, follow_redirects=True,
    )
    page = web.get(f"/ui/placements/{placement_id}").text
    web.post(
        f"/ui/placements/{placement_id}/start",
        data={"csrf_token": csrf(page), "started_on": str(TODAY)},
        follow_redirects=True,
    )
    page = web.get(f"/ui/placements/{placement_id}").text
    absent = web.post(
        f"/ui/placements/{placement_id}/attendance",
        data={
            "csrf_token": csrf(page), "work_date": str(TODAY),
            "present": "false", "confirmed_by": "employer",
            "absence_reason": "did not arrive",
        },
        follow_redirects=True,
    )
    assert "cover this shift" in absent.text.lower()

    # The dashboard leads with the running clock.
    assert "Guarantee clock running" in web.get("/ui/").text


def test_an_absence_without_a_reason_is_refused_in_the_ui(web, make_placement):
    from app.operations.attendance import start_placement

    pid = make_placement()
    page = web.get("/ui/").text
    web.post(
        f"/ui/placements/{pid}/respond",
        data={"csrf_token": csrf(page), "accepted": "true"}, follow_redirects=True,
    )
    start_placement.__doc__  # keep the import meaningful
    page = web.get(f"/ui/placements/{pid}").text
    web.post(
        f"/ui/placements/{pid}/start",
        data={"csrf_token": csrf(page), "started_on": str(TODAY)},
        follow_redirects=True,
    )
    page = web.get(f"/ui/placements/{pid}").text
    r = web.post(
        f"/ui/placements/{pid}/attendance",
        data={
            "csrf_token": csrf(page), "work_date": str(TODAY),
            "present": "false", "confirmed_by": "employer",
        },
        follow_redirects=True,
    )
    assert "needs a reason" in r.text


def test_the_candidate_list_shows_no_identity_data(web, session, make_candidate):
    """Legal names live behind the identity endpoints, never on a list screen."""
    cid = make_candidate(name="Screen Test")
    identity = session.execute(
        text(
            "SELECT legal_first_name, legal_last_name, phone_primary "
            "FROM candidate_identity WHERE candidate_id = :cid"
        ),
        {"cid": cid},
    ).mappings().one()

    listing = web.get("/ui/candidates").text
    # The table itself, not the registration form above it.
    table = listing.split("Registry (")[1]
    assert "Screen Test" in table

    # The phone number is the unambiguous identifier here: the fixture's legal
    # names are common words that legitimately appear in display names.
    assert identity["phone_primary"] not in table
    full_legal = f"{identity['legal_first_name']} {identity['legal_last_name']}"
    assert full_legal not in table


def test_registration_without_a_second_factor_is_refused(api, session, staff_login):
    """The form is hidden without MFA; posting anyway must still be refused."""
    api.post(
        "/ui/login",
        data={"phone": staff_login["phone"], "password": staff_login["password"]},
        follow_redirects=True,
    )
    page = api.get("/ui/mfa").text  # signed in, but not elevated
    r = api.post(
        "/ui/candidates",
        data={
            "csrf_token": csrf(page),
            "legal_first_name": "Sneaky", "legal_last_name": "Post",
            "date_of_birth": "2000-01-01", "phone_primary": "+250780009999",
            "display_name": "Sneaky", "district": "Gasabo", "sector": "Remera",
            "consent_captured_via": "paper",
        },
        follow_redirects=True,
    )
    assert "second factor" in r.text
    assert session.execute(
        text("SELECT count(*) FROM candidates WHERE display_name = 'Sneaky'")
    ).scalar_one() == 0


def test_no_transport_estimate_is_not_shown_as_free(web, session, staff_login):
    """A missing estimate and a walkable commute are opposite facts.

    A candidate with no home location on file passes the transport filter by
    default. Rendering that as "—", the same as a zero fare, hides the one
    thing the coordinator needs to know before offering.
    """
    from app.operations.registry import register_employer
    from app.operations.requests import create_work_request

    employer_id = register_employer(
        session, business_name="Sited Employer", sector="retail",
        district="Gasabo", account_owner=staff_login["staff_id"],
        site_lat=SITE[0], site_lng=SITE[1],
    )
    request_id = create_work_request(
        session, employer_id=employer_id, title="Shop assistant",
        work_type="shift", headcount=1, starts_on=TODAY, pay_rwf=5000,
        pay_unit="day",
    )

    page = web.get("/ui/candidates").text
    for name, lat, lng in [
        ("Walker", str(SITE[0] + 0.001), str(SITE[1] + 0.001)),  # walkable
        ("Unknown", "", ""),                                      # no location
    ]:
        web.post(
            "/ui/candidates",
            data={
                "csrf_token": csrf(page),
                "legal_first_name": name, "legal_last_name": "Test",
                "date_of_birth": str(TODAY - timedelta(days=365 * 25)),
                "phone_primary": f"+25078801{abs(hash(name)) % 1000:03d}",
                "display_name": name, "district": "Gasabo", "sector": "Remera",
                "home_lat": lat, "home_lng": lng,
                "consent_captured_via": "paper",
                "avail_days": ["0", "1", "2", "3", "4", "5", "6"],
                "avail_start": "06:00", "avail_end": "20:00",
            },
            follow_redirects=True,
        )
        page = web.get("/ui/candidates").text

    matches = web.get(f"/ui/requests/{request_id}").text
    assert "walkable" in matches
    assert "no estimate" in matches
    assert "home location missing" in matches


# --- open redirect ---------------------------------------------------------

def test_next_cannot_send_the_browser_off_site(api, staff_login):
    """An attacker-supplied ?next= must not survive login.

    A coordinator bounced to another origin immediately after signing in is
    exactly the setup for a convincing fake login page — they have just typed
    their password, so a second prompt does not look strange.
    """
    for hostile in [
        "https://evil.example.com/steal",
        "//evil.example.com/steal",
        "/\\evil.example.com",
    ]:
        r = api.post(
            "/ui/login",
            data={
                "phone": staff_login["phone"],
                "password": staff_login["password"],
                "next": hostile,
            },
            follow_redirects=False,
        )
        location = r.headers["location"]
        assert "evil.example.com" not in location, hostile
        assert location.startswith("/ui/"), location


def test_a_local_next_still_works(api, staff_login):
    r = api.post(
        "/ui/login",
        data={
            "phone": staff_login["phone"],
            "password": staff_login["password"],
            "next": "/ui/employers",
        },
        follow_redirects=False,
    )
    assert "/ui/employers" in r.headers["location"]


def test_completing_a_checkin_ignores_the_referer_header(
    web, session, make_placement
):
    """The redirect target comes from the record, not from Referer.

    Referer is set by whatever page submitted the form, so trusting it made
    this an open redirect.
    """
    from app.operations.attendance import start_placement

    pid = make_placement()
    page = web.get("/ui/").text
    web.post(
        f"/ui/placements/{pid}/respond",
        data={"csrf_token": csrf(page), "accepted": "true"}, follow_redirects=True,
    )
    start_placement(session, pid, TODAY - timedelta(days=40))

    follow_up_id = session.execute(
        text(
            "SELECT follow_up_id FROM follow_ups "
            "WHERE placement_id = :pid AND checkpoint = 'day_1'"
        ),
        {"pid": pid},
    ).scalar_one()

    page = web.get("/ui/").text
    r = web.post(
        f"/ui/follow-ups/{follow_up_id}/complete",
        data={"csrf_token": csrf(page), "still_working": "true"},
        headers={"Referer": "https://evil.example.com/phish"},
        follow_redirects=False,
    )
    assert "evil.example.com" not in r.headers["location"]
    assert str(pid) in r.headers["location"]
