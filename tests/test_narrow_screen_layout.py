"""Nothing may drag the page sideways on a phone.

Coordinators work from phones in the field. A page wider than the screen does
not merely need scrolling -- it misaligns everything on it, because the
navigation bar stops at the viewport edge while the content runs past it, so
the header no longer sits above what it belongs to.

The stylesheet had no media queries at all, and a seven-column table on
/ui/reports laid out 710px wide inside a 360px viewport. Nothing in a suite of
890 tests could see it: the defect exists only once a browser has laid the page
out. So this file starts a real server and a real browser.

The server shares the test transaction, the same way the `api` fixture does, so
these pages render with test data in them rather than empty. An empty page
cannot overflow, which would make the whole file decorative.

It is deliberately small -- one viewport, one assertion per page. The point is
to catch a layout that escapes the screen, not to become a visual regression
suite.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest
from sqlalchemy import text

pytest.importorskip("playwright.sync_api", reason="browser tests need playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
PHONE = {"width": 360, "height": 760}

PAGES = ["/ui/", "/ui/tomorrow", "/ui/requests", "/ui/employers", "/ui/candidates",
         "/ui/catalogue", "/ui/staff", "/ui/reports"]

pytestmark = pytest.mark.skipif(
    not os.path.exists(CHROMIUM), reason="no chromium in this environment"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    """Launching chromium costs about a second; pay it once."""
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROMIUM)
        yield b
        b.close()


@pytest.fixture
def live(session):
    """The application on a real port, reading the test transaction."""
    import uvicorn

    from app.deps import db_session
    from app.main import app

    app.dependency_overrides[db_session] = lambda: session
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    app.dependency_overrides.clear()


@pytest.fixture
def signed_in_phone(browser, live, api, staff_login):
    """A 360px browser carrying a real, MFA-elevated session cookie.

    The cookie is obtained through the TestClient rather than by driving the
    form, because the login screen is not what is being tested here.
    """
    from tests.conftest import csrf, totp_now

    api.post("/ui/login", data={"phone": staff_login["phone"],
                                "password": staff_login["password"]},
             follow_redirects=True)
    page_html = api.get("/ui/mfa").text
    api.post("/ui/mfa",
             data={"csrf_token": csrf(page_html),
                   "code": totp_now(staff_login["totp_secret"], 1)},
             follow_redirects=True)
    cookie = api.cookies.get("akazi_session")
    assert cookie, "no session cookie -- the sign-in in this fixture failed"

    ctx = browser.new_context(viewport=PHONE)
    ctx.add_cookies([{"name": "akazi_session", "value": cookie,
                      "domain": "127.0.0.1", "path": "/"}])
    page = ctx.new_page()
    yield page, live
    ctx.close()


@pytest.mark.parametrize("path", PAGES)
def test_no_page_scrolls_sideways_on_a_phone(signed_in_phone, path):
    page, base = signed_in_phone
    page.goto(base + path, wait_until="networkidle")

    # Not the login screen. Every width assertion below passes trivially
    # against a redirect, and a file of trivially passing assertions is worse
    # than no file at all.
    assert "/ui/login" not in page.url, (
        f"{path} redirected to the sign-in screen; this measured nothing"
    )

    size = page.evaluate(
        "() => ({doc: document.documentElement.scrollWidth,"
        "        view: document.documentElement.clientWidth})"
    )
    # A pixel or two of rounding is not a broken layout. A column is.
    assert size["doc"] <= size["view"] + 2, (
        f"{path} lays out {size['doc']}px wide in a {size['view']}px viewport. "
        "Wide content must scroll inside its own container -- not the page."
    )


def test_a_wide_table_still_scrolls_rather_than_being_cut_off(
    signed_in_phone, session, staff_id, make_candidate
):
    """Fixing the overflow must not hide the columns instead.

    The first fix stopped the page overflowing and left the widest table
    clipped at the card edge, with "overdue now" off-screen and no way to reach
    it -- the wrong half of a safeguarding metric to lose.
    """
    from app.operations.escalations import raise_escalation

    raise_escalation(session, "harassment", candidate_id=make_candidate(),
                     detail="waiting", owner_staff_id=staff_id)
    page, base = signed_in_phone
    page.goto(base + "/ui/reports", wait_until="networkidle")

    table = page.locator("table").first
    box = table.evaluate(
        "el => ({scroll: el.scrollWidth, client: el.clientWidth})"
    )
    assert box["scroll"] > box["client"], (
        "the escalation table is not wider than its box, so this test is no "
        "longer measuring the case it was written for"
    )
    # Wider content inside a box that can scroll to reach it.
    assert table.evaluate(
        "el => getComputedStyle(el).overflowX"
    ) in ("auto", "scroll")


def test_the_alarming_columns_come_first(signed_in_phone, session, staff_id,
                                         make_candidate):
    """On a phone, only the first columns are visible without scrolling.

    "Answered in time" used to occupy that space while "overdue now" sat off
    the edge.
    """
    from app.operations.escalations import raise_escalation

    raise_escalation(session, "harassment", candidate_id=make_candidate(),
                     detail="waiting", owner_staff_id=staff_id)
    page, base = signed_in_phone
    page.goto(base + "/ui/reports", wait_until="networkidle")

    headers = page.locator("table").first.locator("th").all_inner_texts()
    assert headers[1].strip().lower() == "overdue now", headers
    assert "unanswered" in headers[2].strip().lower(), headers


def test_the_check_is_looking_at_the_real_application(signed_in_phone):
    """Guards the guard: prove the browser rendered Akazi, signed in."""
    page, base = signed_in_phone
    page.goto(base + "/ui/", wait_until="networkidle")
    assert "Akazi" in page.title()
    assert page.locator('a[href="/ui/reports"]').count() >= 1
    assert page.evaluate("() => document.documentElement.scrollWidth") > 300


def test_the_live_server_really_reads_the_test_transaction(signed_in_phone,
                                                           session, staff_id):
    """The override is what makes every page above render with data in it.

    If the server ever stops sharing the test transaction, those pages go empty
    and quietly stop testing anything, because an empty page cannot overflow.
    So: write a distinctive value into the transaction and require the browser
    to see it. /health cannot answer this -- it opens its own connection to
    whatever DATABASE_URL points at, which in tests is not this transaction.
    """
    session.execute(
        text("UPDATE staff SET full_name = :n WHERE staff_id = :s"),
        {"n": "Uwimana Layout-Probe", "s": str(staff_id)},
    )
    page, base = signed_in_phone
    page.goto(base + "/ui/staff", wait_until="networkidle")
    assert "Uwimana Layout-Probe" in page.content(), (
        "the browser did not see a row written in the test transaction; the "
        "pages above are rendering empty and proving nothing"
    )
