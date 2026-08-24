"""Login and session management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import AuthError, change_own_password, login, logout
from app.deps import SessionDep, StaffDep, _bearer
from app.mfa import (
    MFAError,
    begin_enrolment,
    confirm_enrolment,
    elevate_session,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    phone: str
    password: str


class LoginResponse(BaseModel):
    token: str
    staff_id: str
    full_name: str
    role: str
    can_view_identity: bool
    # What the client still has to do before the session is fully usable.
    mfa_required: bool
    must_change_password: bool


class MFACode(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12)


@router.post("/login")
def login_endpoint(
    body: LoginRequest,
    session: SessionDep,
    user_agent: Annotated[str | None, Header()] = None,
):
    try:
        token = login(session, body.phone, body.password, user_agent)
    except AuthError as exc:
        # One message for every failure: a distinct "no such account" or
        # "account locked" would let an attacker enumerate staff.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    from app.auth import authenticate

    staff = authenticate(session, token)
    return LoginResponse(
        token=token,
        staff_id=str(staff.staff_id),
        full_name=staff.full_name,
        role=staff.role,
        can_view_identity=staff.can_view_identity,
        # Enrolled but not yet presented on this session.
        mfa_required=staff.mfa_enrolled and not staff.mfa_satisfied,
        must_change_password=staff.must_change_password,
    )


@router.post("/totp/enrol")
def enrol_totp(session: SessionDep, staff: StaffDep):
    """Start second-factor enrolment.

    The secret is returned exactly once. No endpoint will show it again -- if
    it is lost before the authenticator app is set up, an owner resets the
    enrolment and it starts over.
    """
    try:
        enrolment = begin_enrolment(session, staff.staff_id)
    except MFAError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "secret": enrolment.secret,
        "otpauth_uri": enrolment.otpauth_uri,
        "next": "confirm with POST /auth/totp/confirm",
    }


@router.post("/totp/confirm")
def confirm_totp(body: MFACode, session: SessionDep, staff: StaffDep):
    """Prove the authenticator works before the factor becomes required."""
    try:
        confirm_enrolment(session, staff.staff_id, body.code)
    except MFAError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"enrolled": True}


@router.post("/mfa")
def satisfy_mfa(body: MFACode, session: SessionDep, staff: StaffDep):
    """Elevate this session with a second factor.

    Per session, deliberately: a code presented on a laptop does not elevate a
    token someone else is holding.
    """
    try:
        elevate_session(session, staff.staff_id, staff.session_id, body.code)
    except MFAError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"mfa_satisfied": True}


@router.post("/password")
def change_password(body: PasswordChange, session: SessionDep, staff: StaffDep):
    """Change your own password. Every other session is revoked.

    If the reason for changing is that the old password leaked, leaving the
    attacker's session alive defeats the exercise.
    """
    try:
        revoked = change_own_password(
            session,
            staff.staff_id,
            body.current_password,
            body.new_password,
            keep_session_id=staff.session_id,
        )
    except AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"changed": True, "other_sessions_revoked": revoked}


@router.post("/logout")
def logout_endpoint(
    session: SessionDep,
    staff: StaffDep,
    authorization: Annotated[str | None, Header()] = None,
):
    logout(session, _bearer(authorization))
    return {"logged_out": True}


@router.get("/me")
def me(staff: StaffDep):
    return {
        "staff_id": staff.staff_id,
        "full_name": staff.full_name,
        "role": staff.role,
        "can_view_identity": staff.can_view_identity,
        "mfa_enrolled": staff.mfa_enrolled,
        "mfa_satisfied": staff.mfa_satisfied,
        "must_change_password": staff.must_change_password,
    }
