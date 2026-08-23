-- 010. STAFF SESSIONS
--
-- Opaque, database-backed bearer tokens rather than JWTs. The deciding factor
-- is revocation: this system holds national ID numbers, and when a coordinator
-- leaves, their access has to stop immediately -- not whenever an unexpirable
-- signed token happens to run out. A stateless token cannot be withdrawn.
--
-- Only the SHA-256 of the token is stored. A leaked database backup then yields
-- no usable session, and the plaintext exists only in the client's possession.

BEGIN;

CREATE TABLE staff_sessions (
    session_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    staff_id     UUID NOT NULL REFERENCES staff(staff_id) ON DELETE CASCADE,
    token_sha256 BYTEA       NOT NULL UNIQUE,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    user_agent   VARCHAR(200),
    CONSTRAINT chk_session_window CHECK (expires_at > issued_at)
);

CREATE INDEX idx_session_staff ON staff_sessions (staff_id);
CREATE INDEX idx_session_live
    ON staff_sessions (expires_at) WHERE revoked_at IS NULL;

-- Failed login tracking. Brute-forcing a coordinator account is the cheapest
-- route to a national ID number, so attempts are counted and the account locks
-- rather than relying on the password alone.
ALTER TABLE staff
    ADD COLUMN failed_login_count SMALLINT     NOT NULL DEFAULT 0,
    ADD COLUMN locked_until       TIMESTAMPTZ;

GRANT SELECT, INSERT, UPDATE ON staff_sessions TO app_operations;
GRANT SELECT, UPDATE ON staff TO app_operations;

COMMIT;
