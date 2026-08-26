-- 025. THE CONTRACT MESSAGE IS ONCE-ONLY
--
-- migration 019 lists the template keys that must not be sent twice per
-- placement. placement_contract belongs on that list: a worker receiving two
-- copies of their agreement has reason to wonder which one holds.

BEGIN;

DROP INDEX IF EXISTS idx_messages_once_per_placement;

CREATE UNIQUE INDEX idx_messages_once_per_placement
    ON messages (placement_id, template_key)
 WHERE placement_id IS NOT NULL
   AND template_key IN ('placement_offer','shift_reminder',
                        'placement_contract','placement_cancelled',
                        'followup_day_1','followup_week_1',
                        'followup_day_30','followup_day_90');

COMMIT;
