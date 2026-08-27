-- 036. A SKILL REQUIREMENT COULD NOT BE REMOVED UNDER THE REAL ROLE
--
-- The first DELETE the application ever performed. It was missing a grant,
-- and the source-derived privilege test did not notice because its parser
-- looked for INSERT and UPDATE only -- so the entire verb was invisible to
-- the check that exists to catch exactly this.
--
-- Tests run as the database owner, who bypasses grants, so the suite was
-- green while the live server returned 500. That is the same failure the
-- restricted-role tests were added for.

GRANT DELETE ON request_skills TO app_operations;
