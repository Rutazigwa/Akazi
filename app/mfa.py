"""Time-based one-time passwords.

The policy: a password is enough for operational work, but identity data --
national ID numbers, legal names, home locations -- requires a second factor on
the current session. Identity access is the sharp edge, so it carries the
friction; logging attendance does not.

Two details that are easy to get wrong and matter:

**Replay.** A TOTP code is valid for its whole 30-second step, so a code seen
over a shoulder or replayed from a proxy log works again until the step rolls.
`last_totp_counter` records the highest step already accepted and refuses
anything at or below it.

**Clock drift.** One step of tolerance either side, no more. Widening the window
to be forgiving multiplies the number of codes valid at any moment.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pyotp
from sqlalchemy import text
from sqlalchemy.orm import Session

TOTP_STEP_SECONDS = 30
# Steps of tolerance either side of now, for clock drift.
TOTP_DRIFT_STEPS = 1
ISSUER = "Akazi"


class MFAError(Exception):
    pass


@dataclass(frozen=True)
class Enrolment:
    secret: str
    otpauth_uri: str


def begin_enrolment(session: Session, staff_id: UUID) -> Enrolment:
    """Generate a secret and return it once, with a provisioning URI.

    The secret is stored immediately but enrolment is not complete until a code
    is confirmed -- otherwise a mistyped setup would lock the account out of
    identity data with no way back in.
    """
    row = session.execute(
        text(
            "SELECT phone, totp_enrolled_at FROM staff WHERE staff_id = :sid"
        ),
        {"sid": str(staff_id)},
    ).first()
    if row is None:
        raise MFAError("staff member not found")
    if row.totp_enrolled_at is not None:
        raise MFAError(
            "already enrolled -- an owner must reset the second factor first"
        )

    secret = pyotp.random_base32()
    session.execute(
        text(
            "UPDATE staff SET totp_secret = :secret, last_totp_counter = NULL "
            "WHERE staff_id = :sid"
        ),
        {"secret": secret, "sid": str(staff_id)},
    )
    uri = pyotp.TOTP(secret).provisioning_uri(name=row.phone, issuer_name=ISSUER)
    return Enrolment(secret=secret, otpauth_uri=uri)


def _verify_and_consume(
    session: Session, staff_id: UUID, code: str, secret: str,
    last_counter: int | None,
) -> None:
    totp = pyotp.TOTP(secret, interval=TOTP_STEP_SECONDS)
    if not totp.verify(code, valid_window=TOTP_DRIFT_STEPS):
        raise MFAError("invalid code")

    import time

    counter = int(time.time()) // TOTP_STEP_SECONDS
    # Find which step actually matched, so the replay guard advances correctly
    # when a slightly-drifted code was accepted.
    matched = next(
        (
            counter + offset
            for offset in range(-TOTP_DRIFT_STEPS, TOTP_DRIFT_STEPS + 1)
            if totp.at((counter + offset) * TOTP_STEP_SECONDS) == code
        ),
        counter,
    )

    if last_counter is not None and matched <= last_counter:
        raise MFAError("this code has already been used")

    session.execute(
        text("UPDATE staff SET last_totp_counter = :c WHERE staff_id = :sid"),
        {"c": matched, "sid": str(staff_id)},
    )


def confirm_enrolment(session: Session, staff_id: UUID, code: str) -> None:
    row = session.execute(
        text(
            "SELECT totp_secret, totp_enrolled_at, last_totp_counter "
            "FROM staff WHERE staff_id = :sid"
        ),
        {"sid": str(staff_id)},
    ).first()
    if row is None or row.totp_secret is None:
        raise MFAError("no enrolment in progress")
    if row.totp_enrolled_at is not None:
        raise MFAError("already enrolled")

    _verify_and_consume(
        session, staff_id, code, row.totp_secret, row.last_totp_counter
    )
    session.execute(
        text("UPDATE staff SET totp_enrolled_at = now() WHERE staff_id = :sid"),
        {"sid": str(staff_id)},
    )


def elevate_session(
    session: Session, staff_id: UUID, session_id: UUID, code: str
) -> None:
    """Mark one session as having satisfied the second factor.

    Per session, deliberately. A code presented on a laptop must not elevate a
    token someone else is holding.
    """
    row = session.execute(
        text(
            "SELECT totp_secret, totp_enrolled_at, last_totp_counter "
            "FROM staff WHERE staff_id = :sid"
        ),
        {"sid": str(staff_id)},
    ).first()
    if row is None or row.totp_enrolled_at is None:
        raise MFAError("this account has no second factor enrolled")

    _verify_and_consume(
        session, staff_id, code, row.totp_secret, row.last_totp_counter
    )
    session.execute(
        text(
            "UPDATE staff_sessions SET mfa_satisfied = true "
            "WHERE session_id = :session_id"
        ),
        {"session_id": str(session_id)},
    )


def reset_enrolment(session: Session, staff_id: UUID) -> None:
    """Clear a second factor -- for a lost phone. Owner/admin only.

    Live sessions are cut at the same time: a session elevated with the old
    factor must not survive its removal.
    """
    session.execute(
        text(
            """
            UPDATE staff
               SET totp_secret = NULL, totp_enrolled_at = NULL,
                   last_totp_counter = NULL
             WHERE staff_id = :sid
            """
        ),
        {"sid": str(staff_id)},
    )
    session.execute(
        text(
            "UPDATE staff_sessions SET revoked_at = now() "
            "WHERE staff_id = :sid AND revoked_at IS NULL"
        ),
        {"sid": str(staff_id)},
    )
