-- Create the least-privileged login role the application should run as.
--
--   psql "$ADMIN_DSN" -v app_password="'...'" -f scripts/create_app_role.sql
--
-- Why this matters beyond tidiness: connecting as the database owner means the
-- application can ALTER TABLE audit_log DISABLE RULE, drop the hash-chain
-- trigger, or DROP the audit log outright -- which is precisely the tampering
-- the chain exists to make detectable. A role that owns nothing cannot do any
-- of it, so the evidence trail is protected by the database rather than by the
-- application behaving itself.
--
-- This role holds both app_operations and app_identity because the application
-- uses a single connection. Splitting identity work onto a second connection
-- with only app_operations on the first is the next hardening step -- see
-- docs/DEPLOYMENT.md.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'akazi_app') THEN
        EXECUTE format('CREATE ROLE akazi_app LOGIN PASSWORD %L',
                       current_setting('akazi.app_password'));
    END IF;
END
$$;

GRANT app_operations, app_identity TO akazi_app;
GRANT USAGE ON SCHEMA public TO akazi_app;

-- Explicitly withhold the schema-changing power the owner would have.
REVOKE CREATE ON SCHEMA public FROM akazi_app;

-- Sanity checks. These fail the script rather than leaving a role that quietly
-- has more than intended.
DO $$
BEGIN
    IF has_table_privilege('akazi_app', 'candidate_identity', 'SELECT') THEN
        RAISE EXCEPTION 'akazi_app has direct SELECT on candidate_identity -- '
                        'reads must go through read_candidate_identity()';
    END IF;
    IF pg_has_role('akazi_app', 'pg_read_all_data', 'MEMBER') THEN
        RAISE EXCEPTION 'akazi_app is a member of pg_read_all_data';
    END IF;
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = 'akazi_app') THEN
        RAISE EXCEPTION 'akazi_app is a superuser';
    END IF;
END
$$;

\echo 'akazi_app created: operations + identity functions, no schema ownership'
