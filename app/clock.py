"""Dates and times, in the timezone the people using this system live in.

`date.today()` returns the *server's* date. Servers run on UTC; Kigali is
UTC+2. Between 22:00 and midnight UTC it is already tomorrow in Kigali, so
every date this system offered a coordinator was a day behind for two hours
every night -- and those two hours are 00:00 to 02:00 local, exactly when a
late shift ends and attendance gets logged.

So nothing user-facing calls `date.today()`. Everything goes through
`kigali_today()`, and the database has a matching `kigali_today()` for the
same reason.

Rwanda does not observe daylight saving and has not changed offset since 1935,
so a fixed +02:00 is honest here. It is written as a named zone anyway, so the
assumption is visible rather than buried in an integer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Africa/Kigali. Fixed offset: no daylight saving, no historical changes that
# matter to this system.
KIGALI = timezone(timedelta(hours=2), name="Africa/Kigali")


def kigali_now() -> datetime:
    """The current moment, as it reads on a clock in Kigali."""
    return datetime.now(timezone.utc).astimezone(KIGALI)


def kigali_today() -> date:
    """Today's date as the people using this system would say it.

    Use this anywhere a date is shown to, defaulted for, or compared against
    what a coordinator or worker considers today.
    """
    return kigali_now().date()
