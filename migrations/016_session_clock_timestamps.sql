-- 016. DISTINCT TIMESTAMPS FOR SESSIONS AND AUDIT ENTRIES
--
-- Same trap as migration 011. now() is transaction start time in PostgreSQL, so
-- every row written in one transaction shares it. Ordering sessions by
-- issued_at, or audit entries by occurred_at, is then arbitrary within a
-- transaction.
--
-- In production these are written one per transaction so it rarely bites --
-- which is precisely why it would be a nasty surprise later. clock_timestamp()
-- advances within a transaction and costs nothing.
--
-- audit_log ordering does not depend on this (audit_id is a sequence, and the
-- hash chain follows it), but an evidence trail where three events share a
-- timestamp to the microsecond is harder to explain than one where they do not.

BEGIN;

ALTER TABLE staff_sessions ALTER COLUMN issued_at SET DEFAULT clock_timestamp();
ALTER TABLE audit_log      ALTER COLUMN occurred_at SET DEFAULT clock_timestamp();

COMMIT;
