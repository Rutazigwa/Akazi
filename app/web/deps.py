"""Browser-session dependencies for the admin UI.

The JSON API authenticates with a bearer token. The UI cannot -- a browser will
not attach a bearer header to a form post -- so it uses a cookie, and a cookie
brings CSRF with it: the browser sends it automatically on any request it is
tricked into making, including a form POST from another site.

Two defences, because the consequence of getting this wrong is somebody else's
national ID number:

  SameSite=Strict   the browser should not send the cookie cross-site at all
  a CSRF token      checked on every state-changing form, in case it does

The cookie is HttpOnly so that a script cannot read the session token, and
Secure whenever the deployment is not local development.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.auth import AuthenticatedStaff, AuthError, authenticate
from app.config import Residency, get_settings
from app.deps import SessionDep

SESSION_COOKIE = "akazi_session"


class LoginRequired(Exception):
    """Raised when a browser request has no usable session."""


def set_session_cookie(response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        # Plain HTTP is only ever local development; anywhere else this cookie
        # must not travel unencrypted.
        secure=settings.data_residency is not Residency.LOCAL_DEV,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_web_staff(
    session: SessionDep,
    akazi_session: Annotated[str | None, Cookie()] = None,
) -> AuthenticatedStaff:
    if not akazi_session:
        raise LoginRequired()
    try:
        staff = authenticate(session, akazi_session)
    except AuthError as exc:
        raise LoginRequired() from exc

    from sqlalchemy import text

    session.execute(
        text("SELECT set_config('app.staff_id', :sid, true)"),
        {"sid": str(staff.staff_id)},
    )
    return staff


WebStaffDep = Annotated[AuthenticatedStaff, Depends(current_web_staff)]


async def verify_csrf(request: Request, staff: WebStaffDep) -> AuthenticatedStaff:
    """Reject a form post whose token does not match the session's.

    Compared in constant time. The comparison is not really a secret-leak risk
    here, but a timing-safe compare costs nothing and removes the question.
    """
    form = await request.form()
    submitted = str(form.get("csrf_token", ""))

    if not staff.csrf_token or not secrets.compare_digest(
        submitted, staff.csrf_token
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or missing CSRF token -- reload the page and retry",
        )
    return staff


CsrfStaffDep = Annotated[AuthenticatedStaff, Depends(verify_csrf)]


def redirect_to_login(request: Request) -> RedirectResponse:
    """Send an unauthenticated browser to the login page, remembering where."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    suffix = f"?next={target}" if target not in ("/", "/ui/login") else ""
    return RedirectResponse(f"/ui/login{suffix}", status_code=303)
