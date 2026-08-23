-- 001. IDENTITY  (residency-sensitive; isolated for compliance)
--
-- Law No. 058/2021: this table is the reason the database is Rwanda-hosted.
-- It is deliberately separated from the operational `candidates` table so that
--   (a) if the split-store residency option is adopted later, this table moves
--       and the rest of the schema stays put, and
--   (b) most staff can be granted operational access without ever being able
--       to read a national ID number.
-- Do not add operational columns here, and do not add identifying columns to
-- `candidates`.

BEGIN;

CREATE TABLE candidate_identity (
    candidate_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_first_name  VARCHAR(80)  NOT NULL,
    legal_last_name   VARCHAR(80)  NOT NULL,
    national_id       VARCHAR(32)  UNIQUE,
    date_of_birth     DATE         NOT NULL,
    phone_primary     VARCHAR(20)  NOT NULL UNIQUE,
    phone_alt         VARCHAR(20),
    emergency_contact VARCHAR(120),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- Minimum age 16. Safe as a CHECK despite CURRENT_DATE being STABLE: the
    -- predicate only becomes more true as time passes, so a row that validates
    -- on insert still validates on a later dump/restore.
    -- Apprenticeship exceptions for 13-15 are NOT handled here; they require a
    -- separate authorised-exception record and a deliberate schema change.
    CONSTRAINT chk_minimum_age
        CHECK (date_of_birth <= CURRENT_DATE - INTERVAL '16 years')
);

COMMENT ON TABLE candidate_identity IS
    'Residency-sensitive personal data (Law 058/2021). Rwanda-hosted only. '
    'Every read is audited by trg_audit_identity_read (see 008).';

-- Operational staff get nothing here. Identity access is a separate grant.
REVOKE ALL ON candidate_identity FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON candidate_identity TO app_identity;

COMMIT;
