-- 013. TAMPER-EVIDENT AUDIT LOG
--
-- The audit log lives in the database it audits. Anyone with write access to
-- that database can edit or delete a row -- including the row recording that
-- they read someone's national ID. Until now, nothing would show it.
--
-- Each entry is chained to the one before it: entry_hash = SHA-256 over this
-- row's contents plus the previous row's entry_hash. Changing any historical
-- row, or removing one, breaks every link after it, and verify_audit_chain()
-- reports exactly where.
--
-- What this does and does not give you:
--   DOES  make silent tampering detectable -- an edited or deleted row cannot
--         be hidden without recomputing the entire subsequent chain.
--   DOES  give the NCSA an integrity claim that can be checked, not asserted.
--   NOT   prevent tampering. An attacker with enough access and time can
--         recompute the chain. The defence against that is shipping the head
--         hash off-box (see docs/DEPLOYMENT.md) -- once a hash is published
--         elsewhere, no local rewrite can match it.
--
-- Cost is one extra SELECT and one hash per audit row. At pilot volume that is
-- nothing, and the alternative is an evidence trail nobody can vouch for.

BEGIN;

-- digest() lives in pgcrypto. Added here rather than in 000 so this migration
-- is self-contained: the chain is the only thing that needs it.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE audit_log
    ADD COLUMN prev_hash  BYTEA,
    ADD COLUMN entry_hash BYTEA;

-- Deterministic serialisation of the fields that matter. Column order is fixed
-- and separators are explicit so that no two distinct rows can serialise
-- identically -- without the separators, ('ab','c') and ('a','bc') would.
CREATE OR REPLACE FUNCTION audit_entry_payload(
    p_audit_id    BIGINT,
    p_staff_id    UUID,
    p_table_name  VARCHAR,
    p_record_id   UUID,
    p_action      VARCHAR,
    p_occurred_at TIMESTAMPTZ,
    p_detail      JSONB,
    p_prev_hash   BYTEA
) RETURNS BYTEA AS $$
    SELECT digest(
        concat_ws(E'\x1f',
            p_audit_id::text,
            COALESCE(p_staff_id::text, ''),
            p_table_name,
            p_record_id::text,
            p_action,
            -- Fixed ISO format: the session's DateStyle must not change the hash.
            to_char(p_occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
            COALESCE(p_detail::text, ''),
            COALESCE(encode(p_prev_hash, 'hex'), '')
        ), 'sha256');
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION fn_audit_chain() RETURNS TRIGGER AS $$
DECLARE
    v_prev BYTEA;
BEGIN
    -- Serialise appenders so two concurrent inserts cannot read the same head
    -- and fork the chain. Transaction-scoped; released at commit.
    PERFORM pg_advisory_xact_lock(hashtext('audit_log_chain'));

    SELECT entry_hash INTO v_prev
      FROM audit_log
     WHERE entry_hash IS NOT NULL
     ORDER BY audit_id DESC
     LIMIT 1;

    NEW.prev_hash  := v_prev;
    NEW.entry_hash := audit_entry_payload(
        NEW.audit_id, NEW.staff_id, NEW.table_name, NEW.record_id,
        NEW.action, NEW.occurred_at, NEW.detail, v_prev);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_chain
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION fn_audit_chain();

-- Append-only. The chain makes tampering *detectable*; these rules make the
-- obvious routes fail outright.
CREATE RULE audit_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

-- Walks the chain and reports the first row whose hash does not reconcile.
-- Returns no rows when the log is intact.
CREATE OR REPLACE FUNCTION verify_audit_chain()
RETURNS TABLE (
    broken_at_audit_id BIGINT,
    reason             TEXT
) AS $$
DECLARE
    r          RECORD;
    v_expected BYTEA;
    v_prev     BYTEA := NULL;
BEGIN
    FOR r IN SELECT * FROM audit_log ORDER BY audit_id LOOP
        IF r.prev_hash IS DISTINCT FROM v_prev THEN
            broken_at_audit_id := r.audit_id;
            reason := 'prev_hash does not match the preceding entry -- '
                      'a row was removed or reordered';
            RETURN NEXT;
            RETURN;
        END IF;

        v_expected := audit_entry_payload(
            r.audit_id, r.staff_id, r.table_name, r.record_id,
            r.action, r.occurred_at, r.detail, r.prev_hash);

        IF r.entry_hash IS DISTINCT FROM v_expected THEN
            broken_at_audit_id := r.audit_id;
            reason := 'entry_hash does not match the row contents -- '
                      'this row was modified';
            RETURN NEXT;
            RETURN;
        END IF;

        v_prev := r.entry_hash;
    END LOOP;
END;
$$ LANGUAGE plpgsql STABLE;

-- The current head. Publish this somewhere outside the server -- once a hash
-- is recorded elsewhere, no local rewrite of history can match it.
CREATE OR REPLACE FUNCTION audit_chain_head()
RETURNS TABLE (audit_id BIGINT, entry_hash TEXT, entries BIGINT) AS $$
    SELECT a.audit_id,
           encode(a.entry_hash, 'hex'),
           (SELECT count(*) FROM audit_log)
      FROM audit_log a
     ORDER BY a.audit_id DESC
     LIMIT 1;
$$ LANGUAGE sql STABLE;

COMMIT;
