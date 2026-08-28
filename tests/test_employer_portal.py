"""The employer dashboard.

The critical tests here are the isolation ones. An employer session must see
exactly one employer's data and no candidate identity at all — a mistake in
either direction is a data-protection incident, not a bug report.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import kigali_today
from app.employer_auth import (
    EmployerAuthError,
    authenticate_employer,
    employer_login,
    invite_contact,
)
from app.operations.attendance import start_placement
from app.operations.employer_portal import (
    EmployerPortalError,
    assigned_workers,
    confirm_attendance,
    my_requests,
    rate_worker,
    reorder,
)

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the rendered form"
    return match.group(1)


@pytest.fixture
def employer_account(session, employer_id):
    """An employer contact with a login."""
    contact_id = session.execute(
        text(
            """
            INSERT INTO employer_contacts (employer_id, full_name, phone,
                                           is_primary)
            VALUES (:eid, 'Chantal', '+250788000101', true)
            RETURNING contact_id
            """
        ),
        {"eid": employer_id},
    ).scalar_one()
    password = invite_contact(session, contact_id)
    return {
        "contact_id": contact_id,
        "employer_id": employer_id,
        "phone": "+250788000101",
        "password": password,
    }


@pytest.fixture
def rival(session, staff_id):
    """A second employer, with their own contact, request and worker."""
    from app.operations.registry import register_employer

    employer_id = register_employer(
        session, business_name="Rival Retail", sector="retail",
        district="Kicukiro", account_owner=staff_id,
    )
    contact_id = session.execute(
        text(
            """
            INSERT INTO employer_contacts (employer_id, full_name, phone)
            VALUES (:eid, 'Rival Contact', '+250788000202')
            RETURNING contact_id
            """
        ),
        {"eid": employer_id},
    ).scalar_one()
    password = invite_contact(session, contact_id)
    request_id = session.execute(
        text(
            """
            INSERT INTO work_requests (employer_id, title, work_type, headcount,
                                       starts_on, pay_rwf, pay_unit)
            VALUES (:eid, 'Rival shift', 'shift', 1, kigali_today(), 6000, 'day')
            RETURNING request_id
            """
        ),
        {"eid": employer_id},
    ).scalar_one()
    return {
        "employer_id": employer_id, "contact_id": contact_id,
        "phone": "+250788000202", "password": password,
        "request_id": request_id,
    }


REPLACEMENT_PASSWORD = "an-employer-chosen-password"


@pytest.fixture
def portal(api, employer_account):
    """A signed-in employer who has replaced their invited password.

    The invite is temporary and enforced as such, so this walks the real first
    sign-in: log in, land on the change page, choose a password.
    """
    landed = api.post(
        "/employer/login",
        data={
            "phone": employer_account["phone"],
            "password": employer_account["password"],
        },
        follow_redirects=True,
    )
    assert landed.status_code == 200
    assert "Choose a password" in landed.text, "a temporary password must be forced"

    changed = api.post(
        "/employer/password",
        data={
            "csrf_token": csrf(landed.text),
            "current_password": employer_account["password"],
            "new_password": REPLACEMENT_PASSWORD,
        },
        follow_redirects=True,
    )
    assert changed.status_code == 200
    employer_account["password"] = REPLACEMENT_PASSWORD
    return api


# --- isolation: the tests that matter --------------------------------------

def test_an_employer_sees_their_own_workers_and_only_those(
    session, employer_account, rival, make_request, make_placement,
    make_candidate
):
    """Both halves, deliberately.

    Asserting only that the rival's worker is absent passes if the query is
    broken and returns nothing for everybody -- which is the same result a
    security test wants to see and a completely useless system produces. So
    each employer gets a worker and each must see exactly one.
    """
    ours = make_placement(request_id=make_request(),
                          candidate_id=make_candidate())
    theirs = make_placement(request_id=rival["request_id"],
                            candidate_id=make_candidate())

    mine = assigned_workers(session, employer_account["employer_id"])
    assert [w["placement_id"] for w in mine] == [ours]

    yours = assigned_workers(session, rival["employer_id"])
    assert [w["placement_id"] for w in yours] == [theirs]


def test_an_employer_sees_their_own_requests_and_only_those(
    session, employer_account, rival, make_request
):
    """Same reasoning: a query returning nothing would pass the negative
    half on its own."""
    make_request()

    mine = my_requests(session, employer_account["employer_id"])
    assert mine, "the employer cannot see their own request either"
    assert "Rival shift" not in [r["title"] for r in mine]

    yours = my_requests(session, rival["employer_id"])
    assert [r["title"] for r in yours] == ["Rival shift"]


def test_confirming_attendance_on_someone_elses_placement_is_refused(
    session, employer_account, rival, make_placement, make_candidate
):
    """A placement id in a URL proves nothing. Ownership is rechecked."""
    theirs = make_placement(
        request_id=rival["request_id"], candidate_id=make_candidate()
    )
    with pytest.raises(EmployerPortalError, match="no such placement"):
        confirm_attendance(
            session, employer_account["employer_id"],
            employer_account["contact_id"], theirs, TODAY, True,
        )


def test_rating_someone_elses_worker_is_refused(
    session, employer_account, rival, make_placement, make_candidate
):
    theirs = make_placement(
        request_id=rival["request_id"], candidate_id=make_candidate()
    )
    with pytest.raises(EmployerPortalError, match="no such placement"):
        rate_worker(session, employer_account["employer_id"], theirs, 5)


def test_reordering_someone_elses_request_is_refused(
    session, employer_account, rival
):
    with pytest.raises(EmployerPortalError, match="no such work request"):
        reorder(
            session, employer_account["employer_id"], rival["request_id"], TODAY
        )


def test_the_refusal_does_not_reveal_that_the_record_exists(
    session, employer_account, rival, make_placement, make_candidate
):
    """Same message for 'not yours' and 'does not exist'."""
    import uuid

    theirs = make_placement(
        request_id=rival["request_id"], candidate_id=make_candidate()
    )
    with pytest.raises(EmployerPortalError) as real:
        rate_worker(session, employer_account["employer_id"], theirs, 5)
    with pytest.raises(EmployerPortalError) as fake:
        rate_worker(session, employer_account["employer_id"], uuid.uuid4(), 5)
    assert str(real.value) == str(fake.value)


def test_no_candidate_identity_reaches_the_employer(
    session, employer_account, make_placement, make_candidate, make_request
):
    """Display name only. Legal names and phone numbers never leave the API."""
    cid = make_candidate(name="Aline U.")
    make_placement(candidate_id=cid, request_id=make_request())
    identity = session.execute(
        text(
            "SELECT legal_first_name, legal_last_name, phone_primary, "
            "       date_of_birth FROM candidate_identity WHERE candidate_id = :c"
        ),
        {"c": cid},
    ).mappings().one()

    rows = assigned_workers(session, employer_account["employer_id"])
    assert rows and rows[0]["display_name"] == "Aline U."
    blob = str(rows)
    for value in identity.values():
        assert str(value) not in blob


def test_an_employer_session_is_not_a_staff_session(
    api, employer_account, session
):
    """The two principals live in separate tables; neither token resolves as the other."""
    token = employer_login(
        session, employer_account["phone"], employer_account["password"]
    )
    from app.auth import AuthError, authenticate

    with pytest.raises(AuthError):
        authenticate(session, token)

    api.headers["Authorization"] = f"Bearer {token}"
    assert api.get("/metrics/scorecard").status_code == 401


def test_a_staff_token_does_not_open_the_employer_portal(session, staff_login):
    from app.auth import login

    with pytest.raises(EmployerAuthError):
        authenticate_employer(
            session, login(session, staff_login["phone"], staff_login["password"])
        )


def test_suspending_an_employer_cuts_their_contacts_access(
    session, employer_account
):
    token = employer_login(
        session, employer_account["phone"], employer_account["password"]
    )
    assert authenticate_employer(session, token)

    session.execute(
        text("UPDATE employers SET tier = 'suspended' WHERE employer_id = :eid"),
        {"eid": employer_account["employer_id"]},
    )
    with pytest.raises(EmployerAuthError):
        authenticate_employer(session, token)


def test_a_contact_without_a_password_cannot_sign_in(session, employer_id):
    session.execute(
        text(
            "INSERT INTO employer_contacts (employer_id, full_name, phone) "
            "VALUES (:eid, 'No Login', '+250788000303')"
        ),
        {"eid": employer_id},
    )
    with pytest.raises(EmployerAuthError):
        employer_login(session, "+250788000303", "anything")


# --- the employer's day ----------------------------------------------------

def test_confirming_a_no_show_invokes_the_guarantee(
    session, employer_account, make_placement, make_request, make_candidate
):
    """The employer's own word is what starts the clock."""
    pid = make_placement(request_id=make_request(), candidate_id=make_candidate())
    start_placement(session, pid, TODAY)

    invocation = confirm_attendance(
        session, employer_account["employer_id"], employer_account["contact_id"],
        pid, TODAY, present=False, absence_reason="never arrived",
    )
    assert invocation is not None

    contact = session.execute(
        text(
            "SELECT confirmed_by_contact FROM attendance WHERE placement_id = :p"
        ),
        {"p": pid},
    ).scalar_one()
    assert contact == employer_account["contact_id"]


def test_a_worker_cannot_be_rated_before_they_have_worked(
    session, employer_account, make_placement, make_request, make_candidate
):
    pid = make_placement(request_id=make_request(), candidate_id=make_candidate())
    with pytest.raises(EmployerPortalError, match="actually worked"):
        rate_worker(session, employer_account["employer_id"], pid, 5)


def test_reorder_copies_the_terms_onto_a_new_date(
    session, employer_account, make_request
):
    original = make_request(pay_rwf=7000)
    new_id = reorder(
        session, employer_account["employer_id"], original,
        TODAY + timedelta(days=7),
    )
    row = session.execute(
        text(
            "SELECT pay_rwf, starts_on, title FROM work_requests "
            "WHERE request_id = :rid"
        ),
        {"rid": new_id},
    ).mappings().one()
    assert row["pay_rwf"] == 7000
    assert row["starts_on"] == TODAY + timedelta(days=7)


# --- over HTTP -------------------------------------------------------------

def test_the_portal_requires_a_session(api):
    r = api.get("/employer/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/employer/login"


def test_the_employer_cookie_is_httponly_and_strict(api, employer_account):
    r = api.post(
        "/employer/login",
        data={
            "phone": employer_account["phone"],
            "password": employer_account["password"],
        },
        follow_redirects=False,
    )
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_a_forged_csrf_token_is_refused(portal):
    r = portal.post(
        "/employer/post",
        data={
            "csrf_token": "forged", "title": "X", "work_type": "shift",
            "headcount": "1", "starts_on": str(TODAY), "pay_rwf": "5000",
            "pay_unit": "day",
        },
    )
    assert r.status_code == 403


def test_posting_a_shift_uses_the_session_not_the_form(
    portal, session, employer_account, rival
):
    """An employer must not be able to post work in someone else's name."""
    page = portal.get("/employer/post").text
    portal.post(
        "/employer/post",
        data={
            "csrf_token": csrf(page), "title": "Injected shift",
            "work_type": "shift", "headcount": "1", "starts_on": str(TODAY),
            "pay_rwf": "5000", "pay_unit": "day",
            # Ignored: employer_id comes from the session.
            "employer_id": str(rival["employer_id"]),
        },
        follow_redirects=True,
    )
    owner = session.execute(
        text("SELECT employer_id FROM work_requests WHERE title = 'Injected shift'")
    ).scalar_one()
    assert owner == employer_account["employer_id"]


def test_the_dashboard_reports_the_guarantee_back_to_the_employer(
    portal, session, employer_account, make_placement, make_request,
    make_candidate,
):
    pid = make_placement(request_id=make_request(), candidate_id=make_candidate())
    start_placement(session, pid, TODAY)
    confirm_attendance(
        session, employer_account["employer_id"], employer_account["contact_id"],
        pid, TODAY, present=False, absence_reason="no-show",
    )
    page = portal.get("/employer/").text
    assert "What we promised you" in page
    assert "No-shows" in page


def test_an_employer_cannot_open_another_employers_worker_page(
    portal, session, rival, make_placement, make_candidate
):
    theirs = make_placement(
        request_id=rival["request_id"], candidate_id=make_candidate()
    )
    r = portal.get(f"/employer/workers/{theirs}", follow_redirects=True)
    assert "No such worker" in r.text


# --- the isolation boundary, attacked over HTTP ---------------------------
#
# The tests above check the operations layer, which is where the ownership
# rules live. These go through the routes instead, with a signed-in employer
# putting somebody else's identifiers in the URL -- the way it would actually
# be attempted. A rule enforced in the operation but bypassed by a route that
# forgets to call it would pass every test above.

CROSS_TENANT_ATTEMPTS = [
    ("read their worker", "get", "/employer/workers/{placement}", None),
    ("mark their worker absent", "post", "/employer/workers/{placement}/attendance",
     {"work_date": "2026-08-28", "present": "false", "absence_reason": "injected"}),
    ("rate their worker", "post", "/employer/workers/{placement}/rate",
     {"rating": "1", "note": "injected"}),
    ("cancel their shift", "post", "/employer/requests/{request}/cancel",
     {"reason": "injected"}),
    ("reorder their shift", "post", "/employer/requests/{request}/reorder",
     {"starts_on": "2026-09-30"}),
]


@pytest.fixture
def rivals_placement(session, rival, make_placement, make_candidate):
    return make_placement(request_id=rival["request_id"],
                          candidate_id=make_candidate())


@pytest.mark.parametrize("label,verb,path,payload", CROSS_TENANT_ATTEMPTS,
                         ids=[a[0] for a in CROSS_TENANT_ATTEMPTS])
def test_an_employer_cannot_reach_another_over_http(
    portal, session, rival, rivals_placement, label, verb, path, payload
):
    target = path.format(placement=rivals_placement, request=rival["request_id"])

    if payload is None:
        response = portal.get(target, follow_redirects=True)
    else:
        home = portal.get("/employer/", follow_redirects=True)
        response = portal.post(
            target, data={**payload, "csrf_token": csrf(home.text)},
            follow_redirects=True,
        )

    assert "Rival Retail" not in response.text, f"{label} leaked the employer"
    assert "injected" not in response.text, f"{label} echoed the payload back"


def test_no_attempt_changed_the_targets_data(portal, session, rival,
                                             rivals_placement):
    """The response saying no is not the same as nothing having happened."""
    home = portal.get("/employer/", follow_redirects=True)
    token = csrf(home.text)

    portal.post(f"/employer/workers/{rivals_placement}/attendance",
                data={"csrf_token": token, "work_date": "2026-08-28",
                      "present": "false", "absence_reason": "injected"},
                follow_redirects=True)
    portal.post(f"/employer/workers/{rivals_placement}/rate",
                data={"csrf_token": token, "rating": "1", "note": "injected"},
                follow_redirects=True)
    portal.post(f"/employer/requests/{rival['request_id']}/cancel",
                data={"csrf_token": token, "reason": "injected"},
                follow_redirects=True)

    assert session.execute(
        text("SELECT count(*) FROM attendance WHERE absence_reason = 'injected'")
    ).scalar_one() == 0
    assert session.execute(
        text("SELECT count(*) FROM placements WHERE employer_note = 'injected'")
    ).scalar_one() == 0
    assert session.execute(
        text("SELECT status::text FROM work_requests WHERE request_id = :r"),
        {"r": str(rival["request_id"])},
    ).scalar_one() != "cancelled"


def test_the_same_calls_work_on_their_own_data(portal, session,
                                               employer_account, make_request,
                                               make_placement, make_candidate):
    """The other half. Every assertion above is satisfied by a portal that
    refuses everything, so this proves the calls are real."""
    mine = make_placement(request_id=make_request(),
                          candidate_id=make_candidate())
    session.execute(
        text("UPDATE placements SET status = 'active', started_on = kigali_today() "
             "WHERE placement_id = :p"),
        {"p": str(mine)},
    )
    home = portal.get("/employer/", follow_redirects=True)
    confirmed = portal.post(
        f"/employer/workers/{mine}/attendance",
        data={"csrf_token": csrf(home.text), "work_date": str(kigali_today()),
              "present": "true"},
        follow_redirects=True,
    )
    assert confirmed.status_code == 200
    assert session.execute(
        text("SELECT count(*) FROM attendance WHERE placement_id = :p"),
        {"p": str(mine)},
    ).scalar_one() == 1
