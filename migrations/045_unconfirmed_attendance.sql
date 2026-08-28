-- 045. SILENCE LOOKED EXACTLY LIKE SUCCESS
--
-- Attendance is confirmed by the employer, and it is the input the whole
-- guarantee rests on. Nothing anywhere noticed when it never arrived.
--
-- A shift ran on Tuesday. Nobody recorded whether the worker turned up. The
-- guarantee clock never starts, because we do not know there was a no-show.
-- Pay cannot be computed, because days present is wrong. The placement sits
-- in the system looking exactly like one that went perfectly well.
--
-- For a business whose promise is "the shift gets covered, and if it does not
-- we cover it", an unrecorded no-show is the most expensive silence there is:
-- the employer knows, the worker knows, and we find out when the employer
-- declines to reorder.
--
-- WHAT THIS DELIBERATELY DOES NOT DO
--
-- It does not enumerate the days a placement was expected to work. The system
-- models a shift window and a date range, not a working calendar -- a weekend
-- cleaner and a five-day shop assistant are indistinguishable here. Inventing
-- the missing days would produce confident nonsense for one of them. So it
-- reports only what is certain: a placement that has started and has no
-- attendance at all, or one whose last record is old while it is still
-- running.

BEGIN;

CREATE VIEW v_unconfirmed_attendance AS
SELECT p.placement_id,
       p.candidate_id,
       c.display_name,
       wr.request_id,
       wr.title,
       wr.employer_id,
       e.business_name,
       p.status::text                                  AS status,
       p.started_on,
       p.ended_on,
       att.records,
       att.last_confirmed_on,
       -- Days since anyone said anything about this placement. Measured from
       -- the last confirmation, or from the start if there never was one.
       kigali_today() - COALESCE(att.last_confirmed_on, p.started_on)
                                                       AS days_silent
  FROM placements p
  JOIN work_requests wr ON wr.request_id = p.request_id
  JOIN employers e      ON e.employer_id = wr.employer_id
  JOIN candidates c     ON c.candidate_id = p.candidate_id
  JOIN LATERAL (
      SELECT count(*)        AS records,
             max(a.work_date) AS last_confirmed_on
        FROM attendance a
       WHERE a.placement_id = p.placement_id
  ) att ON TRUE
 WHERE p.status IN ('active', 'completed')
   AND p.started_on IS NOT NULL
   AND p.started_on < kigali_today();

COMMENT ON VIEW v_unconfirmed_attendance IS
    'Placements nobody has confirmed. Silence is not success: an unrecorded '
    'no-show is a guarantee we never knew we owed.';

GRANT SELECT ON v_unconfirmed_attendance TO app_operations;

COMMIT;
