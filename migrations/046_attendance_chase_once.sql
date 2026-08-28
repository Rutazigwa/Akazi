-- 046. THE ATTENDANCE CHASE COULD ASK TWICE
--
-- chase_attendance runs daily and queues a message per unconfirmed placement.
-- Nothing stopped it queueing the same question again the next morning, and
-- the morning after that, until somebody answered. An employer asked the same
-- question five days running stops reading the messages, and the one that
-- matters is the next one.
--
-- migration 025 lists the template keys that must not be sent twice per
-- placement. This belongs on it. The list is the enforcement -- the
-- application's ON CONFLICT DO NOTHING relies on this index existing, which
-- is exactly the sort of dependency that is silently absent for a new
-- template until somebody tests the second call.

BEGIN;

DROP INDEX IF EXISTS idx_messages_once_per_placement;

CREATE UNIQUE INDEX idx_messages_once_per_placement
    ON messages (placement_id, template_key)
 WHERE placement_id IS NOT NULL
   AND template_key IN ('placement_offer','shift_reminder',
                        'placement_contract','placement_cancelled',
                        'attendance_unconfirmed',
                        'followup_day_1','followup_week_1',
                        'followup_day_30','followup_day_90');

COMMIT;
