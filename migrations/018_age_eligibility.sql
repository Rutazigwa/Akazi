-- 018. AGE ELIGIBILITY WITHOUT READING IDENTITY DATA
--
-- Bug fix, and a real one. The matching engine needs each candidate's age to
-- apply the minimum-age filter, and it was getting it by joining
-- candidate_identity directly. Migration 008 revoked SELECT on that table from
-- app_operations precisely so every read goes through the audited function --
-- so on a deployment that actually uses the role model, matching failed with
-- "permission denied for table candidate_identity".
--
-- It went unnoticed because the tests connect as superuser, where grants do not
-- apply. tests/test_privileges.py now checks the queries under the real role.
--
-- The fix exposes the derived fact instead of the underlying data: a boolean
-- per candidate, for a given date. No date of birth crosses the boundary, so no
-- audit entry is warranted -- nothing identifying was disclosed.

BEGIN;

CREATE OR REPLACE FUNCTION candidates_age_eligible(p_on_date DATE)
RETURNS TABLE (candidate_id UUID, age_eligible BOOLEAN)
AS $$
    SELECT ci.candidate_id,
           ci.date_of_birth <= p_on_date - INTERVAL '16 years'
      FROM candidate_identity ci
     WHERE ci.erased_at IS NULL;
$$ LANGUAGE sql STABLE SECURITY DEFINER;

COMMENT ON FUNCTION candidates_age_eligible(DATE) IS
    'Minimum-age check for matching. Returns a boolean per candidate so that '
    'operational code never needs SELECT on candidate_identity.';

REVOKE ALL ON FUNCTION candidates_age_eligible(DATE) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION candidates_age_eligible(DATE)
    TO app_operations, app_identity;

COMMIT;
