-- 030. THE APPLICATION COULD NOT CREATE A STAFF ACCOUNT
--
-- Migration 010 granted SELECT and UPDATE on staff, which is what login and
-- lockout need. Creating an account needs INSERT, and nothing had it -- so the
-- staff console worked in every test (which run as superuser) and would have
-- failed on the first deploy that used the role model, at the moment an owner
-- tried to add their first coordinator.
--
-- Exactly the shape of the matcher bug in migration 018: correct everywhere
-- except where it matters. tests/test_privileges.py now derives the required
-- grants from the application source, so a new INSERT or UPDATE target cannot
-- silently arrive without one.

BEGIN;

GRANT INSERT ON staff TO app_operations;

COMMIT;
