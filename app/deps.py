"""Shared FastAPI dependencies: the database session and the acting staff member.

Order matters here. The session is opened first, the bearer token is resolved
against it, and only then is `app.staff_id` stamped on the transaction. That
stamp is what the audit triggers read, so an unauthenticated request cannot
write an attributed row -- there is nothing to attribute it to.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
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


# Paths reachable while a password change is outstanding. Deliberately tiny:
# enough to change the password or leave, nothing else.
PASSWORD_CHANGE_EXEMPT = frozenset(
    {"/auth/password", "/auth/logout", "/auth/me"}
)


# What a readonly account may still POST to. Managing your own session is not
# an operational write: without these, a readonly account could not complete a
# login, enrol a second factor, or spend its temporary password.
READONLY_ALLOWED_WRITES = frozenset(
    {
        "/auth/login",
        "/auth/logout",
        "/auth/password",
        "/auth/mfa",
        "/auth/totp/enrol",
        "/auth/totp/confirm",
    }
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _refuse_readonly_writes(staff: AuthenticatedStaff, request: Request) -> None:
    """A readonly account may look at anything it is entitled to, and change
    nothing.

    Enforced here rather than route by route. The role existed in the enum,
    was assignable, and was shown back on login, but nothing anywhere checked
    it -- a readonly account could register candidates, promote employers and
    post work requests. Someone handed that role believing they could only
    look had, in fact, full write access.

    Keying on the HTTP method rather than a list of write endpoints is the
    point: "changes nothing" maps exactly onto method semantics, so a route
    added tomorrow is covered without anyone remembering to add it. A GET that
    writes would slip through, but a GET that writes is already a bug.
    """
    if staff.role != "readonly":
        return
    if request.method in SAFE_METHODS:
        return
    if request.url.path in READONLY_ALLOWED_WRITES:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="this account is readonly and cannot change anything",
    )


def current_staff(
    request: Request,
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

    # A temporary password must be spent on choosing a real one. Without this
    # the flag was decoration: an administrator generates a password, hands it
    # over, and both of them can use it indefinitely.
    if (
        staff.must_change_password
        and request.url.path not in PASSWORD_CHANGE_EXEMPT
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "this password is temporary and must be changed before "
                "anything else: POST /auth/password"
            ),
        )

    _refuse_readonly_writes(staff, request)

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
