-- 043. NOBODY COULD ASK WHO WE ARE FAILING
--
-- The matcher shows why each candidate was excluded from each request. What
-- it cannot show is the person excluded from *every* request, always for the
-- same reason, whom nobody has ever looked at. Individually each rejection is
-- explained; in aggregate the registry is silent.
--
-- At pilot volume that is a handful of people. Against 928,426 NEET youth it
-- is the whole question. Someone registered eight weeks ago with no home
-- location on file is not unlucky -- transport cannot be estimated for them,
-- so filter 2 cannot pass them and cover cannot promise an arrival time. They
-- will never be placed, and nothing anywhere says so.
--
-- Every blocker here is fixable by a coordinator in a phone call: capture a
-- location, record availability, score one assessment, take consent. That is
-- the point of listing them. This is a work queue, not a report.

BEGIN;

CREATE VIEW v_candidate_readiness AS
SELECT c.candidate_id,
       c.display_name,
       c.gender,
       c.district,
       c.sector,
       c.status::text                                        AS status,
       c.created_at,
       COALESCE(elig.age_eligible, FALSE)                    AS age_eligible,
       (c.home_lat IS NOT NULL AND c.home_lng IS NOT NULL)    AS has_home_location,
       COALESCE(consent.granted, FALSE)                      AS has_consent,
       av.windows                                            AS availability_windows,
       sk.scored                                             AS skills_scored,
       pl.offers                                             AS offers,
       pl.worked                                             AS placements_worked,
       pl.last_offered_at
  FROM candidates c
  LEFT JOIN candidates_age_eligible(CURRENT_DATE) elig
         ON elig.candidate_id = c.candidate_id
  LEFT JOIN LATERAL (
      SELECT vc.granted FROM v_current_consent vc
       WHERE vc.candidate_id = c.candidate_id AND vc.purpose = 'placement'
  ) consent ON TRUE
  LEFT JOIN LATERAL (
      SELECT count(*) AS windows FROM availability a
       WHERE a.candidate_id = c.candidate_id
  ) av ON TRUE
  LEFT JOIN LATERAL (
      SELECT count(*) AS scored FROM assessment_results r
       WHERE r.candidate_id = c.candidate_id
  ) sk ON TRUE
  LEFT JOIN LATERAL (
      SELECT count(*)                                        AS offers,
             count(*) FILTER (WHERE p.status IN ('active','completed'))
                                                             AS worked,
             max(p.offered_at)                               AS last_offered_at
        FROM placements p WHERE p.candidate_id = c.candidate_id
  ) pl ON TRUE
 WHERE c.status NOT IN ('withdrawn', 'inactive');

COMMENT ON VIEW v_candidate_readiness IS
    'Why somebody in the registry is not working. Every blocker listed is '
    'fixable in a phone call -- this is a work queue, not a report.';

GRANT SELECT ON v_candidate_readiness TO app_operations;

COMMIT;
