"""Check that the audit log has not been tampered with.

    17 3 * * *  cd /app && python scripts/verify_audit_chain.py

Every audit_log row carries the hash of the one before it, so altering or
removing any row breaks every link after it. verify_audit_chain() walks the
whole table rehashing as it goes, and that is exactly as expensive as it
sounds: 432ms at 62,000 rows, 1,251ms at 182,000 -- linear, about seven
microseconds a row.

/ui/staff used to call it on every render. audit_log grows on every identity
read and is never pruned, by design, because it is the evidence produced if the
NCSA asks -- so the page reporting "the trail is intact" got slower forever,
and at a million rows it is a seven-second page load. Nobody would have
diagnosed that from the page; they would have concluded the system was slow.

So it runs here, nightly, on its own heartbeat. A verification that stops
running now shows up as a stalled job rather than as silence, and the page
reports the last result with its age -- because a check nobody has run for a
week is not reassurance.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.operations.jobs import recorded_run  # noqa: E402

JOB = "verify_audit_chain"
log = logging.getLogger(JOB)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    with session_scope() as session:
        with recorded_run(session, JOB) as run:
            row = session.execute(
                text("SELECT entries_checked, intact, broken_at, reason, "
                     "duration_ms FROM record_audit_verification()")
            ).mappings().one()
            run.update(dict(row))

            if row["intact"]:
                log.info("audit chain intact: %s entries in %sms",
                         row["entries_checked"], row["duration_ms"])
                return 0

            # Loud, and a non-zero exit, because this is the one result that
            # means somebody has been in the database.
            log.error("AUDIT CHAIN BROKEN at entry %s: %s",
                      row["broken_at"], row["reason"])
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
