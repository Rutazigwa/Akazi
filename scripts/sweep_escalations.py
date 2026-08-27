"""Raise escalations that missed their response time.

    */5 * * * *  cd /app && python scripts/sweep_escalations.py

A missed response time used to turn a pill red on a page. If nobody had that
page open -- evening, weekend, a coordinator out on a site visit -- a
harassment report sat unacknowledged and nothing happened at all. The
blueprint promises a named escalation path and a defined response time; the
time was stored and nothing enforced it.

Its own cron line and its own heartbeat rather than folding into the message
dispatcher: a failure in one would otherwise hide the state of the other, and
this is the job whose silence is least acceptable.

Alerts are queued to the outbox, so they retry, fall back to SMS, and are
recorded. They are not held for quiet hours -- staff are on duty in a way
candidates are not, and a report that missed its deadline at 22:00 must reach
someone at 22:00.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import session_scope  # noqa: E402
from app.operations.escalations import (  # noqa: E402
    alert_on_missed_response_times,
)
from app.operations.jobs import recorded_run  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    with session_scope() as session:
        with recorded_run(session, "sweep_escalations") as detail:
            report = alert_on_missed_response_times(session)
            detail.update(report)

    if report["unroutable"]:
        # Left unalerted and unmarked, so the next run tries again. Said out
        # loud because an escalation nobody can be told about is the worst of
        # the cases here, not the quietest.
        print(
            f"WARNING: {report['unroutable']} breached escalation(s) had "
            "nobody active to alert",
            file=sys.stderr,
        )
    print(f"breached={report['breached']} alerted={report['alerted']} "
          f"unroutable={report['unroutable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
