"""A date written into a test is a date that will arrive.

tests/test_lifecycle.py set MONDAY = date(2026, 9, 7) so that availability
windows were predictable. It passed for months, and then the calendar caught
up with it.

What made it fail is worth reading twice, because it passed by accident in two
different ways first. Before migration 055 the guarantee clock started when we
noticed the absence, so a fixed shift date could not affect it at all. After
055 the clock runs from the shift -- and while that date was still in the
future, the replacement's offered_at came *before* invoked_at, the difference
was negative, and "within 24 hours" held. Only once the date went into the past
did a no-show on Monday covered on Thursday become what it always was: 72
hours, correctly outside the window.

The test was wrong for months and green throughout. So: module-level fixed
dates are listed here with the reason they cannot drift, and anything else
fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# Fixed dates that are inputs to a pure function, not a position relative to
# now. These cannot drift because nothing compares them to the clock.
ALLOWED = {
    "test_attendance.py": "checkpoint_schedule arithmetic: fixed in, fixed out",
    "test_clock.py": "a timezone conversion table; fixed instants are the point",
    # A self-consistent frame: starts_on, the shift window and the
    # date-of-birth are all relative to MONDAY and nothing is compared to now.
    # It also derives an age from MONDAY.day - 1, which is only valid because
    # that day is the 7th -- computing the Monday would silently produce day 0
    # in the month it fell on the 1st. Left pinned deliberately; if anything
    # ever compares starts_on to today, this allowance stops being true.
    "test_matching.py": "pure algebra relative to one fixed Monday",
    "test_no_fixed_calendar.py": "this file names the others",
}

FIXED_DATE = re.compile(r"\bdate\(\s*20\d\d\s*,")


def module_level_lines(path: Path) -> list[tuple[int, str]]:
    """Lines outside any function or class body."""
    out = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line[:1] in (" ", "\t", "") or line.startswith(("#", "@")):
            continue
        out.append((number, line))
    return out


def test_the_scan_finds_the_test_files():
    """Guards the guard: an empty glob would pass silently."""
    assert len(list(TESTS.glob("test_*.py"))) > 40


@pytest.mark.parametrize(
    "path", sorted(TESTS.glob("test_*.py")), ids=lambda p: p.name
)
def test_no_test_pins_itself_to_a_calendar_date(path: Path):
    if path.name in ALLOWED:
        return

    offenders = [
        f"line {n}: {line.strip()[:70]}"
        for n, line in module_level_lines(path)
        if FIXED_DATE.search(line)
    ]
    assert offenders == [], (
        f"{path.name} pins a date at module level:\n  "
        + "\n  ".join(offenders)
        + "\nDerive it from kigali_today() instead, or add the file to ALLOWED "
          "with the reason it cannot drift."
    )


def test_every_allowance_names_a_file_that_exists():
    """A stale allowance is how a file slips out of scope unnoticed."""
    for name in ALLOWED:
        assert (TESTS / name).exists(), f"{name} is allowed but no longer exists"
