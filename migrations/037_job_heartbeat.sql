-- 037. NOTHING KNEW WHETHER THE DISPATCHER WAS STILL RUNNING
--
-- Messages are queued by the application and sent by a cron. If that cron
-- dies -- a container replaced without its crontab, a failing deploy, a
-- machine that came back up without it -- the queue simply stops draining.
-- No shift reminder goes out, no placement offer reaches a candidate, and the
-- first anyone hears of it is a worker who did not know they had a shift.
--
-- The failure is worse than it looks, because it is silent and it costs
-- money: an unreminded worker is a no-show, a no-show invokes the guarantee,
-- and the guarantee is priced into the fee. Meanwhile /health says "ok",
-- because the web application is perfectly healthy. It is the part nobody is
-- watching that stopped.
--
-- Two signals, because they fail differently and one alone is not enough:
--
--   job_runs is a heartbeat. It answers "is the dispatcher running at all",
--   and it is the only thing that can tell a stalled cron from a quiet
--   evening with nothing to send.
--
--   Overdue messages are the symptom, computed from the outbox itself. It
--   answers "are messages actually moving", which stays true even when the
--   dispatcher runs happily and fails at every send.

BEGIN;

CREATE TABLE job_runs (
    run_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name     VARCHAR(40)  NOT NULL,
    started_at   TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    finished_at  TIMESTAMPTZ,
    ok           BOOLEAN,
    detail       JSONB,
    -- A run that fell over mid-way is more interesting than one that never
    -- started, so the error is kept rather than only the fact of failure.
    error        TEXT
);

CREATE INDEX idx_job_runs_recent ON job_runs (job_name, started_at DESC);

COMMENT ON TABLE job_runs IS
    'Heartbeat for scheduled work. A job that stops running is otherwise '
    'indistinguishable from a job with nothing to do.';

-- Keeping every run forever turns a heartbeat into a growth problem, and
-- nobody reads a dispatcher run from four months ago.
CREATE OR REPLACE FUNCTION prune_job_runs(p_keep_days INT DEFAULT 30)
RETURNS BIGINT AS $$
DECLARE
    removed BIGINT;
BEGIN
    DELETE FROM job_runs
     WHERE started_at < now() - make_interval(days => p_keep_days);
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$ LANGUAGE plpgsql;

-- The last run of each job, with how long ago it was.
CREATE VIEW v_job_health AS
SELECT DISTINCT ON (job_name)
       job_name,
       started_at        AS last_started_at,
       finished_at       AS last_finished_at,
       ok                AS last_ok,
       error             AS last_error,
       detail            AS last_detail,
       ROUND(EXTRACT(EPOCH FROM (now() - started_at)) / 60.0, 1)
                         AS minutes_since
FROM job_runs
ORDER BY job_name, started_at DESC;

-- Messages whose time has passed and which have not moved. Deliberately not
-- restricted to shift reminders: an offer nobody received is the same failure.
CREATE VIEW v_overdue_messages AS
SELECT m.message_id, m.template_key, m.channel::text AS channel,
       m.status::text AS status, m.scheduled_for, m.attempts, m.last_error,
       ROUND(EXTRACT(EPOCH FROM (now() - m.scheduled_for)) / 60.0, 1)
           AS minutes_late
  FROM messages m
 WHERE m.status IN ('queued', 'sending')
   AND m.scheduled_for < now()
 ORDER BY m.scheduled_for;

GRANT SELECT, INSERT, UPDATE ON job_runs TO app_operations;
GRANT SELECT ON v_job_health, v_overdue_messages TO app_operations;
GRANT EXECUTE ON FUNCTION prune_job_runs(INT) TO app_operations;

COMMIT;
