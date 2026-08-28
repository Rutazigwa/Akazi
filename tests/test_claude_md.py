"""CLAUDE.md is the handover artefact, and it accumulates.

It is the file that instructs whoever picks this project up. Over roughly
twenty commits, section 5 "Schema conventions" grew to 1,252 of 1,550 lines --
81% of the document -- because every new invariant landed under whatever
heading happened to be nearby. Most of what it held was not schema
conventions: the readonly rule, the content-security policy, cover ranking,
the fare model, deployment configuration.

Nothing was wrong with any individual addition. The failure was cumulative and
silent, which is the kind a test is for.
"""
from __future__ import annotations

from pathlib import Path


DOC = Path("CLAUDE.md")

# Past which a section stops being a section. Nobody scrolls a thousand lines
# to find a rule; they re-derive it, get it wrong, and write a third copy.
MAX_SECTION_LINES = 350

# Deliberately not tested: how a section opens. An earlier version of this
# file demanded a one-line summary under every heading, and section 4 opens
# with a table -- which says what it is for perfectly well. A test that
# enforces one author's preference over a legitimate alternative is noise
# somebody eventually deletes, along with the tests that were worth keeping.


def sections() -> dict[str, int]:
    lines = DOC.read_text().splitlines()
    found, current, count = {}, None, 0
    for line in lines:
        if line.startswith("## "):
            if current:
                found[current] = count
            current, count = line[3:].strip(), 0
        count += 1
    if current:
        found[current] = count
    return found


def test_the_document_has_real_sections():
    """Guards the guard: no sections would make every check below pass."""
    assert len(sections()) >= 8, sections()
    assert DOC.read_text().count("### ") > 40


def test_no_section_has_swallowed_the_document():
    oversized = {
        name: n for name, n in sections().items() if n > MAX_SECTION_LINES
    }
    assert oversized == {}, (
        f"{oversized} -- past this a section is not a section. Split it by "
        "subject, the way 5a-5g were split out of 'Schema conventions'."
    )


def test_no_section_holds_most_of_the_document():
    """The specific failure that happened: one heading at 81%."""
    counts = sections()
    total = sum(counts.values())
    biggest, n = max(counts.items(), key=lambda kv: kv[1])
    assert n / total < 0.35, (
        f"'{biggest}' is {n / total:.0%} of CLAUDE.md. It is collecting "
        "everything that has nowhere else to go."
    )


def test_the_non_negotiable_constraints_are_still_there():
    """Everything else in the file explains itself. These are the ten rules
    the project owner set, and a restructure must not quietly lose one."""
    text = DOC.read_text()
    for rule in ["No AI/ML matching in v1", "No payment movement in the pilot",
                 "No employer mobile app, ever", "No candidate app before month 4",
                 "Data residency decided before schema design",
                 "Transport cost is a first-class column",
                 "Consent is an append-only record",
                 "Surrogate UUID keys throughout",
                 "No pay-to-apply model", "Minimum age 16"]:
        assert rule in text, f"constraint missing from CLAUDE.md: {rule}"
