"""End-to-end coordinator flow over HTTP.

One test walking the path that matters: a worker fails to arrive, the guarantee
fires with a deadline, a replacement covers it, and the scorecard reflects both.
"""

from __future__ import annotations
from app.clock import kigali_today

import os


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


def test_no_show_to_covered_shift(client, session, make_placement, make_candidate):
    pid = make_placement()

    started = client.post(
        f"/placements/{pid}/start", json={"started_on": TODAY.isoformat()}
    )
    assert started.status_code == 200
    assert sorted(started.json()["follow_ups"]) == [
        "day_1", "day_30", "day_90", "week_1"
    ]

    # The worker does not arrive.
    absent = client.post(
        f"/placements/{pid}/attendance",
        json={
            "work_date": TODAY.isoformat(),
            "present": False,
            "confirmed_by": "employer",
            "absence_reason": "did not arrive",
        },
    )
    assert absent.status_code == 200
    body = absent.json()
    assert body["guarantee_invoked"] is True
    assert body["fill_by"] > body["invoked_at"]

    # It shows up on the coordinator's open-guarantee list.
    open_list = client.get("/guarantees/open").json()["open"]
    assert len(open_list) == 1
    assert open_list[0]["breached"] is False

    # Cover it.
    cover = make_candidate(name="Cover Worker")
    replacement = client.post(
        f"/placements/{pid}/replacement",
        json={
            "candidate_id": str(cover),
            "match_reason": "matched on: availability, 8-min commute",
        },
    )
    assert replacement.status_code == 201
    assert replacement.json()["replaces"] == str(pid)

    assert client.get("/guarantees/open").json()["open"] == []

    card = client.get("/metrics/scorecard").json()
    assert card["guarantee_invocations"] == 1
    assert float(card["guarantee_filled_24h_pct"]) == 100.0


def test_an_absence_without_a_reason_is_rejected(client, make_placement):
    pid = make_placement()
    client.post(f"/placements/{pid}/start", json={"started_on": TODAY.isoformat()})
    r = client.post(
        f"/placements/{pid}/attendance",
        json={
            "work_date": TODAY.isoformat(),
            "present": False,
            "confirmed_by": "employer",
        },
    )
    assert r.status_code == 422
    assert "needs a reason" in r.json()["detail"]


def test_replacing_a_placement_that_did_not_fail_is_a_conflict(
    client, make_placement, make_candidate
):
    pid = make_placement()
    client.post(f"/placements/{pid}/start", json={"started_on": TODAY.isoformat()})
    r = client.post(
        f"/placements/{pid}/replacement",
        json={"candidate_id": str(make_candidate()), "match_reason": "x"},
    )
    assert r.status_code == 409


def test_confirmed_by_is_constrained(client, make_placement):
    pid = make_placement()
    client.post(f"/placements/{pid}/start", json={"started_on": TODAY.isoformat()})
    r = client.post(
        f"/placements/{pid}/attendance",
        json={
            "work_date": TODAY.isoformat(),
            "present": True,
            "confirmed_by": "whoever",
        },
    )
    assert r.status_code == 422


def test_the_followup_queue_is_reachable(client, make_placement):
    pid = make_placement()
    client.post(f"/placements/{pid}/start", json={"started_on": "2026-01-01"})
    due = client.get("/follow-ups/due").json()["due"]
    assert [d["checkpoint"] for d in due] == ["day_1", "week_1", "day_30", "day_90"]


def test_health_reports_ok_when_the_database_is_reachable(api, monkeypatch):
    """/health opens its own connection, so the test session is not in play."""
    import contextlib

    import app.main

    @contextlib.contextmanager
    def reachable(*args, **kwargs):
        class _Session:
            def execute(self, *_a, **_kw):
                return None

        yield _Session()

    monkeypatch.setattr(app.main, "session_scope", reachable)
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "up"


def test_health_returns_503_when_the_database_is_unreachable(api, monkeypatch):
    """An orchestrator must not keep routing to a container that cannot serve."""
    import app.main

    def broken(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(app.main, "session_scope", broken)
    r = api.get("/health")
    assert r.status_code == 503
    assert r.json()["database"] == "down"
