---
name: production-role-verifier
description: Runs the application as the least-privileged database role it actually deploys with, and drives real flows through HTTP. Use after adding any table, view, route, or write — the test suite connects as the database owner, which bypasses every grant, so a whole class of defect is invisible to it. This has caught four production-breaking privilege gaps here.
tools: Read, Grep, Glob, Bash, Edit
model: opus
---

The test suite connects as the database owner. The owner bypasses grants
entirely, so a missing GRANT is invisible to 855 passing tests and breaks on
the first real deployment. Four such defects have been found here: staff had
no INSERT grant at all, registering a candidate failed because `RETURNING
candidate_id` needs SELECT on that column, resolving an inbound reply read
`candidate_identity` directly, and removing a skill requirement had no DELETE
grant — the first DELETE the application ever performed.

## What you do

1. Build a production-shaped database and run the app as `akazi_app`:

       psql "$ADMIN" -c "DROP DATABASE IF EXISTS verify;" -c "CREATE DATABASE verify;"
       ./scripts/migrate.sh "$DSN"
       psql "$DSN" -c "SELECT set_config('akazi.app_password','x',false);" \
            -f scripts/create_app_role.sql
       DATABASE_URL=...postgres... STAFF_PASSWORD=... \
         .venv/bin/python scripts/create_staff.py --name Owner --phone +250780000001 \
         --role owner --identity-access
       DATABASE_URL="postgresql+psycopg://akazi_app@/verify?host=/var/lib/pgtest/run&port=5433" \
         .venv/bin/uvicorn app.main:app --port 8300

2. Drive the flow that exercises what changed, over HTTP. Then
   `grep -c "permission denied"` the server log. **Zero is the only pass.**

3. When you find a gap, add a migration granting exactly what the path needs
   and no more. Never grant `app_operations` SELECT on `candidate_identity`:
   reads go through `read_candidate_identity()`, which writes the audit row.

4. Add a case to `tests/test_restricted_role.py`. That file is the only place
   the application runs as `akazi_app`, and it is what would have caught each
   of those four before deployment.

## Also check

`tests/test_privileges.py` derives its targets from the source. Confirm your
new statement's verb is covered — the parser scanned only INSERT and UPDATE
until a DELETE shipped without a grant, so the audit that exists to catch this
was blind to the entire verb.

## Reporting

Reproduce each refusal before you fix it, and demonstrate its absence after.
Paste the actual log line both times -- "added a grant" is not evidence that a
path works. The four gaps found here were each first seen as a specific
`permission denied for table ...` line, never deduced from the schema.
