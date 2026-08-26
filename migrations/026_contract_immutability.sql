-- 026. CONTRACT IMMUTABILITY, BY TRIGGER RATHER THAN RULE
--
-- Migration 024 protected the terms with a conditional ON UPDATE DO INSTEAD
-- NOTHING rule. That works, but PostgreSQL then refuses UPDATE ... RETURNING
-- on the table at all -- and acknowledgement needs it, to tell "recorded" from
-- "there was nothing to record".
--
-- A trigger gives the same protection without disabling RETURNING, and it
-- raises rather than silently discarding the write. Silence is right for
-- consent_records, where an ORM might blindly re-save a row; here an attempt
-- to alter an agreement after the fact should be loud.

BEGIN;

DROP RULE IF EXISTS contract_terms_immutable ON placement_contracts;

CREATE OR REPLACE FUNCTION fn_contract_terms_immutable() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.terms IS DISTINCT FROM OLD.terms THEN
        RAISE EXCEPTION
            'the agreed terms of contract % cannot be changed', OLD.contract_ref
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.contract_ref IS DISTINCT FROM OLD.contract_ref THEN
        RAISE EXCEPTION 'a contract reference cannot be reassigned'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_contract_terms_immutable
    BEFORE UPDATE ON placement_contracts
    FOR EACH ROW EXECUTE FUNCTION fn_contract_terms_immutable();

COMMIT;
