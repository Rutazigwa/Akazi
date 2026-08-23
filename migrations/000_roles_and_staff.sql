-- 000. DATABASE ROLES + STAFF
-- Must run first: candidates.registered_by, assessment_results.assessed_by,
-- employers.account_owner, consent_records.captured_by and audit_log.staff_id
-- all reference staff(staff_id), and row access control for residency-sensitive
-- identity data depends on the role model defined here.

BEGIN;

-- Two application roles. Most staff connect as app_operations and can never
-- read national ID numbers; only app_identity holds that grant. See 001.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_operations') THEN
        CREATE ROLE app_operations NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_identity') THEN
        CREATE ROLE app_identity NOLOGIN;
    END IF;
END
$$;

CREATE TYPE staff_role AS ENUM
    ('coordinator','supervisor','admin','owner','readonly');

CREATE TABLE staff (
    staff_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name         VARCHAR(120) NOT NULL,
    email             VARCHAR(160) UNIQUE,
    phone             VARCHAR(20)  NOT NULL UNIQUE,
    role              staff_role   NOT NULL DEFAULT 'coordinator',
    -- Gate on residency-sensitive data. Default FALSE: identity access is
    -- granted deliberately, never inherited from seniority.
    can_view_identity BOOLEAN      NOT NULL DEFAULT FALSE,
    password_hash     TEXT,
    is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    deactivated_at    TIMESTAMPTZ,
    CONSTRAINT chk_deactivation CHECK (
        (is_active AND deactivated_at IS NULL)
        OR (NOT is_active AND deactivated_at IS NOT NULL)
    )
);

CREATE INDEX idx_staff_active ON staff (is_active) WHERE is_active;

COMMIT;
