-- Three defects in the metric that carries the whole thesis.
--
-- "If a placed worker does not arrive, we fill the slot free of charge, same
-- day." v_guarantee_invocations is how we know whether that happened, and it
-- appears on the employer's own dashboard as "Replaced within 24h".
--
-- 1. THE CLOCK STARTED WHEN WE NOTICED.
--
--    invoked_at was min(attendance.confirmed_at) -- the moment somebody
--    recorded the absence, not the moment the worker failed to arrive. So a
--    no-show at 08:00 recorded at 16:00 gave us until 16:00 the next day, and
--    the metric got EASIER the slower we were to notice. The employer lost
--    their 08:00 shift either way.
--
--    That is the same shape as the escalation response time fixed in migration
--    050: a number that improves when the operation performs worse. It now
--    runs from when the shift was due to start.
--
-- 2. A COVER THAT DECLINED COUNTED AS FILLED.
--
--    The join took any placement with replaces_placement set, whatever its
--    status. Somebody we rang who said no was recorded as the slot being
--    covered. The promise is that the shift gets worked, not that we made a
--    phone call.
--
-- 3. AFTER A DECLINE, NOTHING ELSE COULD BE OFFERED.
--
--    idx_one_replacement_per_placement was unique on replaces_placement with
--    no regard to status, so the first cover consumed the only slot. Ringing
--    round at 6am and having the first person say no is the ordinary case --
--    and the system then refused to record anybody else, with a raw
--    IntegrityError. The guarantee could not be honoured for that shift by
--    anyone, ever.

-- --- 3 first: one LIVE cover per no-show, not one attempt ever -------------

DROP INDEX idx_one_replacement_per_placement;

CREATE UNIQUE INDEX idx_one_live_replacement_per_placement
    ON placements (replaces_placement)
 WHERE replaces_placement IS NOT NULL
   AND status NOT IN ('declined', 'cancelled');

COMMENT ON INDEX idx_one_live_replacement_per_placement IS
    'One cover on the table at a time -- two people must not both be sent. A '
    'cover that declined or was cancelled is not on the table, so the next '
    'candidate can be offered it.';

-- --- 1 and 2: measure from the shift, and count only cover that was taken --

CREATE OR REPLACE VIEW v_guarantee_invocations AS
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
                                              AS filled_within_24h,
    -- Appended rather than placed next to invoked_at, where it reads better:
    -- CREATE OR REPLACE VIEW cannot insert a column into the middle of an
    -- existing one, and dropping this view would cascade into
    -- v_pilot_scorecard.
    ns.noticed_at
FROM placements failed
JOIN LATERAL (
    SELECT
        -- When the employer was let down: the shift they were expecting
        -- somebody for. Shift work always carries hours (migration 053); for
        -- work that does not, the start of that day in Kigali is the honest
        -- approximation, and it is still not "whenever we got round to it".
        min((a.work_date + COALESCE(wr.shift_start, TIME '00:00'))
            AT TIME ZONE 'Africa/Kigali')       AS invoked_at,
        -- Kept, because the gap between the two is itself worth watching: it
        -- is how long an employer stood there before we knew.
        min(a.confirmed_at)                     AS noticed_at
    FROM attendance a
    JOIN work_requests wr ON wr.request_id = failed.request_id
    WHERE a.placement_id = failed.placement_id
      AND NOT a.present
) ns ON ns.invoked_at IS NOT NULL
LEFT JOIN placements replacement
       ON replacement.replaces_placement = failed.placement_id
      -- Cover that was actually taken up. An offer still unanswered is
      -- pending, not filled; one that was declined or cancelled is neither.
      AND replacement.status IN ('accepted', 'active', 'completed')
WHERE failed.status = 'no_show';
