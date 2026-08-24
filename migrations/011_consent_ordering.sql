-- 011. DETERMINISTIC CONSENT ORDERING
--
-- Bug fix. v_current_consent picked the latest record by captured_at alone,
-- and captured_at defaulted to now() -- which in PostgreSQL is transaction
-- start time, identical for every row written in the same transaction. Two
-- consent records in one transaction therefore tied, and DISTINCT ON resolved
-- the tie arbitrarily: a withdrawal could be silently ignored and the
-- candidate would stay matchable.
--
-- Two changes:
--   1. captured_at defaults to clock_timestamp(), which advances within a
--      transaction, so rows written together no longer collide.
--   2. recorded_seq gives a total order regardless, and breaks any remaining
--      tie -- including one created by backdating captured_at for paper
--      consent collected days earlier.
--
-- captured_at stays the ordering key because it is when the person actually
-- agreed. recorded_seq only decides ties.

BEGIN;

ALTER TABLE consent_records
    ADD COLUMN recorded_seq BIGSERIAL,
    ALTER COLUMN captured_at SET DEFAULT clock_timestamp();

CREATE OR REPLACE VIEW v_current_consent AS
SELECT DISTINCT ON (candidate_id, purpose)
       candidate_id, purpose, policy_version, granted, captured_at
FROM consent_records
ORDER BY candidate_id, purpose, captured_at DESC, recorded_seq DESC;

GRANT USAGE, SELECT ON SEQUENCE consent_records_recorded_seq_seq
    TO app_operations;

COMMIT;
