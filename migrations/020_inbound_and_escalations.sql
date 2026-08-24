-- 020. INBOUND REPLIES AND ESCALATIONS
--
-- Two things this closes.
--
-- The outbound templates ask people to reply -- "Reply YES to accept", "tell us
-- if there is a problem with pay, hours, transport or safety". Nothing read
-- those replies. Asking a worker to report a problem and then not listening is
-- worse than not asking: it teaches them the channel is decorative, and the one
-- time it matters they will not use it.
--
-- And the blueprint requires an in-app harassment report with a NAMED
-- escalation path and a DEFINED response time. follow_ups.issue_flag has
-- carried 'harassment' since the first migration, but nothing escalated it. A
-- flag in a database that nobody is accountable for answering is a reporting
-- line, not a safeguard.
--
-- Escalations therefore have an owner and a deadline, and both are recorded
-- rather than assumed.

BEGIN;

CREATE TABLE inbound_messages (
    inbound_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Who it came from, as delivered by the provider. Stored because we may
    -- not recognise the number: an unmatched inbound message is still evidence
    -- somebody tried to reach us, and dropping it loses that.
    from_phone    VARCHAR(20) NOT NULL,
    channel       message_channel NOT NULL DEFAULT 'whatsapp',
    body          TEXT NOT NULL,
    provider_ref  VARCHAR(120) UNIQUE,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Resolved during handling; null when the number matches nobody.
    candidate_id  UUID REFERENCES candidates(candidate_id) ON DELETE SET NULL,
    contact_id    UUID REFERENCES employer_contacts(contact_id) ON DELETE SET NULL,
    -- What we decided it meant, and what we did. Null intent means we could not
    -- tell, which is a queue for a human rather than a thing to discard.
    intent        VARCHAR(30),
    handled_at    TIMESTAMPTZ,
    handling_note TEXT
);

CREATE INDEX idx_inbound_unhandled
    ON inbound_messages (received_at) WHERE handled_at IS NULL;
CREATE INDEX idx_inbound_candidate ON inbound_messages (candidate_id);

CREATE TYPE escalation_kind AS ENUM
    ('harassment','safety','pay','transport','hours','other');

CREATE TYPE escalation_status AS ENUM
    ('open','acknowledged','resolved','closed_no_action');

CREATE TABLE escalations (
    escalation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind           escalation_kind NOT NULL,
    candidate_id   UUID REFERENCES candidates(candidate_id) ON DELETE CASCADE,
    placement_id   UUID REFERENCES placements(placement_id) ON DELETE SET NULL,
    -- Where it came from: an inbound reply, a follow-up call, a coordinator.
    inbound_id     UUID REFERENCES inbound_messages(inbound_id),
    follow_up_id   UUID REFERENCES follow_ups(follow_up_id),
    raised_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- The named path. Not a role in the abstract: a person, recorded at the
    -- moment it was raised, so "who was supposed to deal with this" has an
    -- answer months later even if the rota has changed since.
    owner_staff_id UUID NOT NULL REFERENCES staff(staff_id),
    -- The defined response time. Set from the kind when raised.
    respond_by     TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by UUID REFERENCES staff(staff_id),
    resolved_at    TIMESTAMPTZ,
    resolved_by    UUID REFERENCES staff(staff_id),
    status         escalation_status NOT NULL DEFAULT 'open',
    detail         TEXT,
    resolution     TEXT,
    CONSTRAINT chk_escalation_resolution CHECK (
        (status IN ('resolved','closed_no_action')
         AND resolved_at IS NOT NULL AND resolution IS NOT NULL)
        OR status IN ('open','acknowledged')
    )
);

CREATE INDEX idx_escalations_open
    ON escalations (respond_by) WHERE status IN ('open','acknowledged');
CREATE INDEX idx_escalations_candidate ON escalations (candidate_id);

-- Whether we met our own response times, by kind. This is the number that
-- shows whether the safeguard is real -- an escalation process nobody measures
-- decays into a form.
CREATE VIEW v_escalation_response AS
SELECT e.escalation_id,
       e.kind::text AS kind,
       e.raised_at,
       e.respond_by,
       e.acknowledged_at,
       e.status::text AS status,
       (e.acknowledged_at IS NOT NULL AND e.acknowledged_at <= e.respond_by)
           AS answered_in_time,
       (e.acknowledged_at IS NULL AND now() > e.respond_by) AS overdue,
       ROUND(EXTRACT(EPOCH FROM (
           COALESCE(e.acknowledged_at, now()) - e.raised_at)) / 3600.0, 2)
           AS hours_to_acknowledge
FROM escalations e;

GRANT SELECT, INSERT, UPDATE ON inbound_messages, escalations
    TO app_operations, app_identity;
GRANT SELECT ON v_escalation_response TO app_operations;

COMMIT;
