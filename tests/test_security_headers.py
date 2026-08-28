"""Headers the browser enforces for us, and the XSS defence behind them.

The application sent none. For a system holding national ID numbers, home
locations and assessment scores -- used on laptops that may be shared -- most
of these are not decoration.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


def headers(response) -> dict:
    return {k.lower(): v for k, v in response.headers.items()}


def test_a_page_cannot_run_script_at_all(api):
    """The dividend of no build step and no framework: an injected script tag
    does not execute even if one gets past Jinja's escaping. Very few
    applications can say this, and one line of JavaScript would cost it.
    """
    csp = headers(api.get("/ui/login"))["content-security-policy"]
    assert "script-src 'none'" in csp


def test_a_page_cannot_be_framed(api):
    """A coordinator's session framed in an attacker's page could be made to
    click "Send Chantal", or an erasure."""
    got = headers(api.get("/ui/login"))
    assert "frame-ancestors 'none'" in got["content-security-policy"]
    assert got["x-frame-options"] == "DENY"


def test_a_form_cannot_post_somewhere_else(api):
    """Stops an injected form exfiltrating a national ID to another server."""
    assert "form-action 'self'" in headers(
        api.get("/ui/login"))["content-security-policy"]


def test_candidate_ids_do_not_leak_through_the_referer(api):
    """URLs here carry candidate UUIDs. Sending one to another origin leaks
    exactly the identifier the rest of the system is careful with."""
    assert headers(api.get("/ui/login"))["referrer-policy"] == "same-origin"


def test_authenticated_pages_are_not_cached(client, make_candidate):
    """A cached page of personal data on a shared laptop outlives the session
    that fetched it."""
    make_candidate()
    assert headers(client.get("/candidates"))["cache-control"] == "no-store"


def test_the_health_check_may_be_cached(api):
    """It holds nothing, and monitoring hits it constantly."""
    assert "cache-control" not in headers(api.get("/health"))


def test_content_type_is_not_sniffed(api):
    assert headers(api.get("/ui/login"))["x-content-type-options"] == "nosniff"


def test_headers_reach_error_responses_too(api):
    """The responses most easily forgotten. A 404 is still a page an attacker
    can get a browser to load."""
    got = headers(api.get("/ui/nothing-here"))
    assert "script-src 'none'" in got["content-security-policy"]
    assert got["x-frame-options"] == "DENY"


def test_headers_reach_redirects_too(api):
    unauthenticated = api.get("/ui/", follow_redirects=False)
    assert unauthenticated.status_code in (302, 303, 307)
    assert "content-security-policy" in headers(unauthenticated)


def test_hsts_is_not_sent_in_local_development(api):
    """Pinning a developer's browser to https for localhost is a nuisance
    nobody expects and few know how to undo."""
    assert "strict-transport-security" not in headers(api.get("/health"))


# --- the escaping the CSP is a second line behind ---------------------------

@pytest.mark.parametrize("field,payload", [
    ("display_name", "<script>alert(1)</script>"),
    ("district", "\" onmouseover=\"alert(1)"),
    ("sector", "<img src=x onerror=alert(1)>"),
])
def test_a_script_in_a_candidate_field_is_escaped(web, session,
                                                  make_candidate, field,
                                                  payload):
    """Autoescaping is on by default, which is exactly the kind of default
    that gets turned off by somebody adding one |safe filter."""
    candidate_id = make_candidate()
    session.execute(
        text(f"UPDATE candidates SET {field} = :value WHERE candidate_id = :c"),
        {"value": payload, "c": str(candidate_id)},
    )
    page = web.get("/ui/candidates").text
    assert payload not in page
    assert "&lt;" in page or "&#34;" in page or "&amp;" in page


def test_a_script_in_a_safety_note_is_escaped(web, session, make_placement):
    """Free text a worker dictated, rendered on a coordinator's screen."""
    from app.operations.safety import record_safety_report

    placement_id = make_placement()
    record_safety_report(
        session, placement_id=placement_id, felt_safe=False,
        concern="other", note="<script>alert('xss')</script> and more words",
    )
    request_id = session.execute(
        text("SELECT request_id FROM placements WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one()
    page = web.get(f"/ui/requests/{request_id}").text
    assert "<script>alert('xss')</script>" not in page


def test_no_template_disables_escaping():
    """One |safe filter is all it takes, and it would not fail any other test."""
    from pathlib import Path

    offenders = [
        p.name for p in Path("app/web/templates").rglob("*.html")
        if "|safe" in p.read_text() or "autoescape false" in p.read_text()
    ]
    assert offenders == [], offenders
