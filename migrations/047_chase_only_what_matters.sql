-- 047. THE CHASE REACHED BACK TOO FAR
--
-- v_unconfirmed_attendance included every completed placement whose last
-- attendance record was older than its end date. On real data that meant
-- five employers being asked about shifts that finished three weeks ago,
-- closed out and paid.
--
-- Chasing those is worse than not chasing: the answer changes nothing -- the
-- guarantee window shut long ago and the pay record is settled -- and an
-- employer asked about a shift they have forgotten stops reading the
-- messages. The next one is the one that matters.
--
-- So a completed placement is only chased while its pay is still being
-- worked out. An active one is chased whatever its age, because the
-- guarantee is still live and a no-show today is still ours to cover.

BEGIN;

CREATE OR REPLACE VIEW v_unconfirmed_attendance AS
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
 WHERE p.started_on IS NOT NULL
   AND p.started_on < kigali_today()
   AND (
        -- Still running: the guarantee is live and a no-show is still ours.
        p.status = 'active'
        -- Finished recently: the answer still changes the pay record.
        OR (p.status = 'completed'
            AND p.ended_on IS NOT NULL
            AND p.ended_on >= kigali_today() - INTERVAL '7 days')
   );

COMMENT ON VIEW v_unconfirmed_attendance IS
    'Placements nobody has confirmed, while the answer still changes '
    'something. Silence is not success: an unrecorded no-show is a guarantee '
    'we never knew we owed.';

COMMIT;
