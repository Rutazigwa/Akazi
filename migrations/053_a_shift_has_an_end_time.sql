-- A shift with no end time skipped two of the four matching filters.
--
-- The employer's "Post a shift" form leaves From and Until blank and does not
-- require them. Leave them blank and:
--
--   * _hard_exclusions skips the availability check entirely -- it is guarded
--     by "if r.shift_start is not None and r.shift_end is not None"
--   * _safety returns None on its first line -- "if r.shift_end is None"
--
-- Demonstrated on one woman with no after-dark opt-in and no employer-covered
-- transport, against two otherwise identical requests:
--
--   ends 22:00            refused  [hard_exclusion] availability does not cover 14:00-22:00
--   no end time recorded  MATCHED  matched on: 20-min commute, net RWF 4500/day
--
-- The safety filter exists because female unemployment is 15.5% against 11.6%
-- male and the blueprint makes women's participation a product requirement,
-- not a reporting line. Two blank fields turned it off, and the match reason
-- did not even mention that it had not been applied.
--
-- Shift work must state when it ends. The other work types are left alone:
-- an internship or a project can legitimately have no daily window, and
-- requiring one would be inventing data. What those get instead is a match
-- reason that says the after-dark check could not be made -- see
-- app/matching/engine.py.
--
-- Enforced here rather than only in Python because this is the same class of
-- rule as the minimum age: something that must be true of the data however it
-- arrived.

ALTER TABLE work_requests
    ADD CONSTRAINT chk_shift_has_times
    CHECK (work_type <> 'shift'
           OR (shift_start IS NOT NULL AND shift_end IS NOT NULL));

COMMENT ON CONSTRAINT chk_shift_has_times ON work_requests IS
    'Shift work states its hours. Availability matching and the after-dark '
    'safety filter both silently skip when shift_end is null.';
