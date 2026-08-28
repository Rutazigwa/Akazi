"""Ask employers about shifts nobody has confirmed.

    0 10 * * *  cd /app && python scripts/chase_attendance.py

Attendance is the input the guarantee rests on and it comes from the employer.
Until they say, a shift that went perfectly and one we should have covered
look identical -- and the expensive one is invisible.

Once a day rather than every five minutes: this is a question for a person
with a business to run, and asking twice before lunch is how a useful message
becomes one that gets ignored.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import session_scope  # noqa: E402
from app.operations.attendance import chase_unconfirmed_attendance  # noqa: E402
from app.operations.jobs import recorded_run  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    with session_scope() as session:
        with recorded_run(session, "chase_attendance") as detail:
            report = chase_unconfirmed_attendance(session)
            detail.update(report)
    print(f"asked={report['asked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
