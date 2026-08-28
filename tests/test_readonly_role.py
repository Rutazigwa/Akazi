"""The readonly role, which until now enforced nothing.

`readonly` was in the staff_role enum, assignable through POST /staff,
validated by the request schema and echoed back on login -- and checked
nowhere. An account handed that role could register candidates, promote
employers and post work requests. The person given it, and the person who
granted it, would both have believed it meant "can only look".

Written as a matrix rather than a handful of examples because the gate is
method-based: the failure mode worth guarding against is a route added later
that nobody remembers to protect.
"""
from __future__ import annotations
from app.clock import kigali_today

from datetime import timedelta

import pytest
from sqlalchemy import text



NEXT_WEEK = kigali_today() + timedelta(days=7)


@pytest.fixture
def readonly_client(api, session, staff_id, staff_login):
    """The standard authenticated client, demoted to readonly."""
    session.execute(
        text("UPDATE staff SET role = 'readonly' WHERE staff_id = :sid"),
        {"sid": staff_id},
    )
    r = api.post(
        "/auth/login",
        json={"phone": staff_login["phone"], "password": staff_login["password"]},
    )
    assert r.status_code == 200, r.text
    api.headers["Authorization"] = f"Bearer {r.json()['token']}"
    return api


WRITES = [
    ("post", "/employers", {"business_name": "Ghost Ltd", "sector": "cleaning",
                            "district": "Gasabo", "site_lat": -1.95,
                            "site_lng": 30.11}),
    ("post", "/work-requests", {"employer_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                "title": "Shift", "work_type": "shift",
                                "headcount": 1, "starts_on": str(NEXT_WEEK),
                                "pay_rwf": 5000, "pay_unit": "day"}),
    ("post", "/cohorts", {"name": "Orientation", "starts_on": str(NEXT_WEEK),
                          "capacity": 10}),
]


@pytest.mark.parametrize("method,path,body", WRITES)
def test_readonly_cannot_write(readonly_client, method, path, body):
    response = getattr(readonly_client, method)(path, json=body)
    assert response.status_code == 403, f"{method.upper()} {path}"
    assert "readonly" in response.json()["detail"]


READS = ["/employers", "/candidates", "/metrics/scorecard", "/escalations"]


@pytest.mark.parametrize("path", READS)
def test_readonly_can_still_read(readonly_client, path):
    """Refusing writes is only correct if the account can still do its job."""
    assert readonly_client.get(path).status_code == 200, path


def test_readonly_can_still_manage_its_own_session(readonly_client, staff_login):
    """Otherwise the account could not finish logging in or rotate its password.

    Managing your own session is not an operational write.
    """
    changed = readonly_client.post(
        "/auth/password",
        json={"current_password": staff_login["password"],
              "new_password": "a-replacement-password-long-enough"},
    )
    assert changed.status_code == 200, changed.text


def test_a_coordinator_is_unaffected(client):
    """The gate must not catch everyone else on the way past."""
    created = client.post(
        "/employers",
        json={"business_name": "Isuku Ltd", "sector": "cleaning",
              "district": "Gasabo", "site_lat": -1.95, "site_lng": 30.11},
    )
    assert created.status_code == 201, created.text


def _all_routes(router):
    """Every real route, descending into included routers.

    app.routes holds router wrappers, not routes. Iterating it directly finds
    six objects and no endpoints -- which is how the first version of this
    test examined zero routes and passed, looking exactly like coverage.
    """
    for route in getattr(router, "routes", []):
        if type(route).__name__ == "_IncludedRouter":
            yield from _all_routes(route.original_router)
        else:
            yield route


def _walk(dependant):
    yield dependant
    for sub in dependant.dependencies:
        yield from _walk(sub)


PUBLIC_PREFIXES = ("/auth", "/webhooks", "/health", "/docs", "/openapi", "/redoc")


def _staff_write_routes():
    from app.main import app

    for route in _all_routes(app.router):
        methods = getattr(route, "methods", None) or set()
        if not methods - {"GET", "HEAD", "OPTIONS"}:
            continue
        path = getattr(route, "path", "")
        if path.startswith(PUBLIC_PREFIXES) or getattr(route, "dependant", None) is None:
            continue
        yield sorted(methods)[0], path, route


def test_the_route_walker_actually_finds_routes():
    """Guards the guard.

    A coverage test that silently examines nothing is worse than no test: it
    reports safety it never checked. This one caught its own first version.
    """
    found = list(_staff_write_routes())
    assert len(found) > 20, f"expected the app's write routes, found {len(found)}"


# Write routes reachable without any principal, each for a stated reason.
# Kept tiny on purpose: this is the list an auditor reads.
UNAUTHENTICATED_WRITES = {
    "POST /ui/login",        # the staff login form itself
    "POST /employer/login",  # the employer login form itself
}


def test_no_write_route_is_reachable_without_a_principal():
    """Broader than the readonly rule, and the more important assertion.

    Every endpoint that changes something must sit behind staff auth, employer
    auth, or the two login forms. Anything else is an unauthenticated write.
    """
    from app.deps import current_staff
    from app.web.deps import current_web_staff
    from app.web.employer_deps import current_employer

    gates = {current_staff, current_web_staff, current_employer}
    open_writes = [
        f"{method} {path}"
        for method, path, route in _staff_write_routes()
        if not gates & {d.call for d in _walk(route.dependant)}
        and f"{method} {path}" not in UNAUTHENTICATED_WRITES
    ]
    assert open_writes == [], f"unauthenticated writes: {open_writes}"


def test_every_staff_write_route_is_behind_the_readonly_gate():
    """Why the gate is method-based rather than a list of endpoints.

    A route added next month is covered without anyone remembering to add it.
    The employer portal is excluded because employers are a separate principal
    with no staff role at all -- their own isolation rules apply there, not
    this one.
    """
    from app.deps import current_staff
    from app.web.deps import current_web_staff

    gates = {current_staff, current_web_staff}
    unguarded = [
        f"{method} {path}"
        for method, path, route in _staff_write_routes()
        if not path.startswith("/employer")
        and f"{method} {path}" not in UNAUTHENTICATED_WRITES
        and not gates & {d.call for d in _walk(route.dependant)}
    ]
    assert unguarded == [], f"staff writes not behind the readonly gate: {unguarded}"
