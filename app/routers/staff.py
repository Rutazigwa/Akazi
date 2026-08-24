"""Staff administration. Owner and admin only.

This is the account lifecycle that was previously only reachable by running a
script on the server: create a coordinator, reset a forgotten password, clear a
lost second factor, grant or withdraw identity access, cut someone's sessions.

Two rules run through all of it:

**Identity access is granted deliberately.** It defaults off, it is not implied
by role, and changing it revokes the target's live sessions so the change takes
effect now rather than whenever their token expires.

**Every privileged change writes an audit row.** Granting someone access to
national ID numbers is exactly as significant as reading one.
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import AuthError, revoke_all_sessions, set_password
from app.deps import AdminDep, SessionDep
from app.mfa import reset_enrolment

router = APIRouter(prefix="/staff", tags=["staff"])

ROLES = ("coordinator", "supervisor", "admin", "owner", "readonly")


class NewStaff(BaseModel):
    full_name: str
    phone: str
    role: str = Field(pattern="^(coordinator|supervisor|admin|owner|readonly)$")
    email: str | None = None
    can_view_identity: bool = False


class StaffUpdate(BaseModel):
    role: str | None = Field(
        default=None, pattern="^(coordinator|supervisor|admin|owner|readonly)$"
    )
    can_view_identity: bool | None = None


def _audit(session, actor: UUID, target: UUID, detail: dict) -> None:
    session.execute(
        text(
            """
            INSERT INTO audit_log (staff_id, table_name, record_id, action,
                                   detail)
            VALUES (:actor, 'staff', :target, 'update', :detail)
            """
        ),
        {"actor": str(actor), "target": str(target), "detail": _json(detail)},
    )


def _json(value: dict) -> str:
    import json

    return json.dumps(value)


@router.get("")
def list_staff(session: SessionDep, admin: AdminDep):
    rows = session.execute(
        text(
            """
            SELECT staff_id, full_name, phone, email, role::text AS role,
                   can_view_identity, is_active,
                   (totp_enrolled_at IS NOT NULL) AS mfa_enrolled,
                   must_change_password, locked_until, created_at
              FROM staff ORDER BY full_name
            """
        )
    ).mappings()
    return {"staff": [dict(r) for r in rows]}


@router.post("", status_code=201)
def create_staff(body: NewStaff, session: SessionDep, admin: AdminDep):
    """Create an account with a single-use temporary password.

    The temporary password is returned once and must be changed at first login.
    It is generated here rather than chosen by the admin: an administrator who
    picks the password knows it, and "temporary" passwords chosen by people are
    reused across accounts.
    """
    temporary = secrets.token_urlsafe(16)

    existing = session.execute(
        text("SELECT 1 FROM staff WHERE phone = :phone"), {"phone": body.phone}
    ).first()
    if existing:
        raise HTTPException(
            status_code=409, detail="a staff member with that phone exists"
        )

    staff_id = session.execute(
        text(
            """
            INSERT INTO staff (full_name, phone, email, role, can_view_identity)
            VALUES (:name, :phone, :email, CAST(:role AS staff_role), :identity)
            RETURNING staff_id
            """
        ),
        {
            "name": body.full_name,
            "phone": body.phone,
            "email": body.email,
            "role": body.role,
            "identity": body.can_view_identity,
        },
    ).scalar_one()

    set_password(session, staff_id, temporary, must_change=True)
    _audit(session, admin.staff_id, staff_id, {
        "event": "staff_created",
        "role": body.role,
        "can_view_identity": body.can_view_identity,
    })

    return {
        "staff_id": staff_id,
        "temporary_password": temporary,
        "note": "shown once; must be changed at first login",
    }


@router.patch("/{staff_id}")
def update_staff(
    staff_id: UUID, body: StaffUpdate, session: SessionDep, admin: AdminDep
):
    """Change role or identity access.

    Changing identity access revokes the target's sessions: withdrawing access
    has to take effect now, not whenever their current token happens to expire.
    """
    current = session.execute(
        text(
            "SELECT role::text AS role, can_view_identity FROM staff "
            "WHERE staff_id = :sid"
        ),
        {"sid": str(staff_id)},
    ).mappings().first()
    if current is None:
        raise HTTPException(status_code=404, detail="staff member not found")

    new_role = body.role or current["role"]
    new_identity = (
        current["can_view_identity"]
        if body.can_view_identity is None
        else body.can_view_identity
    )

    session.execute(
        text(
            """
            UPDATE staff SET role = CAST(:role AS staff_role),
                             can_view_identity = :identity
             WHERE staff_id = :sid
            """
        ),
        {"role": new_role, "identity": new_identity, "sid": str(staff_id)},
    )

    revoked = 0
    if new_identity != current["can_view_identity"]:
        revoked = revoke_all_sessions(session, staff_id)

    _audit(session, admin.staff_id, staff_id, {
        "event": "staff_updated",
        "role": {"from": current["role"], "to": new_role},
        "can_view_identity": {
            "from": current["can_view_identity"], "to": new_identity
        },
    })
    return {"staff_id": staff_id, "sessions_revoked": revoked}


@router.post("/{staff_id}/reset-password")
def reset_password(staff_id: UUID, session: SessionDep, admin: AdminDep):
    """Issue a single-use temporary password and cut every live session."""
    exists = session.execute(
        text("SELECT 1 FROM staff WHERE staff_id = :sid"), {"sid": str(staff_id)}
    ).first()
    if not exists:
        raise HTTPException(status_code=404, detail="staff member not found")

    temporary = secrets.token_urlsafe(16)
    try:
        set_password(session, staff_id, temporary, must_change=True)
    except AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    revoked = revoke_all_sessions(session, staff_id)
    _audit(session, admin.staff_id, staff_id, {
        "event": "password_reset", "sessions_revoked": revoked,
    })
    return {
        "temporary_password": temporary,
        "sessions_revoked": revoked,
        "note": "shown once; must be changed at first login",
    }


@router.post("/{staff_id}/reset-mfa")
def reset_mfa(staff_id: UUID, session: SessionDep, admin: AdminDep):
    """Clear a second factor -- for a lost phone. Sessions are cut with it."""
    reset_enrolment(session, staff_id)
    _audit(session, admin.staff_id, staff_id, {"event": "mfa_reset"})
    return {"staff_id": staff_id, "mfa_enrolled": False}


@router.post("/{staff_id}/deactivate")
def deactivate(staff_id: UUID, session: SessionDep, admin: AdminDep):
    """Withdraw access immediately.

    Staff rows are never deleted: they are referenced by candidates.registered_by,
    assessment_results.assessed_by and audit_log.staff_id, and removing one would
    orphan the record of who did what.
    """
    if staff_id == admin.staff_id:
        raise HTTPException(
            status_code=409,
            detail="you cannot deactivate your own account",
        )

    updated = session.execute(
        text(
            """
            UPDATE staff SET is_active = false, deactivated_at = now()
             WHERE staff_id = :sid AND is_active
            RETURNING staff_id
            """
        ),
        {"sid": str(staff_id)},
    ).scalar_one_or_none()
    if updated is None:
        raise HTTPException(
            status_code=404, detail="staff member not found or already inactive"
        )

    revoked = revoke_all_sessions(session, staff_id)
    _audit(session, admin.staff_id, staff_id, {
        "event": "deactivated", "sessions_revoked": revoked,
    })
    return {"staff_id": staff_id, "sessions_revoked": revoked}


@router.get("/audit/integrity")
def audit_integrity(session: SessionDep, admin: AdminDep):
    """Verify the audit log has not been tampered with.

    Each entry is hash-chained to the one before it, so an edited or removed row
    breaks every link after it. Publish `head_hash` somewhere off this server:
    once a hash is recorded elsewhere, no local rewrite of history can match it.
    """
    broken = session.execute(
        text("SELECT * FROM verify_audit_chain()")
    ).mappings().first()
    head = session.execute(
        text("SELECT * FROM audit_chain_head()")
    ).mappings().first()

    return {
        "intact": broken is None,
        "broken_at": dict(broken) if broken else None,
        "entries": head["entries"] if head else 0,
        "head_audit_id": head["audit_id"] if head else None,
        "head_hash": head["entry_hash"] if head else None,
    }
