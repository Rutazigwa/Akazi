-- 021. ASSESSMENT SCORE INTEGRITY
--
-- assessment_results.score only had CHECK (score >= 0). The maximum lives on
-- the assessment, in another table, so a plain CHECK cannot see it -- and
-- nothing else was looking. A score of 9 out of a maximum of 5 was accepted.
--
-- That corrupts the one number matching ranks on, and it is what a coordinator
-- reads aloud to an employer: "retail_greeting 9/5" is not a defensible answer
-- to "why this person".
--
-- Enforced by trigger rather than application code because the application is
-- not the only thing that will ever write here -- a bulk import of paper
-- assessment sheets is exactly the case where this goes wrong.

BEGIN;

CREATE OR REPLACE FUNCTION fn_check_assessment_score() RETURNS TRIGGER AS $$
DECLARE
    v_max SMALLINT;
BEGIN
    SELECT max_score INTO v_max
      FROM assessments WHERE assessment_id = NEW.assessment_id;

    IF v_max IS NULL THEN
        RAISE EXCEPTION 'assessment % does not exist', NEW.assessment_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NEW.score > v_max THEN
        RAISE EXCEPTION
            'score % exceeds the maximum of % for this assessment',
            NEW.score, v_max
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_assessment_score
    BEFORE INSERT OR UPDATE ON assessment_results
    FOR EACH ROW EXECUTE FUNCTION fn_check_assessment_score();

COMMIT;
