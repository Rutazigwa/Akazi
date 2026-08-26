-- 029. DATES IN THE TIMEZONE THE USERS LIVE IN
--
-- CURRENT_DATE is the server's date, and servers run on UTC. Kigali is UTC+2,
-- so between 22:00 and midnight UTC it is already tomorrow there. Every date
-- this system defaulted, compared or recorded was a day behind for two hours
-- every night -- and those two hours are 00:00 to 02:00 local, exactly when a
-- late shift finishes and someone logs attendance.
--
-- Setting the database timezone would fix it in one line, but silently: a
-- future deploy that missed the setting would reintroduce the bug with nothing
-- to notice it. An explicit function says what it means at every call site.

BEGIN;

CREATE OR REPLACE FUNCTION kigali_today() RETURNS DATE AS $$
    SELECT (now() AT TIME ZONE 'Africa/Kigali')::date;
$$ LANGUAGE sql STABLE;

COMMENT ON FUNCTION kigali_today() IS
    'Today in Africa/Kigali. Use instead of CURRENT_DATE for anything a '
    'coordinator or worker would call today.';

GRANT EXECUTE ON FUNCTION kigali_today() TO app_operations, app_identity;

-- Ending a placement: the date a coordinator means is their date.
CREATE OR REPLACE FUNCTION fn_placement_end_date(
    p_given DATE, p_started DATE
) RETURNS DATE AS $$
    SELECT GREATEST(COALESCE(p_given, kigali_today()),
                    COALESCE(p_started, kigali_today()));
$$ LANGUAGE sql STABLE;

GRANT EXECUTE ON FUNCTION fn_placement_end_date(DATE, DATE)
    TO app_operations, app_identity;

-- The contract reference year. Two hours a year it would have used the wrong
-- one, which is a small thing that looks like a mistake on a printed page.
CREATE OR REPLACE FUNCTION next_contract_ref() RETURNS TEXT AS $$
    SELECT 'AKZ-' || to_char(kigali_today(), 'YYYY') || '-'
           || lpad(nextval('placement_contract_seq')::text, 5, '0');
$$ LANGUAGE sql VOLATILE;

-- Minimum age. The window is two hours on someone's sixteenth birthday, when
-- the server would still say they are fifteen and refuse the registration.
ALTER TABLE candidate_identity DROP CONSTRAINT chk_minimum_age;
ALTER TABLE candidate_identity ADD CONSTRAINT chk_minimum_age
    CHECK (date_of_birth <= kigali_today() - INTERVAL '16 years');

COMMIT;
