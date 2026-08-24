-- 014. TWO-FACTOR AUTHENTICATION AND STAFF LIFECYCLE
--
-- A single leaked coordinator password currently reaches national ID numbers,
-- bounded only by lockout and the identity grant. TOTP closes that.
--
-- The policy this enables: a password is enough for operational work --
-- attendance, follow-ups, the scorecard -- but reading or writing identity data
-- requires a second factor on the current session. Identity access is the
-- sharp edge, so it gets the friction; the daily work does not.
--
-- last_totp_counter exists because a TOTP code stays valid for its whole time
-- step. Without recording the counter, a code observed over someone's shoulder
-- (or replayed from a proxy log) works again until the step rolls over.

BEGIN;

ALTER TABLE staff
    ADD COLUMN totp_secret          TEXT,
    ADD COLUMN totp_enrolled_at     TIMESTAMPTZ,
    -- Highest TOTP time-step already accepted. A code at or below this is a
    -- replay and is refused even while still inside its validity window.
    ADD COLUMN last_totp_counter    BIGINT,
    ADD COLUMN password_changed_at  TIMESTAMPTZ,
    -- Set when an owner resets someone's password: the temporary one works
    -- once, for the purpose of choosing a real one.
    ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    ADD CONSTRAINT chk_totp_enrolment
        CHECK (totp_enrolled_at IS NULL OR totp_secret IS NOT NULL);

-- MFA is a property of the session, not of the account. Logging in with a
-- password gives an unelevated session; presenting a code elevates that one
-- session and no other.
ALTER TABLE staff_sessions
    ADD COLUMN mfa_satisfied BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN staff.totp_secret IS
    'Base32 TOTP seed. Equivalent in value to a password: protected by disk '
    'encryption at rest (see docs/DEPLOYMENT.md), never returned by any '
    'endpoint after enrolment.';

COMMIT;
