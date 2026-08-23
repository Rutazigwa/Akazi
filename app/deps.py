"""Shared FastAPI dependencies: the database session and the acting staff member.

Order matters here. The session is opened first, the bearer token is resolved
against it, and only then is `app.staff_id` stamped on the transaction. That
stamp is what the audit triggers read, so an unauthenticated request cannot
write an attributed row -- there is nothing to attribute it to.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import AuthenticatedStaff, AuthError, authenticate
from app.db import session_scope


def db_session():
    with session_scope() as session:
        yield session


SessionDep = Annotated[Session, Depends(db_session)]


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()


def current_staff(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthenticatedStaff:
    token = _bearer(authorization)
    try:
        staff = authenticate(session, token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Everything written from here on is attributable to this person.
    session.execute(
        text("SELECT set_config('app.staff_id', :sid, true)"),
        {"sid": str(staff.staff_id)},
    )
    return staff


StaffDep = Annotated[AuthenticatedStaff, Depends(current_staff)]


def require_identity_access(staff: StaffDep) -> AuthenticatedStaff:
    """Gate on the per-account identity grant.

    Deliberately not a role check. Seniority does not imply entitlement to read
    national ID numbers -- somebody grants it explicitly, and the audit trail
    records the read either way.
    """
    if not staff.can_view_identity:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this account does not have identity data access",
        )
    return staff


IdentityStaffDep = Annotated[AuthenticatedStaff, Depends(require_identity_access)]
