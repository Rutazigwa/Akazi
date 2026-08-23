-- 005. WORK REQUESTS

BEGIN;

CREATE TYPE work_type AS ENUM
    ('shift','internship','apprenticeship','fixed_term','project');

CREATE TYPE request_status AS ENUM
    ('open','filling','filled','cancelled','expired');

CREATE TABLE work_requests (
    request_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employer_id       UUID         NOT NULL REFERENCES employers(employer_id),
    title             VARCHAR(140) NOT NULL,
    work_type         work_type    NOT NULL,
    headcount         SMALLINT     NOT NULL CHECK (headcount > 0),
    starts_on         DATE         NOT NULL,
    ends_on           DATE,
    shift_start       TIME,
    shift_end         TIME,
    pay_rwf           INTEGER      NOT NULL CHECK (pay_rwf > 0),
    pay_unit          VARCHAR(12)  NOT NULL
                      CHECK (pay_unit IN ('day','hour','month','task')),
    -- First-class, not a note: participates in matching filter 2 and in the
    -- net-earnings-after-transport outcome metric.
    transport_covered BOOLEAN      NOT NULL DEFAULT FALSE,
    meals_provided    BOOLEAN      NOT NULL DEFAULT FALSE,
    safety_notes      TEXT,
    status            request_status NOT NULL DEFAULT 'open',
    opened_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    filled_at         TIMESTAMPTZ,
    CONSTRAINT chk_dates CHECK (ends_on IS NULL OR ends_on >= starts_on),
    CONSTRAINT chk_filled CHECK (
        (status = 'filled' AND filled_at IS NOT NULL)
        OR (status <> 'filled')
    )
);

CREATE INDEX idx_request_status   ON work_requests (status);
CREATE INDEX idx_request_employer ON work_requests (employer_id);
CREATE INDEX idx_request_starts   ON work_requests (starts_on);

CREATE TABLE request_skills (
    request_id   UUID     NOT NULL REFERENCES work_requests(request_id)
                 ON DELETE CASCADE,
    skill_id     UUID     NOT NULL REFERENCES skills(skill_id),
    min_score    SMALLINT NOT NULL DEFAULT 3,
    PRIMARY KEY (request_id, skill_id)
);

GRANT SELECT, INSERT, UPDATE ON work_requests, request_skills TO app_operations;

COMMIT;
