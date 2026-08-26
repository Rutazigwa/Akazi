"""Staff authentication and role gating.

Two decisions worth stating, because both are cheaper to make now than later:

Tokens are opaque and stored in the database, not JWTs. The deciding factor is
revocation. This system holds national ID numbers; when a coordinator leaves,
their access must stop the moment someone says so, not whenever a signed token
happens to expire. Only the SHA-256 of the token is stored, so a leaked backup
yields no usable session.

Identity access is a per-account grant (staff.can_view_identity), not a
consequence of seniority. An owner is not automatically entitled to read
national ID numbers -- somebody has to decide that, and the audit trail records
who read what regardless.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.orm import Session

SESSION_LIFETIME = timedelta(hours=12)

# A coordinator's account is the cheapest route to a national ID number, so
# repeated failures lock it rather than relying on password strength alone.
MAX_FAILED_LOGINS = 5
LOCKOUT_DURATION = timedelta(minutes=15)

_hasher = PasswordHasher()


class AuthError(Exception):
    """Authentication failed. The message is deliberately vague to callers."""


@dataclass(frozen=True)
class AuthenticatedStaff:
    staff_id: UUID
    full_name: str
    role: str
    can_view_identity: bool
    session_id: UUID
    csrf_token: str | None = None
    # Whether a second factor was presented on THIS session, and whether the
    # account has one at all. Identity access requires both.
    mfa_satisfied: bool = False
    mfa_enrolled: bool = False
    must_change_password: bool = False


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


MIN_PASSWORD_LENGTH = 12


def set_password(
    session: Session,
    staff_id: UUID,
    password: str,
    must_change: bool = False,
) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    session.execute(
        text(
            """
            UPDATE staff
               SET password_hash = :h,
                   password_changed_at = now(),
                   must_change_password = :must_change,
                   failed_login_count = 0,
                   locked_until = NULL
             WHERE staff_id = :sid
            """
        ),
        {
            "h": hash_password(password),
            "sid": str(staff_id),
            "must_change": must_change,
        },
    )


def change_own_password(
    session: Session, staff_id: UUID, current_password: str, new_password: str,
    keep_session_id: UUID | None = None,
) -> int:
    """Change a password after re-verifying the current one.

    Every other session is revoked. If the reason for changing is that the old
    password leaked, leaving the attacker's session alive defeats the exercise.
    Returns how many sessions were cut.
    """
    stored = session.execute(
        text("SELECT password_hash FROM staff WHERE staff_id = :sid"),
        {"sid": str(staff_id)},
    ).scalar_one_or_none()
    if stored is None:
        raise AuthError("invalid credentials")

    try:
        _hasher.verify(stored, current_password)
    except VerifyMismatchError:
        raise AuthError("invalid credentials") from None

    if new_password == current_password:
        raise AuthError("the new password must differ from the current one")

    set_password(session, staff_id, new_password)

    return session.execute(
        text(
            """
            UPDATE staff_sessions SET revoked_at = now()
             WHERE staff_id = :sid AND revoked_at IS NULL
               AND expires_at > now()
               AND (CAST(:keep AS uuid) IS NULL
                    OR session_id <> CAST(:keep AS uuid))
            """
        ),
        {"sid": str(staff_id), "keep": str(keep_session_id) if keep_session_id else None},
    ).rowcount


def login(
    session: Session, phone: str, password: str, user_agent: str | None = None
) -> str:
    """Verify credentials and issue a session token.

    Returns the plaintext token, which is never stored and cannot be recovered.
    Every failure path raises the same error with the same message: telling an
    attacker whether the account exists, is locked, or simply has the wrong
    password hands them a user-enumeration oracle.
    """
    row = session.execute(
        text(
            """
            SELECT staff_id, password_hash, is_active,
                   failed_login_count, locked_until
              FROM staff
             WHERE phone = :phone
            """
        ),
        {"phone": phone},
    ).mappings().first()

    generic = AuthError("invalid credentials")

    if row is None or not row["is_active"] or row["password_hash"] is None:
        # Still spend the time hashing, so a missing account is not detectably
        # faster than a wrong password.
        _hasher.hash(password)
        raise generic

    if row["locked_until"] is not None:
        locked = session.execute(
            text("SELECT locked_until > now() FROM staff WHERE staff_id = :sid"),
            {"sid": row["staff_id"]},
        ).scalar_one()
        if locked:
            raise generic

    try:
        _hasher.verify(row["password_hash"], password)
    except VerifyMismatchError:
        _register_failure(session, row["staff_id"], row["failed_login_count"])
        raise generic from None

    session.execute(
        text(
            "UPDATE staff SET failed_login_count = 0, locked_until = NULL "
            "WHERE staff_id = :sid"
        ),
        {"sid": row["staff_id"]},
    )
    return _issue_token(session, row["staff_id"], user_agent)


def _register_failure(session: Session, staff_id: UUID, current: int) -> None:
    """Count one failure and lock if that was the last allowed.

    A single atomic UPDATE, incrementing from the stored value rather than from
    one read earlier. The read-then-write version defeated the lockout
    completely: an attacker sending attempts concurrently had every one of them
    read the same count and write the same number back, so the counter never
    reached the limit. Verified -- five simultaneous wrong passwords left the
    account unlocked.

    That matters more here than anywhere else in the system. Lockout is what
    stands between a weak or leaked coordinator password and a national ID
    number, and an attacker is precisely the caller who would send requests in
    parallel.

    `current` is no longer used for the decision; it is kept only so callers
    read naturally.
    """
    del current  # deliberately unused: the database does the arithmetic

    session.execute(
        text(
            """
            UPDATE staff
               SET failed_login_count =
                       CASE WHEN failed_login_count + 1 >= :limit THEN 0
                            ELSE failed_login_count + 1 END,
                   locked_until =
                       CASE WHEN failed_login_count + 1 >= :limit
                            THEN now() + CAST(:lockout AS interval)
                            ELSE locked_until END
             WHERE staff_id = :sid
            """
        ),
        {
            "sid": str(staff_id),
            "limit": MAX_FAILED_LOGINS,
            "lockout": f"{LOCKOUT_DURATION.seconds} seconds",
        },
    )


def _issue_token(
    session: Session, staff_id: UUID, user_agent: str | None
) -> str:
    token = secrets.token_urlsafe(32)
    session.execute(
        text(
            """
            INSERT INTO staff_sessions (staff_id, token_sha256, expires_at,
                                        user_agent, csrf_token)
            VALUES (:sid, :digest, now() + CAST(:lifetime AS interval), :ua,
                    :csrf)
            """
        ),
        {
            "sid": str(staff_id),
            "digest": _token_digest(token),
            "lifetime": f"{int(SESSION_LIFETIME.total_seconds())} seconds",
            "ua": (user_agent or "")[:200] or None,
            # Only the browser UI uses this; the JSON API sends a bearer token,
            # which a browser never attaches on its own.
            "csrf": secrets.token_urlsafe(32),
        },
    )
    return token


def authenticate(session: Session, token: str) -> AuthenticatedStaff:
    """Resolve a bearer token to the staff member it belongs to.

    Deactivating a staff member cuts their live sessions immediately: is_active
    is checked here on every request, not only at login.
    """
    row = session.execute(
        text(
            """
            SELECT s.staff_id, s.full_name, s.role::text AS role,
                   s.can_view_identity, s.must_change_password,
                   (s.totp_enrolled_at IS NOT NULL) AS mfa_enrolled,
                   ss.session_id, ss.mfa_satisfied, ss.csrf_token
              FROM staff_sessions ss
              JOIN staff s ON s.staff_id = ss.staff_id
             WHERE ss.token_sha256 = :digest
               AND ss.revoked_at IS NULL
               AND ss.expires_at > now()
               AND s.is_active
            """
        ),
        {"digest": _token_digest(token)},
    ).mappings().first()

    if row is None:
        raise AuthError("invalid or expired session")

    session.execute(
        text(
            "UPDATE staff_sessions SET last_seen_at = now() "
            "WHERE session_id = :sid"
        ),
        {"sid": row["session_id"]},
    )

    return AuthenticatedStaff(
        staff_id=row["staff_id"],
        full_name=row["full_name"],
        role=row["role"],
        can_view_identity=row["can_view_identity"],
        session_id=row["session_id"],
        csrf_token=row["csrf_token"],
        mfa_satisfied=row["mfa_satisfied"],
        mfa_enrolled=row["mfa_enrolled"],
        must_change_password=row["must_change_password"],
    )


def logout(session: Session, token: str) -> None:
    session.execute(
        text(
            "UPDATE staff_sessions SET revoked_at = now() "
            "WHERE token_sha256 = :digest AND revoked_at IS NULL"
        ),
        {"digest": _token_digest(token)},
    )


def revoke_all_sessions(session: Session, staff_id: UUID) -> int:
    """Cut every live session for one account. Used when access is withdrawn."""
    return session.execute(
        text(
            """
            UPDATE staff_sessions SET revoked_at = now()
             WHERE staff_id = :sid AND revoked_at IS NULL
               AND expires_at > now()
            """
        ),
        {"sid": str(staff_id)},
    ).rowcount
