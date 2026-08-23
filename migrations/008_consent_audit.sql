-- 008. CONSENT & AUDIT  (data-protection evidence)
--
-- This file is what gets shown to the NCSA. Consent is append-only and
-- versioned so we can prove what someone agreed to and when; every touch of
-- candidate_identity -- including reads -- leaves a row in audit_log.

BEGIN;

CREATE TABLE consent_records (
    consent_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id   UUID NOT NULL REFERENCES candidates(candidate_id)
                   ON DELETE CASCADE,
    policy_version VARCHAR(20) NOT NULL,
    purpose        VARCHAR(60) NOT NULL
                   CHECK (purpose IN ('placement','training','reporting')),
    granted        BOOLEAN NOT NULL,
    captured_via   VARCHAR(20) NOT NULL
                   CHECK (captured_via IN ('paper','whatsapp','app')),
    captured_by    UUID REFERENCES staff(staff_id),
    captured_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_consent_cand ON consent_records (candidate_id, purpose);

-- Append-only. A withdrawal is a new row with granted = FALSE, never an UPDATE:
-- the history IS the evidence.
CREATE RULE consent_no_update AS ON UPDATE TO consent_records DO INSTEAD NOTHING;
CREATE RULE consent_no_delete AS ON DELETE TO consent_records DO INSTEAD NOTHING;

-- Current consent state per (candidate, purpose): latest row wins.
CREATE VIEW v_current_consent AS
SELECT DISTINCT ON (candidate_id, purpose)
       candidate_id, purpose, policy_version, granted, captured_at
FROM consent_records
ORDER BY candidate_id, purpose, captured_at DESC;

CREATE TABLE audit_log (
    audit_id    BIGSERIAL PRIMARY KEY,
    staff_id    UUID REFERENCES staff(staff_id),
    table_name  VARCHAR(60) NOT NULL,
    record_id   UUID NOT NULL,
    action      VARCHAR(10) NOT NULL
                CHECK (action IN ('insert','update','delete','read')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail      JSONB
);

CREATE INDEX idx_audit_record ON audit_log (table_name, record_id);
CREATE INDEX idx_audit_time   ON audit_log (occurred_at);

-- The acting staff member is carried on the session: SET LOCAL app.staff_id.
CREATE OR REPLACE FUNCTION current_staff_id() RETURNS UUID AS $$
BEGIN
    RETURN NULLIF(current_setting('app.staff_id', true), '')::UUID;
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- Write auditing on candidate_identity.
CREATE OR REPLACE FUNCTION fn_audit_identity_write() RETURNS TRIGGER AS $$
DECLARE
    v_record UUID := COALESCE(NEW.candidate_id, OLD.candidate_id);
BEGIN
    INSERT INTO audit_log (staff_id, table_name, record_id, action, detail)
    VALUES (current_staff_id(), 'candidate_identity', v_record,
            lower(TG_OP),
            jsonb_build_object('changed_fields',
                CASE WHEN TG_OP = 'UPDATE'
                     THEN (SELECT jsonb_agg(key)
                           FROM jsonb_each(to_jsonb(NEW)) n
                           WHERE n.value IS DISTINCT FROM
                                 (to_jsonb(OLD) -> n.key))
                     ELSE NULL END));
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_identity_write
    AFTER INSERT OR UPDATE OR DELETE ON candidate_identity
    FOR EACH ROW EXECUTE FUNCTION fn_audit_identity_write();

-- Read auditing. PostgreSQL has no SELECT trigger, so reads are funnelled
-- through a SECURITY DEFINER function and direct SELECT is revoked. This is
-- the only supported way to guarantee the read trail the blueprint requires:
-- if the NCSA asks who looked at a national ID number, audit_log answers.
CREATE OR REPLACE FUNCTION read_candidate_identity(p_candidate_id UUID)
RETURNS SETOF candidate_identity AS $$
BEGIN
    INSERT INTO audit_log (staff_id, table_name, record_id, action)
    VALUES (current_staff_id(), 'candidate_identity', p_candidate_id, 'read');

    RETURN QUERY
    SELECT * FROM candidate_identity WHERE candidate_id = p_candidate_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE ALL ON FUNCTION read_candidate_identity(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION read_candidate_identity(UUID) TO app_identity;

-- Direct reads are closed off; app_identity keeps write access only.
REVOKE SELECT ON candidate_identity FROM app_identity;

GRANT SELECT, INSERT ON consent_records TO app_operations;
GRANT SELECT ON v_current_consent TO app_operations;
GRANT SELECT, INSERT ON audit_log TO app_operations, app_identity;
GRANT USAGE, SELECT ON SEQUENCE audit_log_audit_id_seq
    TO app_operations, app_identity;

COMMIT;
