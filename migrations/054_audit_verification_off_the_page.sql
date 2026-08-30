-- Verifying the audit chain on every page load stops working exactly when the
-- chain matters most.
--
-- /ui/staff calls verify_audit_chain() on every render, and that function walks
-- every row in audit_log rehashing as it goes. Measured on a database with a
-- year's worth of operating in it:
--
--     62,000 rows   ->   432 ms
--    182,000 rows   -> 1,251 ms
--
-- Dead linear, about 6.9 microseconds a row. audit_log grows on every identity
-- read and every write, and CLAUDE.md is explicit that it is append-only and
-- never pruned -- it is the evidence produced if the NCSA asks. So the page
-- that reports "the trail is intact" gets slower forever, and at a million
-- rows it is a seven-second page load.
--
-- The verification itself is not the problem; doing it in a request is. It
-- moves to the job infrastructure that already runs the dispatcher, the
-- attendance chase and the backup check, with its own heartbeat -- so a
-- verification that stops running is visible as a stalled job rather than as
-- silence. The page reports the last result and when it was taken.

CREATE TABLE audit_verifications (
    verification_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    checked_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    entries_checked  BIGINT NOT NULL,
    intact           BOOLEAN NOT NULL,
    broken_at        BIGINT,
    reason           TEXT,
    duration_ms      INTEGER NOT NULL,
    -- A failed verification must never be overwritten by a later passing one:
    -- the row saying the chain was broken is itself evidence.
    CONSTRAINT chk_break_explained
        CHECK (intact OR (broken_at IS NOT NULL AND reason IS NOT NULL))
);

COMMENT ON TABLE audit_verifications IS
    'One row per run of the chain check. Append-only in practice: a '
    'passing run does not replace a failing one.';

CREATE INDEX idx_audit_verifications_recent
    ON audit_verifications (checked_at DESC);

-- Run the check and record it. Returns the row it wrote.
CREATE FUNCTION record_audit_verification()
RETURNS audit_verifications AS $$
DECLARE
    v_started TIMESTAMPTZ := clock_timestamp();
    v_broken  RECORD;
    v_count   BIGINT;
    v_row     audit_verifications;
BEGIN
    SELECT count(*) INTO v_count FROM audit_log;
    SELECT * INTO v_broken FROM verify_audit_chain();

    INSERT INTO audit_verifications
        (entries_checked, intact, broken_at, reason, duration_ms)
    VALUES (v_count,
            v_broken.broken_at_audit_id IS NULL,
            v_broken.broken_at_audit_id,
            v_broken.reason,
            -- EPOCH, not MILLISECONDS: the latter already carries the
            -- seconds, so adding them again reported 2266ms for a run that
            -- took 1251ms.
            (EXTRACT(EPOCH FROM clock_timestamp() - v_started) * 1000)::int)
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$ LANGUAGE plpgsql;

-- The last word on the chain, and how old it is. A verification nobody has
-- run for a week is not reassurance.
CREATE VIEW v_audit_chain_status AS
SELECT v.checked_at,
       v.entries_checked,
       v.intact,
       v.broken_at,
       v.reason,
       v.duration_ms,
       ROUND(EXTRACT(EPOCH FROM (now() - v.checked_at)) / 3600.0, 1) AS hours_ago,
       -- Anything ever found broken outranks a later pass: tampering does not
       -- stop being true because the next run came back clean.
       EXISTS (SELECT 1 FROM audit_verifications WHERE NOT intact) AS ever_broken
  FROM audit_verifications v
 ORDER BY v.checked_at DESC
 LIMIT 1;

GRANT SELECT ON audit_verifications, v_audit_chain_status
    TO app_operations, app_identity;
GRANT INSERT ON audit_verifications TO app_operations, app_identity;
GRANT EXECUTE ON FUNCTION record_audit_verification()
    TO app_operations, app_identity;
