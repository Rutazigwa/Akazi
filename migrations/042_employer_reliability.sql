-- 042. WHICH EMPLOYERS COST US, AND WHICH ONES DO WORKERS LEAVE
--
-- Every fact needed to answer this was already recorded and none of it was
-- ever grouped by employer. Retention is measured per worker, guarantee
-- invocations per placement, pay accuracy per pay record. Nobody could ask
-- the question the whole operation turns on: is this client worth having.
--
-- It is a question only an operator carrying the guarantee can ask. The fee
-- includes covering a shift when someone does not arrive, so an employer
-- whose shifts repeatedly go uncovered is being subsidised by the ones whose
-- do not. A competitor that ends its responsibility at the introduction never
-- pays that cost and never needs the number.
--
-- The other half is the worker's: an employer people leave in week two is one
-- we should stop sending people to, whatever the fee.
--
-- ATTRIBUTION, CAREFULLY
--
-- A no-show is usually the worker's doing and says nothing about the
-- employer. What says something is *several different workers* failing to
-- arrive at the same site -- that is a pattern about the place: unpaid,
-- unreachable, or unpleasant on arrival. So distinct workers are counted, not
-- events, and one worker's no-show is deliberately not enough to mark
-- anybody.

BEGIN;

CREATE VIEW v_employer_reliability AS
WITH placed AS (
    SELECT wr.employer_id,
           p.placement_id,
           p.candidate_id,
           p.status::text AS status
      FROM placements p
      JOIN work_requests wr ON wr.request_id = p.request_id
),
guarantees AS (
    SELECT wr.employer_id,
           count(*)                                        AS invocations,
           count(*) FILTER (WHERE g.filled_within_24h)      AS filled_in_24h
      FROM v_guarantee_invocations g
      JOIN work_requests wr ON wr.request_id = g.request_id
     GROUP BY wr.employer_id
),
retention AS (
    SELECT employer_id,
           count(*)                                        AS checked_at_30_days,
           count(*) FILTER (WHERE still_working)            AS still_there
      FROM v_retention_30day
     GROUP BY employer_id
),
paid AS (
    SELECT pl.employer_id,
           count(*)                                        AS pay_records,
           count(*) FILTER (WHERE a.paid_in_full_on_time)   AS paid_correctly
      FROM v_pay_accuracy a
      JOIN placed pl ON pl.placement_id = a.placement_id
     GROUP BY pl.employer_id
)
SELECT e.employer_id,
       e.business_name,
       e.tier::text                                        AS tier,
       e.is_cooperative,
       count(pl.placement_id)                              AS placements,
       -- Distinct people, not events. One worker failing to arrive says
       -- nothing about the employer; several different ones is a pattern
       -- about the site.
       count(DISTINCT pl.candidate_id) FILTER
           (WHERE pl.status = 'no_show')                    AS workers_who_did_not_arrive,
       COALESCE(g.invocations, 0)                          AS guarantee_invocations,
       COALESCE(g.filled_in_24h, 0)                        AS guarantee_filled_in_24h,
       COALESCE(r.checked_at_30_days, 0)                   AS checked_at_30_days,
       COALESCE(r.still_there, 0)                          AS still_there_at_30_days,
       COALESCE(pd.pay_records, 0)                         AS pay_records,
       COALESCE(pd.paid_correctly, 0)                      AS paid_correctly,
       COALESCE(sf.reports, 0)                             AS safety_reports,
       COALESCE(sf.felt_unsafe, 0)                         AS felt_unsafe
  FROM employers e
  LEFT JOIN placed pl        ON pl.employer_id = e.employer_id
  LEFT JOIN guarantees g     ON g.employer_id = e.employer_id
  LEFT JOIN retention r      ON r.employer_id = e.employer_id
  LEFT JOIN paid pd          ON pd.employer_id = e.employer_id
  LEFT JOIN v_employer_safety sf ON sf.employer_id = e.employer_id
 GROUP BY e.employer_id, e.business_name, e.tier, e.is_cooperative,
          g.invocations, g.filled_in_24h, r.checked_at_30_days, r.still_there,
          pd.pay_records, pd.paid_correctly, sf.reports, sf.felt_unsafe;

COMMENT ON VIEW v_employer_reliability IS
    'Per-employer operating evidence: who costs us guarantee invocations, '
    'whose workers stay, who pays correctly. Counts only -- the judgement is '
    'made in application code where it can be explained.';

GRANT SELECT ON v_employer_reliability TO app_operations;

COMMIT;
