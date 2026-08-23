"""Login and session management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from app.auth import AuthError, login, logout
from app.deps import SessionDep, StaffDep, _bearer

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
    )


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
    }
