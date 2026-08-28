-- 044. ERASURE STOPPED AT THE TABLES THAT EXISTED WHEN IT WAS WRITTEN
--
-- erase_candidate_identity redacts candidate_identity and the coordinates on
-- candidates. Since it was written, four tables have arrived holding free text
-- that a person can be identified from -- their own messages, what they told
-- us about an employer, what an escalation records -- and none of them were
-- touched. A redaction that leaves "this is Aline, the supervisor keeps
-- shouting at me" in inbound_messages has not erased anybody.
--
-- WHAT IS KEPT, AND WHY
--
-- The rows survive; only the words go. That is not squeamishness about
-- deleting data, it is about the other people in it:
--
--   An employer safety report keeps felt_safe and the concern. If erasing one
--   woman's record also erased her warning, the next woman placed there loses
--   the protection -- and the employer gains from her leaving. Her words go;
--   the fact that somebody felt unsafe stays.
--
--   An escalation keeps its kind, dates and status. The pattern "this
--   employer had a harassment escalation" is what protects the next person.
--
--   Messages keep their template key and timestamps, because "we sent a
--   reminder and it was delivered" is operational evidence about us, not
--   personal data about her.
--
-- audit_log is untouched and remains append-only. Its rows record that a read
-- or an erasure happened, which is the evidence the NCSA would ask for, and
-- overwriting it would defeat the purpose of having it.

BEGIN;

CREATE OR REPLACE FUNCTION erase_candidate_identity(
    p_candidate_id UUID,
    p_erasure_id   UUID
) RETURNS VOID AS $$
DECLARE
    v_staff UUID := current_staff_id();
BEGIN
    IF v_staff IS NULL THEN
        RAISE EXCEPTION 'erasure requires an acting staff member: '
                        'set app.staff_id before calling this'
            USING ERRCODE = 'no_data_found';
    END IF;

    UPDATE candidate_identity
       SET legal_first_name  = 'ERASED',
           legal_last_name   = 'ERASED',
           national_id       = NULL,
           phone_primary     = NULL,
           phone_alt         = NULL,
           emergency_contact = NULL,
           erased_at         = now()
     WHERE candidate_id = p_candidate_id;

    UPDATE candidates
       SET home_lat = NULL,
           home_lng = NULL,
           cell     = NULL,
           display_name = 'Erased candidate',
           status   = 'withdrawn'
     WHERE candidate_id = p_candidate_id;

    -- Her own words, in the messages she sent us and the ones we sent her.
    UPDATE inbound_messages
       SET body = '[erased]', from_phone = 'ERASED'
     WHERE candidate_id = p_candidate_id;

    UPDATE messages
       SET body = '[erased]'
     WHERE candidate_id = p_candidate_id;

    -- The structural facts stay so the next person is still protected.
    UPDATE employer_safety_reports
       SET note = NULL
     WHERE candidate_id = p_candidate_id;

    UPDATE escalations
       SET detail = '[erased]',
           resolution = CASE WHEN resolution IS NULL THEN NULL
                             ELSE '[erased]' END
     WHERE candidate_id = p_candidate_id;

    UPDATE follow_ups f
       SET notes = NULL
      FROM placements p
     WHERE p.placement_id = f.placement_id
       AND p.candidate_id = p_candidate_id;

    UPDATE transport_reports
       SET note = NULL
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

COMMIT;
