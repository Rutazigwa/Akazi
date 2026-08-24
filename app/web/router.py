"""The admin web UI.

Server-rendered HTML, one stylesheet, no build step and no JavaScript
framework. The blueprint's instruction for this layer was "low traffic, high
data density -- do not over-engineer", and a handful of coordinators on laptops
and good phones is exactly the case where a SPA costs more than it returns.

This reuses the same session model as the JSON API. The difference is the
transport: a cookie rather than a bearer header, which is why every
state-changing form carries a CSRF token (see app/web/deps.py).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.auth import AuthError, login, logout
from app.deps import SessionDep
from app.matching.repository import find_matches
from app.messaging.inbound import needs_attention
from app.mfa import MFAError, elevate_session
from app.operations.attendance import (
    AttendanceError,
    log_attendance,
    open_guarantees,
    record_replacement,
    start_placement,
)
from app.operations.escalations import (
    EscalationError,
    acknowledge,
    open_escalations,
)
from app.operations.follow_ups import complete_follow_up, due_follow_ups
from app.operations.pay import (
    PayError,
    confirm_with_worker,
    mark_paid,
    overdue_pay,
    pay_records_for_placement,
    record_pay_period,
    suggest_pay_period,
)
from app.operations.registry import (
    AvailabilitySlot,
    RegistryError,
    register_candidate,
    register_employer,
    set_employer_tier,
)
from app.operations.requests import (
    RequestError,
    create_work_request,
    offer_placement,
    open_requests,
    respond_to_offer,
)
from app.web.deps import (
    CsrfStaffDep,
    WebStaffDep,
    clear_session_cookie,
    safe_path,
    set_session_cookie,
)

router = APIRouter(prefix="/ui", include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _render(request: Request, name: str, staff, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, {"staff": staff, "today": date.today().isoformat(), **ctx}
    )


def _back(target: str, message: str, kind: str = "ok") -> RedirectResponse:
    from urllib.parse import quote

    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{sep}flash={quote(message)}&kind={kind}", status_code=303
    )


def _flash(request: Request) -> dict:
    return {
        "flash": request.query_params.get("flash"),
        "flash_kind": request.query_params.get("kind", "ok"),
    }


# --- sign in ---------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        request, "login.html",
        {"staff": None, "next": request.query_params.get("next"), **_flash(request)},
    )


@router.post("/login")
def do_login(
    request: Request,
    session: SessionDep,
    phone: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
):
    try:
        token = login(session, phone, password, request.headers.get("user-agent"))
    except AuthError:
        return _back("/ui/login", "Invalid credentials", "err")

    from app.auth import authenticate

    staff = authenticate(session, token)
    # `next` arrives from a query string, so it is attacker-controllable in a
    # link. Only a local path survives.
    destination = safe_path(next, "/ui/")
    # Enrolled but not yet elevated: offer the code now rather than letting the
    # coordinator hit a wall on the first screen that needs it.
    target = destination
    if staff.mfa_enrolled:
        target = "/ui/mfa"
        if destination != "/ui/":
            from urllib.parse import quote

            target = f"/ui/mfa?next={quote(destination)}"

    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, token)
    return response


@router.get("/mfa", response_class=HTMLResponse)
def mfa_form(request: Request, staff: WebStaffDep):
    return _render(
        request, "mfa.html", staff,
        next=request.query_params.get("next"), **_flash(request),
    )


@router.post("/mfa")
def do_mfa(
    request: Request,
    session: SessionDep,
    staff: CsrfStaffDep,
    code: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
):
    try:
        elevate_session(session, staff.staff_id, staff.session_id, code)
    except MFAError as exc:
        return _back("/ui/mfa", str(exc), "err")
    return _back(safe_path(next, "/ui/"), "Second factor verified")


@router.post("/logout")
def do_logout(
    session: SessionDep,
    staff: CsrfStaffDep,
    akazi_session: Annotated[str | None, __import__("fastapi").Cookie()] = None,
):
    if akazi_session:
        logout(session, akazi_session)
    response = RedirectResponse("/ui/login", status_code=303)
    clear_session_cookie(response)
    return response


# --- dashboard -------------------------------------------------------------

SCORECARD_LABELS = [
    ("active_employers", "Active employers", "10"),
    ("paid_placements", "Placements", "30–50"),
    ("avg_days_to_fill", "Days to fill", "< 7"),
    ("retention_30day_pct", "30-day retention %", "≥ 60"),
    ("avg_transport_pct", "Transport % of pay", "≤ 25"),
    ("guarantee_filled_24h_pct", "Guarantee filled 24h %", "≥ 90"),
    ("women_placed_pct", "Women placed %", "≥ 45"),
    ("pay_accuracy_pct", "Pay accuracy %", "≥ 95"),
]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: SessionDep, staff: WebStaffDep):
    card = session.execute(
        text("SELECT * FROM v_pilot_scorecard")
    ).mappings().one()
    return _render(
        request, "dashboard.html", staff,
        nav="dashboard",
        escalations=open_escalations(session),
        unread_replies=needs_attention(session),
        overdue_pay=overdue_pay(session),
        guarantees=open_guarantees(session),
        follow_ups=due_follow_ups(session, date.today()),
        requests=open_requests(session),
        scorecard_rows=[
            (label, card[key], target) for key, label, target in SCORECARD_LABELS
        ],
        **_flash(request),
    )


@router.post("/escalations/{escalation_id}/acknowledge")
def acknowledge_escalation(
    escalation_id: UUID, session: SessionDep, staff: CsrfStaffDep
):
    try:
        acknowledge(session, escalation_id, staff.staff_id)
    except EscalationError as exc:
        return _back("/ui/", str(exc), "err")
    return _back("/ui/", "Picked up — the response clock has stopped")


# --- employers -------------------------------------------------------------

def _employers(session):
    return [
        dict(r)
        for r in session.execute(
            text(
                "SELECT employer_id, business_name, sector, district, "
                "       tier::text AS tier, is_cooperative "
                "FROM employers ORDER BY business_name"
            )
        ).mappings()
    ]


@router.get("/employers", response_class=HTMLResponse)
def employers_page(request: Request, session: SessionDep, staff: WebStaffDep):
    return _render(
        request, "employers.html", staff, nav="employers",
        employers=_employers(session), **_flash(request),
    )


@router.post("/employers")
def create_employer(
    session: SessionDep,
    staff: CsrfStaffDep,
    business_name: Annotated[str, Form()],
    sector: Annotated[str, Form()],
    district: Annotated[str, Form()],
    tin: Annotated[str, Form()] = "",
    site_lat: Annotated[str, Form()] = "",
    site_lng: Annotated[str, Form()] = "",
    is_cooperative: Annotated[str, Form()] = "false",
):
    register_employer(
        session,
        business_name=business_name,
        sector=sector,
        district=district,
        account_owner=staff.staff_id,
        tin=tin or None,
        site_lat=float(site_lat) if site_lat else None,
        site_lng=float(site_lng) if site_lng else None,
        is_cooperative=is_cooperative == "true",
    )
    return _back("/ui/employers", f"Registered {business_name}")


@router.post("/employers/{employer_id}/tier")
def change_tier(
    employer_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    tier: Annotated[str, Form()],
):
    try:
        set_employer_tier(session, employer_id, tier)
    except RegistryError as exc:
        return _back("/ui/employers", str(exc), "err")
    return _back("/ui/employers", f"Marked {tier}")


# --- candidates ------------------------------------------------------------

@router.get("/candidates", response_class=HTMLResponse)
def candidates_page(request: Request, session: SessionDep, staff: WebStaffDep):
    rows = session.execute(
        text(
            """
            SELECT c.candidate_id, c.display_name, c.district, c.sector,
                   c.status::text AS status,
                   COALESCE(v.granted, false) AS placement_consent
              FROM candidates c
              LEFT JOIN v_current_consent v
                     ON v.candidate_id = c.candidate_id AND v.purpose = 'placement'
             ORDER BY c.display_name
            """
        )
    ).mappings()
    return _render(
        request, "candidates.html", staff, nav="candidates",
        candidates=[dict(r) for r in rows], **_flash(request),
    )


@router.post("/candidates")
async def create_candidate(
    request: Request, session: SessionDep, staff: CsrfStaffDep
):
    # Registration writes a national ID, so it needs the same gate the API
    # applies -- enforced here rather than trusted from the hidden template.
    from app.config import get_settings

    if not staff.can_view_identity or (
        get_settings().require_mfa_for_identity and not staff.mfa_satisfied
    ):
        return _back(
            "/ui/candidates",
            "Registering a candidate needs identity access with a second factor",
            "err",
        )

    form = await request.form()

    def value(name: str):
        raw = str(form.get(name, "")).strip()
        return raw or None

    from datetime import time as _time

    start = str(form.get("avail_start", "")) or "06:00"
    end = str(form.get("avail_end", "")) or "18:00"
    availability = [
        AvailabilitySlot(
            int(day), _time.fromisoformat(start), _time.fromisoformat(end)
        )
        for day in form.getlist("avail_days")
    ]

    try:
        register_candidate(
            session,
            legal_first_name=form["legal_first_name"],
            legal_last_name=form["legal_last_name"],
            date_of_birth=date.fromisoformat(str(form["date_of_birth"])),
            phone_primary=str(form["phone_primary"]),
            display_name=str(form["display_name"]),
            district=str(form["district"]),
            sector=str(form["sector"]),
            registered_by=staff.staff_id,
            consent_captured_via=str(form.get("consent_captured_via", "paper")),
            national_id=value("national_id"),
            gender=value("gender"),
            home_lat=float(form["home_lat"]) if value("home_lat") else None,
            home_lng=float(form["home_lng"]) if value("home_lng") else None,
            max_commute_rwf=int(form["max_commute_rwf"])
            if value("max_commute_rwf") else None,
            accepts_after_dark=form.get("accepts_after_dark") == "true",
            availability=availability,
        )
    except RegistryError as exc:
        return _back("/ui/candidates", str(exc), "err")

    return _back("/ui/candidates", f"Registered {form['display_name']}")


# --- work requests and matching -------------------------------------------

@router.get("/requests", response_class=HTMLResponse)
def requests_page(request: Request, session: SessionDep, staff: WebStaffDep):
    return _render(
        request, "requests.html", staff, nav="requests",
        requests=open_requests(session), employers=_employers(session),
        **_flash(request),
    )


@router.post("/requests")
async def create_request(
    request: Request, session: SessionDep, staff: CsrfStaffDep
):
    form = await request.form()
    from datetime import time as _time

    def when(name):
        raw = str(form.get(name, "")).strip()
        return _time.fromisoformat(raw) if raw else None

    try:
        request_id = create_work_request(
            session,
            employer_id=UUID(str(form["employer_id"])),
            title=str(form["title"]),
            work_type=str(form["work_type"]),
            headcount=int(form["headcount"]),
            starts_on=date.fromisoformat(str(form["starts_on"])),
            pay_rwf=int(form["pay_rwf"]),
            pay_unit=str(form["pay_unit"]),
            shift_start=when("shift_start"),
            shift_end=when("shift_end"),
            transport_covered=form.get("transport_covered") == "true",
        )
    except (RequestError, ValueError) as exc:
        return _back("/ui/requests", str(exc), "err")
    return _back(f"/ui/requests/{request_id}", "Posted — here is who matches")


@router.get("/requests/{request_id}", response_class=HTMLResponse)
def request_matches(
    request_id: UUID, request: Request, session: SessionDep, staff: WebStaffDep
):
    row = session.execute(
        text(
            """
            SELECT wr.request_id, wr.title, wr.headcount, wr.starts_on,
                   wr.shift_start, wr.shift_end, wr.pay_rwf, wr.pay_unit,
                   wr.transport_covered, e.business_name
              FROM work_requests wr
              JOIN employers e ON e.employer_id = wr.employer_id
             WHERE wr.request_id = :rid
            """
        ),
        {"rid": str(request_id)},
    ).mappings().first()
    if row is None:
        return _back("/ui/requests", "No such request", "err")

    result = find_matches(session, request_id)
    placements = session.execute(
        text(
            """
            SELECT p.placement_id, p.status::text AS status, c.display_name
              FROM placements p
              JOIN candidates c ON c.candidate_id = p.candidate_id
             WHERE p.request_id = :rid ORDER BY p.offered_at
            """
        ),
        {"rid": str(request_id)},
    ).mappings()

    return _render(
        request, "matches.html", staff, nav="requests",
        request_row=dict(row),
        matches=[
            {
                "candidate_id": m.candidate.candidate_id,
                "display_name": m.candidate.display_name,
                "reason": m.reason,
                "est_transport_rwf": m.candidate.est_transport_rwf,
                "est_commute_min": m.candidate.est_commute_min,
            }
            for m in result.matches
        ],
        rejections=[
            {
                "display_name": r.candidate.display_name,
                "filter": r.filter_name,
                "reason": r.reason,
            }
            for r in result.rejections
        ],
        placements=[dict(p) for p in placements],
        **_flash(request),
    )


@router.post("/requests/{request_id}/offer")
def make_offer(
    request_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    candidate_id: Annotated[UUID, Form()],
):
    try:
        placement_id = offer_placement(session, request_id, candidate_id)
    except RequestError as exc:
        return _back(f"/ui/requests/{request_id}", str(exc), "err")
    return _back(f"/ui/placements/{placement_id}", "Offered")


# --- placements ------------------------------------------------------------

@router.get("/placements/{placement_id}", response_class=HTMLResponse)
def placement_page(
    placement_id: UUID, request: Request, session: SessionDep, staff: WebStaffDep
):
    row = session.execute(
        text(
            """
            SELECT p.placement_id, p.request_id, p.status::text AS status,
                   p.agreed_pay_rwf, p.pay_unit, p.est_transport_rwf,
                   p.match_reason, c.display_name, wr.title, e.business_name
              FROM placements p
              JOIN candidates c     ON c.candidate_id = p.candidate_id
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e      ON e.employer_id = wr.employer_id
             WHERE p.placement_id = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()
    if row is None:
        return _back("/ui/", "No such placement", "err")

    attendance = session.execute(
        text(
            "SELECT work_date, present, hours_worked, confirmed_by, "
            "absence_reason FROM attendance WHERE placement_id = :pid "
            "ORDER BY work_date DESC"
        ),
        {"pid": str(placement_id)},
    ).mappings()
    follow_ups = session.execute(
        text(
            "SELECT follow_up_id, checkpoint::text AS checkpoint, due_on, "
            "completed_at, still_working, issue_flag FROM follow_ups "
            "WHERE placement_id = :pid ORDER BY due_on"
        ),
        {"pid": str(placement_id)},
    ).mappings()
    replacement = session.execute(
        text(
            """
            SELECT r.placement_id, r.offered_at, c.display_name,
                   g.hours_to_fill
              FROM placements r
              JOIN candidates c ON c.candidate_id = r.candidate_id
              LEFT JOIN v_guarantee_invocations g
                     ON g.replacement_placement_id = r.placement_id
             WHERE r.replaces_placement = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()

    # For a no-show, show who could cover it right now rather than making the
    # coordinator navigate back to the request while the clock runs.
    eligible = []
    if row["status"] == "no_show" and replacement is None:
        result = find_matches(session, row["request_id"])
        eligible = [
            {
                "candidate_id": m.candidate.candidate_id,
                "display_name": m.candidate.display_name,
                "reason": m.reason,
            }
            for m in result.matches
        ]

    return _render(
        request, "placement.html", staff,
        p=dict(row),
        pay_records=pay_records_for_placement(session, placement_id),
        pay_suggestion=suggest_pay_period(session, placement_id),
        attendance=[dict(a) for a in attendance],
        follow_ups=[dict(f) for f in follow_ups],
        replacement=dict(replacement) if replacement else None,
        hours_to_fill=(
            float(replacement["hours_to_fill"])
            if replacement and replacement["hours_to_fill"] is not None
            else None
        ),
        eligible=eligible,
        **_flash(request),
    )


@router.post("/placements/{placement_id}/respond")
def respond(
    placement_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    accepted: Annotated[str, Form()],
):
    try:
        respond_to_offer(session, placement_id, accepted == "true")
    except RequestError as exc:
        return _back(f"/ui/placements/{placement_id}", str(exc), "err")
    return _back(
        f"/ui/placements/{placement_id}",
        "Accepted" if accepted == "true" else "Declined",
    )


@router.post("/placements/{placement_id}/start")
def start(
    placement_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    started_on: Annotated[str, Form()],
):
    try:
        start_placement(session, placement_id, date.fromisoformat(started_on))
    except AttendanceError as exc:
        return _back(f"/ui/placements/{placement_id}", str(exc), "err")
    return _back(f"/ui/placements/{placement_id}", "Started — check-ins scheduled")


@router.post("/placements/{placement_id}/attendance")
def attendance(
    placement_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    work_date: Annotated[str, Form()],
    present: Annotated[str, Form()],
    confirmed_by: Annotated[str, Form()],
    hours_worked: Annotated[str, Form()] = "",
    absence_reason: Annotated[str, Form()] = "",
):
    try:
        invocation = log_attendance(
            session,
            placement_id,
            date.fromisoformat(work_date),
            present == "true",
            confirmed_by,
            float(hours_worked) if hours_worked else None,
            absence_reason or None,
        )
    except AttendanceError as exc:
        return _back(f"/ui/placements/{placement_id}", str(exc), "err")

    if invocation is None:
        return _back(f"/ui/placements/{placement_id}", "Recorded")
    return _back(
        f"/ui/placements/{placement_id}",
        f"No-show recorded — cover this shift by "
        f"{invocation.due_by.strftime('%H:%M on %d %b')}",
        "err",
    )


@router.post("/placements/{placement_id}/replace")
def replace(
    placement_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    candidate_id: Annotated[UUID, Form()],
    match_reason: Annotated[str, Form()] = "",
):
    try:
        new_id = record_replacement(
            session, placement_id, candidate_id, match_reason or "coordinator choice"
        )
    except AttendanceError as exc:
        return _back(f"/ui/placements/{placement_id}", str(exc), "err")
    return _back(f"/ui/placements/{new_id}", "Covered — guarantee honoured")


@router.post("/placements/{placement_id}/pay")
async def create_pay_period(
    placement_id: UUID, request: Request, session: SessionDep, staff: CsrfStaffDep
):
    form = await request.form()
    try:
        record_pay_period(
            session, placement_id,
            date.fromisoformat(str(form["period_start"])),
            date.fromisoformat(str(form["period_end"])),
            int(form["gross_rwf"]),
            date.fromisoformat(str(form["due_on"])),
            int(form.get("deductions_rwf") or 0),
            str(form.get("method") or "") or None,
        )
    except (PayError, ValueError) as exc:
        return _back(f"/ui/placements/{placement_id}", str(exc), "err")
    return _back(f"/ui/placements/{placement_id}", "Pay period recorded")


@router.post("/pay/{pay_id}/paid")
def pay_paid(
    pay_id: UUID,
    request: Request,
    session: SessionDep,
    staff: CsrfStaffDep,
    paid_on: Annotated[str, Form()],
):
    placement = session.execute(
        text("SELECT placement_id FROM pay_records WHERE pay_id = :p"),
        {"p": str(pay_id)},
    ).scalar_one_or_none()
    try:
        mark_paid(session, pay_id, date.fromisoformat(paid_on))
    except PayError as exc:
        return _back(f"/ui/placements/{placement}", str(exc), "err")
    return _back(f"/ui/placements/{placement}", "Marked paid — confirm with the worker")


@router.post("/pay/{pay_id}/confirm")
def pay_confirm(
    pay_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    shortfall: Annotated[str, Form()] = "",
):
    placement = session.execute(
        text("SELECT placement_id FROM pay_records WHERE pay_id = :p"),
        {"p": str(pay_id)},
    ).scalar_one_or_none()
    try:
        escalation_id = confirm_with_worker(session, pay_id, shortfall != "true")
    except PayError as exc:
        return _back(f"/ui/placements/{placement}", str(exc), "err")
    if escalation_id:
        return _back(
            "/ui/",
            "Shortfall recorded — a pay escalation is open and the clock is running",
            "err",
        )
    return _back(f"/ui/placements/{placement}", "Worker confirmed — paid in full")


@router.post("/follow-ups/{follow_up_id}/complete")
def complete(
    follow_up_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    still_working: Annotated[str, Form()],
    issue_flag: Annotated[str, Form()] = "",
):
    complete_follow_up(
        session,
        follow_up_id,
        still_working == "true",
        issue_flag=issue_flag or None,
    )
    # Derived from the record, not from the Referer header. Referer is set by
    # whatever page submitted the form, so trusting it made this an open
    # redirect: a coordinator could be bounced to an attacker's site with a
    # fresh session in hand.
    placement_id = session.execute(
        text("SELECT placement_id FROM follow_ups WHERE follow_up_id = :fid"),
        {"fid": str(follow_up_id)},
    ).scalar_one_or_none()
    target = f"/ui/placements/{placement_id}" if placement_id else "/ui/"
    return _back(target, "Check-in recorded")
