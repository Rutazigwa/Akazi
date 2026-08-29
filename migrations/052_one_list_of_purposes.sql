-- Every identity read records why. Three functions write that reason, and only
-- one of them checked it.
--
-- read_candidate_identity() rejects anything outside a list of six purposes.
-- resolve_inbound_sender() and message_recipient_phone() insert 'inbound_message'
-- and 'messaging' straight into audit_log, past that check. So the list in
-- migration 033 was never the set of purposes in the log -- somebody reading
-- the function would conclude six, and the database holds eight.
--
-- That matters more than tidiness. The purpose column is the answer to "why
-- did your staff open this person's record", and an answer is only as good as
-- the vocabulary it is drawn from. A validation two of three writers ignore
-- does not constrain the vocabulary; it just makes it look constrained.
--
-- One function now owns the list, and all three call it.

CREATE FUNCTION assert_identity_read_purpose(p_purpose TEXT) RETURNS TEXT AS $$
BEGIN
    -- The six a person can state, plus the two the system states for itself.
    -- Machine purposes are named separately because they are the ones with no
    -- staff member behind them: an inbound webhook and the message dispatcher
    -- both act with current_staff_id() null, and an audit row with a null
    -- actor proves less than one with a name on it. Keeping them in the same
    -- vocabulary is what makes that visible rather than hiding it.
    IF p_purpose NOT IN ('operations', 'placement', 'support',
                         'data_request', 'erasure', 'reporting',
                         'inbound_message', 'messaging') THEN
        RAISE EXCEPTION 'unknown identity read purpose: %', p_purpose;
    END IF;
    RETURN p_purpose;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION assert_identity_read_purpose(TEXT) IS
    'The only list of identity read purposes. Every writer of '
    'audit_log.detail->>''purpose'' must pass through this.';

-- --- the three writers ----------------------------------------------------

CREATE OR REPLACE FUNCTION read_candidate_identity(
    p_candidate_id UUID,
    p_purpose      TEXT DEFAULT 'operations'
)
RETURNS SETOF candidate_identity AS $$
BEGIN
    PERFORM assert_identity_read_purpose(p_purpose);

    INSERT INTO audit_log (staff_id, table_name, record_id, action, detail)
    VALUES (current_staff_id(), 'candidate_identity', p_candidate_id, 'read',
            jsonb_build_object('purpose', p_purpose));

    RETURN QUERY
    SELECT * FROM candidate_identity WHERE candidate_id = p_candidate_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

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
                jsonb_build_object('purpose',
                                   assert_identity_read_purpose('inbound_message')));
    END IF;

    SELECT ec.contact_id INTO v_contact
      FROM employer_contacts ec
     WHERE ec.phone = p_phone AND ec.is_active
     LIMIT 1;

    RETURN QUERY SELECT v_candidate, v_contact;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION message_recipient_phone(p_message_id UUID)
RETURNS TABLE (phone TEXT, is_candidate BOOLEAN) AS $$
DECLARE
    m RECORD;
BEGIN
    SELECT candidate_id, contact_id, staff_id INTO m
      FROM messages WHERE message_id = p_message_id;

    IF m.candidate_id IS NOT NULL THEN
        INSERT INTO audit_log (staff_id, table_name, record_id, action, detail)
        VALUES (current_staff_id(), 'candidate_identity', m.candidate_id, 'read',
                jsonb_build_object('purpose',
                                   assert_identity_read_purpose('messaging'),
                                   'message_id', p_message_id));

        RETURN QUERY
        SELECT ci.phone_primary::text, true
          FROM candidate_identity ci
         WHERE ci.candidate_id = m.candidate_id
           AND ci.erased_at IS NULL
           AND ci.phone_primary IS NOT NULL;
    ELSIF m.contact_id IS NOT NULL THEN
        RETURN QUERY
        SELECT ec.phone::text, false
          FROM employer_contacts ec
         WHERE ec.contact_id = m.contact_id AND ec.is_active;
    ELSE
        RETURN QUERY
        SELECT s.phone::text, false
          FROM staff s
         WHERE s.staff_id = m.staff_id AND s.is_active;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- The list is the answer to a regulator's question, so anyone who can read the
-- log can read the vocabulary it is drawn from.
GRANT EXECUTE ON FUNCTION assert_identity_read_purpose(TEXT)
    TO app_operations, app_identity;
