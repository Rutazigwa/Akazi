"""Employer authentication.

A separate principal from staff, deliberately kept in separate tables. A staff
session can see everything; an employer session must see exactly one employer's
data and no candidate identity at all. Two token tables mean a coding mistake
cannot make one resolve as the other -- there is no shared lookup to get wrong.

Everything an employer sees goes through `scope`, which is not decoration: it
carries the employer_id that every query must filter on.
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
MAX_FAILED_LOGINS = 5
LOCKOUT_DURATION = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()


class EmployerAuthError(Exception):
    pass


@dataclass(frozen=True)
class EmployerPrincipal:
    """Who is signed in, and the single employer they may see."""

    contact_id: UUID
    employer_id: UUID
    full_name: str
    business_name: str
    session_id: UUID
    csrf_token: str
    must_change_password: bool = False


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def set_contact_password(
    session: Session, contact_id: UUID, password: str, must_change: bool = False
) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise EmployerAuthError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    session.execute(
        text(
            """
            UPDATE employer_contacts
               SET password_hash = :h, password_changed_at = now(),
                   must_change_password = :must_change,
                   failed_login_count = 0, locked_until = NULL
             WHERE contact_id = :cid
            """
        ),
        {
            "h": _hasher.hash(password),
            "cid": str(contact_id),
            "must_change": must_change,
        },
    )


def invite_contact(
    session: Session, contact_id: UUID
) -> str:
    """Issue a single-use password for an employer contact.

    Returned once. Generated rather than chosen so that the coordinator setting
    it up does not know the employer's password.
    """
    temporary = secrets.token_urlsafe(12)
    set_contact_password(session, contact_id, temporary, must_change=True)
    return temporary


def employer_login(
    session: Session, phone: str, password: str, user_agent: str | None = None
) -> str:
    row = session.execute(
        text(
            """
            SELECT contact_id, password_hash, is_active, failed_login_count,
                   locked_until
              FROM employer_contacts
             WHERE phone = :phone AND password_hash IS NOT NULL
            """
        ),
        {"phone": phone},
    ).mappings().first()

    generic = EmployerAuthError("invalid credentials")

    if row is None or not row["is_active"]:
        _hasher.hash(password)  # keep the timing indistinguishable
        raise generic

    if row["locked_until"] is not None:
        still_locked = session.execute(
            text(
                "SELECT locked_until > now() FROM employer_contacts "
                "WHERE contact_id = :cid"
            ),
            {"cid": row["contact_id"]},
        ).scalar_one()
        if still_locked:
            raise generic

    try:
        _hasher.verify(row["password_hash"], password)
    except VerifyMismatchError:
        attempts = row["failed_login_count"] + 1
        if attempts >= MAX_FAILED_LOGINS:
            session.execute(
                text(
                    """
                    UPDATE employer_contacts
                       SET failed_login_count = 0,
                           locked_until = now() + CAST(:lockout AS interval)
                     WHERE contact_id = :cid
                    """
                ),
                {
                    "cid": row["contact_id"],
                    "lockout": f"{LOCKOUT_DURATION.seconds} seconds",
                },
            )
        else:
            session.execute(
                text(
                    "UPDATE employer_contacts SET failed_login_count = :n "
                    "WHERE contact_id = :cid"
                ),
                {"n": attempts, "cid": row["contact_id"]},
            )
        raise generic from None

    session.execute(
        text(
            "UPDATE employer_contacts "
            "   SET failed_login_count = 0, locked_until = NULL, "
            "       last_login_at = now() "
            " WHERE contact_id = :cid"
        ),
        {"cid": row["contact_id"]},
    )

    token = secrets.token_urlsafe(32)
    session.execute(
        text(
            """
            INSERT INTO employer_sessions (contact_id, token_sha256, csrf_token,
                                           expires_at, user_agent)
            VALUES (:cid, :digest, :csrf,
                    now() + CAST(:lifetime AS interval), :ua)
            """
        ),
        {
            "cid": row["contact_id"],
            "digest": _digest(token),
            "csrf": secrets.token_urlsafe(32),
            "lifetime": f"{int(SESSION_LIFETIME.total_seconds())} seconds",
            "ua": (user_agent or "")[:200] or None,
        },
    )
    return token


def authenticate_employer(session: Session, token: str) -> EmployerPrincipal:
    """Resolve a token to a contact and the one employer they may see.

    Suspending an employer cuts their contacts' access on the next request, the
    same way deactivating a staff member does -- checked here rather than only
    at login.
    """
    row = session.execute(
        text(
            """
            SELECT ec.contact_id, ec.employer_id, ec.full_name,
                   ec.must_change_password, e.business_name,
                   es.session_id, es.csrf_token
              FROM employer_sessions es
              JOIN employer_contacts ec ON ec.contact_id = es.contact_id
              JOIN employers e          ON e.employer_id = ec.employer_id
             WHERE es.token_sha256 = :digest
               AND es.revoked_at IS NULL
               AND es.expires_at > now()
               AND ec.is_active
               AND e.tier <> 'suspended'
            """
        ),
        {"digest": _digest(token)},
    ).mappings().first()

    if row is None:
        raise EmployerAuthError("invalid or expired session")

    session.execute(
        text(
            "UPDATE employer_sessions SET last_seen_at = now() "
            "WHERE session_id = :sid"
        ),
        {"sid": row["session_id"]},
    )

    return EmployerPrincipal(
        contact_id=row["contact_id"],
        employer_id=row["employer_id"],
        full_name=row["full_name"],
        business_name=row["business_name"],
        session_id=row["session_id"],
        csrf_token=row["csrf_token"],
        must_change_password=row["must_change_password"],
    )


def employer_logout(session: Session, token: str) -> None:
    session.execute(
        text(
            "UPDATE employer_sessions SET revoked_at = now() "
            "WHERE token_sha256 = :digest AND revoked_at IS NULL"
        ),
        {"digest": _digest(token)},
    )
