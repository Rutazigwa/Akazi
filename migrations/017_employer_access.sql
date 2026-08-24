-- 017. EMPLOYER LOGINS
--
-- Weeks 7-12 of the build order: employers post a shift, see who is assigned,
-- confirm attendance, rate the worker, reorder. Responsive web only -- no
-- employer app, ever.
--
-- Employers are a different principal from staff, and the separation is the
-- whole security story of this file. A staff session can see everything; an
-- employer session must see exactly one employer's data and no candidate
-- identity at all. Rather than bolt a role onto `staff`, employer credentials
-- live on employer_contacts -- the people who already exist in the schema --
-- with their own session table.
--
-- Two tables mean the two principals cannot be confused by a coding mistake:
-- a token from one will never resolve in the other's lookup.

BEGIN;

ALTER TABLE employer_contacts
    ADD COLUMN password_hash        TEXT,
    ADD COLUMN is_active            BOOLEAN     NOT NULL DEFAULT TRUE,
    ADD COLUMN must_change_password BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN failed_login_count   SMALLINT    NOT NULL DEFAULT 0,
    ADD COLUMN locked_until         TIMESTAMPTZ,
    ADD COLUMN password_changed_at  TIMESTAMPTZ,
    ADD COLUMN last_login_at        TIMESTAMPTZ;

-- A contact can only sign in if someone gave them a password. Contacts without
-- one are still just contact details, which is what most of them are.
CREATE UNIQUE INDEX idx_contact_login
    ON employer_contacts (phone) WHERE password_hash IS NOT NULL;

CREATE TABLE employer_sessions (
    session_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id   UUID NOT NULL REFERENCES employer_contacts(contact_id)
                 ON DELETE CASCADE,
    token_sha256 BYTEA       NOT NULL UNIQUE,
    csrf_token   TEXT        NOT NULL,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    user_agent   VARCHAR(200),
    CONSTRAINT chk_employer_session_window CHECK (expires_at > issued_at)
);

CREATE INDEX idx_employer_session_contact ON employer_sessions (contact_id);

-- Employer-confirmed attendance is the evidence the guarantee rests on. Until
-- now every attendance row was typed by a coordinator, which makes it our word
-- rather than the employer's. This records who actually pressed the button.
ALTER TABLE attendance
    ADD COLUMN confirmed_by_contact UUID REFERENCES employer_contacts(contact_id);

-- Employer's rating of the worker, and the reorder signal. employer_reorder_rate
-- is a pilot metric; a reorder is a new request from an employer who already
-- had one, which is derivable -- but the rating is not.
ALTER TABLE placements
    ADD COLUMN employer_rating SMALLINT
        CHECK (employer_rating BETWEEN 1 AND 5),
    ADD COLUMN employer_note   TEXT,
    ADD COLUMN rated_at        TIMESTAMPTZ;

GRANT SELECT, INSERT, UPDATE ON employer_sessions TO app_operations;

COMMIT;
