"""Employer browser-session dependencies.

Separate cookie name and separate session table from staff, so the two
principals cannot be confused. A staff cookie will not resolve here and an
employer cookie will not resolve in the staff path.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status

from app.config import Residency, get_settings
from app.deps import SessionDep
from app.employer_auth import (
    EmployerAuthError,
    EmployerPrincipal,
    authenticate_employer,
)

EMPLOYER_COOKIE = "akazi_employer"


class EmployerLoginRequired(Exception):
    pass


def set_employer_cookie(response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        EMPLOYER_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.data_residency is not Residency.LOCAL_DEV,
        path="/",
    )


def clear_employer_cookie(response) -> None:
    response.delete_cookie(EMPLOYER_COOKIE, path="/")


def current_employer(
    session: SessionDep,
    akazi_employer: Annotated[str | None, Cookie()] = None,
) -> EmployerPrincipal:
    if not akazi_employer:
        raise EmployerLoginRequired()
    try:
        return authenticate_employer(session, akazi_employer)
    except EmployerAuthError as exc:
        raise EmployerLoginRequired() from exc


EmployerDep = Annotated[EmployerPrincipal, Depends(current_employer)]


async def verify_employer_csrf(
    request: Request, employer: EmployerDep
) -> EmployerPrincipal:
    form = await request.form()
    if not secrets.compare_digest(
        str(form.get("csrf_token", "")), employer.csrf_token
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or missing CSRF token -- reload the page and retry",
        )
    return employer


EmployerCsrfDep = Annotated[
    EmployerPrincipal, Depends(verify_employer_csrf)
]
