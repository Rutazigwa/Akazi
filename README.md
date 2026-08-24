# Placement Operations

Internal admin system for a demand-led youth placement operator in Rwanda.

This is the phase-one system from the *Revised Blueprint v2* build order: the
operational CRM that coordinators use to run placements. There is no candidate
app and no employer dashboard yet — see `CLAUDE.md` for why that order matters.

**What we sell is a covered shift, not a candidate.** The schema and the matching
engine are both built around that: transport cost is a hard matching constraint,
replacement chains are preserved as evidence, and every read of a national ID
number is audited.

## Quick start

```bash
docker compose up -d db                 # Postgres 16
./scripts/migrate.sh                    # apply migrations in order

python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env                    # set DATA_RESIDENCY
.venv/bin/python -m pytest              # 167 tests
.venv/bin/uvicorn app.main:app --reload
```

The app will refuse to start until `DATA_RESIDENCY` is set. That is deliberate —
see [Data residency](#data-residency).

## Layout

```
migrations/     Numbered SQL, applied in order. Blocks match the blueprint.
app/config.py   Settings + the residency guard
app/db.py       Engine, session scope, audit attribution
app/matching/   The v1 matching engine, transport estimation, DB loading
app/operations/ Registration, work requests, offers, attendance, guarantee,
                data subject rights
app/auth.py     Passwords, sessions, lockout
app/mfa.py      TOTP enrolment, session elevation, replay guard
app/deps.py     Session + authenticated-staff dependencies
app/routers/    JSON API endpoints
app/web/        The admin UI — server-rendered templates, cookie session, CSRF
tests/          Filters, residency guard, and DB-backed operations tests
scripts/        migrate.sh, testdb.sh, create_staff.py, backup.sh
deploy/         Production stack: Postgres + app + Caddy (automatic TLS)
docs/           DEPLOYMENT.md — the Rwanda VPS runbook
```

## Data residency

Law No. 058/2021 requires personal data to be stored in Rwanda unless a
registration certificate authorises otherwise. The realistic way to breach it is
not a decision but a default: a convenient managed Postgres in a foreign region,
"just for now", and then real national ID numbers land on it.

So `DATA_RESIDENCY` has no default and the app will not start without it:

| Value | Meaning |
|---|---|
| `rwanda_self_hosted` | Postgres on a Rwandan/regional VPS — pilot default |
| `split_store` | Identity data in Rwanda, opaque UUIDs abroad |
| `cross_border_authorised` | NCSA certificate held; requires `NCSA_CERTIFICATE_REF` |
| `local_dev` | Throwaway data on a local database only |

`local_dev` is rejected if `DATABASE_URL` points anywhere remote.

## Identity isolation and the audit trail

`candidate_identity` holds legal names, national IDs and dates of birth. It is
separate from the operational `candidates` table so that most staff can work
without identity access, and so one table moves if the split-store option is
adopted later.

PostgreSQL has no `SELECT` trigger, so **direct reads of `candidate_identity` are
revoked**. Reads go through `read_candidate_identity(uuid)`, a `SECURITY DEFINER`
function that writes an `audit_log` row first. Writes are audited by trigger.

Attribution comes from a transaction-local setting, so use `session_scope`:

```python
with session_scope(staff_id=coordinator_id) as s:
    ...
```

An audit row with a null `staff_id` is evidence that proves nothing.

## Matching

Five sequential filters, in `app/matching/engine.py`. Not weights, not a model —
at pilot volume an employer asking "why this person" must get a straight answer,
and a model trained on 40 placements is noise.

1. **Hard exclusions** — age, consent, availability, required skill scores
2. **Transport viability** — under the candidate's ceiling, and under 30% of
   daily pay unless the employer covers transport
3. **Safety** — after-dark shifts need covered transport or a recorded opt-in
4. **Ranking** — prior placements with this employer → 30-day retention →
   assessment score → commute time
5. **Reason** — every match carries the sentence a coordinator reads to the
   employer

Filter 2 is the one that earns its keep. The blueprint's example — RWF 3,000/day
against RWF 1,600 in moto fare — is 53% of pay and is rejected outright; there is
a test named for it.

`match_candidates` returns rejections as well as matches. A request that fills
nobody is a demand signal, and the reason tells you whether the problem is pay,
transport, skills or supply. `GET /work-requests/{id}/matches` returns both:

```
MATCH   Aline U.       matched on: availability, 11-min commute,
                       net RWF 3850/day after transport
EXCLUDE Beatrice M.    [transport_viability] transport RWF 7600/day exceeds
                       the candidate's ceiling of RWF 2000
EXCLUDE Claude N.      [hard_exclusion] availability does not cover 08:00-16:00
```

An offer **re-runs the filters** rather than trusting the list the coordinator is
looking at. Someone who withdrew consent five minutes ago must not be placeable
from a stale screen.

### Transport estimates are provisional

`app/matching/transport.py` converts straight-line distance to a moto fare with a
base plus per-kilometre rate. **Those rates are placeholders.** Real Kigali fares
vary by route, time, weather and negotiation, and straight-line distance
understates a hilly city. Calibrate against receipts from the first cohort and
replace the model; until then, confirm the fare with the candidate before
offering.

A candidate with no home coordinates gets **no** estimate — `None`, never zero.
Zero would silently disable the filter that prevents most 30-day dropouts, which
is why capturing home location at registration matters.

## The admin UI

`/ui` — server-rendered HTML, one stylesheet, no build step and no JavaScript
framework. The blueprint's instruction for this layer was "low traffic, high data
density, do not over-engineer", and a handful of coordinators on laptops is
exactly the case where a SPA costs more than it returns.

The screens follow the operation rather than the schema:

- **Dashboard** — open guarantees first, with the 24-hour clock and a *Cover it*
  link; then the pilot scorecard against its targets; then check-ins due and open
  requests.
- **Work request → matches** — ranked candidates each with the sentence a
  coordinator reads back to the employer, and every exclusion with its reason.
- **Placement** — accept, start, log a day, and for a no-show, the candidates who
  can cover it *right there*, without navigating away while the clock runs.

The UI authenticates with a cookie rather than a bearer token, so CSRF applies to
it in a way it does not to the JSON API. Two defences: `SameSite=Strict` on an
`HttpOnly` cookie, and a per-session CSRF token on every state-changing form. The
cookie is `Secure` in every deployment except local development.

## Authentication

There is no self-registration — this is an internal admin system. Create the
first account from the command line:

```bash
python scripts/create_staff.py --name "Owner" --phone +250780000001 \
    --role owner --identity-access
```

The password is read from the terminal or `STAFF_PASSWORD`, never from an
argument (shell history, process list). Then:

```bash
curl -X POST localhost:8000/auth/login \
  -d '{"phone":"+250780000001","password":"..."}' -H 'Content-Type: application/json'
# -> {"token": "...", ...}   then send: Authorization: Bearer <token>
```

**Tokens are opaque and stored in the database, not JWTs.** The deciding factor
is revocation. This system holds national ID numbers; when a coordinator leaves,
their access has to stop when someone says so — not whenever a signed token
happens to expire. Deactivating a staff member invalidates their live sessions on
the very next request. Only the SHA-256 of a token is stored, so a leaked backup
yields no usable session.

Five failed logins lock an account for 15 minutes. Every failure path — unknown
account, wrong password, locked, deactivated — returns the same 401 with the same
body, so there is no way to enumerate staff.

### Two-factor authentication

A password is enough for operational work — attendance, follow-ups, the
scorecard. It is **not** enough to reach a national ID number. Identity data
requires a second factor on the current session:

```
POST /auth/totp/enrol      -> secret + otpauth:// URI (shown once)
POST /auth/totp/confirm    -> prove the authenticator works
POST /auth/mfa             -> elevate THIS session
```

Elevation is per session, not per account. A code presented on a laptop does not
elevate a token someone else is holding.

A TOTP code stays valid for its whole 30-second step, so a code seen over a
shoulder or replayed from a proxy log would otherwise work again. The highest
accepted step is recorded and anything at or below it is refused — reusing a
code returns `this code has already been used`.

Set `REQUIRE_MFA_FOR_IDENTITY=false` only for local development against
throwaway data.

### Identity access is a separate grant

`staff.can_view_identity` gates `/candidates/{id}/identity`, and it is **not**
implied by role. An owner is not automatically entitled to read national ID
numbers; somebody grants it deliberately.

Reads go through `read_candidate_identity()`, so every one lands in `audit_log`
attributed to a named person. `GET /candidates/{id}/access-log` is the answer to
"who looked at this record" — for the NCSA, and for the candidate, who is
entitled to ask.

A refused read logs nothing: the gate runs before the function that logs, so a
403 is not recorded as a read. There's a test for that.

## Data subject rights

**Access** — `GET /candidates/{id}/data-export` returns everything held about one
person: identity, profile, consent history, assessments, placements, attendance,
pay records, follow-ups, and the log of who has read their identity record.
Producing an export is itself an identity access, so it appears in that log.

**Erasure** — `POST /candidates/{id}/erasure-requests`, then a separate
`/complete`. It is two steps on purpose: the gap is where someone checks whether
acting on it would strand an unpaid wage or an active placement. Those blockers
are returned with the request, and they are **advisory** — the right belongs to
the person, not to us.

Erasure **redacts rather than deletes**, and this is the important part.
`candidate_identity` is the parent of `candidates`, which is the parent of
placements, attendance, pay records and follow-ups, all with `ON DELETE CASCADE`.
A literal `DELETE` would destroy an employer's confirmed attendance, the pay
records proving someone was paid, and the replacement chain belonging to an
entirely different candidate. So the identity row is overwritten in place: names,
national ID and phone numbers become `NULL` or `ERASED`, home coordinates are
cleared, and the surrogate key survives. The employment history stays intact and
is no longer attached to an identifiable person.

Consent records and the audit log survive erasure deliberately — they are the
evidence that the processing was lawful, and destroying that is the opposite of
compliance.

Erasure **refuses to run unattributed**. It is the one operation where "who did
this" can never be reconstructed afterwards, because the data it describes is
gone.

## Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). In short: a Rwandan or
regionally-hosted VPS, disk encrypted with LUKS before anything is installed,
Postgres on an internal Docker network with no published port, and Caddy in front
for automatic TLS. Bearer tokens over plaintext HTTP are simply readable, so
there is no acceptable non-TLS deployment of this system.

`scripts/backup.sh` refuses to write an unencrypted dump and verifies that what
it wrote decrypts before reporting success.

## Staff administration

Owner and admin only, under `/staff`: create an account, reset a forgotten
password, clear a lost second factor, change role or identity access, deactivate
someone.

New accounts get a generated single-use password, returned once and forced to
change at first login — an administrator who picks the password knows it.
Identity access defaults **off** even for an admin.

Withdrawing identity access, resetting a password, resetting MFA and deactivating
all **revoke the target's live sessions**. A change of access that waits for a
token to expire is not a change of access.

Staff rows are never deleted — they are referenced by `candidates.registered_by`,
`assessment_results.assessed_by` and `audit_log.staff_id`, and removing one would
orphan the record of who did what.

## The audit log is tamper-evident

`audit_log` lives in the database it audits, so anyone with write access could
edit the row recording that they read someone's national ID. Each entry is now
hash-chained to the one before it:

```
entry_hash = SHA-256(row contents ‖ previous entry_hash)
```

`GET /staff/audit/integrity` walks the chain and reports the first row that does
not reconcile:

```json
{"intact": false,
 "broken_at": {"broken_at_audit_id": 7,
               "reason": "entry_hash does not match the row contents — this row was modified"}}
```

Editing a row and removing one are distinguishable: an edit breaks that row's own
hash, a deletion breaks the *next* row's `prev_hash`.

`UPDATE` and `DELETE` on `audit_log` are refused outright by rules. The chain is
what catches someone who disables those rules.

**This makes tampering detectable, not impossible.** An attacker with enough
access and time can recompute the whole chain. The defence against that is
publishing `head_hash` somewhere off the server — once a hash exists elsewhere,
no local rewrite of history can match it.

## The guarantee

A no-show is recorded like any other absence, but `log_attendance` returns a
`GuaranteeInvocation` when it is the worker's *first* scheduled day — that is the
case where the employer got nothing. A later absence on a placement that already
ran is an absence, not a no-show: the shift was covered and the employer got what
they bought.

An invocation starts a 24-hour clock. `GET /guarantees/open` is the coordinator's
queue, ordered oldest first, with a `breached` flag once the window passes.

**The failed placement stays `no_show` forever.** Coverage is recorded by a new
placement row whose `replaces_placement` points back at it — never by editing the
original. Flipping it to `replaced` would remove the invocation from
`v_guarantee_invocations` and silently improve the reliability numbers. There is a
test named for that.

## Metrics

`v_pilot_scorecard` is one row carrying every headline target from the blueprint —
active employers, time to fill, 30-day retention, average transport share,
guarantee fill rate, women placed, pay accuracy. Everything derives from the
operational rows; no metric can be set independently of the events it describes.

Two deliberate definitions:

- **Retention** counts only *answered* day-30 check-ins. An unanswered one is
  missing data, not a failure, and must not drag the number down.
- **Pay accuracy** requires in full *and* on the agreed date. Late-but-complete is
  still a broken promise to someone living on the wage.

## Running the tests

The operations and metrics tests run against a real PostgreSQL instance, because
most of what they protect is enforced by the database. Point `TEST_DATABASE_URL`
at a throwaway server, or run `scripts/testdb.sh`. Without one they skip; the
matching and config tests still run anywhere.

## What is not here, on purpose

- **No payments.** `pay_records` records terms, amounts and dates so pay accuracy
  is measurable. Moving money is phase 2.
- **No ML matching.** See above.
- **No candidate or employer UI.** Weeks 7–12 and month 4+ respectively.
- **Apprenticeship exceptions for ages 13–15.** The age constraint is a hard 16;
  exceptions need an authorised-exception record and a deliberate schema change.

## Before this runs on real data

See `CLAUDE.md` §9. In short: the Ministerial Order on private employment
agencies has to be read by a Rwandan employment lawyer, NCSA controller
registration takes up to 30 working days, and a Data Protection Officer has to be
named. None of those are code, and all of them block go-live.
