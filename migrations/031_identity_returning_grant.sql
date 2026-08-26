-- 031. REGISTERING A CANDIDATE FAILED UNDER THE REAL ROLE
--
-- Two decisions in this schema collided, and the collision only appears when
-- the role model is actually used:
--
--   008  revoked SELECT on candidate_identity, so every read goes through
--        read_candidate_identity() and lands in audit_log.
--   registry.register_candidate  uses INSERT ... RETURNING candidate_id.
--
-- PostgreSQL requires SELECT on any column named in RETURNING. So the core
-- operation of the whole system -- registering a candidate -- failed with
-- "permission denied for table candidate_identity" on any deployment using the
-- role model, while passing every test, because the tests run as superuser.
--
-- The fix is a column-level grant on the surrogate key alone. candidate_id is
-- already present throughout the operational schema -- candidates, placements,
-- attendance -- so this discloses nothing that was not already readable. The
-- identifying columns (legal names, national ID, date of birth, phone) remain
-- unreadable, and the audit trail stays complete.

BEGIN;

GRANT SELECT (candidate_id) ON candidate_identity TO app_identity;

COMMIT;
