#!/usr/bin/env python
"""Send whatever is due in the message outbox.

Run on a cron, every few minutes:

    */5 * * * *  cd /app && python scripts/dispatch_messages.py

Safe to run concurrently — due rows are claimed with FOR UPDATE SKIP LOCKED, so
two dispatchers divide the work rather than sending the same message twice.

Defaults to the recording provider, which logs instead of sending. That is
deliberate: a pilot can run its first week this way and read exactly what would
have gone out before any of it reaches a real person. Pass --live once a real
provider is configured.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import session_scope  # noqa: E402
from app.messaging.outbox import dispatch, outbox_summary  # noqa: E402
from app.messaging.providers import RecordingProvider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--live", action="store_true",
        help="use the configured live provider instead of recording",
    )
    parser.add_argument("--summary", action="store_true", help="show the outbox and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    with session_scope() as session:
        if args.summary:
            for row in outbox_summary(session):
                print(f"  {row['status']:11} {row['n']}")
            return 0

        if args.live:
            print(
                "no live provider is configured -- see app/messaging/providers.py",
                file=sys.stderr,
            )
            return 2

        report = dispatch(session, RecordingProvider(), limit=args.limit)
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
