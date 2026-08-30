"""Verifying the audit chain, off the page that used to do it on every render.

/ui/staff called verify_audit_chain() on every load. That function walks every
row in audit_log rehashing as it goes, and audit_log is append-only by design
-- it is the evidence produced if the NCSA asks -- so the page reporting "the
trail is intact" got slower forever. Measured:

    62,000 entries  ->   432ms
   182,000 entries  -> 1,251ms

Linear, about seven microseconds a row. At a million entries that is a
seven-second page load, and nobody would have diagnosed it from the page.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


def status(session):
    return session.execute(
        text("SELECT * FROM v_audit_chain_status")
    ).mappings().first()


def test_nothing_is_claimed_before_anything_has_been_checked(session):
    assert status(session) is None, (
        "a status with no verification behind it would be reassurance nobody "
        "earned"
    )


def test_a_run_records_what_it_checked(session, make_candidate):
    make_candidate()
    row = session.execute(
        text("SELECT * FROM record_audit_verification()")
    ).mappings().one()
    assert row["intact"] is True
    assert row["entries_checked"] > 0
    assert row["duration_ms"] >= 0
    assert status(session)["entries_checked"] == row["entries_checked"]


def test_the_page_can_tell_how_stale_the_last_check_is(session, make_candidate):
    """A verification nobody has run for a week is not reassurance."""
    make_candidate()
    session.execute(text("SELECT record_audit_verification()"))
    session.execute(
        text("UPDATE audit_verifications SET checked_at = now() - INTERVAL '50 hours'")
    )
    assert status(session)["hours_ago"] >= 49


def test_a_break_outranks_every_later_pass(session, make_candidate):
    """Tampering does not stop being true because the next run came back clean.

    Without this, breaking the chain and waiting a day turns the card green.
    """
    make_candidate()
    session.execute(
        text("INSERT INTO audit_verifications (entries_checked, intact, "
             "broken_at, reason, duration_ms) "
             "VALUES (5, false, 3, 'entry_hash does not match', 4)")
    )
    session.execute(text("SELECT record_audit_verification()"))

    current = status(session)
    assert current["intact"] is True, "the latest run itself passed"
    assert current["ever_broken"] is True, (
        "a chain that was ever found broken must keep saying so"
    )


def test_a_break_must_say_where_and_why(session):
    """A row claiming a break with no detail is not evidence of anything."""
    with pytest.raises(Exception, match="chk_break_explained"):
        session.execute(
            text("INSERT INTO audit_verifications (entries_checked, intact, "
                 "duration_ms) VALUES (5, false, 4)")
        )


def test_a_real_break_is_found_and_recorded(session, make_candidate):
    """Guards the guard: the whole file is worthless if the check cannot fail.

    audit_log has rules refusing UPDATE and DELETE, so the tamper is done as
    the table's owner with those rules disabled -- which is precisely the
    attacker this chain exists to catch.
    """
    make_candidate()
    session.execute(text("SET session_replication_role = replica"))
    session.execute(
        text("UPDATE audit_log SET action = 'read' "
             "WHERE audit_id = (SELECT min(audit_id) FROM audit_log)")
    )
    session.execute(text("SET session_replication_role = origin"))

    row = session.execute(
        text("SELECT * FROM record_audit_verification()")
    ).mappings().one()
    assert row["intact"] is False, "a modified row was not detected"
    assert row["broken_at"] is not None
    assert "does not match" in row["reason"]
