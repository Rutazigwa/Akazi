-- 034. THE REORDER RATE HAD NO DATA BEHIND IT
--
-- "Employer reorder rate >= 40%" is one of the ten pilot targets, and it is
-- the go/no-go test for the whole thesis: if the guarantee generates no
-- pricing power, the blueprint says pivot. The employer portal had a reorder
-- button, but pressing it created an ordinary new request with the fields
-- copied across and nothing recording that it repeated anything. The number
-- the pivot decision rests on could not be computed at all.
--
-- Two things are needed, and they are not the same thing:
--
--   reorders_request records provenance -- did they use the one-click path.
--   That is a product question about the button.
--
--   The METRIC deliberately does not depend on the button. An employer who
--   posts a second shift by filling the form again has come back just the
--   same, and counting only button presses would understate the real rate and
--   could produce a false pivot signal. So the rate counts employers who
--   ordered more than once by any route.
--
-- The denominator is employers we actually placed someone with. An employer
-- who posted once and never got anyone has not declined to reorder -- they
-- have not been served yet, and holding them against the rate makes the
-- number say something other than what it is asked to say.

BEGIN;

ALTER TABLE work_requests
    ADD COLUMN reorders_request UUID REFERENCES work_requests(request_id);

COMMENT ON COLUMN work_requests.reorders_request IS
    'The request this one repeats, when posted through the reorder path. '
    'Provenance only -- the reorder metric counts repeat employers by any route.';

CREATE INDEX idx_request_reorders ON work_requests (reorders_request)
    WHERE reorders_request IS NOT NULL;

-- A reorder must belong to the same employer as the request it repeats.
-- Without this, a copied request id in a form post attributes one employer's
-- repeat business to another, and the employer-facing reliability summary
-- reads from the same rows.
CREATE OR REPLACE FUNCTION fn_reorder_same_employer() RETURNS TRIGGER AS $$
DECLARE
    v_owner UUID;
BEGIN
    IF NEW.reorders_request IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.reorders_request = NEW.request_id THEN
        RAISE EXCEPTION 'a request cannot reorder itself';
    END IF;

    SELECT employer_id INTO v_owner
      FROM work_requests WHERE request_id = NEW.reorders_request;

    IF v_owner IS NULL THEN
        RAISE EXCEPTION 'reordered request % does not exist',
                        NEW.reorders_request;
    END IF;

    IF v_owner <> NEW.employer_id THEN
        RAISE EXCEPTION 'a reorder must belong to the same employer as the '
                        'request it repeats';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_reorder_same_employer
    BEFORE INSERT OR UPDATE OF reorders_request ON work_requests
    FOR EACH ROW EXECUTE FUNCTION fn_reorder_same_employer();

-- now() is the transaction timestamp, so two rows written in one transaction
-- carry the identical value and cannot be ordered against each other. The
-- reorder metric asks exactly that question -- was this request opened after
-- we first placed someone -- and a tie answers "no". Migration 011 hit this
-- for consent ordering and fixed it the same way.
--
-- Requests and placements are normally written in separate transactions, so
-- in production the ordering usually holds by luck. A metric the pivot
-- decision rests on should not depend on that.
ALTER TABLE work_requests ALTER COLUMN opened_at SET DEFAULT clock_timestamp();
ALTER TABLE placements    ALTER COLUMN offered_at SET DEFAULT clock_timestamp();

-- One row per employer we have actually served.
--
-- A reorder is a request opened AFTER we first put someone in front of them,
-- not merely their second request. An employer who posts two different roles
-- on the same morning has ordered twice but has not come back -- they have
-- not seen the work done yet, so nothing about that second request says the
-- guarantee was worth paying for. Counting it would inflate the one number
-- the pivot decision rests on, in the direction that avoids the pivot.
CREATE VIEW v_employer_reorder AS
WITH served AS (
    SELECT r.employer_id, min(p.offered_at) AS first_served_at
      FROM work_requests r
      JOIN placements p ON p.request_id = r.request_id
     WHERE p.status IN ('active', 'completed')
     GROUP BY r.employer_id
)
SELECT
    e.employer_id,
    e.business_name,
    e.is_cooperative,
    s.first_served_at,
    count(DISTINCT r.request_id)                       AS requests_posted,
    count(DISTINCT r.request_id) FILTER
        (WHERE r.opened_at > s.first_served_at)        AS requests_after_serving,
    count(DISTINCT r.request_id) FILTER
        (WHERE r.reorders_request IS NOT NULL)         AS reorders_via_button,
    max(r.opened_at) FILTER
        (WHERE r.opened_at > s.first_served_at)        AS latest_reorder_at,
    (count(DISTINCT r.request_id) FILTER
        (WHERE r.opened_at > s.first_served_at) > 0)   AS has_reordered
FROM employers e
JOIN served s        ON s.employer_id = e.employer_id
JOIN work_requests r ON r.employer_id = e.employer_id
GROUP BY e.employer_id, e.business_name, e.is_cooperative, s.first_served_at;

COMMENT ON VIEW v_employer_reorder IS
    'Repeat business per served employer: requests opened after we first '
    'placed someone with them. Employers nobody was placed with are excluded '
    '-- they have not declined to reorder, they have not been served yet.';

DROP VIEW v_pilot_scorecard;

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
        AS pay_accuracy_pct,
    (SELECT ROUND(100.0 * avg(has_reordered::int), 1) FROM v_employer_reorder)
        AS employer_reorder_pct,
    (SELECT count(*) FROM v_employer_reorder)
        AS employers_served;

GRANT SELECT ON v_employer_reorder, v_pilot_scorecard TO app_operations;

COMMIT;
