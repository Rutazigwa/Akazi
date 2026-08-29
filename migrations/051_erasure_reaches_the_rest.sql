-- Eight more columns held her name after she had been erased.
--
-- Found by seeding one candidate through the application's own functions, then
-- searching every text and jsonb column in the schema -- not by reading the
-- function, and not by the coverage test, which could not see five of these
-- tables at all. tests/test_erasure_leaves_nothing.py is that search, kept.
--
--   assessment_results.notes      a coordinator's written observation
--   attendance.absence_reason     why she did not come in
--   cohort_members.notes          how she did on the course
--   pay_deductions.note           why money came off her wage
--   placements.employer_note      what the employer wrote about her
--   placements.match_reason       why she was chosen
--   placement_contracts.terms     her name, snapshotted into every contract
--                                 (retained -- see the note at that statement)
--   work_requests.safety_notes    "ask for <her> at the gate"
--
-- The last two are the important ones, for different reasons.
--
-- placement_contracts.terms is system-generated: issue_contract() writes
-- {"worker": display_name} for every placement of every candidate. This was
-- never a stray note somebody typed -- it is universal, and it means no
-- erasure this system has ever performed was complete.
--
-- work_requests.safety_notes belongs to the employer, not to her. Her name is
-- inside somebody else's record, so the row cannot be blanked: the shift still
-- has real safety instructions on it. Her name is replaced within the text and
-- the rest is left alone. The same applies to the messages we send employers
-- about her, which are keyed to the employer contact and so were never
-- reachable by "WHERE candidate_id = her".
--
-- What stays, deliberately: the amount and kind of a deduction, the fact of an
-- absence, the contract and its terms, the assessment score. The record that
-- something happened is not hers to erase -- the words describing her are.

CREATE OR REPLACE FUNCTION erase_candidate_identity(
    p_candidate_id UUID,
    p_erasure_id   UUID
) RETURNS VOID AS $$
DECLARE
    v_staff UUID := current_staff_id();
    v_display TEXT;
    v_legal   TEXT;
BEGIN
    IF v_staff IS NULL THEN
        RAISE EXCEPTION 'erasure requires an acting staff member: '
                        'set app.staff_id before calling this'
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Read the names before destroying them: they are how her name is found
    -- inside records belonging to other people. Anything shorter than three
    -- characters is not searched for -- a two-letter string would match half
    -- the words in an employer's safety note.
    SELECT c.display_name,
           NULLIF(trim(ci.legal_first_name || ' ' || ci.legal_last_name), '')
      INTO v_display, v_legal
      FROM candidates c
      JOIN candidate_identity ci USING (candidate_id)
     WHERE c.candidate_id = p_candidate_id;

    IF length(coalesce(v_display, '')) < 3 THEN v_display := NULL; END IF;
    IF length(coalesce(v_legal, '')) < 3 THEN v_legal := NULL; END IF;

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

    -- --- what somebody wrote about her --------------------------------

    UPDATE assessment_results
       SET notes = NULL
     WHERE candidate_id = p_candidate_id;

    UPDATE cohort_members
       SET notes = NULL
     WHERE candidate_id = p_candidate_id;

    -- The absence stays recorded; the sentence explaining it does not.
    -- Redacted rather than blanked: chk_absence_reason requires an absence to
    -- carry a reason, and that constraint is right -- an unexplained absence
    -- feeds the retention figures and should never be creatable, least of all
    -- as a side effect of somebody exercising a data right.
    UPDATE attendance a
       SET absence_reason = '[erased]'
      FROM placements p
     WHERE p.placement_id = a.placement_id
       AND p.candidate_id = p_candidate_id;

    -- The deduction, its kind and its amount are a financial record and
    -- remain. The written justification named her.
    --
    -- chk_deduction_explained requires at least ten characters of reason for a
    -- damage or other deduction, "in enough words that the worker could
    -- dispute it". The replacement has to satisfy that rather than defeat it,
    -- so it says what happened instead of going blank.
    UPDATE pay_deductions d
       SET note = '[erased on request]'
      FROM pay_records pr
      JOIN placements p ON p.placement_id = pr.placement_id
     WHERE pr.pay_id = d.pay_id
       AND p.candidate_id = p_candidate_id;

    UPDATE placements
       SET employer_note = NULL,
           match_reason  = NULL
     WHERE candidate_id = p_candidate_id;

    -- --- her name inside records that are not hers ---------------------

    -- placement_contracts.terms is NOT redacted here, and the omission is
    -- deliberate rather than an oversight.
    --
    -- fn_contract_terms_immutable() refuses any change to an agreed contract,
    -- and that trigger is the reason a contract is worth anything: it is what
    -- stops the terms somebody accepted being edited afterwards. A contract
    -- also names its parties by necessity -- one that does not is not evidence
    -- of an agreement, and it protects her as much as the employer.
    --
    -- Retention for establishing or defending a legal claim is a recognised
    -- basis, so this is a lawful retention rather than a gap. It is recorded
    -- as an open question for the owner and counsel in CLAUDE.md, because the
    -- boundary is a legal judgement and not an engineering one. Until that is
    -- settled the honest position is that this system's erasure leaves a
    -- person's name in her contracts, and says so.

    -- The employer's shift, with real instructions on it that other people
    -- still need. Replace her name in place rather than blanking the field.
    UPDATE work_requests wr
       SET safety_notes = replace(wr.safety_notes, n.name, '[erased]')
      FROM placements p,
           LATERAL (VALUES (v_display), (v_legal)) AS n(name)
     WHERE p.request_id = wr.request_id
       AND p.candidate_id = p_candidate_id
       AND n.name IS NOT NULL
       AND wr.safety_notes LIKE '%' || n.name || '%';

    -- Messages about her sent to the employer. These carry contact_id, never
    -- candidate_id -- a CHECK constraint allows exactly one of the three --
    -- so the redaction above could not see them, and the templates that
    -- generate them interpolate her display name by design.
    UPDATE messages m
       SET body = replace(m.body, v_display, '[erased]')
      FROM placements p
     WHERE p.candidate_id = p_candidate_id
       AND m.candidate_id IS NULL
       AND v_display IS NOT NULL
       AND m.body LIKE '%' || v_display || '%';

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
