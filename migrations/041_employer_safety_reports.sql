-- 041. THE RATING ONLY RAN ONE WAY
--
-- The employer rates the worker, and that rating is shown. The worker rates
-- the employer at follow-up, and nothing has ever read it back -- it appears
-- in a subject access export and nowhere else. That asymmetry is exactly the
-- power imbalance the blueprint asks this business to correct, and it matters
-- most for the group it names: female unemployment is 15.5% against 11.6%
-- male, and the blueprint lists "employer safety ratings written by women who
-- worked there" as a product requirement, not a reporting line.
--
-- A woman deciding whether to take a shift that finishes after dark at an
-- employer she has never worked for is making a safety judgement with no
-- information. Somebody else already has that information. This is how it
-- reaches her coordinator.
--
-- WHO MAY SEE THIS
--
-- Coordinators, and no one else. An employer must never see it, in aggregate
-- or otherwise. An employer told "one of the two women who worked here did
-- not feel safe" knows precisely who said it, and the consequence of that
-- lands on her, not on us. There is no threshold that makes it safe to show
-- an employer their own safety reports, so none is offered.
--
-- Internally, suppression would defeat the purpose without protecting anyone:
-- a coordinator can already see who worked which shift. The LMIS rule
-- (MIN_CELL) exists because those figures leave the building. These do not.

BEGIN;

CREATE TABLE employer_safety_reports (
    report_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employer_id   UUID NOT NULL REFERENCES employers(employer_id),
    candidate_id  UUID NOT NULL REFERENCES candidates(candidate_id)
                  ON DELETE CASCADE,
    placement_id  UUID REFERENCES placements(placement_id) ON DELETE SET NULL,
    felt_safe     BOOLEAN NOT NULL,
    would_return  BOOLEAN,
    -- A closed list, for the same reason deductions have one: free text
    -- becomes "other" for everything, and "what are women telling us about
    -- this employer" has to be answerable without reading every note.
    concern       VARCHAR(30)
                  CHECK (concern IS NULL OR concern IN (
                      'harassment',
                      'unsafe_equipment',
                      'unsafe_hours',
                      'transport_after_dark',
                      'pressure_to_work_unpaid',
                      'pay',
                      'other'
                  )),
    note          TEXT,
    reported_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    recorded_by   UUID REFERENCES staff(staff_id),
    -- One standing answer per worker per employer. Asked again at week 1 and
    -- day 30, the later answer replaces the earlier: what she thinks now is
    -- the thing worth knowing.
    UNIQUE (employer_id, candidate_id)
);

CREATE INDEX idx_safety_employer ON employer_safety_reports (employer_id);

COMMENT ON TABLE employer_safety_reports IS
    'What workers say about an employer. Coordinator-facing only -- an '
    'employer shown their own safety reports can identify who wrote them.';

-- Gender is read from candidates, which is operational rather than
-- residency-sensitive, so this needs no grant on candidate_identity.
CREATE VIEW v_employer_safety AS
SELECT e.employer_id,
       e.business_name,
       count(r.report_id)                                    AS reports,
       count(*) FILTER (WHERE r.felt_safe)                   AS felt_safe,
       count(*) FILTER (WHERE r.would_return)                AS would_return,
       count(*) FILTER (WHERE c.gender = 'F')                AS reports_women,
       count(*) FILTER (WHERE c.gender = 'F' AND r.felt_safe)
                                                             AS felt_safe_women,
       count(*) FILTER (WHERE NOT r.felt_safe)               AS felt_unsafe,
       count(*) FILTER (WHERE c.gender = 'F' AND NOT r.felt_safe)
                                                             AS felt_unsafe_women,
       array_remove(array_agg(DISTINCT r.concern), NULL)     AS concerns,
       max(r.reported_at)                                    AS last_reported_at
  FROM employers e
  JOIN employer_safety_reports r ON r.employer_id = e.employer_id
  JOIN candidates c ON c.candidate_id = r.candidate_id
 GROUP BY e.employer_id, e.business_name;

COMMENT ON VIEW v_employer_safety IS
    'Never expose on an employer-facing route. See migration 041.';

GRANT SELECT, INSERT, UPDATE ON employer_safety_reports TO app_operations;
GRANT SELECT ON v_employer_safety TO app_operations;

COMMIT;
