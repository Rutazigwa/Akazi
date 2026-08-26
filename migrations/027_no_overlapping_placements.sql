-- 027. NO OVERLAPPING PLACEMENTS, ENFORCED UNDER CONCURRENCY
--
-- The matcher already excludes a candidate who is committed elsewhere, and
-- offer_placement re-runs that check at the moment of offering. Both are reads
-- followed by a write, and two coordinators offering the same person at the
-- same time each pass the check before either commits. Demonstrated with two
-- concurrent connections: both succeeded, and one worker ended up placed on two
-- overlapping shifts.
--
-- That is the exact failure the application check exists to prevent, and it is
-- invisible -- both employers are told someone is coming.
--
-- An EXCLUDE constraint would be the natural tool, but the dates and hours live
-- on work_requests rather than on placements, and an exclusion constraint can
-- only see its own table. So: a trigger that takes a per-candidate advisory
-- lock before checking. The lock is transaction-scoped, so concurrent inserts
-- for the same candidate serialise and the second one sees the first.
--
-- The application check stays. It produces a message a coordinator can act on
-- and a rejection reason on the match screen; this is the backstop that makes
-- the guarantee true rather than likely.

BEGIN;

CREATE OR REPLACE FUNCTION fn_no_overlapping_placement() RETURNS TRIGGER AS $$
DECLARE
    v_title    TEXT;
    v_employer TEXT;
BEGIN
    -- Only live commitments matter. A declined, cancelled or finished
    -- placement holds nothing.
    IF NEW.status NOT IN ('offered', 'accepted', 'active') THEN
        RETURN NEW;
    END IF;

    -- Serialise writes for this candidate. Transaction-scoped, so it is held
    -- until commit and a concurrent offer waits rather than racing.
    PERFORM pg_advisory_xact_lock(
        hashtext('placement:' || NEW.candidate_id::text)
    );

    SELECT other_request.title, e.business_name
      INTO v_title, v_employer
      FROM placements p
      JOIN work_requests other_request ON other_request.request_id = p.request_id
      JOIN employers e ON e.employer_id = other_request.employer_id
      JOIN work_requests this_request ON this_request.request_id = NEW.request_id
     WHERE p.candidate_id = NEW.candidate_id
       AND p.placement_id IS DISTINCT FROM NEW.placement_id
       AND p.status IN ('offered', 'accepted', 'active')
       AND other_request.request_id <> NEW.request_id
       AND daterange(other_request.starts_on,
                     COALESCE(other_request.ends_on, other_request.starts_on),
                     '[]')
           && daterange(this_request.starts_on,
                        COALESCE(this_request.ends_on, this_request.starts_on),
                        '[]')
       AND (other_request.shift_start IS NULL
            OR this_request.shift_start IS NULL
            OR (other_request.shift_start, other_request.shift_end)
               OVERLAPS (this_request.shift_start, this_request.shift_end))
     LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'already committed to overlapping work: % at %', v_title, v_employer
            USING ERRCODE = 'exclusion_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_no_overlapping_placement
    BEFORE INSERT OR UPDATE OF status, candidate_id, request_id ON placements
    FOR EACH ROW EXECUTE FUNCTION fn_no_overlapping_placement();

COMMIT;
