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
from app.matching.repository import find_cover_for, find_matches
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
from app.operations.catalogue import (
    CATEGORIES,
    METHODS,
    CatalogueError,
    assessment_for_scoring,
    create_assessment,
    create_skill,
    list_assessments,
    list_skills,
)
from app.operations.follow_ups import complete_follow_up, due_follow_ups
from app.operations.jobs import backup_status, messaging_status
from app.operations.readiness import shifts_on, unstaffed_shifts_on
from app.operations.transport import (
    TransportReportError,
    record_transport_report,
    route_history,
)
from app.operations.pay import (
    PayError,
    confirm_with_worker,
    deduction_lines,
    mark_paid,
    overdue_pay,
    pay_records_for_placement,
    pay_variances,
    record_pay_period,
    suggest_pay_period,
)
from app.operations.registry import (
    AvailabilitySlot,
    RegistryError,
    record_assessment_result,
    register_candidate,
    register_employer,
    set_employer_tier,
)
from app.operations.requests import (
    RequestError,
    cancel_work_request,
    create_work_request,
    drop_skill_requirement,
    offer_placement,
    open_requests,
    request_requirements,
    require_skill,
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
        # The preventive half. Everything else on this page reports something
        # that has already gone wrong.
        tomorrow_flagged=[
            s for s in shifts_on(session) if s["flags"]
        ],
        tomorrow_unstaffed=unstaffed_shifts_on(session),
        # Monitoring catches this eventually; a coordinator standing in front
        # of the screen catches it now, and they are the one who can phone the
        # worker the reminder never reached.
        messaging=messaging_status(session),
        backups=backup_status(session),
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
                   wr.transport_covered, wr.status::text AS status,
                   e.business_name
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
        # What this shift asks for. Without it a coordinator sees candidates
        # excluded by "the skill filter" and no way to learn what the filter
        # is, which is the question an employer asks them next.
        requirements=request_requirements(session, request_id),
        scoreable_skills=[
            s for s in list_skills(session) if s["assessment_count"] > 0
        ],
        editable=dict(row).get("status") in ("open", "filling"),
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

    pay_records = pay_records_for_placement(session, placement_id)

    return _render(
        request, "placement.html", staff,
        p=dict(row),
        contract=(
            lambda c: {**c, "text": render_contract(c["terms"], c["contract_ref"])}
            if c else None
        )(get_contract(session, placement_id)),
        pay_records=pay_records,
        pay_suggestion=suggest_pay_period(session, placement_id),
        attendance=[dict(a) for a in attendance],
        follow_ups=[dict(f) for f in follow_ups],
        # What this journey has actually cost, when anyone has said. The
        # estimate beside it is a straight line; these are receipts.
        fares=route_history(session, placement_id),
        # Why a wage was reduced, beside the reduction itself. A total with no
        # reason is what this exists to prevent.
        deductions={
            pr["pay_id"]: deduction_lines(session, pr["pay_id"])
            for pr in pay_records if pr["deductions_rwf"]
        },
        variances={v["pay_id"]: v for v in pay_variances(session, placement_id)},
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
            # One line is enough for the ordinary case -- an advance, a
            # uniform. More than one is rare and can be recorded through the
            # API. What matters is that zero is not an option when money is
            # being taken off. See migration 040.
            deductions=(
                [{
                    "kind": str(form.get("deduction_kind") or "other"),
                    "amount_rwf": int(form.get("deductions_rwf") or 0),
                    "note": str(form.get("deduction_note") or "") or None,
                }]
                if int(form.get("deductions_rwf") or 0) else None
            ),
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
    transport_rwf: Annotated[str, Form()] = "",
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

    # Asked here because the coordinator is already on the telephone and the
    # journey is fresh. Until this was collected, the fare model was a
    # straight line, and it decides both who is offered work and the net
    # earnings figure that goes in front of a funder. See migration 039.
    if transport_rwf.strip() and placement_id:
        try:
            with session.begin_nested():
                record_transport_report(
                    session, placement_id=placement_id,
                    reported_rwf=int(transport_rwf), recorded_by=staff.staff_id,
                )
        except (TransportReportError, ValueError) as exc:
            return _back(
                target, f"Check-in recorded, but the fare was not: {exc}", "err"
            )
        return _back(target, f"Check-in recorded — fare RWF {int(transport_rwf):,}")

    return _back(target, "Check-in recorded")


# --- skills, assessments and one candidate ---------------------------------
#
# The catalogue had no browser surface at all: skills and assessments could
# only be defined through the API, and there was no candidate detail page, so
# a coordinator working where the build order says they work could not score
# anyone. See CLAUDE.md, "The catalogue".

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday")


@router.get("/catalogue", response_class=HTMLResponse)
def catalogue_page(request: Request, session: SessionDep, staff: WebStaffDep):
    return _render(
        request, "catalogue.html", staff, nav="catalogue",
        skills=list_skills(session),
        assessments=list_assessments(session),
        categories=CATEGORIES,
        methods=METHODS,
        # Defining a pass mark decides who is eligible for work. That is
        # policy, so the forms are not shown to whoever cannot act on them.
        can_author=staff.role in ADMIN_ROLES,
        **_flash(request),
    )


@router.post("/catalogue/skills")
def create_skill_form(
    session: SessionDep,
    staff: CsrfStaffDep,
    skill_code: Annotated[str, Form()],
    skill_name: Annotated[str, Form()],
    category: Annotated[str, Form()],
):
    if staff.role not in ADMIN_ROLES:
        return _back("/ui/catalogue", "Only an administrator can define skills", "err")
    try:
        with session.begin_nested():
            create_skill(
                session, skill_code=skill_code, skill_name=skill_name,
                category=category,
            )
    except CatalogueError as exc:
        return _back("/ui/catalogue", str(exc), "err")
    except Exception as exc:
        detail = str(getattr(exc, "orig", exc)).split("\n")[0].strip()
        return _back("/ui/catalogue", detail or "Could not define that skill", "err")
    return _back("/ui/catalogue", f"Skill {skill_code} defined")


@router.post("/catalogue/assessments")
def create_assessment_form(
    session: SessionDep,
    staff: CsrfStaffDep,
    skill_id: Annotated[UUID, Form()],
    title: Annotated[str, Form()],
    method: Annotated[str, Form()],
    pass_score: Annotated[int, Form()],
    max_score: Annotated[int, Form()] = 5,
    rubric: Annotated[str, Form()] = "",
):
    if staff.role not in ADMIN_ROLES:
        return _back(
            "/ui/catalogue", "Only an administrator can define assessments", "err"
        )
    try:
        with session.begin_nested():
            create_assessment(
                session, skill_id=skill_id, title=title, method=method,
                pass_score=pass_score, max_score=max_score, rubric=rubric,
            )
    except CatalogueError as exc:
        return _back("/ui/catalogue", str(exc), "err")
    except Exception as exc:
        detail = str(getattr(exc, "orig", exc)).split("\n")[0].strip()
        return _back("/ui/catalogue", detail or "Could not define that assessment", "err")
    return _back("/ui/catalogue", f"Assessment “{title}” defined")


@router.get("/candidates/{candidate_id}", response_class=HTMLResponse)
def candidate_page(
    candidate_id: UUID, request: Request, session: SessionDep,
    staff: WebStaffDep,
):
    """One person: what they can do, when they are free, and what they agreed to.

    No identity data here -- legal names, national ID and phone numbers stay
    behind the audited read. This page is the operational record, so most
    staff can open it without reaching anything residency-sensitive.
    """
    candidate = session.execute(
        text(
            """
            SELECT candidate_id, display_name, district, sector, gender,
                   status::text AS status
              FROM candidates WHERE candidate_id = :cid
            """
        ),
        {"cid": str(candidate_id)},
    ).mappings().first()
    if candidate is None:
        return _back("/ui/candidates", "No such candidate", "err")

    results = session.execute(
        text(
            """
            SELECT r.score, r.notes, r.assessed_at,
                   a.title, a.max_score, a.pass_score,
                   s.skill_code, st.full_name AS assessed_by_name,
                   r.score >= a.pass_score AS passed
              FROM assessment_results r
              JOIN assessments a ON a.assessment_id = r.assessment_id
              JOIN skills s ON s.skill_id = a.skill_id
              LEFT JOIN staff st ON st.staff_id = r.assessed_by
             WHERE r.candidate_id = :cid
             ORDER BY r.assessed_at DESC
            """
        ),
        {"cid": str(candidate_id)},
    ).mappings()

    availability = [
        {"day": DAYS[row["day_of_week"]], "start_time": row["start_time"],
         "end_time": row["end_time"]}
        for row in session.execute(
            text(
                "SELECT day_of_week, start_time, end_time FROM availability "
                "WHERE candidate_id = :cid ORDER BY day_of_week, start_time"
            ),
            {"cid": str(candidate_id)},
        ).mappings()
    ]

    consent = session.execute(
        text(
            "SELECT purpose, policy_version, granted, captured_at "
            "FROM v_current_consent WHERE candidate_id = :cid ORDER BY purpose"
        ),
        {"cid": str(candidate_id)},
    ).mappings()

    placements = session.execute(
        text(
            """
            SELECT p.placement_id, p.status::text AS status,
                   wr.title, e.business_name
              FROM placements p
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e ON e.employer_id = wr.employer_id
             WHERE p.candidate_id = :cid
             ORDER BY p.offered_at DESC
            """
        ),
        {"cid": str(candidate_id)},
    ).mappings()

    return _render(
        request, "candidate.html", staff, nav="candidates",
        candidate=dict(candidate),
        results=[dict(r) for r in results],
        availability=availability,
        consent=[dict(c) for c in consent],
        placements=[dict(p) for p in placements],
        assessments=list_assessments(session),
        **_flash(request),
    )


@router.post("/candidates/{candidate_id}/assessments")
def record_result_form(
    candidate_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    assessment_id: Annotated[UUID, Form()],
    score: Annotated[int, Form()],
    notes: Annotated[str, Form()] = "",
):
    target = f"/ui/candidates/{candidate_id}"
    try:
        scored = assessment_for_scoring(session, assessment_id)
    except CatalogueError as exc:
        return _back(target, str(exc), "err")

    try:
        # A savepoint, because the out-of-range case is refused by trigger
        # rather than by this form -- a paper-sheet import is held to the same
        # rule. A raising trigger aborts the whole transaction, so without
        # this the refusal would take the rest of the request with it.
        with session.begin_nested():
            record_assessment_result(
                session, candidate_id, assessment_id, score, staff.staff_id,
                notes or None,
            )
    except Exception as exc:
        # Show what the database said rather than a generic failure: "score 9
        # exceeds the maximum of 5" is a correctable mistake, "error" is not.
        detail = str(getattr(exc, "orig", exc)).split("\n")[0].strip()
        return _back(target, detail or "Could not record that score", "err")

    verdict = "passed" if score >= scored["pass_score"] else "below the pass mark"
    return _back(
        target,
        f"{scored['skill_code']} {score}/{scored['max_score']} — {verdict}",
    )


@router.post("/requests/{request_id}/skills")
def require_skill_form(
    request_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
    skill_code: Annotated[str, Form()],
    min_score: Annotated[int, Form()] = 3,
):
    """Attach a requirement. Nothing in the browser could, so matching
    filter 1 never engaged for anyone working where the build order puts
    them."""
    target = f"/ui/requests/{request_id}"
    try:
        with session.begin_nested():
            require_skill(session, request_id, skill_code, min_score)
    except RequestError as exc:
        return _back(target, str(exc), "err")
    return _back(target, f"Now requires {skill_code} at {min_score} or above")


@router.post("/requests/{request_id}/skills/{skill_id}/remove")
def drop_skill_form(
    request_id: UUID,
    skill_id: UUID,
    session: SessionDep,
    staff: CsrfStaffDep,
):
    target = f"/ui/requests/{request_id}"
    try:
        with session.begin_nested():
            drop_skill_requirement(session, request_id, skill_id)
    except RequestError as exc:
        return _back(target, str(exc), "err")
    return _back(target, "Requirement removed")


@router.get("/tomorrow", response_class=HTMLResponse)
def tomorrow_page(
    request: Request, session: SessionDep, staff: WebStaffDep,
    day: str | None = None,
):
    """What is due to happen, and what is still unresolved about it.

    The one preventive view. An invoked guarantee costs real money, so the
    cheapest one is the one that never happens.
    """
    today = kigali_today()
    try:
        target = date.fromisoformat(day) if day else today + timedelta(days=1)
    except ValueError:
        return _back("/ui/tomorrow", f"{day!r} is not a date", "err")

    if target == today:
        heading = "Today"
    elif target == today + timedelta(days=1):
        heading = "Tomorrow"
    else:
        heading = target.strftime("%A %d %B")

    return _render(
        request, "tomorrow.html", staff, nav="tomorrow",
        heading=heading,
        day=target.isoformat(),
        prev_day=(target - timedelta(days=1)).isoformat(),
        next_day=(target + timedelta(days=1)).isoformat(),
        shifts=shifts_on(session, target),
        unstaffed=unstaffed_shifts_on(session, target),
        **_flash(request),
    )


@router.get("/placements/{placement_id}/cover", response_class=HTMLResponse)
def cover_page(
    placement_id: UUID, request: Request, session: SessionDep,
    staff: WebStaffDep,
):
    """Who can still get to a shift whose worker did not arrive.

    The guarantee, as a screen. A coordinator opens this with an unhappy
    employer on the telephone, so it answers the employer's question -- who
    is coming and when -- rather than the general matching question of who
    is the better worker.
    """
    failed = session.execute(
        text(
            """
            SELECT p.placement_id, c.display_name, wr.title,
                   wr.shift_start, wr.shift_end, e.business_name,
                   (SELECT min(a.confirmed_at) FROM attendance a
                     WHERE a.placement_id = p.placement_id AND NOT a.present)
                       AS invoked_at
              FROM placements p
              JOIN candidates c ON c.candidate_id = p.candidate_id
              JOIN work_requests wr ON wr.request_id = p.request_id
              JOIN employers e ON e.employer_id = wr.employer_id
             WHERE p.placement_id = :pid
            """
        ),
        {"pid": str(placement_id)},
    ).mappings().first()
    if failed is None:
        return _back("/ui/", "No such placement", "err")
    if failed["invoked_at"] is None:
        return _back(
            f"/ui/placements/{placement_id}",
            "Nobody has been marked absent on this placement", "err",
        )

    return _render(
        request, "cover.html", staff, nav="dashboard",
        failed=dict(failed),
        result=find_cover_for(session, placement_id),
        **_flash(request),
    )
