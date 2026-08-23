-- 009. PILOT METRIC VIEWS
--
-- Every headline metric from the blueprint, as a view. None of these needs a
-- new table: the operational records already carry the evidence, and deriving
-- the metric from the evidence is what keeps the two from drifting apart.
--
-- The guarantee metric in particular is derived from the replacement chain
-- rather than from a status flag someone has to remember to set. If a no-show
-- was covered, there is a placement row pointing back at the one it replaced,
-- and the gap between the two timestamps is the fill time. Nothing to maintain.

BEGIN;

-- Reliability guarantee: invoked when a no-show is recorded, honoured when a
-- replacement placement is offered within 24 hours. Target: >= 90% within 24h.
CREATE VIEW v_guarantee_invocations AS
SELECT
    failed.placement_id                       AS failed_placement_id,
    failed.request_id,
    failed.candidate_id                       AS no_show_candidate_id,
    ns.invoked_at,
    replacement.placement_id                  AS replacement_placement_id,
    replacement.candidate_id                  AS replacement_candidate_id,
    replacement.offered_at                    AS filled_at,
    ROUND(EXTRACT(EPOCH FROM (replacement.offered_at - ns.invoked_at))
          / 3600.0, 2)                        AS hours_to_fill,
    (replacement.placement_id IS NOT NULL
     AND replacement.offered_at <= ns.invoked_at + INTERVAL '24 hours')
                                              AS filled_within_24h
FROM placements failed
JOIN LATERAL (
    SELECT min(a.confirmed_at) AS invoked_at
    FROM attendance a
    WHERE a.placement_id = failed.placement_id
      AND NOT a.present
) ns ON ns.invoked_at IS NOT NULL
LEFT JOIN placements replacement
       ON replacement.replaces_placement = failed.placement_id
WHERE failed.status = 'no_show';

-- 30-day retention. Only completed day_30 follow-ups count: an unanswered
-- check-in is missing data, not a success.  Target: >= 60%.
CREATE VIEW v_retention_30day AS
SELECT p.placement_id,
       p.candidate_id,
       wr.employer_id,
       f.still_working
FROM follow_ups f
JOIN placements p    ON p.placement_id = f.placement_id
JOIN work_requests wr ON wr.request_id = p.request_id
WHERE f.checkpoint = 'day_30'
  AND f.completed_at IS NOT NULL
  AND f.still_working IS NOT NULL;

-- Pay accuracy: in full AND on the agreed date. Both halves are required --
-- late-but-complete is still a broken promise to someone living on the wage.
-- Target: >= 95%.
CREATE VIEW v_pay_accuracy AS
SELECT pr.pay_id,
       pr.placement_id,
       pr.net_rwf,
       pr.due_on,
       pr.paid_on,
       (pr.paid_on IS NOT NULL
        AND pr.due_on IS NOT NULL
        AND pr.paid_on <= pr.due_on
        AND pr.worker_confirmed) AS paid_in_full_on_time
FROM pay_records pr;

-- Time to fill: request opened -> first placement offered. Target: < 7 days.
CREATE VIEW v_time_to_fill AS
SELECT wr.request_id,
       wr.employer_id,
       wr.opened_at,
       first_offer.offered_at,
       ROUND(EXTRACT(EPOCH FROM (first_offer.offered_at - wr.opened_at))
             / 86400.0, 2) AS days_to_fill
FROM work_requests wr
JOIN LATERAL (
    SELECT min(p.offered_at) AS offered_at
    FROM placements p
    WHERE p.request_id = wr.request_id
      AND p.replaces_placement IS NULL   -- a replacement is not a first fill
) first_offer ON first_offer.offered_at IS NOT NULL;

-- The pilot scorecard: one row, every headline target. This is the query the
-- owner runs, and the one that goes in front of a funder.
CREATE VIEW v_pilot_scorecard AS
SELECT
    (SELECT count(*) FROM employers WHERE tier IN ('pilot','active'))
        AS active_employers,
    (SELECT count(*) FROM employers
      WHERE is_cooperative AND tier IN ('pilot','active'))
        AS active_cooperatives,
    (SELECT count(*) FROM placements
      WHERE status IN ('active','completed'))
        AS paid_placements,
    (SELECT ROUND(avg(days_to_fill), 2) FROM v_time_to_fill)
        AS avg_days_to_fill,
    (SELECT ROUND(100.0 * avg(still_working::int), 1) FROM v_retention_30day)
        AS retention_30day_pct,
    (SELECT ROUND(avg(transport_pct), 1) FROM v_placement_net_pay)
        AS avg_transport_pct,
    (SELECT count(*) FROM v_guarantee_invocations)
        AS guarantee_invocations,
    (SELECT ROUND(100.0 * avg(filled_within_24h::int), 1)
       FROM v_guarantee_invocations)
        AS guarantee_filled_24h_pct,
    (SELECT ROUND(100.0 * avg((c.gender = 'F')::int), 1)
       FROM placements p JOIN candidates c USING (candidate_id)
      WHERE p.status IN ('active','completed'))
        AS women_placed_pct,
    (SELECT ROUND(100.0 * avg(paid_in_full_on_time::int), 1)
       FROM v_pay_accuracy)
        AS pay_accuracy_pct;

GRANT SELECT ON v_guarantee_invocations, v_retention_30day, v_pay_accuracy,
                v_time_to_fill, v_pilot_scorecard
    TO app_operations;

COMMIT;
