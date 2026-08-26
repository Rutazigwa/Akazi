-- 024. PLACEMENT CONTRACTS
--
-- placements.contract_ref has existed since the first migration with nothing
-- writing it. The blueprint lists placement contracts among the seven things
-- for weeks 1-6, and there is a concrete need underneath: when a pay dispute
-- reaches the escalation path, nothing currently says what was agreed.
--
-- The terms are stored as a SNAPSHOT, not read back from the live rows. A work
-- request can be edited after someone accepts it -- the shift moved, the rate
-- changed -- and a contract that quietly follows those edits is not a record of
-- an agreement, it is a record of the current intention. In a dispute the
-- question is what the worker was told when they said yes, and only a snapshot
-- answers it.
--
-- Both sides acknowledge separately. An employer confirming terms the worker
-- never saw is how informal work already goes wrong.

BEGIN;

CREATE SEQUENCE placement_contract_seq;

CREATE TABLE placement_contracts (
    contract_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    placement_id UUID NOT NULL UNIQUE REFERENCES placements(placement_id)
                 ON DELETE CASCADE,
    -- Human-readable and quotable over the phone. A UUID is unusable for
    -- someone reading a reference off a printed page to a coordinator.
    contract_ref VARCHAR(32) NOT NULL UNIQUE,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    issued_by    UUID REFERENCES staff(staff_id),
    -- The agreed terms as they stood at acceptance. Never recomputed.
    terms        JSONB NOT NULL,
    worker_acknowledged_at   TIMESTAMPTZ,
    employer_acknowledged_at TIMESTAMPTZ,
    employer_acknowledged_by UUID REFERENCES employer_contacts(contact_id)
);

CREATE INDEX idx_contracts_unacknowledged
    ON placement_contracts (issued_at)
 WHERE worker_acknowledged_at IS NULL OR employer_acknowledged_at IS NULL;

-- Contracts are evidence of what was agreed. Editing one after the fact is
-- exactly what a party to a dispute would want to do.
CREATE RULE contract_terms_immutable AS
    ON UPDATE TO placement_contracts
    WHERE NEW.terms IS DISTINCT FROM OLD.terms
       OR NEW.contract_ref IS DISTINCT FROM OLD.contract_ref
    DO INSTEAD NOTHING;

CREATE OR REPLACE FUNCTION next_contract_ref() RETURNS TEXT AS $$
    SELECT 'AKZ-' || to_char(CURRENT_DATE, 'YYYY') || '-'
           || lpad(nextval('placement_contract_seq')::text, 5, '0');
$$ LANGUAGE sql VOLATILE;

GRANT SELECT, INSERT, UPDATE ON placement_contracts TO app_operations;
GRANT USAGE ON SEQUENCE placement_contract_seq TO app_operations;
GRANT EXECUTE ON FUNCTION next_contract_ref() TO app_operations;

COMMIT;
