-- 048. THE HEADLINE METRIC WAS STILL READING THE GUESS
--
-- Migration 039 collected what the commute actually costs and fed it into
-- matching, so a real fare displaces the straight line when deciding who to
-- offer work to. It did not touch v_placement_net_pay, which is the view
-- behind "net earnings after transport" -- a headline pilot metric, and one
-- of the four gaps this business exists to close.
--
-- On the demo data the scorecard reported transport at 26.0% of pay while the
-- reported fares said 44.2%. The blueprint's target is 25%. The number going
-- in front of a funder would have read as a near miss, when the workers'
-- own receipts describe a placement that dies in week two -- which is exactly
-- the failure this metric exists to detect.
--
-- The placement's est_transport_rwf is left alone. It is the figure as it
-- stood when the work was agreed, the contract quotes it, and rewriting it
-- later would change what somebody was told they accepted. The view resolves
-- at read time instead, and says which basis it used: "1,600 estimated" and
-- "2,720 reported" are different claims and only one is a measurement.

BEGIN;

-- The existing columns keep their names, types and order: v_pilot_scorecard
-- reads this view, and CREATE OR REPLACE cannot insert a column ahead of one
-- that already exists. Only the expressions behind them change, and the new
-- columns are appended.
CREATE OR REPLACE VIEW v_placement_net_pay AS
SELECT p.placement_id,
       p.candidate_id,
       p.agreed_pay_rwf,
       -- What the journey actually costs, where anybody has said. The
       -- estimate is the fallback, not the answer.
       COALESCE(obs.median_rwf, p.est_transport_rwf)     AS est_transport_rwf,
       p.agreed_pay_rwf - COALESCE(obs.median_rwf, p.est_transport_rwf)
                                                         AS net_daily_rwf,
       ROUND(100.0 * COALESCE(obs.median_rwf, p.est_transport_rwf)
             / NULLIF(p.agreed_pay_rwf, 0), 1)           AS transport_pct,
       p.est_transport_rwf                               AS estimated_rwf,
       obs.median_rwf                                    AS reported_rwf,
       (obs.median_rwf IS NOT NULL)                      AS from_receipts
  FROM placements p
  JOIN work_requests wr ON wr.request_id = p.request_id
  LEFT JOIN v_transport_observed obs
         ON obs.candidate_id = p.candidate_id
        AND obs.employer_id = wr.employer_id
 WHERE p.pay_unit = 'day';

COMMENT ON VIEW v_placement_net_pay IS
    'Net earnings after transport, from reported fares where they exist and '
    'the estimate otherwise. from_receipts says which -- a measured number '
    'and a guessed one should not be reported as the same thing.';

COMMIT;
