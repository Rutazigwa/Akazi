-- 038. A DEFINED RESPONSE TIME THAT NOTHING ENFORCED
--
-- The blueprint promises an in-app harassment report with a named escalation
-- path and a defined response time. The path is named and the time is stored
-- in escalations.respond_by -- and when it passes, the only thing that happens
-- is that a pill turns red on a page.
--
-- If nobody has that page open, nothing happens at all. Evening, weekend, a
-- coordinator out doing a site visit: a woman's harassment report sits
-- unacknowledged and the system is content. A response time nobody is told
-- about is the same shape of defect as a readonly role that permits writes --
-- the promise exists, the mechanism does not.
--
-- Two things are needed. Staff have to be reachable through the outbox, which
-- until now could only address candidates and employer contacts; and a breach
-- has to be recorded, so the alert goes once rather than every five minutes
-- until someone acknowledges it.

BEGIN;

ALTER TABLE messages
    ADD COLUMN staff_id UUID REFERENCES staff(staff_id) ON DELETE CASCADE;

-- Exactly one recipient, now of three kinds. A message with none has nobody
-- to go to; one with two is ambiguous about whose consent applies -- and for
-- staff there is no consent question at all, which is precisely why they must
-- not be conflated.
ALTER TABLE messages DROP CONSTRAINT chk_one_recipient;
ALTER TABLE messages ADD CONSTRAINT chk_one_recipient CHECK (
    (candidate_id IS NOT NULL)::int
  + (contact_id   IS NOT NULL)::int
  + (staff_id     IS NOT NULL)::int = 1
);

CREATE INDEX idx_messages_staff ON messages (staff_id)
    WHERE staff_id IS NOT NULL;

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
                jsonb_build_object('purpose', 'messaging',
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

REVOKE ALL ON FUNCTION message_recipient_phone(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION message_recipient_phone(UUID)
    TO app_operations, app_identity;

-- Recorded so the alert is raised once. Re-alerting every five minutes until
-- someone acknowledges is how an alert becomes noise, and noise is how the
-- next one gets ignored.
ALTER TABLE escalations ADD COLUMN breach_alerted_at TIMESTAMPTZ;

COMMENT ON COLUMN escalations.breach_alerted_at IS
    'When the missed response time was raised with someone. Set once.';

COMMIT;
