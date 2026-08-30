"""The numbers the business runs on, in one place.

Each of these was written down more than once. The transport rule existed as
0.30 in the matcher and 30 in the Tomorrow view; the minimum age as a constant
in two modules and two SQL expressions; the guarantee window as two
independent INTERVAL clauses. None of the copies disagreed yet, which is the
only reason it had not caused a problem.

The failure mode is quiet. Somebody changes the matcher's transport threshold
after a month of real fares, and the Tomorrow view keeps flagging at the old
one -- so a coordinator sees a warning for a placement the matcher considers
fine, or worse, sees nothing for one it would now refuse. Nothing errors.

**Where SQL needs the same number it keeps its own copy**, because a view
cannot import Python, and `tests/test_rules.py` asserts the two agree. That is
the same shape as the privilege and data-rights audits: derive the check from
the source rather than trusting that somebody remembered.

These are deliberately constants rather than settings. A threshold that can be
changed at runtime is one that gets changed under pressure, on a Friday, to
make a particular placement go through.
"""
from __future__ import annotations

# --- who may work ---------------------------------------------------------

# Law, not policy. Narrow apprenticeship exceptions for 13-15 exist in the
# labour law but are NOT handled anywhere here: they need an authorised
# exception record and a deliberate schema change. Enforced by
# chk_minimum_age on candidate_identity, so a bulk import cannot get past it.
MINIMUM_AGE = 16

# --- transport ------------------------------------------------------------

# The pilot target is 25% of daily pay. The matcher refuses at 30% to leave
# headroom: a placement accepted at exactly the target has no room for a fare
# rise, and the fare is the thing that moves.
MAX_TRANSPORT_SHARE = 0.30

# --- the guarantee --------------------------------------------------------

# The promise sold to the employer: if a placed worker does not arrive, the
# slot is covered free of charge within this window.
GUARANTEE_HOURS = 24

# --- what the pilot is aiming at ------------------------------------------
#
# Targets rather than rules: nothing refuses anything on these. They are here
# so the scorecard and the blueprint cannot drift apart silently.
TARGET_TRANSPORT_SHARE = 0.25
TARGET_RETENTION_30DAY = 0.60
TARGET_GUARANTEE_FILLED = 0.90
TARGET_WOMEN_PLACED = 0.45
TARGET_REORDER_RATE = 0.40
TARGET_PAY_ACCURACY = 0.95
TARGET_DAYS_TO_FILL = 7


# --- how much of a list a screen shows ------------------------------------
#
# Every dashboard list was unbounded. Measured on a database with a year of
# operating in it -- 5,000 placements, 2,000 candidates -- the dashboard came
# to 1,177 KB of HTML and 3,969 table rows, of which 3,590 were follow-ups due.
# That is not a work queue, it is 90% of the page burying the hundred things
# that need a response, and a coordinator opens it every morning on a phone.
#
# The lists are already ordered most-urgent-first, so a cap keeps the right
# rows. What matters is that the screen says how many it is not showing: a
# page silently displaying 25 of 3,590 is worse than a slow one.
DASHBOARD_ROWS = 25
REGISTRY_ROWS = 100


# --- how many alerts one person can usefully receive ----------------------
#
# sweep_escalations sent one SMS per breached escalation, with no bound.
# Measured at scale: 100 breached at once produced 100 text messages to one
# staff member in a single five-minute run. That is not a hundred prompts, it
# is a phone nobody can use, and among a hundred texts the harassment ones are
# indistinguishable from the rest.
#
# The module already knew this about repetition -- "re-alerting every five
# minutes until someone acknowledges is how an alert becomes noise, and noise
# is how the next one gets ignored" -- and then sent a hundred at once. Above
# this many, one message names the counts by kind instead.
ALERT_BURST_LIMIT = 5
