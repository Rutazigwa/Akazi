-- 040. MONEY COULD BE TAKEN OFF A WAGE WITH NO REASON RECORDED
--
-- pay_records.deductions_rwf is a bare integer. Nothing anywhere says what a
-- deduction was for. "Whether the money moves correctly" is one of the four
-- gaps this business exists to close, and it is not only about the money
-- arriving late -- it is about arriving short.
--
-- The people being placed are 16-to-30-year-olds in their first formal work,
-- with no payslip, no union and little bargaining power. An unexplained
-- deduction is the oldest way to quietly reduce a wage, and a system that
-- records the amount but not the reason is a system that helps.
--
-- So a deduction must be itemised. The check is a DEFERRABLE constraint
-- trigger, fired at commit rather than at statement time, because the lines
-- are necessarily written after the pay record they belong to -- an immediate
-- check would refuse every correct sequence.

BEGIN;

CREATE TABLE pay_deductions (
    deduction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pay_id       UUID NOT NULL REFERENCES pay_records(pay_id) ON DELETE CASCADE,
    -- A closed list. Free text would become "other" for everything, and the
    -- point is to be able to answer "what is being deducted, across all our
    -- employers" without reading a thousand notes.
    kind         VARCHAR(20) NOT NULL CHECK (kind IN (
                     'advance',      -- money already paid to the worker
                     'uniform',
                     'equipment',
                     'transport',    -- employer-arranged, deducted at cost
                     'absence',      -- days not worked
                     'statutory',    -- tax or social security
                     'damage',
                     'other'
                 )),
    amount_rwf   INTEGER NOT NULL CHECK (amount_rwf > 0),
    -- Required for the two kinds most open to abuse. A damage or "other"
    -- deduction without a written reason is precisely the thing this table
    -- exists to make impossible.
    note         TEXT,
    recorded_by  UUID REFERENCES staff(staff_id),
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT chk_deduction_explained CHECK (
        kind NOT IN ('damage', 'other')
        OR (note IS NOT NULL AND length(btrim(note)) >= 10)
    )
);

CREATE INDEX idx_pay_deductions_pay ON pay_deductions (pay_id);

COMMENT ON TABLE pay_deductions IS
    'What each deduction was for. A wage reduced without a stated reason is '
    'how a worker with no payslip gets underpaid.';

CREATE OR REPLACE FUNCTION fn_deductions_itemised() RETURNS TRIGGER AS $$
DECLARE
    v_pay     UUID := COALESCE(NEW.pay_id, OLD.pay_id);
    v_header  INTEGER;
    v_lines   INTEGER;
BEGIN
    IF TG_TABLE_NAME = 'pay_records' THEN
        v_pay := COALESCE(NEW.pay_id, OLD.pay_id);
    END IF;

    SELECT deductions_rwf INTO v_header
      FROM pay_records WHERE pay_id = v_pay;

    -- The parent is gone; the cascade is taking the lines with it.
    IF v_header IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT COALESCE(sum(amount_rwf), 0) INTO v_lines
      FROM pay_deductions WHERE pay_id = v_pay;

    IF v_header <> v_lines THEN
        RAISE EXCEPTION
            'deductions on pay record % total RWF % but the itemised lines '
            'come to RWF %. Every deduction needs a stated reason',
            v_pay, v_header, v_lines;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Deferred to commit: the lines are written after the record they belong to,
-- so an immediate check would refuse every correct sequence.
CREATE CONSTRAINT TRIGGER trg_pay_deductions_itemised
    AFTER INSERT OR UPDATE OF deductions_rwf ON pay_records
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_deductions_itemised();

CREATE CONSTRAINT TRIGGER trg_deduction_lines_match
    AFTER INSERT OR UPDATE OR DELETE ON pay_deductions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_deductions_itemised();

-- What the attendance record says the worker earned, beside what was actually
-- recorded as owed. A gross figure below what the days worked imply is not
-- proof of anything -- rates change, a half day happens -- but it is the
-- question worth asking before the money moves, not after.
CREATE VIEW v_pay_expected AS
SELECT pr.pay_id,
       pr.placement_id,
       pr.gross_rwf,
       pr.deductions_rwf,
       pr.net_rwf,
       p.agreed_pay_rwf,
       p.pay_unit,
       att.days_present,
       CASE WHEN p.pay_unit = 'day'
            THEN p.agreed_pay_rwf * att.days_present
            ELSE NULL END                            AS expected_gross_rwf,
       CASE WHEN p.pay_unit = 'day'
            THEN pr.gross_rwf - (p.agreed_pay_rwf * att.days_present)
            ELSE NULL END                            AS variance_rwf
  FROM pay_records pr
  JOIN placements p ON p.placement_id = pr.placement_id
  JOIN LATERAL (
      SELECT count(*) FILTER (WHERE a.present) AS days_present
        FROM attendance a
       WHERE a.placement_id = pr.placement_id
         AND a.work_date BETWEEN pr.period_start AND pr.period_end
  ) att ON TRUE;

COMMENT ON VIEW v_pay_expected IS
    'Recorded pay against what confirmed attendance implies. A shortfall is a '
    'question to ask before the money moves, not after.';

GRANT SELECT, INSERT, UPDATE, DELETE ON pay_deductions TO app_operations;
GRANT SELECT ON v_pay_expected TO app_operations;

COMMIT;
