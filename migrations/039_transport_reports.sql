-- 039. THE FARE MODEL WAS A GUESS DRIVING TWO REAL DECISIONS
--
-- app/matching/transport.py has said from the start that its fare model is a
-- placeholder to be calibrated against real receipts. Nothing collected any.
-- TransportEstimate even carries is_estimate, a flag nothing has ever set to
-- false, because there was never anything but an estimate.
--
-- Two things rest on that guess, and both are load-bearing:
--
--   Matching filter 2 refuses a placement when transport exceeds 30% of daily
--   pay. Underestimating puts someone in a job that costs them money, which
--   is the failure the filter exists to prevent.
--
--   "Net earnings after transport" is a headline pilot metric. Reporting a
--   number derived from straight-line distance, to a funder, as though it
--   were measured, is not a small thing.
--
-- So workers are asked what it actually cost, and the answer displaces the
-- guess. No model is fitted -- at pilot volume that would be fitting noise.
-- Two rules, in order:
--
--   1. If anyone has reported this exact route, use the median of those
--      reports. Even one real fare beats a straight line for that pair, and
--      it is the pair that matters when the same person goes back.
--   2. Otherwise correct the straight-line estimate by the median ratio of
--      reported to estimated across every route. One number, correcting the
--      model's systematic bias -- roads bend, hills, changing moto twice.
--
-- Both are explainable to a coordinator and to an employer, which is the same
-- reason matching uses sequential filters rather than a score.

BEGIN;

CREATE TABLE transport_reports (
    report_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id  UUID NOT NULL REFERENCES candidates(candidate_id)
                  ON DELETE CASCADE,
    employer_id   UUID NOT NULL REFERENCES employers(employer_id),
    placement_id  UUID REFERENCES placements(placement_id) ON DELETE SET NULL,
    work_date     DATE NOT NULL,
    -- Round trip, the whole day. People come home, and a one-way figure that
    -- looks like a daily cost is exactly how this gets underestimated.
    reported_rwf  INTEGER NOT NULL CHECK (reported_rwf >= 0),
    reported_min  SMALLINT CHECK (reported_min IS NULL OR reported_min >= 0),
    -- What we predicted at the time. Kept so the calibration ratio can be
    -- computed without re-deriving geometry that may since have changed.
    estimated_rwf INTEGER CHECK (estimated_rwf IS NULL OR estimated_rwf >= 0),
    source        VARCHAR(20) NOT NULL DEFAULT 'follow_up'
                  CHECK (source IN ('follow_up', 'coordinator', 'inbound')),
    note          TEXT,
    recorded_by   UUID REFERENCES staff(staff_id),
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- One report per person, employer and day. A worker asked twice about the
    -- same Tuesday should correct the record, not double-weight it.
    UNIQUE (candidate_id, employer_id, work_date)
);

CREATE INDEX idx_transport_route
    ON transport_reports (candidate_id, employer_id);

COMMENT ON TABLE transport_reports IS
    'What the commute actually cost, asked of the worker. Displaces the '
    'straight-line estimate for that route and calibrates it for every other.';

-- What has actually been observed, per route.
CREATE VIEW v_transport_observed AS
SELECT candidate_id,
       employer_id,
       count(*)                                            AS reports,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY reported_rwf)::int
                                                           AS median_rwf,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY reported_min)
           FILTER (WHERE reported_min IS NOT NULL)::int     AS median_min,
       max(work_date)                                      AS last_reported_on
  FROM transport_reports
 GROUP BY candidate_id, employer_id;

-- One number: how wrong the straight-line model is, on the evidence.
--
-- Median rather than mean, because one worker reporting a 5,000 RWF day after
-- being stranded in the rain should inform the number, not define it. Held to
-- a sane band so a handful of odd reports early in the pilot cannot make
-- every estimate absurd -- the filter this feeds decides who is offered work.
CREATE VIEW v_transport_calibration AS
SELECT count(*)                                            AS reports,
       LEAST(GREATEST(
           COALESCE(percentile_cont(0.5) WITHIN GROUP (
               ORDER BY reported_rwf::numeric / NULLIF(estimated_rwf, 0)
           ), 1.0), 0.5), 2.5)                              AS factor,
       COALESCE(percentile_cont(0.5) WITHIN GROUP (
           ORDER BY reported_rwf::numeric / NULLIF(estimated_rwf, 0)
       ), 1.0)                                              AS raw_factor
  FROM transport_reports
 WHERE estimated_rwf IS NOT NULL AND estimated_rwf > 0;

GRANT SELECT, INSERT, UPDATE ON transport_reports TO app_operations;
GRANT SELECT ON v_transport_observed, v_transport_calibration TO app_operations;

COMMIT;
