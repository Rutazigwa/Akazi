-- 032. RESOLVING AN INBOUND SENDER IS AN IDENTITY READ
--
-- record_inbound matched an incoming phone number against
-- candidate_identity.phone_primary directly. Two things wrong with that, and
-- the second is the one that matters:
--
--   1. It failed under the real role, because SELECT on candidate_identity is
--      revoked -- so every reply a worker sent was dropped on a deployment
--      using the role model.
--   2. Looking someone up by phone number IS a read of their identity record,
--      and it was leaving no trace. The whole point of revoking that SELECT is
--      that no path reads identity data without the audit trail catching it.
--
-- So it goes through a SECURITY DEFINER function, exactly as the outbound
-- direction already does in message_recipient_phone(). Audited only on a hit:
-- a number matching nobody has no record to attach a read to, and inventing
-- one would be noise in the log an auditor has to wade through.

BEGIN;

CREATE OR REPLACE FUNCTION resolve_inbound_sender(p_phone TEXT)
RETURNS TABLE (candidate_id UUID, contact_id UUID) AS $$
DECLARE
    v_candidate UUID;
    v_contact   UUID;
BEGIN
    SELECT ci.candidate_id INTO v_candidate
      FROM candidate_identity ci
     WHERE ci.phone_primary = p_phone
       AND ci.erased_at IS NULL
     LIMIT 1;

    IF v_candidate IS NOT NULL THEN
        INSERT INTO audit_log (staff_id, table_name, record_id, action, detail)
        VALUES (current_staff_id(), 'candidate_identity', v_candidate, 'read',
                jsonb_build_object('purpose', 'inbound_message'));
    END IF;

    SELECT ec.contact_id INTO v_contact
      FROM employer_contacts ec
     WHERE ec.phone = p_phone AND ec.is_active
     LIMIT 1;

    RETURN QUERY SELECT v_candidate, v_contact;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE ALL ON FUNCTION resolve_inbound_sender(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_inbound_sender(TEXT)
    TO app_operations, app_identity;

COMMIT;
