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
from app.config import get_settings
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
    """Gate on the per-account identity grant, plus a second factor.

    The grant is deliberately not a role check. Seniority does not imply
    entitlement to read national ID numbers -- somebody grants it explicitly,
    and the audit trail records the read either way.

    On top of that, identity data requires MFA on the *current session*. A
    password alone is enough for operational work; it is not enough to reach a
    national ID number. The errors say which of the two is missing, because
    "403" with no explanation turns into a support ticket.
    """
    if not staff.can_view_identity:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this account does not have identity data access",
        )

    if not get_settings().require_mfa_for_identity:
        return staff

    if not staff.mfa_enrolled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "identity data requires a second factor: enrol one at "
                "POST /auth/totp/enrol"
            ),
        )
    if not staff.mfa_satisfied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "identity data requires a second factor on this session: "
                "present a code at POST /auth/mfa"
            ),
        )
    return staff


def require_role(*roles: str):
    """Dependency factory gating an endpoint on staff role."""

    def _check(staff: StaffDep) -> AuthenticatedStaff:
        if staff.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires one of: {', '.join(roles)}",
            )
        return staff

    return _check


AdminDep = Annotated[AuthenticatedStaff, Depends(require_role("owner", "admin"))]


IdentityStaffDep = Annotated[AuthenticatedStaff, Depends(require_identity_access)]
