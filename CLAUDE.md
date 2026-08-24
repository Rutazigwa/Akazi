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
app/deps.py     db_session, current_staff, require_identity_access
app/routers/    Coordinator HTTP endpoints -- all require a bearer session
app/main.py     FastAPI admin app (coordinators and owner only)
tests/          Filter behaviour and the residency guard
scripts/        migrate.sh
```

Run `env DATA_RESIDENCY=local_dev .venv/bin/python -m pytest` before pushing.

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

### Auth invariants -- do not relax these

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
