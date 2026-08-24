-- 019. MESSAGE OUTBOX
--
-- WhatsApp and SMS carry intake, shift reminders and the day-1 / week-1 /
-- day-30 follow-ups. Three decisions are load-bearing here.
--
-- 1. Outbox, not direct send. A message is written in the same transaction as
--    the thing that caused it. If the request then fails, the message is rolled
--    back with it; if the send fails, the row is still there to retry. Sending
--    inline would lose messages on a rollback and duplicate them on a retry --
--    and a worker who gets two conflicting shift reminders stops trusting them.
--
-- 2. No phone numbers in this table. The recipient is a candidate_id or a
--    contact_id, resolved to a number at dispatch time. Phone numbers are
--    residency-sensitive and live in candidate_identity; copying them into an
--    operational queue would quietly create a second store of personal data
--    outside the boundary the whole schema is built around.
--
-- 3. Rendered body IS stored. It is what the person actually received, and if
--    someone disputes what they were told about pay, the template rendered
--    later with today's data is not evidence. Bodies are operational text, not
--    identity data.

BEGIN;

CREATE TYPE message_channel AS ENUM ('whatsapp', 'sms');

CREATE TYPE message_status AS ENUM
    ('queued','sending','sent','delivered','failed','cancelled','suppressed');

CREATE TABLE messages (
    message_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Exactly one recipient kind. A message with neither has nobody to go to;
    -- one with both is ambiguous about whose consent applies.
    candidate_id     UUID REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    contact_id       UUID REFERENCES employer_contacts(contact_id) ON DELETE CASCADE,
    channel          message_channel NOT NULL DEFAULT 'whatsapp',
    template_key     VARCHAR(60)  NOT NULL,
    body             TEXT         NOT NULL,
    -- What this message is about, for threading and for cancellation: a
    -- reminder for a placement that got cancelled must not still go out.
    placement_id     UUID REFERENCES placements(placement_id) ON DELETE CASCADE,
    status           message_status NOT NULL DEFAULT 'queued',
    scheduled_for    TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    attempts         SMALLINT     NOT NULL DEFAULT 0,
    last_attempt_at  TIMESTAMPTZ,
    sent_at          TIMESTAMPTZ,
    delivered_at     TIMESTAMPTZ,
    provider_ref     VARCHAR(120),
    last_error       TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT chk_one_recipient CHECK (
        (candidate_id IS NOT NULL AND contact_id IS NULL)
        OR (candidate_id IS NULL AND contact_id IS NOT NULL)
    )
);

CREATE INDEX idx_messages_due
    ON messages (scheduled_for) WHERE status = 'queued';
CREATE INDEX idx_messages_placement ON messages (placement_id);
CREATE INDEX idx_messages_candidate ON messages (candidate_id);

-- Some messages must not be sent twice however many times the causing action
-- runs -- one shift reminder per placement, one day-30 nudge. Others (an ad-hoc
-- note) legitimately repeat, so this is a partial index on the keys that are
-- once-only rather than a blanket constraint.
CREATE UNIQUE INDEX idx_messages_once_per_placement
    ON messages (placement_id, template_key)
 WHERE placement_id IS NOT NULL
   AND template_key IN ('placement_offer','shift_reminder',
                        'followup_day_1','followup_week_1',
                        'followup_day_30','followup_day_90');

-- Resolving a recipient to a phone number is an identity read, so it goes
-- through a SECURITY DEFINER function and lands in audit_log like any other.
-- Reminders are frequent, so the detail says why -- an auditor seeing hundreds
-- of reads should be able to tell routine messaging from someone browsing.
CREATE OR REPLACE FUNCTION message_recipient_phone(p_message_id UUID)
RETURNS TABLE (phone TEXT, is_candidate BOOLEAN) AS $$
DECLARE
    m RECORD;
BEGIN
    SELECT candidate_id, contact_id INTO m
      FROM messages WHERE message_id = p_message_id;

    IF m.candidate_id IS NOT NULL THEN
        INSERT INTO audit_log (staff_id, table_name, record_id, action, detail)
        VALUES (current_staff_id(), 'candidate_identity', m.candidate_id, 'read',
                jsonb_build_object('purpose', 'messaging',
                                   'message_id', p_message_id));

        RETURN QUERY
        SELECT ci.phone_primary::text, true
          FROM candidate_identity ci
         WHERE ci.candidate_id = m.candidate_id
           AND ci.erased_at IS NULL
           AND ci.phone_primary IS NOT NULL;
    ELSE
        RETURN QUERY
        SELECT ec.phone::text, false
          FROM employer_contacts ec
         WHERE ec.contact_id = m.contact_id AND ec.is_active;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

REVOKE ALL ON FUNCTION message_recipient_phone(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION message_recipient_phone(UUID)
    TO app_operations, app_identity;

GRANT SELECT, INSERT, UPDATE ON messages TO app_operations, app_identity;

COMMIT;
