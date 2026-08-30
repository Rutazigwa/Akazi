-- The one field a woman's safety turns on was a boolean nobody could change.
--
-- The after-dark filter refuses to send a woman to a shift ending after dark
-- unless the employer covers transport or she has opted in. That opt-in is the
-- escape the blueprint names, and it belongs to her -- a coordinator cannot
-- consent on her behalf.
--
-- It was candidates.accepts_after_dark: a boolean, set once at registration,
-- with no update path anywhere in the application. Two consequences.
--
-- She could not change her mind in either direction. Somebody who said no to a
-- stranger with a clipboard -- the sensible cautious answer -- was excluded
-- from evening work permanently. Worse, somebody who said yes could never
-- withdraw it. A safety consent that cannot be revoked is not a consent.
--
-- And it could not answer the question it exists for. CLAUDE.md rule 7:
-- "Consent is an append-only record with a version, never a boolean on the
-- profile. We must be able to prove what someone agreed to and when." If a
-- woman is sent to a shift finishing at 22:00 and something happens, "did she
-- agree to that, and when, and who wrote it down" is exactly what has to be
-- answerable. A boolean has no timestamp, no author and no policy version.
--
-- So it becomes what the rule already required: a consent record, append-only,
-- superseded by recording a new one. The column stays as a generated read of
-- the current state, so every existing reader keeps working and no code can
-- write to it by accident.

-- 'migrated' is a real answer to "how was this captured": nobody witnessed it,
-- it was transcribed from a boolean. Saying so is better than borrowing
-- 'paper' and implying a signature that does not exist.
--
-- Without this the backfill below fails, and it fails only on a database that
-- has candidates in it -- which is every database this migration is for, and
-- none of the empty ones it was first tested against.
ALTER TABLE consent_records DROP CONSTRAINT consent_records_captured_via_check;
ALTER TABLE consent_records ADD CONSTRAINT consent_records_captured_via_check
    CHECK (captured_via IN ('paper', 'whatsapp', 'app', 'migrated'));

ALTER TABLE consent_records DROP CONSTRAINT consent_records_purpose_check;
ALTER TABLE consent_records ADD CONSTRAINT consent_records_purpose_check
    CHECK (purpose IN ('placement', 'training', 'reporting', 'after_dark'));

COMMENT ON COLUMN consent_records.purpose IS
    'What she agreed to. after_dark is a safety opt-in rather than a data '
    'processing one, and is here for the same reason: it must be provable, '
    'attributable and revocable.';

-- Carry the existing booleans over, so nobody silently loses an opt-in they
-- already gave. captured_via 'migrated' says plainly that this row is a
-- transcription rather than something somebody witnessed.
INSERT INTO consent_records (candidate_id, policy_version, purpose, granted,
                             captured_via, captured_at)
SELECT candidate_id, 'v1.0', 'after_dark', accepts_after_dark, 'migrated',
       created_at
  FROM candidates;

-- The column becomes a read of the consent record rather than a thing to set.
-- A plain view column would have meant changing every reader; this way
-- app/matching/repository.py and the rest are untouched, and an UPDATE that
-- tries to set it now fails loudly instead of quietly disagreeing with the
-- record.
ALTER TABLE candidates DROP COLUMN accepts_after_dark;

CREATE OR REPLACE FUNCTION accepts_after_dark(p_candidate_id UUID)
RETURNS BOOLEAN AS $$
    SELECT COALESCE(
        (SELECT granted FROM v_current_consent
          WHERE candidate_id = p_candidate_id AND purpose = 'after_dark'),
        FALSE)
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION accepts_after_dark(UUID) IS
    'Whether she currently agrees to shifts ending after dark. The latest '
    'consent row wins, so withdrawing is recording a new one.';

GRANT EXECUTE ON FUNCTION accepts_after_dark(UUID)
    TO app_operations, app_identity;
