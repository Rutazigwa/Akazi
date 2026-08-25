"""The employer-facing dashboard (weeks 7-12).

Post a shift, see who is assigned, confirm attendance, rate the worker, reorder.
Responsive web -- there is no employer app and there is not going to be one.

Every handler passes `employer.employer_id` from the session into the operations
layer, which filters on it. The employer id never comes from a form or a URL.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.deps import SessionDep
from app.employer_auth import (
    EmployerAuthError,
    change_employer_password,
    employer_login,
    employer_logout,
)
from app.operations.attendance import AttendanceError
from app.operations.employer_portal import (
    EmployerPortalError,
    assigned_workers,
    confirm_attendance,
    my_requests,
    post_request,
    rate_worker,
    reliability_summary,
    reorder,
)
from app.operations.requests import RequestError
from app.web.employer_deps import (
    EmployerCsrfDep,
    EmployerDep,
    clear_employer_cookie,
    set_employer_cookie,
)

router = APIRouter(prefix="/employer", include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _render(request: Request, name: str, employer, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request, f"employer/{name}",
        {"employer": employer, "today": date.today().isoformat(), **ctx},
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


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        request, "employer/login.html", {"employer": None, **_flash(request)}
    )


@router.post("/login")
def do_login(
    request: Request,
    session: SessionDep,
    phone: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    try:
        token = employer_login(
            session, phone, password, request.headers.get("user-agent")
        )
    except EmployerAuthError:
        return _back("/employer/login", "Invalid credentials", "err")

    from app.employer_auth import authenticate_employer

    principal = authenticate_employer(session, token)
    target = (
        "/employer/password" if principal.must_change_password else "/employer/"
    )
    response = RedirectResponse(target, status_code=303)
    set_employer_cookie(response, token)
    return response


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, employer: EmployerDep):
    return _render(request, "password.html", employer, **_flash(request))


@router.post("/password")
def change_password(
    session: SessionDep,
    employer: EmployerCsrfDep,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    try:
        change_employer_password(
            session, employer.contact_id, current_password, new_password,
            keep_session_id=employer.session_id,
        )
    except EmployerAuthError as exc:
        return _back("/employer/password", str(exc), "err")
    return _back("/employer/", "Password changed")


@router.post("/logout")
def do_logout(
    session: SessionDep,
    employer: EmployerCsrfDep,
    akazi_employer: Annotated[str | None, Cookie()] = None,
):
    if akazi_employer:
        employer_logout(session, akazi_employer)
    response = RedirectResponse("/employer/login", status_code=303)
    clear_employer_cookie(response)
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: SessionDep, employer: EmployerDep):
    return _render(
        request, "home.html", employer, nav="home",
        summary=reliability_summary(session, employer.employer_id),
        workers=assigned_workers(session, employer.employer_id),
        requests=my_requests(session, employer.employer_id),
        **_flash(request),
    )


@router.get("/post", response_class=HTMLResponse)
def post_form(request: Request, employer: EmployerDep):
    return _render(request, "post.html", employer, nav="post", **_flash(request))


@router.post("/post")
async def do_post(request: Request, session: SessionDep, employer: EmployerCsrfDep):
    form = await request.form()

    def when(name):
        raw = str(form.get(name, "")).strip()
        return time.fromisoformat(raw) if raw else None

    try:
        post_request(
            session,
            # From the session, never the form.
            employer.employer_id,
            title=str(form["title"]),
            work_type=str(form["work_type"]),
            headcount=int(form["headcount"]),
            starts_on=date.fromisoformat(str(form["starts_on"])),
            pay_rwf=int(form["pay_rwf"]),
            pay_unit=str(form["pay_unit"]),
            shift_start=when("shift_start"),
            shift_end=when("shift_end"),
            transport_covered=form.get("transport_covered") == "true",
            meals_provided=form.get("meals_provided") == "true",
            safety_notes=str(form.get("safety_notes", "")).strip() or None,
        )
    except (RequestError, ValueError) as exc:
        return _back("/employer/post", str(exc), "err")
    return _back("/employer/", "Posted — we will fill it and tell you who is coming")


@router.get("/workers/{placement_id}", response_class=HTMLResponse)
def worker(
    placement_id: UUID, request: Request, session: SessionDep, employer: EmployerDep
):
    rows = assigned_workers(session, employer.employer_id)
    match = next(
        (w for w in rows if str(w["placement_id"]) == str(placement_id)), None
    )
    if match is None:
        return _back("/employer/", "No such worker", "err")

    attendance = session.execute(
        text(
            """
            SELECT a.work_date, a.present, a.hours_worked, a.confirmed_by,
                   ec.full_name AS contact_name
              FROM attendance a
              LEFT JOIN employer_contacts ec
                     ON ec.contact_id = a.confirmed_by_contact
             WHERE a.placement_id = :pid
             ORDER BY a.work_date DESC
            """
        ),
        {"pid": str(placement_id)},
    ).mappings()

    return _render(
        request, "worker.html", employer,
        w=match, attendance=[dict(a) for a in attendance], **_flash(request),
    )


@router.post("/workers/{placement_id}/attendance")
def attendance(
    placement_id: UUID,
    session: SessionDep,
    employer: EmployerCsrfDep,
    work_date: Annotated[str, Form()],
    present: Annotated[str, Form()],
    hours_worked: Annotated[str, Form()] = "",
    absence_reason: Annotated[str, Form()] = "",
):
    try:
        invocation = confirm_attendance(
            session,
            employer.employer_id,
            employer.contact_id,
            placement_id,
            date.fromisoformat(work_date),
            present == "true",
            float(hours_worked) if hours_worked else None,
            absence_reason or None,
        )
    except (EmployerPortalError, AttendanceError) as exc:
        return _back(f"/employer/workers/{placement_id}", str(exc), "err")

    if invocation is None:
        return _back(f"/employer/workers/{placement_id}", "Thank you — recorded")
    return _back(
        "/employer/",
        "Recorded. We are covering this slot free of charge and will confirm "
        f"who is coming by {invocation.due_by.strftime('%H:%M on %d %b')}.",
    )


@router.post("/workers/{placement_id}/rate")
def rate(
    placement_id: UUID,
    session: SessionDep,
    employer: EmployerCsrfDep,
    rating: Annotated[int, Form()],
    note: Annotated[str, Form()] = "",
):
    try:
        rate_worker(
            session, employer.employer_id, placement_id, rating, note or None
        )
    except EmployerPortalError as exc:
        return _back(f"/employer/workers/{placement_id}", str(exc), "err")
    return _back(f"/employer/workers/{placement_id}", "Rating saved")


@router.post("/requests/{request_id}/reorder")
def do_reorder(
    request_id: UUID,
    session: SessionDep,
    employer: EmployerCsrfDep,
    starts_on: Annotated[str, Form()],
):
    try:
        reorder(
            session, employer.employer_id, request_id,
            date.fromisoformat(starts_on),
        )
    except (EmployerPortalError, RequestError) as exc:
        return _back("/employer/", str(exc), "err")
    return _back("/employer/", "Ordered again — we will fill it")
