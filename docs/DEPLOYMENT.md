# Deployment

This system holds Rwandan national ID numbers, home locations, and assessment
scores. Everything below follows from that.

## Before you deploy anything

Three things are not code and all of them block go-live:

1. **Private employment agency authorisation.** Obtain the Ministerial Order
   under Labour Law No. 66/2018 and have a Rwandan employment lawyer confirm
   which of your activities trigger it.
2. **NCSA controller/processor registration.** Free, online via the Data
   Protection and Privacy Office, up to **30 working days** for a decision.
   Start it early — it is a dependency, not a formality.
3. **A named Data Protection Officer.** Required, and they need to actually be
   reachable within the 48-hour breach notification window.

## Where this runs

**A Rwandan or regionally-hosted VPS.** Not a managed database in a foreign
region. Law No. 058/2021 requires personal data to be stored in Rwanda unless a
registration certificate authorises otherwise, and the realistic way to breach
that is not a decision — it is a convenient default nobody revisited.

The app refuses to start unless `DATA_RESIDENCY` is set. Setting it to something
untrue is a thing a person has to do deliberately.

Sizing for the pilot: 2 vCPU, 4 GB RAM, 40 GB disk is ample. This is a
low-traffic, high-data-density admin system for a handful of coordinators.

## Encryption at rest

Required by Law 058/2021, and **not** provided by the compose file. Postgres
does not encrypt its own data files. Encrypt the disk before installing
anything:

```bash
# On a fresh volume, before Docker is installed
cryptsetup luksFormat /dev/sdb
cryptsetup open /dev/sdb akazi-data
mkfs.ext4 /dev/mapper/akazi-data
mount /dev/mapper/akazi-data /var/lib/docker
```

The passphrase must be entered at boot, or supplied by a key file on separate
removable media. A key stored on the same disk it unlocks protects against
nothing except a stolen disk that was never powered on.

## First deploy

```bash
git clone https://github.com/Rutazigwa/Akazi.git && cd Akazi
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env          # SITE_ADDRESS, POSTGRES_PASSWORD, DATA_RESIDENCY

docker compose -f deploy/docker-compose.prod.yml up -d db

# Migrations run as the owner.
docker compose -f deploy/docker-compose.prod.yml run --rm app \
    ./scripts/migrate.sh "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/$POSTGRES_DB"

# The application then runs as a role that owns nothing. Do NOT skip this.
docker compose -f deploy/docker-compose.prod.yml exec -T db \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "SELECT set_config('akazi.app_password', '$APP_DB_PASSWORD', false);" \
    -f /scripts/create_app_role.sql

docker compose -f deploy/docker-compose.prod.yml up -d

# The first account. There is no self-registration.
docker compose -f deploy/docker-compose.prod.yml exec app \
    python scripts/create_staff.py --name "Owner" --phone "+250..." \
    --role owner --identity-access
```

### The Caddy rate-limit plugin

`deploy/Caddyfile` uses `rate_limit`, which is not in the stock Caddy image.
Either build an image with it:

```dockerfile
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/mholt/caddy-ratelimit
FROM caddy:2-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

…or delete the `@login` / `rate_limit` block. **Caddy fails to start rather than
silently serving without it** — which is the correct failure mode, but it will
look like a broken deploy if you have not read this.

Removing it is a real reduction in protection. Per-account lockout (five
failures, 15 minutes) still applies, but nothing then limits an attacker
spraying one attempt each across many accounts.

## Run the app as a role that owns nothing

`APP_DB_URL` should point at `akazi_app`, not at `POSTGRES_USER`. The owner role
is for migrations only.

This is not tidiness. Connecting as the owner means the application can
`ALTER TABLE audit_log DISABLE RULE`, drop the hash-chain trigger, or drop the
audit log altogether — which is exactly the tampering the chain exists to make
detectable. `akazi_app` owns nothing, so:

```
direct SELECT on candidate_identity: false   # reads go through the audited function
EXECUTE read_candidate_identity:      true
is superuser:                         false
ALTER TABLE audit_log DISABLE RULE:   ERROR: must be owner of table audit_log
```

`scripts/create_app_role.sql` refuses to finish if the role ends up with more
than it should — a superuser, `pg_read_all_data`, or direct read access to
identity data.

**Known limitation.** The application uses a single database connection, so
`akazi_app` holds both `app_operations` and `app_identity`. The two roles are
therefore not separated at runtime; the separation that *is* enforced is
against the table (no direct `SELECT` on `candidate_identity` from either) and
against schema ownership. Splitting identity work onto a second connection
holding only `app_identity`, with the first holding only `app_operations`, is
the next hardening step and would make an operational-code bug physically
unable to reach a national ID number.

## Server timezone

The application does not depend on it -- every user-facing date goes through
`kigali_today()`, which asks for `Africa/Kigali` explicitly. Leaving the server
on UTC is fine and is what the compose file assumes.

Do not "fix" it by setting the database timezone instead. That would work, and
it would work silently: the next deploy that missed the setting would put every
date a day behind for two hours each night, with nothing to notice it.

## Network posture

Postgres is on an `internal: true` Docker network and is **not** published to
the host. It is reachable only by the app container. Do not add a `ports:`
mapping to the `db` service to make `psql` convenient — use
`docker compose exec db psql` instead.

Only 80 and 443 should be open at the firewall. SSH on a key, no passwords.

## Backups

```bash
# Nightly, e.g. via cron on the host
BACKUP_PASSPHRASE=... docker compose -f deploy/docker-compose.prod.yml \
    exec -T db /backups/../scripts/backup.sh
```

`scripts/backup.sh` refuses to write an unencrypted dump, and verifies that what
it wrote decrypts and unpacks before reporting success. An unverified backup is
a guess, and the day you discover it was wrong is the worst possible day.

**Store the passphrase somewhere other than this server.** Keep at least one
copy of the backups off the machine — a backup that burns with the building is
not a backup.

Restores are tested automatically: `tests/test_backup.py` takes a real backup
and restores it into a fresh database on every CI run, verifying that row counts
match and that the audit hash chain still validates afterwards.

That covers the mechanism. Still rehearse a restore **onto the real
infrastructure** quarterly — what CI cannot test is whether you can find the
passphrase under pressure.

## If there is a breach

**48 hours** to notify the NCSA. That clock is short enough that the decisions
have to be made in advance:

- `audit_log` records every read of `candidate_identity`, attributed to a named
  staff member. `GET /candidates/{id}/access-log` answers "who saw this record".
- Revoke a compromised account immediately — deactivating a staff member cuts
  their live sessions on the next request:
  ```sql
  UPDATE staff SET is_active = false, deactivated_at = now() WHERE staff_id = '...';
  ```
- The DPO makes the notification. Have their contact details somewhere other
  than this system.

## What is still missing

Known gaps, so nobody discovers them at the wrong moment:

- **No automated log shipping.** `audit_log` is hash-chained, so tampering is
  *detectable* — but an attacker with enough access can recompute the chain.
  Publish the head hash off-box to close that (see below).
- **Rate limiting is per-IP at the proxy.** A distributed attempt across many
  addresses is not covered.
- **TOTP secrets sit in the database.** Protected by disk encryption at rest and
  never returned after enrolment, but not separately encrypted. A database
  compromise yields the second factors along with everything else.
- **No account recovery without an admin.** If the only owner loses both their
  password and their phone, recovery means direct database access.

## Message dispatch

```bash
*/5 * * * *  cd /app && python scripts/dispatch_messages.py
```

Safe to run concurrently and safe to re-run. Until a live provider is
configured it records instead of sending — run the pilot's first week that way
and read `scripts/dispatch_messages.py --summary` plus the message bodies before
anything reaches a real person.

Nothing is sent between 21:00 and 07:00 Kigali; messages due then are deferred to
07:00 rather than dropped.

### Alert when it stops

Every run writes a row to `job_runs`, so a cron that dies is distinguishable
from an evening with nothing to send — without that, the two look identical
and the queue simply stops draining. Nothing reaches a worker while that is
true: no shift reminders, no placement offers. An unreminded worker is a
no-show, a no-show invokes the guarantee, and the guarantee is priced into
the fee.

`GET /health` reports it:

```json
{"status": "ok",
 "messaging": {"state": "stalled",
               "reason": "the dispatcher last ran 40 minutes ago; it is meant to run every 5",
               "overdue": 12}}
```

**Alert on `messaging.state`, not on the HTTP status.** The status code stays
200: a stalled cron does not mean the web container is unwell, and a 503 would
have the orchestrator restart the one part that is still working. States are
`ok`, `behind` (messages are late but the dispatcher is running), `failing`
(the last run raised), `stalled` (no run in fifteen minutes) and `unknown`
(no run ever recorded, or the database is unreachable).

The dashboard shows the same thing to whoever is sitting in front of it —
they are the one who can phone the worker the reminder never reached.

Prune the heartbeat occasionally so it does not grow without bound:

```bash
0 4 * * *  cd /app && psql "$DATABASE_URL" -c "SELECT prune_job_runs(30)"
```

## Publish the audit chain head

`GET /staff/audit/integrity` returns `head_hash`. Record it somewhere the server
cannot reach — a daily message to a phone, a separate mailbox, a printed log.

The chain makes tampering detectable; publishing the head makes it
*undeniable*. Once a hash exists off the machine, no local rewrite of history
can match it. Without that, an attacker with database access can edit a row and
recompute every hash after it.

```bash
# e.g. daily, from a machine that is not the server
curl -s -H "Authorization: Bearer $TOKEN" https://$SITE/staff/audit/integrity \
    | tee -a ~/akazi-audit-heads.log
```
