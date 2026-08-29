-- The response-time metric moved the wrong way.
--
-- v_escalation_response computed hours_to_acknowledge as
--     COALESCE(acknowledged_at, now()) - raised_at
-- so an escalation nobody had answered contributed its elapsed-time-so-far to
-- the average. A freshly raised report contributes nearly zero. The Reports
-- page averages that column and labels it "avg hours".
--
-- Measured on the demo database: one harassment report answered after 5.00
-- hours, against a 2-hour target. Three more harassment reports then arrive
-- and nobody touches them -- and the figure improves to 1.25 hours. The number
-- whose stated purpose is to show whether the safeguard is real gets better
-- the more reports go unanswered.
--
-- The view's own comment says this is "the number that shows whether the
-- safeguard is real". It was showing the opposite.
--
-- A response time needs a response. hours_to_acknowledge is now NULL until
-- there is one, and how long something has been waiting gets its own column,
-- because that is a different question and the page needs to ask both.

DROP VIEW v_escalation_response;

CREATE VIEW v_escalation_response AS
SELECT e.escalation_id,
       e.kind::text AS kind,
       e.raised_at,
       e.respond_by,
       e.acknowledged_at,
       e.status::text AS status,
       (e.acknowledged_at IS NOT NULL AND e.acknowledged_at <= e.respond_by)
           AS answered_in_time,
       (e.acknowledged_at IS NULL AND now() > e.respond_by) AS overdue,
       -- NULL until answered. avg() skips NULLs, so an unanswered report can
       -- no longer flatter the average -- it is counted as unanswered instead.
       CASE WHEN e.acknowledged_at IS NOT NULL THEN
           ROUND(EXTRACT(EPOCH FROM (e.acknowledged_at - e.raised_at)) / 3600.0, 2)
       END AS hours_to_acknowledge,
       -- How long it has been waiting, answered or not. This is the number a
       -- coordinator wants beside an open report, and it is the one that gets
       -- worse when nobody responds.
       ROUND(EXTRACT(EPOCH FROM (
           COALESCE(e.acknowledged_at, now()) - e.raised_at)) / 3600.0, 2)
           AS hours_elapsed
FROM escalations e;

GRANT SELECT ON v_escalation_response TO app_operations, app_identity;
