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
docker compose -f deploy/docker-compose.prod.yml run --rm app \
    ./scripts/migrate.sh "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/$POSTGRES_DB"

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

Test a restore into a scratch database before you need one. Quarterly is
reasonable.

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
