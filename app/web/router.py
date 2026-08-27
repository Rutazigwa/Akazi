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

from datetime import date, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.clock import kigali_today
from sqlalchemy import text

from app.auth import AuthError, login, logout
from app.deps import SessionDep
from app.matching.repository import find_matches
from app.messaging.inbound import needs_attention
from app.mfa import MFAError, elevate_session
from app.operations.attendance import (
    AttendanceError,
    complete_placement,
    log_attendance,
    open_guarantees,
    record_replacement,
    start_placement,
    terminate_placement,
)
from app.employer_auth import invite_contact
from app.operations.contracts import get_contract, render_contract
from app.operations.escalations import (
    EscalationError,
    acknowledge,
    open_escalations,
    response_performance,
)
from app.operations.lmis import MIN_CELL, reporting_consent_counts
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
    cancel_work_request,
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
        request, name, {"staff": staff, "today": kigali_today().isoformat(), **ctx}
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
    # A temporary password is the first thing to deal with, ahead of MFA.
    if staff.must_change_password:
        response = RedirectResponse("/ui/password", status_code=303)
        set_session_cookie(response, token)
        return response

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


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, staff: WebStaffDep):
    return _render(request, "password.html", staff, **_flash(request))


@router.post("/password")
def change_password(
    request: Request,
    session: SessionDep,
    staff: CsrfStaffDep,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    from app.auth import AuthError, change_own_password

    try:
        revoked = change_own_password(
            session, staff.staff_id, current_password, new_password,
            keep_session_id=staff.session_id,
        )
    except AuthError as exc:
        return _back("/ui/password", str(exc), "err")
    return _back(
        "/ui/",
        f"Password changed — {revoked} other session(s) signed out",
    )


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
    ("employer_reorder_pct", "Employer reorder %", "≥ 40"),
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
        follow_ups=due_follow_ups(session, kigali_today()),
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


# --- staff administration --------------------------------------------------

ADMIN_ROLES = ("owner", "admin")


def _require_admin(staff):
    """Owner and admin only. Returns a redirect when refused, else None.

    Enforced here rather than only by hiding the nav link: a hidden link is a
    UI affordance, never a control.
    """
    if staff.role not in ADMIN_ROLES:
        return _back("/ui/", "Staff administration is for owners and admins", "err")
    return None


@router.get("/staff", response_class=HTMLResponse)
def staff_page(request: Request, session: SessionDep, staff: WebStaffDep):
    refused = _require_admin(staff)
    if refused:
        return refused

    people = session.execute(
        text(
            """
            SELECT staff_id, full_name, phone, email, role::text AS role,
                   can_view_identity, is_active, must_change_password,
                   locked_until,
                   (totp_enrolled_at IS NOT NULL) AS mfa_enrolled
              FROM staff ORDER BY is_active DESC, full_name
            """
        )
    ).mappings()

    broken = session.execute(
        text("SELECT * FROM verify_audit_chain()")
    ).mappings().first()
    head = session.execute(
        text("SELECT * FROM audit_chain_head()")
    ).mappings().first()

    # Carried through one redirect so a generated password survives the
    # post-redirect-get without ever being stored.
    new_account = None
    if request.query_params.get("account_label"):
        new_account = {
            "label": request.query_params["account_label"],
            "password": request.query_params.get("account_password", ""),
        }

    return _render(
        request, "staff.html", staff, nav="staff",
        people=[dict(p) for p in people],
        new_account=new_account,
        audit={
            "intact": broken is None,
            "broken_at": dict(broken) if broken else None,
            "entries": head["entries"] if head else 0,
            "head_hash": head["entry_hash"] if head else "",
        },
        **_flash(request),
    )


def _with_password(target: str, label: str, password: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(
        f"{target}?account_label={quote(label)}"
        f"&account_password={quote(password)}",
        status_code=303,
    )


@router.post("/staff")
async def create_staff_member(
    request: Request, session: SessionDep, staff: CsrfStaffDep
):
    refused = _require_admin(staff)
    if refused:
        return refused

    import secrets

    from app.auth import AuthError, set_password

    form = await request.form()
    phone = str(form["phone"]).strip()

    if session.execute(
        text("SELECT 1 FROM staff WHERE phone = :phone"), {"phone": phone}
    ).first():
        return _back("/ui/staff", "Someone already has that phone number", "err")

    temporary = secrets.token_urlsafe(16)
    new_id = session.execute(
        text(
            """
            INSERT INTO staff (full_name, phone, email, role, can_view_identity)
            VALUES (:name, :phone, :email, CAST(:role AS staff_role), :identity)
            RETURNING staff_id
            """
        ),
        {
            "name": str(form["full_name"]).strip(),
            "phone": phone,
            "email": str(form.get("email", "")).strip() or None,
            "role": str(form["role"]),
            "identity": form.get("can_view_identity") == "true",
        },
    ).scalar_one()

    try:
        set_password(session, new_id, temporary, must_change=True)
    except AuthError as exc:
        return _back("/ui/staff", str(exc), "err")

    _audit_staff_change(session, staff.staff_id, new_id, {
        "event": "staff_created",
        "role": str(form["role"]),
        "can_view_identity": form.get("can_view_identity") == "true",
    })
    return _with_password(
        "/ui/staff", f"Created {form['full_name']}", temporary
    )


def _audit_staff_change(session, actor, target, detail) -> None:
    import json

    session.execute(
        text(
            "INSERT INTO audit_log (staff_id, table_name, record_id, action, "
            "                       detail) "
            "VALUES (:actor, 'staff', :target, 'update', :detail)"
        ),
        {"actor": str(actor), "target": str(target), "detail": json.dumps(detail)},
    )


@router.post("/staff/{staff_id}/identity")
def toggle_identity(
    staff_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    can_view_identity: Annotated[str, Form()],
):
    refused = _require_admin(staff)
    if refused:
        return refused

    from app.auth import revoke_all_sessions

    grant = can_view_identity == "true"
    session.execute(
        text("UPDATE staff SET can_view_identity = :g WHERE staff_id = :sid"),
        {"g": grant, "sid": str(staff_id)},
    )
    revoked = revoke_all_sessions(session, staff_id)
    _audit_staff_change(session, staff.staff_id, staff_id, {
        "event": "staff_updated", "can_view_identity": grant,
    })
    return _back(
        "/ui/staff",
        f"Identity access {'granted' if grant else 'revoked'} — "
        f"{revoked} session(s) ended",
    )


@router.post("/staff/{staff_id}/reset-password")
def reset_staff_password(
    staff_id: UUID, session: SessionDep, staff: CsrfStaffDep
):
    refused = _require_admin(staff)
    if refused:
        return refused

    import secrets

    from app.auth import revoke_all_sessions, set_password

    name = session.execute(
        text("SELECT full_name FROM staff WHERE staff_id = :sid"),
        {"sid": str(staff_id)},
    ).scalar_one_or_none()
    if name is None:
        return _back("/ui/staff", "No such staff member", "err")

    temporary = secrets.token_urlsafe(16)
    set_password(session, staff_id, temporary, must_change=True)
    revoke_all_sessions(session, staff_id)
    _audit_staff_change(session, staff.staff_id, staff_id,
                        {"event": "password_reset"})
    return _with_password("/ui/staff", f"New password for {name}", temporary)


@router.post("/staff/{staff_id}/reset-mfa")
def reset_staff_mfa(staff_id: UUID, session: SessionDep, staff: CsrfStaffDep):
    """For a lost phone. Sessions go with it -- one elevated by the old factor
    must not survive its removal."""
    refused = _require_admin(staff)
    if refused:
        return refused

    from app.mfa import reset_enrolment

    reset_enrolment(session, staff_id)
    _audit_staff_change(session, staff.staff_id, staff_id, {"event": "mfa_reset"})
    return _back("/ui/staff", "Second factor cleared — they must enrol again")


@router.post("/staff/{staff_id}/deactivate")
def deactivate_staff(staff_id: UUID, session: SessionDep, staff: CsrfStaffDep):
    refused = _require_admin(staff)
    if refused:
        return refused
    if staff_id == staff.staff_id:
        return _back("/ui/staff", "You cannot deactivate your own account", "err")

    from app.auth import revoke_all_sessions

    updated = session.execute(
        text(
            "UPDATE staff SET is_active = false, deactivated_at = now() "
            "WHERE staff_id = :sid AND is_active RETURNING full_name"
        ),
        {"sid": str(staff_id)},
    ).scalar_one_or_none()
    if updated is None:
        return _back("/ui/staff", "Already inactive", "err")

    revoked = revoke_all_sessions(session, staff_id)
    _audit_staff_change(session, staff.staff_id, staff_id,
                        {"event": "deactivated"})
    return _back("/ui/staff", f"{updated} deactivated — {revoked} session(s) ended")


# --- reports ---------------------------------------------------------------

@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, session: SessionDep, staff: WebStaffDep):
    refused = _require_admin(staff)
    if refused:
        return refused

    return _render(
        request, "reports.html", staff, nav="reports",
        performance=response_performance(session),
        consent=reporting_consent_counts(session),
        min_cell=MIN_CELL,
        default_from=(kigali_today() - timedelta(days=90)).isoformat(),
        **_flash(request),
    )


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
    contacts = session.execute(
        text(
            """
            SELECT employer_id, contact_id, full_name, phone, is_primary,
                   (password_hash IS NOT NULL) AS has_login
              FROM employer_contacts
             WHERE is_active
             ORDER BY is_primary DESC, full_name
            """
        )
    ).mappings()
    grouped: dict = {}
    for row in contacts:
        grouped.setdefault(str(row["employer_id"]), []).append(dict(row))

    new_account = None
    if request.query_params.get("account_label"):
        new_account = {
            "label": request.query_params["account_label"],
            "password": request.query_params.get("account_password", ""),
        }

    return _render(
        request, "employers.html", staff, nav="employers",
        employers=_employers(session), contacts=grouped,
        new_account=new_account, **_flash(request),
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


@router.post("/employers/{employer_id}/contacts")
async def add_contact(
    employer_id: UUID, request: Request, session: SessionDep, staff: CsrfStaffDep
):
    """Add a contact, optionally with a login to the employer dashboard."""
    from app.operations.registry import add_employer_contact

    form = await request.form()
    contact_id = add_employer_contact(
        session,
        employer_id,
        str(form["full_name"]).strip(),
        str(form["phone"]).strip(),
        str(form.get("role_title", "")).strip() or None,
        str(form.get("email", "")).strip() or None,
        form.get("is_primary") == "true",
    )

    if form.get("give_login") != "true":
        return _back("/ui/employers", "Contact added")

    return _with_password(
        "/ui/employers",
        f"Login for {form['full_name']}",
        invite_contact(session, contact_id),
    )


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


@router.post("/requests/{request_id}/cancel")
def cancel_request_page(
    request_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    reason: Annotated[str, Form()],
):
    try:
        result = cancel_work_request(session, request_id, reason)
    except RequestError as exc:
        return _back(f"/ui/requests/{request_id}", str(exc), "err")
    return _back(
        "/ui/requests",
        f"Cancelled — {result['placements_cancelled']} worker(s) told it is off",
    )


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
        contract=(
            lambda c: {**c, "text": render_contract(c["terms"], c["contract_ref"])}
            if c else None
        )(get_contract(session, placement_id)),
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


@router.post("/placements/{placement_id}/end")
def end_placement(
    placement_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    outcome: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
):
    """Close a placement: finished as agreed, or ended early with a reason."""
    try:
        if outcome == "completed":
            complete_placement(session, placement_id)
        else:
            terminate_placement(session, placement_id, reason)
    except AttendanceError as exc:
        return _back(f"/ui/placements/{placement_id}", str(exc), "err")
    return _back(
        f"/ui/placements/{placement_id}",
        "Completed — the worker is available for new shifts"
        if outcome == "completed"
        else "Ended early — recorded, and the worker is available again",
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
