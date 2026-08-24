-- 012. ERASURE REQUESTS  (Law No. 058/2021 -- right to erasure)
--
-- Handling deletion requests is a stated requirement, and doing it naively
-- would be worse than not doing it at all.
--
-- candidate_identity.candidate_id is the parent of candidates, which is the
-- parent of placements, attendance, pay records and follow-ups -- all with
-- ON DELETE CASCADE. A literal DELETE would therefore erase the employment
-- history of a real job: the attendance an employer confirmed, the pay records
-- that prove someone was paid, and the placement rows another person's
-- replacement chain points at. That destroys other people's records and our own
-- evidence of having met our obligations.
--
-- So erasure REDACTS the identity row in place rather than deleting it. The
-- personal data -- legal names, national ID, phone numbers, emergency contact,
-- home coordinates -- is overwritten. The surrogate key survives, so the
-- operational history stays intact but is no longer attached to an identifiable
-- person. That is the outcome the law is asking for: the individual is no longer
-- identifiable from what we hold.
--
-- What is deliberately NOT erased:
--   consent_records  -- the evidence that we had a lawful basis at the time.
--                       Erasing it would leave us unable to show the processing
--                       was ever lawful, which is the opposite of compliance.
--   audit_log        -- who accessed what. Also the evidence trail.
--   placements etc.  -- employment records, now pseudonymous.

BEGIN;

CREATE TYPE erasure_status AS ENUM
    ('requested','in_review','completed','refused');

CREATE TABLE erasure_requests (
    erasure_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id   UUID NOT NULL REFERENCES candidate_identity(candidate_id),
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    requested_via  VARCHAR(20) NOT NULL
                   CHECK (requested_via IN ('paper','whatsapp','app','phone','email')),
    received_by    UUID REFERENCES staff(staff_id),
    status         erasure_status NOT NULL DEFAULT 'requested',
    -- Why, if refused. A refusal without a stated reason is not defensible.
    decision_note  TEXT,
    completed_at   TIMESTAMPTZ,
    completed_by   UUID REFERENCES staff(staff_id),
    CONSTRAINT chk_completion CHECK (
        (status = 'completed' AND completed_at IS NOT NULL)
        OR (status <> 'completed')
    ),
    CONSTRAINT chk_refusal CHECK (
        (status = 'refused' AND decision_note IS NOT NULL)
        OR (status <> 'refused')
    )
);

CREATE INDEX idx_erasure_candidate ON erasure_requests (candidate_id);
CREATE INDEX idx_erasure_open
    ON erasure_requests (requested_at) WHERE status IN ('requested','in_review');

-- Marks an identity record as redacted. Kept on the identity table rather than
-- on candidates so that a coordinator without identity access cannot tell
-- whether a given person exercised the right -- that itself is personal data.
ALTER TABLE candidate_identity
    ADD COLUMN erased_at TIMESTAMPTZ;

-- An erased record has no phone number. phone_primary was NOT NULL, which left
-- no way to remove it -- and a placeholder string is still a value we chose to
-- keep. NULL is the honest representation of "we no longer hold this", and
-- NULLs do not collide under the UNIQUE index, so many erased rows coexist.
-- The CHECK keeps the original guarantee for every live record.
ALTER TABLE candidate_identity
    ALTER COLUMN phone_primary DROP NOT NULL,
    ADD CONSTRAINT chk_phone_present_unless_erased
        CHECK (phone_primary IS NOT NULL OR erased_at IS NOT NULL);

-- Erasure is a privileged, audited operation. SECURITY DEFINER so that the
-- redaction can touch the identity table while direct writes stay restricted,
-- and so the audit row is written in the same transaction as the redaction.
CREATE OR REPLACE FUNCTION erase_candidate_identity(
    p_candidate_id UUID,
    p_erasure_id   UUID
) RETURNS VOID AS $$
DECLARE
    v_staff UUID := current_staff_id();
BEGIN
    -- Erasure is irreversible, so it must be attributable. An audit row with a
    -- null staff_id is evidence that proves nothing, and this is the one
    -- operation where "who did this" can never be reconstructed afterwards --
    -- the data it describes is gone.
    IF v_staff IS NULL THEN
        RAISE EXCEPTION 'erasure requires an acting staff member: '
                        'SET LOCAL app.staff_id before calling'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM candidate_identity
         WHERE candidate_id = p_candidate_id AND erased_at IS NULL
    ) THEN
        RAISE EXCEPTION 'candidate % is unknown or already erased', p_candidate_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Overwrite in place. Identifiers become NULL rather than a placeholder
    -- string: a placeholder is still data we chose to retain, and NULLs do not
    -- collide under the UNIQUE indexes on national_id and phone_primary.
    UPDATE candidate_identity
       SET legal_first_name  = 'ERASED',
           legal_last_name   = 'ERASED',
           national_id       = NULL,
           phone_primary     = NULL,
           phone_alt         = NULL,
           emergency_contact = NULL,
           erased_at         = now()
     WHERE candidate_id = p_candidate_id;

    -- Home coordinates and cell are identifying too: a home location plus shift
    -- times re-identifies a person even without a name.
    UPDATE candidates
       SET home_lat = NULL,
           home_lng = NULL,
           cell     = NULL,
           display_name = 'Erased candidate',
           status   = 'withdrawn'
     WHERE candidate_id = p_candidate_id;

    UPDATE erasure_requests
       SET status = 'completed',
           completed_at = now(),
           completed_by = v_staff
     WHERE erasure_id = p_erasure_id;

    INSERT INTO audit_log (staff_id, table_name, record_id, action, detail)
    VALUES (v_staff, 'candidate_identity', p_candidate_id, 'delete',
            jsonb_build_object('erasure_id', p_erasure_id,
                               'method', 'redaction_in_place'));
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE ALL ON FUNCTION erase_candidate_identity(UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION erase_candidate_identity(UUID, UUID) TO app_identity;

GRANT SELECT, INSERT, UPDATE ON erasure_requests TO app_operations, app_identity;

COMMIT;
