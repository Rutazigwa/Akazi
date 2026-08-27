# CLAUDE.md

Guidance for Claude Code when working on this project.

> **Status: phase one in progress.** The schema and the matching engine exist
> and are tested; there is no UI yet. This file is the specification-of-record,
> derived from *Revised Blueprint — Version 2* (23 August 2026), which supersedes
> v1 of 16 August 2026. Read it before proposing architecture, schema, or scope.

## Repository layout

```
migrations/     Numbered SQL. File numbers match the blueprint's schema blocks.
                000 (roles + staff) must stay first: five tables reference it.
app/config.py   Settings + the residency guard (Settings refuses to construct
                without DATA_RESIDENCY -- do not add a default)
app/db.py       Engine + session_scope(staff_id=...), which stamps app.staff_id
                so the audit triggers can attribute the action
app/matching/   engine.py is pure functions, no DB access. repository.py is
                the seam that loads candidates and runs the filters.
                transport.py estimates fares -- rates are PLACEHOLDERS.
app/operations/ registry.py (employers, candidates, consent), requests.py
                (work requests, offers), attendance.py, follow_ups.py,
                data_rights.py (access + erasure)
deploy/         Production stack. docs/DEPLOYMENT.md is the runbook.
app/auth.py     Passwords (argon2), DB-backed session tokens, lockout
app/mfa.py      TOTP: enrolment, per-session elevation, replay guard
app/deps.py     db_session, current_staff, require_identity_access
app/routers/    JSON API -- all require a bearer session
app/web/        Admin UI (/ui) and employer dashboard (/employer). Jinja,
                cookie sessions, CSRF. No build step and no JS framework --
                keep it that way unless "do not over-engineer" stops being true.
app/employer_auth.py, app/operations/employer_portal.py
                The employer principal. Separate session table from staff.
app/main.py     FastAPI admin app (coordinators and owner only)
tests/          Filter behaviour and the residency guard
scripts/        migrate.sh
```

Run `env DATA_RESIDENCY=local_dev .venv/bin/python -m pytest` before pushing.
CI (.github/workflows/ci.yml) runs lint, applies every migration to an empty
Postgres 16, and runs the full suite on each push to main.

---

## 1. What this project is

A **demand-led youth placement operator for Rwanda** — an operations business
with software, not a software business.

The system being built is the internal machinery that runs placements: employer
records, candidate registry, assessment scoring, cohort management, placement
contracts, attendance logging, follow-up scheduling, and replacement tracking.

**Thesis:** the defensible advantage is not matching, verification, or an
outcomes dashboard — competitors already have those. It is **operational
reliability**: the employer's shift gets covered, and if it doesn't, we cover
it. We sell certainty, not candidates.

**Addressable population:** 928,426 youth aged 16–30 not in employment,
education or training (24.9% of the cohort, NISR Q2 2026).

### The four gaps that define the product

Every competitor ends its responsibility at the introduction. Nobody owns:

1. Whether the worker actually arrives (attendance guarantee)
2. Whether the wage survives the commute (net earnings after transport)
3. Whether the money moves correctly (payment terms, timing, deductions)
4. Whether a no-show is replaced inside 24 hours

These are operations capabilities, not software features — which is exactly why
software companies won't build them. Design every feature to serve one of them.

---

## 2. Non-negotiable constraints

Do not violate these without an explicit decision from the project owner. If a
request appears to conflict with one, flag it before writing code.

| # | Rule | Why |
|---|------|-----|
| 1 | **No AI/ML matching in v1.** Use a sequential rules engine. | Explainability beats optimality at this volume. A model trained on 40 placements is noise. When an employer asks "why this person," we must be able to answer. |
| 2 | **No payment movement in the pilot.** Record payment terms, amounts and dates as data only. | Moving money triggers regulatory work that pilot volume does not justify. Wage rails are act two. |
| 3 | **No employer mobile app, ever.** Responsive web only. | Employers are on laptops and good phones. |
| 4 | **No candidate app before month 4.** | Build it only after we know empirically what a placement looks like. |
| 5 | **Data residency decided before schema design.** | Retrofitting data separation into a live system is expensive and error-prone. See §4. |
| 6 | **Transport cost is a first-class column**, on both the request and the placement — never a note field. | It participates in matching and in outcome reporting. |
| 7 | **Consent is an append-only record with a version**, never a boolean on the profile. | We must be able to prove what someone agreed to and when. |
| 8 | **Surrogate UUID keys throughout**, not natural composite keys. | Records get created offline and reconciled; natural keys are unstable here. |
| 9 | **No pay-to-apply model.** Pay must be displayed transparently before acceptance. | Legal requirement. |
| 10 | **Minimum age 16** (narrow apprenticeship exceptions for 13–15). Enforce at the database level. | Legal requirement. |

### Claims that must not appear in pitch or marketing copy

"Verified candidates", "skills assessment", and "outcomes dashboard" are **table
stakes, not differentiators**. DEVY (IPA/MIFOTRA/City of Kigali) is building
verified work-readiness with a randomised evaluation; the Cabinet-approved
National LMIS is making the outcomes dashboard a public utility. Position as a
**data supplier to the LMIS, not a competitor of it**.

---

## 3. Build order

Building these in the wrong order is the most common way ventures like this die.
The sequence front-loads the system that makes the operation possible and defers
the one that looks impressive.

**Weeks 1–6 — Internal admin web app** (coordinators and owner only)
: Employer records, candidate registry, assessment scoring, cohort management,
  placement contracts, attendance logging, follow-up scheduling, replacement
  tracking. An unglamorous operational CRM. This is the system that runs the
  business.
: Candidate intake in this phase is **WhatsApp and assisted in-person
  registration**. No candidate app — a coordinator types the data in.

**Weeks 7–12 — Employer-facing web dashboard**
: Post a shift, see who is assigned, confirm attendance, rate the worker,
  reorder. Responsive web is sufficient.

**Month 4+ — Candidate mobile app**
: Flutter, Android-first. Keep the PWA good enough that the app is an *upgrade
  rather than a prerequisite* — a meaningful share of users are on low-storage
  devices where installing anything is a real cost.

---

## 4. Stack and the residency blocker

| Layer | Choice | Reasoning |
|---|---|---|
| Database | PostgreSQL, self-managed, **Rwanda-hosted** | Residency requirement |
| Backend | Python (FastAPI) or Node | Whichever ships fastest; FastAPI to reuse Python experience |
| Admin + employer web | Server-rendered or React SPA | Low traffic, high data density — do not over-engineer |
| Candidate client | PWA first, Flutter later | One codebase, Android-first market |
| Messaging | WhatsApp Business API + SMS fallback | Intake, shift reminders, day-1 / week-1 / day-30 follow-ups |
| Payments | MTN MoMo / Airtel Money — **phase 2** | Record terms first, move money later |
| Matching | Rules engine, not ML | Explainable to employers |

### Data protection — Law No. 058/2021

Supervisory authority: **National Cyber Security Authority**, via its Data
Protection and Privacy Office. Enforcement live since the transition period
closed 15 October 2023. Penalties: RWF 2,000,000–5,000,000, or 1% of global
turnover.

Requirements that bear directly on architecture:

- Registration is mandatory for controllers and processors (free, online via the
  DPO, up to **30 working days** for a decision)
- **Data must be stored in Rwanda** unless a valid registration certificate
  authorises storage or transfer abroad; third-party transfers need NCSA
  authorisation
- Designate a Data Protection Officer; maintain records of processing activities
- **Breach notification within 48 hours**
- Written consent capture with purpose limitation, role-based access, deletion
  request handling, encryption at rest

> **Architecture blocker.** A default cloud Postgres project on US or EU
> infrastructure holding Rwandan national ID numbers, home locations and
> assessment scores is non-compliant from day one. Do not scaffold onto a
> managed US/EU database "just for now."

**Three workable options** (option 1 recommended for the pilot):

1. **Self-host in Rwanda** — Postgres on a Rwandan or regionally-hosted VPS.
   Cleanest legally, most operational overhead.
2. **Split store** — identifying data in a Rwanda-hosted database; a
   foreign-hosted service holds only opaque UUIDs and non-identifying
   operational data. Adds real complexity: the join key alone can re-identify if
   the foreign store also holds location and shift times.
3. **Cross-border authorisation** — viable, but a 30-working-day dependency that
   must start in week 1.

### Private employment agency authorisation

Labour Law No. 66/2018 defines private employment agencies broadly — matching
workers to employers, making job seekers available, training job seekers, and
providing employment information all fall inside the definition. A Ministerial
Order determines the modalities.

**Weeks 1–2 action:** obtain the Ministerial Order itself (not just the Labour
Law) and have a Rwandan employment lawyer confirm which activities trigger
authorisation. If authorisation is required and cannot be obtained, the entire
model changes — and that must be known before code is written.

---

## 5. Schema conventions

The phase-one DDL is organised in eight blocks. Follow this structure when
extending it.

| Block | Tables |
|---|---|
| 01. Identity (residency-sensitive) | `candidate_identity` |
| 02. Candidates (operational) | `candidates`, `availability` |
| 03. Skills & assessment | `skills`, `assessments`, `assessment_results` |
| 04. Employers | `employers`, `employer_contacts` |
| 05. Work requests | `work_requests`, `request_skills` |
| 06. Placements | `placements`, view `v_placement_net_pay` |
| 07. Attendance, pay, follow-up | `attendance`, `pay_records`, `follow_ups` |
| 08. Consent & audit | `consent_records`, `audit_log` |

### Deliberate design decisions — preserve these

- **`candidate_identity` is isolated from `candidates`.** Legal names, national
  ID, date of birth and phone numbers live in the identity table; everything
  operational lives in `candidates`, keyed by the same UUID. If the split-store
  residency option is later adopted, one table moves and the rest stays. It also
  allows granting most staff operational access without access to national ID
  numbers.
- **`est_transport_rwf` and `est_commute_min` are columns on `placements`**, and
  `transport_covered` is a column on `work_requests`. The
  `v_placement_net_pay` view derives `net_daily_rwf` and `transport_pct` from
  them — net earnings after transport is a headline metric, so it must be
  queryable, not reconstructed.
- **`placements.replaces_placement`** is a self-reference. Replacement chains are
  the evidence that the reliability guarantee was honoured; never overwrite a
  placement row to record a replacement.
- **`consent_records`** carries `policy_version`, `purpose`, `captured_via` and
  `captured_at`. Append only.

### Audit trail — how reads are captured

PostgreSQL has no `SELECT` trigger, so the read trail the blueprint requires
cannot be a trigger. Instead, migration 008 **revokes direct `SELECT` on
`candidate_identity`** and routes reads through `read_candidate_identity(uuid)`,
a `SECURITY DEFINER` function that writes the `audit_log` row before returning
the row. Writes are audited by `trg_audit_identity_write`.

Do not re-grant direct `SELECT` on `candidate_identity` to an application role.
It silently removes the evidence trail without breaking a single test.

Attribution comes from the transaction-local `app.staff_id` setting, so identity
work must go through `session_scope(staff_id=...)`. An audit row with a null
`staff_id` proves nothing.

### no_show vs replaced -- do not conflate these

`no_show` means the worker did not arrive. It stays on the placement
permanently. Coverage is recorded as a *separate* placement row pointing back
via `replaces_placement`.

`replaced` is for a placement that ended early and was substituted for some
other reason.

Flipping a covered no-show to `replaced` would drop it out of
`v_guarantee_invocations` and quietly inflate the reliability numbers -- the
one metric the whole thesis rests on. The chain is the evidence; the status is
the truth.

### Matching invariants

- **Double-booking is prevented by checking overlapping placements, never by
  candidate status.** Status is a summary and it drifted once already, with the
  result that an actively-placed worker could be sent to two employers for the
  same hours. `refresh_candidate_status` keeps the summary honest for display
  and reporting; it is not the control.
- **A failed assessment is not a low score.** Below the assessment's
  `pass_score`, a candidate has no evidence of that skill and cannot clear a
  lower employer bar. Keep the failed attempt visible so the exclusion can be
  explained -- "never assessed" and "assessed and failed" are different things
  to a coordinator.
- **Never hardcode a score denominator.** It was `/5` once, which would read
  "8/5" for an assessment scored out of ten. Carry `max_score` through.
- **The overlap check exists twice and both are needed.** The Python check
  gives a usable message; the trigger in migration 027 closes the race between
  checking and inserting. Two coordinators offering the same person
  concurrently both passed the application check before the trigger existed --
  verified with live connections, not reasoned about.
- **Do not exclude `placed` candidates from the pool.** Shift work is the
  point. Excluding them would also hide them from the rejection list, so a
  coordinator would not see why nobody matched.

- **An offer re-runs the filters.** Never place someone straight from a
  previously rendered match list -- consent, availability and transport can all
  have changed since it was drawn.
- **A missing transport estimate is None, not zero.** Zero silently disables
  filter 2, which is the filter that prevents most 30-day dropouts.
- **`match_reason` is written once, at offer time, and never recomputed.** An
  employer may ask months later why a person was sent; "the algorithm would
  pick them again today" is not an answer.
- **Transport fare rates in `transport.py` are placeholders.** Calibrate from
  real receipts before the pilot reports any net-earnings figure.

### Consent ordering -- fixed bug, do not reintroduce

`v_current_consent` must order by `captured_at DESC, recorded_seq DESC`.
Ordering by `captured_at` alone is wrong: it defaults to `now()`, which in
PostgreSQL is *transaction* start time, so rows written in one transaction tie
and `DISTINCT ON` resolves the tie arbitrarily -- a withdrawal could be ignored
and the candidate stay matchable. See migration 011 and tests/test_consent.py.

### Erasure is redaction, never DELETE

`candidate_identity` cascades into `candidates` and from there into placements,
attendance, pay records and follow-ups. A literal DELETE destroys an employer's
confirmed attendance, the pay records proving someone was paid, and another
candidate's replacement chain. Erasure overwrites the identity row in place and
keeps the surrogate key.

Never erase `consent_records` or `audit_log`: they are the evidence that the
processing was lawful and that access was controlled. Destroying them to honour
a data-protection request is the opposite of compliance.

`erase_candidate_identity()` refuses to run when `app.staff_id` is unset. It is
the one operation whose actor cannot be reconstructed later.

### Database privileges -- tests run as superuser, production does not

The suite connects as superuser, where GRANT and REVOKE do not apply. That
hid a production-breaking bug once: the matcher joined `candidate_identity`,
which `app_operations` cannot read, so matching failed on any deployment
using the role model.

`tests/test_privileges.py` runs the real queries under `SET ROLE
app_operations`. **Add a case there whenever operational code touches a new
table**, or the same class of bug comes back.

Operational code must never need `SELECT` on `candidate_identity`. When it
needs a fact derived from identity data, expose the derived fact through a
`SECURITY DEFINER` function -- see `candidates_age_eligible()` in migration
018, which returns a boolean rather than a date of birth.

The application runs as `akazi_app`, which owns nothing. Do not "fix" a
permission error by connecting as the owner: an app connected as owner can
disable the audit-log rules, which is the tampering the hash chain exists to
detect.

### Reporting out -- suppression is not optional

Anything leaving for the LMIS is grouped and disclosure-controlled. Never
include `candidate_id`: it is stable across exports, so two reports would let
someone track an individual between them. Never lower `MIN_CELL` without
deciding, deliberately, that a group that size is unidentifiable in a Rwandan
district -- "aggregate" is not a synonym for "safe".

Reporting consent is a separate purpose from placement consent. Do not treat
one as implying the other.

### Tests that must COMMIT need their own database

The `session` fixture rolls everything back, which is what keeps tests
isolated. Anything that shells out (backup, migrate.sh) connects separately
and cannot see that transaction, so it needs `scratch_database`. Do not commit
into the shared test database to make such a test work: rows leak into every
later test, and `audit_log` is append-only by rule, so the leak cannot even be
cleaned up. That exact mistake broke the audit-chain test.

### Pay records are claims, not facts

`paid_on` is the employer's claim; `worker_confirmed` is the worker's answer.
`v_pay_accuracy` requires both plus on-time -- never relax it to count the
employer's word alone, because the gap between claim and confirmation is the
metric's entire reason to exist. Open a pay period when terms are known, not
when money lands: a record created only at payment can never show the payment
that failed to happen.

### Dates belong to Kigali, never to the server

Never use `date.today()` or `CURRENT_DATE` for anything a coordinator or
worker would call today. Use `kigali_today()` -- `app/clock.py`, and the SQL
function of the same name. Servers run on UTC and Kigali is UTC+2, so those
two hours before midnight UTC are 00:00-02:00 local: a late shift ending,
attendance being logged, and every date field a day behind.

`tests/test_clock.py` fails the build if either reappears in a user-facing
path. Keep it that way -- the bug is correct for twenty-two hours a day,
which is exactly why nobody would find it.

`KIGALI` is defined once in `app/clock.py`. Two modules disagreeing about
the offset would be worse than one being wrong consistently.

### Read-then-write is a race until it is serialised

Three checks in this system had the shape "count or look, then insert", and
all three raced under two concurrent connections -- each demonstrated, not
reasoned about:

- placements    one worker on two overlapping shifts (migration 027)
- cohorts       two people admitted to a room with one chair (028)
- pay_records   RWF 30,000 recorded for a 15,000 week (028)
- lockout       five simultaneous wrong passwords left the account unlocked;
                fixed by incrementing in the UPDATE rather than in Python

The fix in each case is a transaction-scoped `pg_advisory_xact_lock` on the
row everything hangs off -- the candidate, the cohort, the placement -- taken
*before* the check.

The lesson that cost the most: **the cohort capacity check was already a
trigger and raced anyway.** Moving a check into the database does not make it
concurrency-safe. Serialising the writers that could conflict does.

A counter is the same bug wearing different clothes: `count = read + 1` is
read-then-write. Increment in the UPDATE (`SET n = n + 1`), which takes a row
lock and serialises for free.

When adding any new "is there already one of these?" rule, or any counter,
assume it races until a test with real threads says otherwise.
`tests/test_concurrency.py` has the harness.

Checked and genuinely safe, so nobody re-does the work: contracts
(UNIQUE placement_id), replacement chains (partial unique index),
once-only messages (ON CONFLICT plus partial unique index), and contract
acknowledgement (UPDATE ... WHERE ... IS NULL).

### Attendance is only meaningful on live work

`log_attendance` refuses anything but an accepted, active or no-show
placement. Without that guard a stray log against a cancelled placement
flipped it to `no_show`, inventing a guarantee invocation for a shift that
was not happening -- inflating our own failure rate and sending a cover to
an employer who had cancelled.

Logging attendance as present against a `no_show` restores it to active. A
no-show recorded in error would otherwise stand permanently, against us in
the metrics and against the worker on their record.

### Cancellation belongs to whoever decided it

An employer withdrawing a shift produces `cancelled` placements, never
`declined`. Never reuse `declined` for this: it writes the employer's
decision onto the worker, and prior behaviour feeds the ranking.

### Escalations -- the safeguard, not a reporting line

- **An escalation has a named owner and a deadline, both recorded at the time.**
  Raising one with nobody to own it must fail loudly, not succeed quietly.
- **Harassment is owned by the owner role**, not a coordinator: the recipient
  may need to end a commercial relationship with an employer a coordinator
  manages daily.
- **A flag raised on a call escalates exactly like a texted one.** The
  safeguard must not depend on how someone happened to tell us.
- **Resolution text is required.** "Resolved" with no account of what was done
  is indistinguishable from ignoring it, and this is the record someone may
  have to defend.
- **Do not shorten RESPONSE_TIMES casually.** A target quietly missed is worse
  than a longer one that is met.

### Reading replies

- **Never guess.** An uninterpretable reply queues for a human. Acting on a
  misread reply cancels someone's work or ignores a report of harassment.
- **"No problem" is not a refusal**, and an issue beats a yes/no.
- **Issue matching is pattern-based and deliberately broad.** A false positive
  costs two minutes; a false negative costs someone a response to being
  assaulted. A live run once caught "keeps touching me" slipping past a literal
  phrase match -- add variants, never narrow them.

### Contract terms are a snapshot and stay one

`placement_contracts.terms` is written once at acceptance and never
recomputed from the live rows. A request edited afterwards must not change
what the contract says: the question in a dispute is what the worker was told
when they said yes.

Immutability is a trigger, not a rule. A conditional `ON UPDATE DO INSTEAD`
rule disables `UPDATE ... RETURNING` on the whole table, which acknowledgement
needs. The trigger raises rather than silently discarding -- unlike
`consent_records`, where silence is right; here an attempt to alter an
agreement should be loud.

### The women-only cohort rule

Enforced by trigger, not only in `add_member`. It is a promise made to the
people who agreed to attend, and a rule that lives in one code path is a rule
that a second code path will not have. An unrecorded gender is refused: "we
did not ask" does not satisfy a specific promise.

The rejection runs inside a savepoint, so a refusal does not abort the caller's
transaction -- a coordinator enrolling a list would otherwise lose everyone
after the first person turned away.

### Enum values that are deliberately never written

Three remain, and each is a decision rather than an oversight. Do not "wire
them up" without revisiting the reasoning:

- `placement_status.replaced` -- a covered no-show stays `no_show`; the
  replacement chain is the evidence. See the no_show/replaced note above.
- `message_status.sending` -- dispatch claims a row with `FOR UPDATE SKIP
  LOCKED` and resolves it in the same transaction, so there is no window for
  a transient state. PostgreSQL cannot drop an enum value, so it stays.
(`candidate_status.trained` was on this list until cohorts were built; it is
now set by finishing one.)

Re-run the audit after schema changes: list every enum value and check
whether app code ever writes it. It has found real bugs twice -- the
double-booking, and assessment scores exceeding their own maximum.

### Messaging invariants

- **Queue inside the causing transaction; send separately.** Never send inline:
  a rollback would leave a message already delivered, and a retry would send it
  twice.
- **No phone numbers in the messages table.** Resolve at dispatch through
  `message_recipient_phone()`, which audits the read. Copying numbers into the
  outbox creates a second store of personal data outside the identity boundary.
- **Suppressed is not failed.** No consent, no phone, erased record -- these are
  facts about the recipient, not faults to retry. Counting them as failures
  buries the real errors.
- **Cancel queued messages when a placement ends.** A reminder for a job someone
  declined sends them to the wrong place.
- **Templates state pay net of transport.** Transparent pay before acceptance is
  a legal requirement, and gross pay is not what someone takes home.

### Redirect targets

Never redirect to a value taken from `Referer`, a query parameter, or a form
field without passing it through `safe_path()`. Both were live open redirects
at one point: a coordinator bounced to another origin straight after signing
in is the setup for a convincing fake login page.

### Employer isolation -- the sharpest edge in the system

- **Employers and staff are separate principals in separate tables.** Never
  merge the session tables or add an "employer" staff role: two tables are what
  makes it impossible for one token to resolve as the other by mistake.
- **Every employer query filters on employer_id from the session.** Never from
  a form, never from a URL. `post_request` takes it as an argument for exactly
  this reason.
- **Ownership is rechecked server-side on every action.** `_own_placement` and
  `_own_request` exist so no handler has to remember.
- **Refusals must not distinguish "not yours" from "does not exist".** Otherwise
  the portal is an oracle for what other employers have.
- **Never join candidate_identity in anything an employer can reach.** They get
  display_name. An employer buying a covered shift does not need a national ID
  to receive someone.

### Web UI invariants

- **Role checks belong in the handler, not the template.** Hiding a nav link
  is an affordance; `_require_admin` is the control. Tests post the hidden
  forms directly.
- **Generated passwords are shown once and never stored readable.** They ride
  through a single redirect and are gone on reload -- do not "improve" this by
  persisting them.
- **`must_change_password` is a gate, not a label.** It blocks every path
  except the change form and signing out, for staff and employers alike. It
  spent a long time being displayed and not enforced, which is worse than not
  having it: the console claimed a control that did not exist.
- **Every state-changing form carries a CSRF token**, checked against the
  session. The bearer API does not need this; the cookie UI does, because a
  browser sends a cookie on any request it is tricked into making.
- **Server-side checks are not optional because the template hid the form.**
  The candidate registration handler re-checks identity access and MFA -- a
  hidden form is a UI affordance, never a control.
- **A missing transport estimate must not render like a zero fare.** They are
  opposite facts: a candidate with no home location passes the transport
  filter by default, and the coordinator has to see that before offering.

### The reorder rate decides the pivot, so its denominator is not a detail

`v_employer_reorder` answers one question: having been served, did they come
back. Three choices in it are deliberate, and each one moves the number the
blueprint's go/no-go rests on.

- **Served employers only.** An employer nobody was placed with has not
  declined to reorder -- they have not been served yet. Holding them against
  the rate makes it measure something other than repeat business.
- **A reorder is a request opened *after* the first placement**, not merely a
  second request. An employer posting a cleaner and a guard on the same
  morning has ordered twice and come back zero times. Counting that inflates
  the metric precisely when the evidence for pivoting is strongest.
- **Any route counts, not just the button.** `reorders_request` records
  provenance and answers a product question about the one-click path.
  Measuring only button presses would understate real repeat business.

`opened_at` and `offered_at` default to `clock_timestamp()` for this view's
sake: `now()` is the transaction timestamp, so rows written together tie, and
a tie reads as "did not reorder". Same defect migration 011 fixed for consent.

### A test connected as the owner proves nothing about privileges

The database owner bypasses every grant. So a suite that connects as the owner
-- which is every fixture except `restricted_session` -- cannot see a
privilege bug at all, and four of them shipped green:

| What failed under the real role | Why the grant was missing |
|---|---|
| Creating any staff account | `staff` had no `INSERT` grant at all |
| Registering a candidate | `INSERT ... RETURNING candidate_id` needs `SELECT` on that column, and `SELECT` had been revoked wholesale |
| Every inbound reply from a worker | the sender lookup read `candidate_identity` directly |
| Accepting an erasure request | the already-erased check reads `erased_at` |

The first three broke the system's core paths outright: on a deployment using
the role model nobody could be onboarded, nobody could be registered, and every
message a worker sent was dropped.

Asserting grants from the migration source does not catch this -- it proves
what was granted, not that what the application does is permitted. The only
thing that catches it is running the real writes as `akazi_app`, which is what
`tests/test_restricted_role.py` does. **Anything touching identity data, or any
new table, gets a test there.** When a grant looks unnecessary, check whether a
`RETURNING` clause or a subselect needs it before removing it.

### Reading identity data records why, not only who

`read_candidate_identity()` takes a purpose (`operations`, `placement`,
`support`, `data_request`, `erasure`, `reporting`) and rejects anything else.
"Who looked at a national ID number and when" was never the whole question --
the first thing an auditor asks is what for, and a log that cannot separate a
coordinator staffing a shift from a bulk export is not much of a safeguard.

Resolving an inbound phone number to a person goes through
`resolve_inbound_sender()` for the same reason: matching a number to a human
being *is* a read of their identity record, and it audits on a hit. A number
matching nobody writes nothing -- there is no record to attach a read to, and
inventing one is noise an auditor has to wade through.

Only two columns of `candidate_identity` are directly readable, `candidate_id`
and `erased_at`, both record metadata rather than facts about a person. Every
identifying column stays behind the audited function. A test asserts that list
exactly, so if it grows it grew deliberately.

### The catalogue: assessments have to be definable, not just recordable

`skills` and `assessments` shipped empty with no code able to insert into
either. Everything downstream failed quietly as a result: `require_skill()`
raised `unknown skill` for every code there was, so no work request could
carry a requirement; `record_assessment_result()` needed an `assessment_id`
that could not exist; and with no results, **matching filter 1 and rank
criterion 3 never engaged**. Half of a weeks 1-6 deliverable -- the half that
says what is being scored.

Three rules hold in the database, because a bulk import of paper assessment
sheets is exactly the path that skips the application:

- **`max_score` and `pass_score` freeze once a result exists.** A candidate
  assessed 3 of 5 against a pass mark of 3 passed; raise the mark to 4 and
  they have retroactively always failed, including for placements already
  made on that score. Migration 026 froze contract terms for the same reason.
  Bounds stay editable until the first result, so a setup typo is cheap.
- **`skill_code` is immutable**; the display name is not. The code is what
  `require_skill()` resolves and what appears in notes and import sheets.
- **Rubric wording stays editable forever.** Sharpening how a criterion is
  described does not change who passed.

Authoring is admin/owner: a pass mark decides who is eligible for work, which
is policy. Recording a result stays with coordinators, and the response gives
the scale back -- `4 out of 5, passed` -- because "4" alone is what a
coordinator would otherwise read aloud to an employer asking why this person.

The rubric is surfaced to whoever scores. It was stored and displayed nowhere,
which is how two coordinators score the same performance differently and
matching then ranks on noise.


The browser surface came later than the API and was missing entirely at first:
no catalogue page, and **no candidate detail page at all**. The build order
puts coordinators in the admin web app, so scoring -- a weeks 1-6 deliverable
-- was reachable only by someone willing to call the API by hand.

`/ui/catalogue` and `/ui/candidates/{id}` close that. The candidate page shows
availability, consent and placement history alongside assessments, and prints
each rubric beside the scoring form. It deliberately shows **no identity
data**: legal names, national ID and phone numbers stay behind the audited
read, so most staff can open the operational record without touching anything
residency-sensitive. A test asserts that.

### A raising trigger takes the whole transaction with it

Recording 9 out of 5 is refused by trigger (migration 021), and catching that
exception is not enough -- the transaction is already aborted, so every later
statement in the same request fails with `InFailedSqlTransaction`. Wrap any
write that a trigger may reject in `session.begin_nested()`, as the cohort
capacity path already does.

This hides well: each HTTP request gets its own session, so a live server
looks fine while the shared-session test client fails. The test client is
right -- one poisoned statement should not be able to take out the rest of a
request.

Report what the database said, not a generic failure. "score 9 exceeds the
maximum of 5" is a correctable mistake; "error" is a support call.

### readonly means readonly, and it is enforced by HTTP method

`readonly` sat in the `staff_role` enum, was assignable through `POST /staff`,
was validated by the request schema and echoed back on login -- and was
checked nowhere. An account holding it could register candidates, promote
employers and post work requests. Both the person given the role and the
person granting it would have believed it meant "can only look".

The gate lives in `current_staff` and `current_web_staff`, keyed on the
request method rather than on a list of write endpoints. That is deliberate:
"changes nothing" maps exactly onto method semantics, so a route added later
is covered without anyone remembering to protect it. A GET that writes would
slip through, but a GET that writes is already a bug.

`READONLY_ALLOWED_WRITES` is the narrow exception -- login, logout, password,
MFA, TOTP enrolment. Managing your own session is not an operational write,
and without those a readonly account could not finish logging in. Keep that
list tiny, for the same reason `PASSWORD_CHANGE_EXEMPT` is tiny.

The employer portal is out of scope here: employers are a separate principal
with no staff role, and their isolation rules apply instead.

### A coverage test that examines nothing is worse than no test

`test_every_staff_write_route_is_behind_the_readonly_gate` passed on its first
run while checking **zero** routes. `app.routes` holds router wrappers, not
endpoints; iterating it finds six objects and no routes, so the assertion was
`[] == []`. It looked exactly like coverage and proved nothing.

Walk into `_IncludedRouter.original_router` to reach real routes -- there are
132, 80 of them writes. `test_the_route_walker_actually_finds_routes` asserts
the walker returns a plausible number, so the next person to break the walk
gets a failure instead of a green tick. **Any test asserting "nothing is
wrong" across a collection needs a companion asserting the collection is not
empty.**

### Auth invariants -- do not relax these

- **Identity data requires MFA on the current session.** A password alone
  reaches operational data; it must never reach a national ID number.
  Elevation is per session -- never per account.
- **The TOTP replay guard stays.** A code is valid for its whole 30-second
  step; without `last_totp_counter` an observed code works again until the
  step rolls over. Do not widen `TOTP_DRIFT_STEPS` beyond 1 to be forgiving --
  it multiplies the number of codes valid at any moment.
- **The audit chain is append-only and verified, never rebuilt.** If
  `verify_audit_chain()` reports a break, that is a finding to investigate --
  never a thing to "fix" by recomputing hashes. Recomputing the chain is
  precisely what an attacker would do.
- **Access changes revoke sessions.** Withdrawing identity access, resetting a
  password or deactivating an account must cut live sessions immediately.
- **Tokens stay stateful.** Swapping the `staff_sessions` table for JWTs would
  remove revocation, and revocation is the point: deactivating a coordinator
  must cut their access on the next request, not at token expiry.
- **Only the token hash is stored.** Never persist the plaintext.
- **Every login failure returns the same 401 body.** Distinguishing "no such
  account" from "wrong password" from "locked" hands over a staff-enumeration
  oracle.
- **`can_view_identity` is a per-account grant, never a role check.** Seniority
  does not imply entitlement to national ID numbers.
- **Never grant `app_operations` direct `SELECT` on `candidate_identity`.** It
  would bypass `read_candidate_identity()` and silently end the read trail. A
  test asserts the privilege is absent.

### Metric views (migration 009)

Every pilot target in §7 is a view, derived from operational rows rather than
stored. `v_pilot_scorecard` is the one-row summary. When adding a metric,
derive it -- a number that can be set independently of the events it describes
will eventually disagree with them.

### Enum types

`candidate_status`, `employer_tier`, `work_type`, `request_status`,
`placement_status`, `checkpoint`. Extend these rather than introducing free-text
status columns.

---

## 6. Matching rules (v1)

Apply as **sequential filters, not weights**. Explainability matters more than
optimality at this volume.

1. **Hard exclusions** — age below 16; no valid consent record; availability does
   not cover the shift window; required skill score below `min_score`.
2. **Transport viability** — estimated daily transport cost must not exceed
   `max_commute_rwf`, *and* must be under **30% of daily pay** unless the
   employer covers transport. This filter alone prevents most 30-day dropouts.
3. **Safety filter** — for female candidates where a shift ends after dark,
   require either employer-covered transport or an explicit opt-in on record.
4. **Rank remaining by**, in order: prior completed placements with this
   employer → 30-day retention history → assessment score → commute time
   ascending.
5. **Always surface the reason.** The coordinator must see something like
   *"matched on: availability, retail greeting 4/5, 12-min commute"* and be able
   to defend the choice to the employer.

### Women's participation is a product requirement, not a reporting line

Female unemployment is 15.5% against 11.6% male. Build for the gap: shift-time
filters that exclude late finishes where transport is unsafe, employer safety
ratings written by women who worked there, all-female cohort options, and an
in-app harassment report with a **named escalation path and a defined response
time**. `follow_ups.issue_flag` includes `harassment` for this reason.

---

## 7. Pilot metrics

Anything built should make these measurable.

| Metric | Target |
|---|---|
| Employer interviews | 20, including 5 cooperatives |
| Active employers | 10 |
| Paid placements (90 days) | 30–50 |
| Time to fill | Under 7 days |
| 30-day retention | ≥ 60% |
| **Net earnings after transport** | Transport ≤ 25% of daily pay |
| **Guarantee invocations** | Track rate; fill ≥ 90% within 24h |
| **Women placed** | ≥ 45% of placements |
| Employer reorder rate | ≥ 40% |
| **Pay accuracy** | ≥ 95% paid in full, on the agreed date |

Bold rows are new in v2 and are the ones that prove the thesis.

---

## 8. Domain glossary

- **NEET** — not in employment, education or training. The headline addressable
  population, roughly six times larger than the unemployment count implies.
- **Reliability guarantee** — if a placed worker does not arrive, we fill the
  slot free of charge, same day. Priced into the placement fee.
- **Net earnings after transport** — daily pay minus estimated daily transport
  cost. A role paying RWF 3,000/day that costs RWF 1,600 in moto fare is a
  placement that dies in week two.
- **Cooperative channel** — youth-led cooperatives (cleaning, environmental
  maintenance) as pre-aggregated demand. One cooperative relationship can yield
  the placement volume of fifteen individual SME relationships.
- **NISR** — National Institute of Statistics of Rwanda; source of the Labour
  Force Survey.
- **MIFOTRA** — Ministry of Public Service and Labour.
- **NCSA** — National Cyber Security Authority; hosts the Data Protection and
  Privacy Office.
- **LMIS** — the Cabinet-approved national Labour Market Information System.

### Competitors — know these names

Kazispace (near-total concept overlap, already live), Kora (RDB matching
portal), Kigali JobNet (city-led, potential channel), DEVY (IPA/MIFOTRA/City of
Kigali — building the Skills Passport, results 2027), National LMIS, Umurava
(remote/global digital work). Job boards (jobinrwanda, rwandajob, kigalijob,
jobsinrwanda.ai, New Times) serve mid-career formal hiring and are low overlap.

---

## 9. Open questions — resolve before or alongside first code

1. Which activities trigger private employment agency authorisation? (Ministerial
   Order + Rwandan employment lawyer, weeks 1–2)
2. Which of the three residency options is chosen? Blocks schema design.
3. NCSA controller/processor registration submitted? (30 working days)
4. Who is the designated Data Protection Officer?
5. `staff` table and role model defined?

## 10. Go / no-go

**Proceed if**, after 20–30 employer interviews: at least five commit real
vacancies; at least three will pay or sponsor a pilot; at least three say the
reliability guarantee is the reason; the legal position on agency authorisation
is clear; and candidates can reach the work for under a quarter of the wage.

**Pivot if**: employers want free candidates and nothing else; the guarantee
generates no pricing power; roles are too irregular to sustain retention;
authorisation cannot be obtained; or a competitor has locked up the employer
relationships in the chosen district.

---

*Derived from Revised Blueprint v2, 23 August 2026. Research basis: NISR Labour
Force Survey Q4 2025 / Q1 2026 / Q2 2026; Law No. 66/2018; Law No. 058/2021 and
NCSA DPO guidance; MIFOTRA and Cabinet LMIS statements; IPA Rwanda DEVY
evaluation; City of Kigali employment reporting, May 2026.*

*Verify all statutory requirements with Rwandan counsel before operating.*
