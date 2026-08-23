-- 007. ATTENDANCE, PAY RECORDS, FOLLOW-UP
--
-- Attendance is the product. Everything above this file is setup; this is where
-- the reliability guarantee is proved or disproved.

BEGIN;

CREATE TABLE attendance (
    attendance_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    placement_id   UUID NOT NULL REFERENCES placements(placement_id)
                   ON DELETE CASCADE,
    work_date      DATE NOT NULL,
    present        BOOLEAN NOT NULL,
    hours_worked   NUMERIC(4,2) CHECK (hours_worked >= 0),
    confirmed_by   VARCHAR(20) NOT NULL
                   CHECK (confirmed_by IN ('employer','coordinator','worker')),
    confirmed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    absence_reason TEXT,
    UNIQUE (placement_id, work_date),
    CONSTRAINT chk_absence_reason
        CHECK (present OR absence_reason IS NOT NULL)
);

CREATE INDEX idx_attendance_date ON attendance (work_date);

-- pay_records are RECORDS, not instructions to move money. No payment
-- integration in the pilot: terms, amounts and dates are captured as data so
-- that pay accuracy (>= 95% paid in full, on the agreed date) is measurable
-- before any regulatory work on wage rails begins.
CREATE TABLE pay_records (
    pay_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    placement_id     UUID NOT NULL REFERENCES placements(placement_id)
                     ON DELETE CASCADE,
    period_start     DATE NOT NULL,
    period_end       DATE NOT NULL,
    gross_rwf        INTEGER NOT NULL CHECK (gross_rwf >= 0),
    deductions_rwf   INTEGER NOT NULL DEFAULT 0 CHECK (deductions_rwf >= 0),
    net_rwf          INTEGER GENERATED ALWAYS AS
                     (gross_rwf - deductions_rwf) STORED,
    -- The date the employer agreed to pay, vs the date it actually landed.
    -- Both are needed: pay accuracy is "in full AND on the agreed date".
    due_on           DATE,
    paid_on          DATE,
    method           VARCHAR(20) CHECK (method IN ('momo','cash','bank')),
    worker_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT chk_pay_period CHECK (period_end >= period_start),
    CONSTRAINT chk_deductions CHECK (deductions_rwf <= gross_rwf)
);

CREATE INDEX idx_pay_placement ON pay_records (placement_id);

CREATE TYPE checkpoint AS ENUM ('day_1','week_1','day_30','day_90');

CREATE TABLE follow_ups (
    follow_up_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    placement_id    UUID NOT NULL REFERENCES placements(placement_id)
                    ON DELETE CASCADE,
    checkpoint      checkpoint NOT NULL,
    due_on          DATE NOT NULL,
    completed_at    TIMESTAMPTZ,
    still_working   BOOLEAN,
    worker_rating   SMALLINT CHECK (worker_rating BETWEEN 1 AND 5),
    employer_rating SMALLINT CHECK (employer_rating BETWEEN 1 AND 5),
    -- harassment is a first-class flag, not a free-text note: it triggers the
    -- named escalation path with a defined response time.
    issue_flag      VARCHAR(40)
                    CHECK (issue_flag IN
                        ('pay','safety','harassment','transport','hours')),
    notes           TEXT,
    UNIQUE (placement_id, checkpoint)
);

CREATE INDEX idx_followup_due
    ON follow_ups (due_on) WHERE completed_at IS NULL;
CREATE INDEX idx_followup_issue
    ON follow_ups (issue_flag) WHERE issue_flag IS NOT NULL;

GRANT SELECT, INSERT, UPDATE ON attendance, pay_records, follow_ups
    TO app_operations;

COMMIT;
