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
.venv/bin/python -m pytest              # 29 tests
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
tests/          Filter behaviour and the residency guard
scripts/        migrate.sh
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
