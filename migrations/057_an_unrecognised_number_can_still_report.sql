-- Her own alternate number was not one we recognised.
--
-- resolve_inbound_sender matches candidate_identity.phone_primary and nothing
-- else. phone_alt is captured at registration, sits in the same row, and is
-- never consulted -- so a woman messaging us from the second number she gave us
-- is a stranger.
--
-- Demonstrated on one candidate and one message, "the supervisor keeps
-- touching me and I feel unsafe", which interpret() classifies as harassment
-- in every case:
--
--   her primary number      resolved=yes  escalations=1
--   her recorded alternate  resolved=NO   escalations=0
--   a borrowed phone        resolved=NO   escalations=0
--
-- The first of those is an omission and is fixed here. The second -- that an
-- unattributable harassment report raises nothing at all -- is in
-- app/messaging/inbound.py, because it is a decision about what to do rather
-- than about who somebody is.
--
-- phone_primary still wins. Two candidates could in principle share a number
-- -- a household phone -- and the one who put it down as their main one is the
-- better guess.

CREATE OR REPLACE FUNCTION resolve_inbound_sender(p_phone TEXT)
RETURNS TABLE (candidate_id UUID, contact_id UUID) AS $$
DECLARE
    v_candidate UUID;
    v_contact   UUID;
BEGIN
    SELECT ci.candidate_id INTO v_candidate
      FROM candidate_identity ci
     WHERE (ci.phone_primary = p_phone OR ci.phone_alt = p_phone)
       AND ci.erased_at IS NULL
     ORDER BY (ci.phone_primary = p_phone) DESC
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
