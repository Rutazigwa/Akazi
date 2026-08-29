"""Which routes need a CSRF token, and why the rest do not.

Two families of route live in this application and they defend against
different things:

* The browser UI (`/ui/...`, `/employer/...`) authenticates with a cookie. A
  browser attaches a cookie to any request any page can cause, so every
  state-changing route here needs a token bound to the session.
* The JSON API authenticates with `Authorization: Bearer`. A browser never
  attaches that header on someone else's behalf, so those routes need no
  token -- but only for as long as the cookie stays worthless against them.

Both halves are asserted here. Checking the first alone would leave the second
free to quietly acquire a cookie fallback, at which point thirty-odd unguarded
API routes become CSRF-reachable in one commit -- including POST /staff, which
mints an owner account.

The dependency graph is walked rather than the source text: a route that lists
the dependency in a way a regex does not recognise is still guarded, and one
that names it in a comment is not.
"""
from __future__ import annotations

import pytest

from app.main import app
from app.web.deps import verify_csrf
from app.web.employer_deps import verify_employer_csrf

# The sign-in routes. A CSRF token is bound to a session, and these are what
# create one -- there is nothing to bind to yet. Anything else appearing here
# is a hole, so the list is exact rather than a prefix.
NO_SESSION_YET = frozenset({"/ui/login", "/employer/login"})


def all_routes():
    found = []

    def walk(router):
        for route in getattr(router, "routes", []):
            if hasattr(route, "methods") and hasattr(route, "dependant"):
                found.append(route)
            if hasattr(route, "original_router"):
                walk(route.original_router)
            elif hasattr(route, "routes"):
                walk(route)

    walk(app)
    return found


def reaches(dependant, target, seen=None) -> bool:
    """Whether `target` appears anywhere in a route's dependency tree."""
    seen = seen or set()
    if id(dependant) in seen:
        return False
    seen.add(id(dependant))
    if dependant.call is target:
        return True
    return any(reaches(d, target, seen) for d in dependant.dependencies)


def cookie_authenticated_posts():
    return [
        r for r in all_routes()
        if "POST" in (r.methods or set())
        and r.path.startswith(("/ui/", "/employer/"))
    ]


def test_the_scan_finds_the_routes_it_is_meant_to_check():
    """Guards the guard.

    Every assertion below iterates. If the walk ever returns nothing -- a
    router refactor, a changed attribute name -- this file would pass while
    checking no routes at all.
    """
    assert len(all_routes()) > 100
    assert len(cookie_authenticated_posts()) >= 30


@pytest.mark.parametrize(
    "route", cookie_authenticated_posts(), ids=lambda r: r.path
)
def test_every_cookie_authenticated_post_requires_a_csrf_token(route):
    if route.path in NO_SESSION_YET:
        pytest.skip("sign-in: no session exists yet to bind a token to")
    guarded = (reaches(route.dependant, verify_csrf)
               or reaches(route.dependant, verify_employer_csrf))
    assert guarded, (
        f"POST {route.path} is reachable with the browser's cookie and no "
        "CSRF token. Depend on CsrfStaffDep (or EmployerCsrfDep)."
    )


def test_only_the_sign_in_routes_are_exempt():
    """The exemption list must not grow by accident."""
    exempt = {
        r.path for r in cookie_authenticated_posts()
        if not (reaches(r.dependant, verify_csrf)
                or reaches(r.dependant, verify_employer_csrf))
    }
    assert exempt == set(NO_SESSION_YET), (
        f"unexpected routes without CSRF protection: {exempt - NO_SESSION_YET}"
    )


# --- and the reason the API needs no token -------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/skills", {"code": "x", "name": "X"}),
    ("POST", "/staff", {"full_name": "Mallory", "phone": "+250780009999",
                        "role": "owner"}),
    ("POST", "/auth/logout", {}),
    ("GET", "/candidates", None),
])
def test_the_browser_cookie_is_worthless_against_the_json_api(
    web, method, path, body
):
    """The JSON API is exempt from CSRF because a cookie cannot reach it.

    `web` is a real signed-in browser session, MFA satisfied -- the cookie
    works on /ui/. If a cookie fallback is ever added to the API's
    authentication, these routes become CSRF-reachable with no token, and
    POST /staff creates an owner.
    """
    assert web.get("/ui/").status_code == 200, "the fixture is not signed in"

    response = web.request(method, path, json=body)
    assert response.status_code == 401, (
        f"{method} {path} accepted the browser session cookie "
        f"({response.status_code}). Either it must require a CSRF token, or "
        "the API must keep refusing cookies."
    )
    assert "bearer" in response.json()["detail"].lower()
