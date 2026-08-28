-- 049. TWO MESSAGES COULD CARRY THE SAME PROVIDER REFERENCE
--
-- A delivery receipt identifies its message by provider_ref, and nothing said
-- that had to be unique. The recording provider -- which DEPLOYMENT.md
-- recommends running the pilot's first week on, so that a week of messages can
-- be read before any of them reaches a real person -- numbered its references
-- from a counter that resets with each process. The cron starts a fresh one
-- every five minutes, so the first message of every run was 'recording:1'.
--
-- The consequence was not a wrong number in a report. record_delivery matches
-- on provider_ref, so one receipt marked every message sharing that reference
-- as delivered, and then raised MultipleResultsFound -- returning a 500 to the
-- provider after the update had already happened. The provider retries, and
-- the delivery record drifts further from what actually reached a handset.
--
-- The counter is now a uuid, and the database refuses a repeat. A real
-- provider that ever reuses an id is a bug worth failing loudly on rather than
-- absorbing: delivery state is the input to the "sent but not confirmed
-- delivered" flag, which is how a coordinator learns a worker never heard
-- about their shift.

BEGIN;

-- Any existing collisions are demonstration data. Keep the earliest reference
-- and clear the rest: a null reference means "we cannot match a receipt to
-- this", which is true and honest, where a duplicate means "this receipt
-- applies to several messages", which is not.
WITH ranked AS (
    SELECT message_id,
           row_number() OVER (PARTITION BY provider_ref ORDER BY created_at,
                              message_id) AS n
      FROM messages
     WHERE provider_ref IS NOT NULL
)
UPDATE messages m
   SET provider_ref = NULL
  FROM ranked
 WHERE m.message_id = ranked.message_id AND ranked.n > 1;

CREATE UNIQUE INDEX idx_messages_provider_ref
    ON messages (provider_ref) WHERE provider_ref IS NOT NULL;

COMMIT;
