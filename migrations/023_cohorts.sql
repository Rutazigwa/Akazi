-- 023. COHORTS
--
-- The last of the seven things the blueprint lists for weeks 1-6. Candidates
-- are prepared in groups: a week of orientation for a sector, run by a named
-- facilitator, before anyone is placed.
--
-- women_only is not a filter for convenience. The blueprint asks for
-- all-female cohort options as a concrete measure, alongside shift-time limits
-- and employer safety ratings, because female unemployment runs 15.5% against
-- 11.6% male and "track the gap" is not a plan. A woman who will not attend a
-- mixed session is not served by a system that offers her one anyway.
--
-- Enforced in the database rather than only in the application: the constraint
-- is a promise made to the people in the room, and it should not depend on
-- every future code path remembering it.

BEGIN;

CREATE TYPE cohort_status AS ENUM ('planned','running','completed','cancelled');

CREATE TYPE cohort_outcome AS ENUM ('completed','withdrew','did_not_finish');

CREATE TABLE cohorts (
    cohort_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR(120) NOT NULL,
    sector        VARCHAR(60),
    starts_on     DATE NOT NULL,
    ends_on       DATE,
    -- A person, not a role. "Who ran this cohort" needs an answer months
    -- later, and rotas change.
    facilitator   UUID NOT NULL REFERENCES staff(staff_id),
    women_only    BOOLEAN NOT NULL DEFAULT FALSE,
    capacity      SMALLINT CHECK (capacity IS NULL OR capacity > 0),
    location      VARCHAR(160),
    status        cohort_status NOT NULL DEFAULT 'planned',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT chk_cohort_dates CHECK (ends_on IS NULL OR ends_on >= starts_on)
);

CREATE INDEX idx_cohorts_open
    ON cohorts (starts_on) WHERE status IN ('planned','running');

CREATE TABLE cohort_members (
    cohort_id    UUID NOT NULL REFERENCES cohorts(cohort_id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(candidate_id)
                 ON DELETE CASCADE,
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    outcome      cohort_outcome,
    completed_at TIMESTAMPTZ,
    notes        TEXT,
    PRIMARY KEY (cohort_id, candidate_id),
    CONSTRAINT chk_member_outcome CHECK (
        (outcome IS NULL AND completed_at IS NULL)
        OR (outcome IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX idx_cohort_members_candidate ON cohort_members (candidate_id);

-- The women-only promise, enforced where it cannot be forgotten.
CREATE OR REPLACE FUNCTION fn_check_cohort_membership() RETURNS TRIGGER AS $$
DECLARE
    v_women_only BOOLEAN;
    v_gender     CHAR(1);
    v_capacity   SMALLINT;
    v_taken      INTEGER;
BEGIN
    SELECT women_only, capacity INTO v_women_only, v_capacity
      FROM cohorts WHERE cohort_id = NEW.cohort_id;
    SELECT gender INTO v_gender
      FROM candidates WHERE candidate_id = NEW.candidate_id;

    IF v_women_only AND v_gender IS DISTINCT FROM 'F' THEN
        RAISE EXCEPTION
            'this cohort is women-only; the promise is to the people in the room'
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_capacity IS NOT NULL THEN
        SELECT count(*) INTO v_taken
          FROM cohort_members
         WHERE cohort_id = NEW.cohort_id
           AND (TG_OP = 'INSERT' OR candidate_id <> NEW.candidate_id);
        IF v_taken >= v_capacity THEN
            RAISE EXCEPTION 'this cohort is full (% places)', v_capacity
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_cohort_membership
    BEFORE INSERT OR UPDATE OF candidate_id ON cohort_members
    FOR EACH ROW EXECUTE FUNCTION fn_check_cohort_membership();

-- Did the training help? Placement rate and retention for people who finished
-- a cohort, against those who never sat one.
CREATE VIEW v_cohort_outcomes AS
SELECT co.cohort_id,
       co.name,
       co.sector,
       co.women_only,
       co.starts_on,
       count(cm.candidate_id)                                    AS members,
       count(*) FILTER (WHERE cm.outcome = 'completed')          AS finished,
       count(*) FILTER (WHERE cm.outcome = 'withdrew')           AS withdrew,
       count(DISTINCT p.candidate_id) FILTER (
           WHERE p.status IN ('active','completed'))             AS placed_since
  FROM cohorts co
  LEFT JOIN cohort_members cm ON cm.cohort_id = co.cohort_id
  LEFT JOIN placements p      ON p.candidate_id = cm.candidate_id
                             AND p.offered_at >= co.starts_on
 GROUP BY co.cohort_id, co.name, co.sector, co.women_only, co.starts_on;

GRANT SELECT, INSERT, UPDATE ON cohorts, cohort_members TO app_operations;
GRANT SELECT ON v_cohort_outcomes TO app_operations;

COMMIT;
