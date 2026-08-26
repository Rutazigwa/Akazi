-- 022. A PLACEMENT CANCELLED BY THE EMPLOYER
--
-- When an employer withdraws a shift, the placements against it have to go
-- somewhere. None of the existing statuses is honest about what happened:
--
--   declined    the CANDIDATE turned it down. Recording the employer's
--               decision this way puts a refusal on the worker's record that
--               they did not make, and prior behaviour feeds the ranking.
--   terminated  the work ended early -- but it never started.
--   no_show     they failed to arrive at work that was, in fact, cancelled.
--
-- So: a status of its own. The blueprint's instruction is to extend the enums
-- rather than reach for a free-text status column, and this is exactly the
-- case it had in mind.
--
-- Cancelled placements are deliberately excluded from the guarantee metrics:
-- a shift the employer withdrew is not a shift we failed to cover.

BEGIN;

ALTER TYPE placement_status ADD VALUE IF NOT EXISTS 'cancelled';

COMMIT;
