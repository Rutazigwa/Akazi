-- 003. SKILLS & ASSESSMENT
-- Table stakes, not a differentiator (DEVY's Skills Passport is the state-backed
-- version). Kept because matching filter 1 needs a score to filter on.

BEGIN;

CREATE TABLE skills (
    skill_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_code  VARCHAR(40)  NOT NULL UNIQUE,
    skill_name  VARCHAR(120) NOT NULL,
    category    VARCHAR(60)  NOT NULL   -- hospitality | retail | trades | soft
);

CREATE TABLE assessments (
    assessment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id      UUID NOT NULL REFERENCES skills(skill_id),
    title         VARCHAR(140) NOT NULL,
    method        VARCHAR(40)  NOT NULL,  -- practical | observed | written
    max_score     SMALLINT     NOT NULL DEFAULT 5,
    pass_score    SMALLINT     NOT NULL,
    rubric        TEXT,
    CONSTRAINT chk_pass_score CHECK (pass_score BETWEEN 0 AND max_score)
);

CREATE TABLE assessment_results (
    result_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id  UUID NOT NULL REFERENCES candidates(candidate_id)
                  ON DELETE CASCADE,
    assessment_id UUID NOT NULL REFERENCES assessments(assessment_id),
    score         SMALLINT NOT NULL CHECK (score >= 0),
    assessed_by   UUID NOT NULL REFERENCES staff(staff_id),
    assessed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes         TEXT,
    UNIQUE (candidate_id, assessment_id, assessed_at)
);

CREATE INDEX idx_result_cand ON assessment_results (candidate_id);

GRANT SELECT, INSERT, UPDATE ON skills, assessments, assessment_results
    TO app_operations;

COMMIT;
