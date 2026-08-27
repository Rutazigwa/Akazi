-- 035. NOTHING COULD DEFINE A SKILL OR AN ASSESSMENT
--
-- skills and assessments were empty on a fresh deployment and no application
-- code inserted into either. The consequences ran through the whole matching
-- path: require_skill() looks a skill up by code and raises 'unknown skill'
-- for every code there is, so no work request could carry a skill
-- requirement; record_assessment_result() needs an assessment_id that could
-- not exist; and with no results, matching filter 1 (score below min_score)
-- and rank criterion 3 (assessment score) never engaged at all.
--
-- "Assessment scoring" is a weeks 1-6 deliverable. The scoring half was
-- built. The half that says what is being scored was not.
--
-- Authoring the catalogue is a policy decision, not data entry: pass_score
-- decides who is eligible for work. It is gated on admin/owner in the API,
-- and the rules below hold whatever writes.

BEGIN;

-- Rescoring an assessment silently rewrites history. A candidate assessed
-- 3 out of 5 against a pass mark of 3 passed; raise pass_score to 4 and they
-- have retroactively always failed, including for placements already made on
-- the strength of that score. Migration 026 froze contract terms for the same
-- reason -- an agreed term that can be edited afterwards was never agreed.
--
-- Bounds stay editable until the first result lands, so a typo during setup
-- is cheap to fix. After that, a changed pass mark is a new assessment.
CREATE OR REPLACE FUNCTION fn_assessment_bounds_frozen() RETURNS TRIGGER AS $$
DECLARE
    v_results BIGINT;
BEGIN
    IF NEW.max_score = OLD.max_score AND NEW.pass_score = OLD.pass_score THEN
        RETURN NEW;
    END IF;

    SELECT count(*) INTO v_results
      FROM assessment_results WHERE assessment_id = OLD.assessment_id;

    IF v_results > 0 THEN
        RAISE EXCEPTION
            'assessment % has % recorded result(s): changing max_score or '
            'pass_score would retroactively change who passed. Create a new '
            'assessment instead',
            OLD.assessment_id, v_results;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_assessment_bounds_frozen
    BEFORE UPDATE OF max_score, pass_score ON assessments
    FOR EACH ROW EXECUTE FUNCTION fn_assessment_bounds_frozen();

-- skill_code is the stable handle require_skill() resolves against, and it is
-- what appears in an operator's notes and any import sheet. The display name
-- stays editable; the code does not.
CREATE OR REPLACE FUNCTION fn_skill_code_immutable() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.skill_code <> OLD.skill_code THEN
        RAISE EXCEPTION
            'skill_code % is the stable handle for this skill and cannot be '
            'changed; the display name can', OLD.skill_code;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_skill_code_immutable
    BEFORE UPDATE OF skill_code ON skills
    FOR EACH ROW EXECUTE FUNCTION fn_skill_code_immutable();

-- Categories were a free-text column with the intended values only in a
-- comment. Matching does not read them, but a coordinator browsing the
-- catalogue does, and 'retail'/'Retail'/'retails' as three categories is how
-- that stops being useful.
ALTER TABLE skills ADD CONSTRAINT chk_skill_category
    CHECK (category IN ('hospitality', 'retail', 'trades', 'soft',
                        'cleaning', 'logistics', 'agriculture', 'other'));

ALTER TABLE assessments ADD CONSTRAINT chk_assessment_method
    CHECK (method IN ('practical', 'observed', 'written'));

GRANT INSERT, UPDATE ON skills, assessments TO app_operations;

COMMIT;
