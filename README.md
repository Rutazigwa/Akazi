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
.venv/bin/python -m pytest              # 59 tests
.venv/bin/uvicorn app.main:app --reload
```

The app will refuse to start until `DATA_RESIDENCY` is set. That is deliberate —
see [Data residency](#data-residency).

## Layout

```
migrations/     Numbered SQL, applied in order. Blocks match the blueprint.
app/config.py   Settings + the residency guard
app/db.py       Engine, session scope, audit attribution
app/matching/   The v1 matching engine (sequential filters)
app/operations/ Attendance, the guarantee, follow-up scheduling
app/routers/    Coordinator HTTP endpoints
tests/          Filters, residency guard, and DB-backed operations tests
scripts/        migrate.sh, testdb.sh
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
transport, skills or supply.

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
- **No authentication.** `X-Staff-Id` stamps the audit trail; it does not prove
  anything. Do not expose this beyond a trusted network until real auth exists.
- **Apprenticeship exceptions for ages 13–15.** The age constraint is a hard 16;
  exceptions need an authorised-exception record and a deliberate schema change.

## Before this runs on real data

See `CLAUDE.md` §9. In short: the Ministerial Order on private employment
agencies has to be read by a Rwandan employment lawyer, NCSA controller
registration takes up to 30 working days, and a Data Protection Officer has to be
named. None of those are code, and all of them block go-live.
