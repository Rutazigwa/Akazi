"""Fill an empty database with a working operation, for demonstration.

Twenty to thirty employer interviews decide whether this business proceeds,
and "let me show you" beats a slide. This produces a system with real history
in it: employers, a scored candidate registry, completed placements, a
guarantee invocation with its clock running, overdue pay, tomorrow's shifts
and two escalations awaiting a response.

Everything goes through the API rather than straight into the database, so
every rule applies on the way in -- the transport filter really does refuse a
wage that does not survive the commute, and pay accuracy really does need the
worker's confirmation, not the employer's word. Seeded data that bypassed the
rules would demonstrate a system nobody has.

    python scripts/seed_demo.py http://localhost:8000 --dsn "$DSN"

Needs the dev dependencies (httpx2, pyotp, psycopg) -- it drives the running
application the way a client would.

REFUSES TO RUN AGAINST ANYTHING THAT IS NOT EMPTY. This writes invented people
with invented national identifiers; the one place it must never reach is a
database with real ones in it. The check is deliberately crude -- any existing
candidate or employer stops it -- because a subtle check is one that can be
argued around at the moment it matters.
"""
from __future__ import annotations

import argparse
import datetime
import sys
import time

import httpx2 as httpx
import psycopg
import pyotp

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("base_url", help="the running application, e.g. http://localhost:8000")
parser.add_argument("--dsn", required=True,
                    help="psql DSN for the same database, for the empty check")
parser.add_argument("--phone", default="+250780000001",
                    help="an existing owner account to seed through")
parser.add_argument("--password", default="a-sufficiently-long-password")
args = parser.parse_args()

B = args.base_url
DSN = args.dsn
TODAY = datetime.date.today()
TOMORROW = TODAY + datetime.timedelta(days=1)

# The guard. Invented people with invented national identifiers must never
# land in a database holding real ones.
with psycopg.connect(DSN) as check:
    existing = check.execute(
        "SELECT (SELECT count(*) FROM candidates), (SELECT count(*) FROM employers)"
    ).fetchone()
if any(existing):
    sys.exit(
        f"refusing to seed: this database already has {existing[0]} candidate(s) "
        f"and {existing[1]} employer(s). Seed an empty database."
    )

api = httpx.Client(base_url=B, follow_redirects=True, timeout=30)

tok = api.post("/auth/login", json={"phone": args.phone,
       "password": args.password}).json()["token"]
H = {"Authorization": f"Bearer {tok}"}
sec = api.post("/auth/totp/enrol", headers=H).json()["secret"]
api.post("/auth/totp/confirm", json={"code": pyotp.TOTP(sec).now()}, headers=H)
api.post("/auth/mfa", json={"code": pyotp.TOTP(sec).at(int(time.time()) + 30)}, headers=H)

api.post("/staff", json={"full_name": "Chantal Mukamana", "phone": "+250780002222",
         "role": "coordinator", "can_view_identity": True}, headers=H)
api.post("/staff", json={"full_name": "Eric Habimana", "phone": "+250780003333",
         "role": "supervisor", "can_view_identity": False}, headers=H)

# --- the catalogue --------------------------------------------------------
skills = {}
for code, name, cat, rubric, pas in [
    ("retail_greeting", "Greeting customers", "retail",
     "1 no greeting; 3 greets and offers help; 5 greets, offers help and closes the interaction", 3),
    ("food_hygiene", "Food hygiene", "hospitality",
     "1 unaware of basic rules; 3 washes, separates raw and cooked; 5 also checks temperatures and logs them", 3),
    ("deep_cleaning", "Deep cleaning", "cleaning",
     "1 surface wipe only; 3 correct products, correct order; 4 leaves a checked, signed-off room", 3),
]:
    sid = api.post("/skills", json={"skill_code": code, "skill_name": name,
                   "category": cat}, headers=H).json()["skill_id"]
    skills[code] = sid
    api.post("/assessments", json={"skill_id": sid,
             "title": f"{name}, observed", "method": "observed",
             "pass_score": pas, "max_score": 5, "rubric": rubric}, headers=H)
api.post("/skills", json={"skill_code": "forklift", "skill_name": "Forklift operation",
         "category": "logistics"}, headers=H)

assessments = {a["skill_code"]: a for a in api.get("/assessments", headers=H).json()["assessments"]}

# --- employers ------------------------------------------------------------
employers = {}
logins: list[tuple[str, str, str]] = []
for name, sector, coop, lat, lng, tier in [
    ("Isuku Cooperative", "cleaning", True, -1.9550, 30.1150, "active"),
    ("Kimironko Market Stores", "retail", False, -1.9530, 30.1260, "active"),
    ("Café Kivu", "hospitality", False, -1.9490, 30.0920, "active"),
    ("Gasabo Facilities Ltd", "cleaning", False, -1.9600, 30.1300, "pilot"),
]:
    eid = api.post("/employers", json={"business_name": name, "sector": sector,
           "district": "Gasabo", "site_lat": lat, "site_lng": lng,
           "is_cooperative": coop}, headers=H).json()["employer_id"]
    api.patch(f"/employers/{eid}", json={"tier": tier}, headers=H)
    contact_phone = f"+2507888{abs(hash(name)) % 90000 + 10000}"
    contact = api.post(f"/employers/{eid}/contacts", json={
        "full_name": "Site manager", "phone": contact_phone,
        "role_title": "Site manager", "is_primary": True}, headers=H).json()
    # A login for the employer dashboard, so the other half of the system can
    # actually be shown. The password is generated and returned once.
    invited = api.post(
        f"/employers/{eid}/contacts/{contact['contact_id']}/invite", headers=H
    ).json()
    logins.append((name, contact_phone, invited["temporary_password"]))
    employers[name] = eid

# --- candidates -----------------------------------------------------------
people = [
    ("Aline",   "Uwase",     "F", -1.9480, 30.1050, True,  2000, "2002-03-04"),
    ("Chantal", "Ingabire",  "F", -1.9515, 30.1210, True,  2500, "2004-07-19"),
    ("Divine",  "Mutesi",    "F", -1.9470, 30.0980, False, 2200, "2001-11-02"),
    ("Eric",    "Nshimiyimana","M",-1.9620, 30.1330, True,  2500, "2000-01-25"),
    ("Fabrice", "Niyonzima", "M", -1.9560, 30.1180, True,  1800, "2003-05-30"),
    ("Grace",   "Umutoni",   "F", -1.9505, 30.1105, True,  2000, "2005-09-14"),
    ("Honorine","Mukandayisenga","F",-1.9700,30.1450,True, 900,  "1999-02-08"),
]
cands = {}
for first, last, gender, lat, lng, phone_ok, commute, dob in people:
    r = api.post("/candidates", json={
        "legal_first_name": first, "legal_last_name": last,
        "date_of_birth": dob, "phone_primary": f"+25078{abs(hash(first+last))%900000+100000}",
        "display_name": f"{first} {last[0]}.", "district": "Gasabo",
        "sector": "Remera", "gender": gender, "home_lat": lat, "home_lng": lng,
        "max_commute_rwf": commute, "consent_captured_via": "whatsapp",
        "has_smartphone": phone_ok,
        "availability": [{"day_of_week": d, "start": "06:00:00", "end": "20:00:00"}
                         for d in range(7)]}, headers=H)
    cands[first] = r.json()["candidate_id"]

# scores, so matching has something to filter and rank on
for first, code, score in [
    ("Aline", "retail_greeting", 4), ("Aline", "deep_cleaning", 4),
    ("Chantal", "retail_greeting", 5), ("Chantal", "food_hygiene", 4),
    ("Divine", "deep_cleaning", 3), ("Eric", "deep_cleaning", 5),
    ("Fabrice", "food_hygiene", 3), ("Grace", "retail_greeting", 2),
    ("Honorine", "deep_cleaning", 4),
]:
    api.post(f"/candidates/{cands[first]}/assessments",
             json={"assessment_id": assessments[code]["assessment_id"],
                   "score": score, "notes": ""}, headers=H)

# --- a cohort -------------------------------------------------------------
co = api.post("/cohorts", json={"name": "Orientation — March intake",
      "starts_on": str(TODAY - datetime.timedelta(days=20)), "women_only": True,
      "capacity": 12}, headers=H).json()["cohort_id"]
for first in ("Aline", "Chantal", "Divine", "Grace"):
    api.post(f"/cohorts/{co}/members", json={"candidate_id": cands[first]}, headers=H)
    api.post(f"/cohorts/{co}/outcomes", json={"candidate_id": cands[first],
             "outcome": "completed"}, headers=H)


def offer(rid, first):
    """Offer a placement, reporting the matcher's reason when it refuses.

    The transport filter is doing real work on this data -- a wage that does
    not survive the commute is refused, which is the point of it.
    """
    r = api.post(f"/work-requests/{rid}/offers",
                 json={"candidate_id": cands[first]}, headers=H)
    if r.status_code != 201:
        print(f"  refused {first}: {r.json().get('detail', '')[:90]}")
        return None
    return r.json()["placement_id"]


def request_for(employer, title, starts, headcount=1, pay=5000,
                start_t="08:00:00", end_t="16:00:00", transport=False):
    return api.post("/work-requests", json={
        "employer_id": employers[employer], "title": title, "work_type": "shift",
        "headcount": headcount, "starts_on": str(starts), "shift_start": start_t,
        "shift_end": end_t, "pay_rwf": pay, "pay_unit": "day",
        "transport_covered": transport}, headers=H).json()["request_id"]


# --- history: completed placements, so the metrics are not empty ----------
past = TODAY - datetime.timedelta(days=35)
for first, employer, title in [("Aline", "Kimironko Market Stores", "Shop assistant"),
                               ("Chantal", "Café Kivu", "Counter service"),
                               ("Eric", "Isuku Cooperative", "Deep clean crew"),
                               ("Divine", "Isuku Cooperative", "Deep clean crew"),
                               ("Fabrice", "Café Kivu", "Kitchen assistant")]:
    rid = request_for(employer, title, past, pay=6500, transport=True)
    pid = offer(rid, first)
    api.post(f"/placements/{pid}/response", json={"accepted": True}, headers=H)
    api.post(f"/placements/{pid}/start", json={"started_on": str(past)}, headers=H)
    for d in range(0, 20, 4):
        api.post(f"/placements/{pid}/attendance", json={
            "work_date": str(past + datetime.timedelta(days=d)), "present": True,
            "confirmed_by": "employer"}, headers=H)
    api.post(f"/placements/{pid}/end", json={"ended_on": str(past + datetime.timedelta(days=20))}, headers=H)
    pay_id = api.post(f"/placements/{pid}/pay", json={
        "period_start": str(past), "period_end": str(past + datetime.timedelta(days=20)),
        "gross_rwf": 110000, "due_on": str(past + datetime.timedelta(days=22))}, headers=H).json().get("pay_id")
    if pay_id:
        api.post(f"/pay/{pay_id}/paid", json={"paid_on": str(past + datetime.timedelta(days=22)),
                 "method": "momo"}, headers=H)
        api.post(f"/pay/{pay_id}/worker-confirmation",
                 json={"received_in_full": True}, headers=H)

# Work the historical follow-ups, so 30-day retention is a real figure rather
# than a dash. day_30 with still_working is what the metric reads.
for f in api.get("/follow-ups/due", params={"as_of": str(TODAY)}, headers=H).json().get("due", []):
    api.post(f"/follow-ups/{f['follow_up_id']}/complete",
             json={"still_working": True, "worker_rating": 4,
                   "employer_rating": 4}, headers=H)

# reorders: the same employers coming back
for employer, title in [("Kimironko Market Stores", "Shop assistant"),
                        ("Café Kivu", "Counter service")]:
    request_for(employer, title, TODAY + datetime.timedelta(days=4), pay=5500)

# --- a guarantee invocation, still running --------------------------------
rid = request_for("Gasabo Facilities Ltd", "Office clean", TODAY, pay=5000)
pid = offer(rid, "Fabrice")
if pid:
    api.post(f"/placements/{pid}/response", json={"accepted": True}, headers=H)
    api.post(f"/placements/{pid}/start", json={"started_on": str(TODAY)}, headers=H)
    api.post(f"/placements/{pid}/attendance", json={"work_date": str(TODAY),
             "present": False, "confirmed_by": "employer",
             "absence_reason": "did not arrive, phone off"}, headers=H)

# --- overdue pay ----------------------------------------------------------
rid = request_for("Isuku Cooperative", "Weekend deep clean", TODAY - datetime.timedelta(days=12), pay=6000)
pid = offer(rid, "Divine")
if pid:
    api.post(f"/placements/{pid}/response", json={"accepted": True}, headers=H)
    api.post(f"/placements/{pid}/start", json={"started_on": str(TODAY - datetime.timedelta(days=12))}, headers=H)
    api.post(f"/placements/{pid}/pay", json={
        "period_start": str(TODAY - datetime.timedelta(days=12)),
        "period_end": str(TODAY - datetime.timedelta(days=5)),
        "gross_rwf": 42000, "due_on": str(TODAY - datetime.timedelta(days=3))}, headers=H)

# --- tomorrow -------------------------------------------------------------
rid = request_for("Café Kivu", "Morning barista", TOMORROW, headcount=1, pay=7000,
                  start_t="07:00:00", end_t="13:00:00")
pid = offer(rid, "Chantal")   # left unaccepted on purpose

rid = request_for("Kimironko Market Stores", "Shop assistant", TOMORROW, pay=7000,
                  start_t="09:00:00", end_t="17:00:00")
pid = offer(rid, "Divine")   # Divine has no smartphone
if pid:
    api.post(f"/placements/{pid}/response", json={"accepted": True}, headers=H)

# nobody assigned at all
request_for("Gasabo Facilities Ltd", "Night guard", TOMORROW, headcount=2, pay=6000,
            start_t="18:00:00", end_t="02:00:00")

# --- an open request to match against ------------------------------------
rid = request_for("Isuku Cooperative", "Office cleaning team", TODAY + datetime.timedelta(days=3),
                  headcount=2, pay=5500, transport=True)
api.post(f"/work-requests/{rid}/skills", json={"skill_code": "deep_cleaning",
         "min_score": 3}, headers=H)

# --- an inbound harassment report, escalated ------------------------------
with psycopg.connect(DSN) as c:
    phone = c.execute("SELECT phone_primary FROM candidate_identity ci "
                      "JOIN candidates cc USING (candidate_id) "
                      "WHERE cc.display_name LIKE 'Grace%'").fetchone()[0]
api.post("/webhooks/inbound", json={"from_phone": phone,
         "body": "the supervisor keeps shouting at me and it made me feel uncomfortable",
         "provider_ref": "wa-demo-1"}, headers={"X-Webhook-Secret": "shared-secret"})
with psycopg.connect(DSN) as c:
    unpaid = c.execute("SELECT phone_primary FROM candidate_identity ci "
                       "JOIN candidates cc USING (candidate_id) "
                       "WHERE cc.display_name LIKE 'Divine%'").fetchone()[0]
api.post("/webhooks/inbound", json={"from_phone": unpaid,
         "body": "I have still not been paid for last week",
         "provider_ref": "wa-demo-2"},
         headers={"X-Webhook-Secret": "shared-secret"})
# One nobody can interpret, so the "replies we could not read" queue is real.
api.post("/webhooks/inbound", json={"from_phone": "+250788000999",
         "body": "???", "provider_ref": "wa-demo-3"},
         headers={"X-Webhook-Secret": "shared-secret"})

# The dispatcher runs on a cron; do the same here so the escalation exists.
api.post("/inbound/process", headers=H)

print("seeded\n")
print("Staff sign-in at /ui/login")
print(f"  {args.phone} / {args.password}")
print("\nEmployer dashboard sign-in at /employer/login")
for business, phone, temporary in logins:
    print(f"  {business:26} {phone:16} {temporary}")
print("\nEach employer password must be changed on first use.")
