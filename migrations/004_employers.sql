-- 004. EMPLOYERS

BEGIN;

CREATE TYPE employer_tier AS ENUM ('prospect','pilot','active','suspended');

CREATE TABLE employers (
    employer_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name    VARCHAR(160)  NOT NULL,
    tin              VARCHAR(20),
    sector           VARCHAR(60)   NOT NULL,
    district         VARCHAR(60)   NOT NULL,
    site_lat         NUMERIC(9,6),
    site_lng         NUMERIC(9,6),
    tier             employer_tier NOT NULL DEFAULT 'prospect',
    -- Cooperatives are pre-aggregated demand: one relationship can yield the
    -- placement volume of fifteen SMEs. Flagged so the channel is measurable.
    is_cooperative   BOOLEAN       NOT NULL DEFAULT FALSE,
    safety_verified  BOOLEAN       NOT NULL DEFAULT FALSE,
    verified_at      DATE,
    account_owner    UUID          REFERENCES staff(staff_id),
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_verified CHECK (
        (safety_verified AND verified_at IS NOT NULL)
        OR NOT safety_verified
    )
);

CREATE INDEX idx_employer_tier ON employers (tier);

CREATE TABLE employer_contacts (
    contact_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employer_id  UUID NOT NULL REFERENCES employers(employer_id)
                 ON DELETE CASCADE,
    full_name    VARCHAR(120) NOT NULL,
    role_title   VARCHAR(80),
    phone        VARCHAR(20)  NOT NULL,
    email        VARCHAR(160),
    is_primary   BOOLEAN NOT NULL DEFAULT FALSE
);

-- At most one primary contact per employer.
CREATE UNIQUE INDEX idx_one_primary_contact
    ON employer_contacts (employer_id) WHERE is_primary;

GRANT SELECT, INSERT, UPDATE ON employers, employer_contacts TO app_operations;

COMMIT;
