"""The whole operation, end to end, through the HTTP API.

One coordinator's working day: register a cooperative, register candidates,
post a shift, look at who matches and why, offer the work, the worker accepts,
starts, fails to arrive, gets covered inside the window, and the scorecard
reflects all of it.

If this test passes, the system does the job it was built for.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from sqlalchemy import text

os.environ.setdefault("DATA_RESIDENCY", "local_dev")

# A Monday, so availability windows are predictable.
MONDAY = date(2026, 9, 7)

# Two points in Kigali about 2 km apart, and one much further out.
SITE = (-1.9550, 30.1150)
NEARBY = (-1.9480, 30.1050)
FAR = (-1.8900, 29.9800)


def register_candidate(client, name, home, **overrides):
    body = {
        "legal_first_name": name,
        "legal_last_name": "Test",
        "date_of_birth": str(date.today() - timedelta(days=365 * 24)),
        "phone_primary": f"+2507{abs(hash(name)) % 100_000_000:08d}",
        "display_name": name,
        "district": "Gasabo",
        "sector": "Remera",
        "gender": "F",
        "home_lat": home[0],
        "home_lng": home[1],
        "max_commute_rwf": 2000,
        "consent_captured_via": "paper",
        "availability": [
            {"day_of_week": 0, "start": "06:00:00", "end": "20:00:00"}
        ],
    }
    body.update(overrides)
    r = client.post("/candidates", json=body)
    assert r.status_code == 201, r.text
    return r.json()["candidate_id"]


def test_a_coordinators_working_day(client, session):
    # --- register the employer -------------------------------------------
    employer = client.post(
        "/employers",
        json={
            "business_name": "Isuku Cooperative",
            "sector": "cleaning",
            "district": "Gasabo",
            "site_lat": SITE[0],
            "site_lng": SITE[1],
            "is_cooperative": True,
        },
    )
    assert employer.status_code == 201
    employer_id = employer.json()["employer_id"]

    client.post(
        f"/employers/{employer_id}/contacts",
        json={"full_name": "Chantal", "phone": "+250788000001", "is_primary": True},
    )

    # A new employer is a prospect. They only count as active once someone
    # decides the relationship is real.
    assert client.get("/employers").json()["employers"][0]["tier"] == "prospect"
    client.patch(
        f"/employers/{employer_id}",
        json={"tier": "active", "safety_verified": True},
    )

    # --- register candidates ---------------------------------------------
    near = register_candidate(client, "Aline", NEARBY)
    register_candidate(client, "Beatrice", FAR)

    # --- post the shift ---------------------------------------------------
    request = client.post(
        "/work-requests",
        json={
            "employer_id": employer_id,
            "title": "Morning cleaner",
            "work_type": "shift",
            "headcount": 1,
            "starts_on": str(MONDAY),
            "shift_start": "08:00:00",
            "shift_end": "16:00:00",
            "pay_rwf": 5000,
            "pay_unit": "day",
        },
    )
    assert request.status_code == 201
    request_id = request.json()["request_id"]

    assert client.get("/work-requests").json()["requests"][0]["status"] == "open"

    # --- who matches, and why --------------------------------------------
    matched = client.get(f"/work-requests/{request_id}/matches").json()
    names = [m["display_name"] for m in matched["matches"]]
    assert "Aline" in names

    # The far candidate is filtered on transport, not silently dropped.
    rejected = {r["display_name"]: r for r in matched["rejections"]}
    assert "Beatrice" in rejected
    assert rejected["Beatrice"]["filter"] == "transport_viability"

    aline = next(m for m in matched["matches"] if m["display_name"] == "Aline")
    assert aline["reason"].startswith("matched on: ")
    assert "commute" in aline["reason"]
    assert aline["est_transport_rwf"] > 0

    # --- offer, and the candidate accepts ---------------------------------
    offer = client.post(
        f"/work-requests/{request_id}/offers", json={"candidate_id": near}
    )
    assert offer.status_code == 201
    placement_id = offer.json()["placement_id"]

    # Headcount of 1 is now spoken for, so it leaves the open queue.
    assert client.get("/work-requests").json()["requests"] == []
    filled = client.get("/work-requests", params={"status": "filled"}).json()
    assert filled["requests"][0]["request_id"] == request_id

    # The reason is stored on the placement, not recomputed later.
    stored = session.execute(
        text("SELECT match_reason FROM placements WHERE placement_id = :pid"),
        {"pid": placement_id},
    ).scalar_one()
    assert stored == aline["reason"]

    assert client.post(
        f"/placements/{placement_id}/response", json={"accepted": True}
    ).status_code == 200

    # --- the shift starts, and the worker does not arrive -----------------
    started = client.post(
        f"/placements/{placement_id}/start", json={"started_on": str(MONDAY)}
    )
    assert started.status_code == 200

    absent = client.post(
        f"/placements/{placement_id}/attendance",
        json={
            "work_date": str(MONDAY),
            "present": False,
            "confirmed_by": "employer",
            "absence_reason": "did not arrive",
        },
    ).json()
    assert absent["guarantee_invoked"] is True

    open_now = client.get("/guarantees/open").json()["open"]
    assert len(open_now) == 1

    # --- cover it ---------------------------------------------------------
    cover = register_candidate(client, "Claudine", NEARBY)
    replacement = client.post(
        f"/placements/{placement_id}/replacement",
        json={"candidate_id": cover, "match_reason": "matched on: availability"},
    )
    assert replacement.status_code == 201
    assert client.get("/guarantees/open").json()["open"] == []

    # --- the scorecard reflects the day -----------------------------------
    card = client.get("/metrics/scorecard").json()
    assert card["active_employers"] == 1
    assert card["active_cooperatives"] == 1
    assert card["guarantee_invocations"] == 1
    assert float(card["guarantee_filled_24h_pct"]) == 100.0
    assert float(card["avg_days_to_fill"]) < 1


def test_an_offer_is_revalidated_at_the_moment_it_is_made(client, session):
    """A stale match list must not be able to place someone who now fails a filter."""
    employer_id = client.post(
        "/employers",
        json={
            "business_name": "Cafe", "sector": "hospitality", "district": "Gasabo",
            "site_lat": SITE[0], "site_lng": SITE[1],
        },
    ).json()["employer_id"]

    candidate = register_candidate(client, "Diane", NEARBY)
    request_id = client.post(
        "/work-requests",
        json={
            "employer_id": employer_id, "title": "Server", "work_type": "shift",
            "headcount": 1, "starts_on": str(MONDAY), "pay_rwf": 5000,
            "pay_unit": "day", "shift_start": "08:00:00", "shift_end": "16:00:00",
        },
    ).json()["request_id"]

    assert client.get(f"/work-requests/{request_id}/matches").json()["matches"]

    # The candidate withdraws consent after the coordinator loaded the list.
    client.post(
        f"/candidates/{candidate}/consent",
        json={"purpose": "placement", "granted": False, "captured_via": "whatsapp"},
    )

    blocked = client.post(
        f"/work-requests/{request_id}/offers", json={"candidate_id": candidate}
    )
    assert blocked.status_code == 409
    assert "consent" in blocked.json()["detail"]


def test_registration_requires_the_identity_grant(api, session, staff_login):
    """Writing a national ID is gated exactly like reading one."""
    from app.auth import login

    session.execute(
        text("UPDATE staff SET can_view_identity = false WHERE staff_id = :sid"),
        {"sid": staff_login["staff_id"]},
    )
    token = login(session, staff_login["phone"], staff_login["password"])
    api.headers["Authorization"] = f"Bearer {token}"

    r = api.post(
        "/candidates",
        json={
            "legal_first_name": "X", "legal_last_name": "Y",
            "date_of_birth": "2000-01-01", "phone_primary": "+250780000009",
            "display_name": "X", "district": "Gasabo", "sector": "Remera",
            "consent_captured_via": "paper",
        },
    )
    assert r.status_code == 403


def test_an_under_16_registration_is_refused(client):
    r = client.post(
        "/candidates",
        json={
            "legal_first_name": "Too", "legal_last_name": "Young",
            "date_of_birth": str(date.today() - timedelta(days=365 * 15)),
            "phone_primary": "+250780000010", "display_name": "Too Young",
            "district": "Gasabo", "sector": "Remera",
            "consent_captured_via": "paper",
        },
    )
    assert r.status_code == 422
    assert "minimum working age" in r.json()["detail"]


def test_consent_is_captured_at_intake(client, session):
    cid = register_candidate(client, "Esperance", NEARBY)
    row = session.execute(
        text(
            "SELECT policy_version, purpose, granted, captured_via "
            "FROM consent_records WHERE candidate_id = :cid"
        ),
        {"cid": cid},
    ).mappings().one()
    assert row["granted"] is True
    assert row["purpose"] == "placement"
    assert row["policy_version"] == "v1.0"


def test_the_candidate_list_exposes_no_identity_data(client):
    register_candidate(client, "Francine", NEARBY)
    listed = client.get("/candidates").json()["candidates"]
    assert listed
    for row in listed:
        assert set(row) == {
            "candidate_id", "display_name", "gender", "district", "sector",
            "status", "placement_consent",
        }


def test_declining_an_offer_reopens_the_request(client):
    employer_id = client.post(
        "/employers",
        json={
            "business_name": "Shop", "sector": "retail", "district": "Gasabo",
            "site_lat": SITE[0], "site_lng": SITE[1],
        },
    ).json()["employer_id"]
    candidate = register_candidate(client, "Grace", NEARBY)
    request_id = client.post(
        "/work-requests",
        json={
            "employer_id": employer_id, "title": "Shop assistant",
            "work_type": "shift", "headcount": 1, "starts_on": str(MONDAY),
            "pay_rwf": 5000, "pay_unit": "day",
            "shift_start": "08:00:00", "shift_end": "16:00:00",
        },
    ).json()["request_id"]

    placement_id = client.post(
        f"/work-requests/{request_id}/offers", json={"candidate_id": candidate}
    ).json()["placement_id"]

    client.post(f"/placements/{placement_id}/response", json={"accepted": False})

    still_open = [
        r for r in client.get("/work-requests").json()["requests"]
        if r["request_id"] == request_id
    ]
    assert still_open, "a declined offer must free the slot again"
    assert still_open[0]["status"] == "open"
