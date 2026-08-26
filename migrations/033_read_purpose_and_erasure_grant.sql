-- 033. TWO GAPS IN THE IDENTITY READ TRAIL
--
-- (a) Requesting erasure failed under the real role. Deciding whether someone
--     has already been erased reads candidate_identity.erased_at, and 031
--     granted only candidate_id. erased_at is lifecycle metadata about the
--     record, not a fact about the person -- it says a row was redacted, not
--     who they are -- so it can be read without going through the audited
--     path. The identifying columns stay closed.
--
-- (b) The read trail recorded WHO and WHEN but never WHY. That is the first
--     question an NCSA auditor asks, and until now nothing in the system could
--     answer it: read_candidate_identity() wrote a bare 'read' row with no
--     detail at all. A log that cannot distinguish a coordinator opening a
--     profile to staff a shift from a bulk export is not much of a safeguard.
--
--     Purpose is a defaulted argument rather than a required one so existing
--     one-argument callers keep working and simply record 'operations'. The
--     old single-argument function is dropped first: leaving it in place would
--     make read_candidate_identity(x) ambiguous between the two overloads.

BEGIN;

GRANT SELECT (erased_at) ON candidate_identity TO app_identity;

DROP FUNCTION IF EXISTS read_candidate_identity(UUID);

CREATE FUNCTION read_candidate_identity(
    p_candidate_id UUID,
    p_purpose      TEXT DEFAULT 'operations'
)
RETURNS SETOF candidate_identity AS $$
BEGIN
    IF p_purpose NOT IN ('operations', 'placement', 'support',
                         'data_request', 'erasure', 'reporting') THEN
        RAISE EXCEPTION 'unknown identity read purpose: %', p_purpose;
    END IF;

    INSERT INTO audit_log (staff_id, table_name, record_id, action, detail)
    VALUES (current_staff_id(), 'candidate_identity', p_candidate_id, 'read',
            jsonb_build_object('purpose', p_purpose));

    RETURN QUERY
    SELECT * FROM candidate_identity WHERE candidate_id = p_candidate_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE ALL ON FUNCTION read_candidate_identity(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION read_candidate_identity(UUID, TEXT) TO app_identity;

COMMIT;
