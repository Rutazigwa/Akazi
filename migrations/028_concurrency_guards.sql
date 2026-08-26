-- 028. THE SAME RACE, IN TWO MORE PLACES
--
-- Migration 027 closed the read-then-write race on placements. The same shape
-- appears twice more, and both were demonstrated with concurrent connections
-- rather than reasoned about:
--
--   cohort capacity   two enrolments into a cohort with one place left both
--                     counted the places before either inserted. Two people
--                     admitted to a room with one chair.
--
--   pay periods       two coordinators recording the same week both found no
--                     overlap before either wrote. RWF 30,000 recorded for a
--                     15,000 week -- which tells an employer they owe double,
--                     or tells us a worker was paid twice.
--
-- Worth stating plainly, because it is the lesson: the capacity check was
-- ALREADY a trigger and raced anyway. Moving a check into the database does
-- not make it concurrency-safe. What makes it safe is serialising the writers
-- that could conflict -- here a transaction-scoped advisory lock on the row
-- everything hangs off.

BEGIN;

-- Cohort membership: lock the cohort before counting its places.
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
        -- Serialise enrolments for this cohort, so a concurrent one is seen.
        PERFORM pg_advisory_xact_lock(hashtext('cohort:' || NEW.cohort_id::text));

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

-- Pay periods: the overlap check lived only in the application, so two
-- coordinators recording the same week both passed it.
CREATE OR REPLACE FUNCTION fn_no_overlapping_pay_period() RETURNS TRIGGER AS $$
DECLARE
    v_start DATE;
    v_end   DATE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('pay:' || NEW.placement_id::text));

    SELECT period_start, period_end INTO v_start, v_end
      FROM pay_records
     WHERE placement_id = NEW.placement_id
       AND pay_id IS DISTINCT FROM NEW.pay_id
       AND period_start <= NEW.period_end
       AND period_end   >= NEW.period_start
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'pay for % to % already covers part of this period',
            v_start, v_end
            USING ERRCODE = 'exclusion_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_no_overlapping_pay_period
    BEFORE INSERT OR UPDATE OF period_start, period_end, placement_id
    ON pay_records
    FOR EACH ROW EXECUTE FUNCTION fn_no_overlapping_pay_period();

COMMIT;
