"""Consent records: append-only, versioned, and correctly ordered.

Regression coverage for a real bug. v_current_consent originally ordered only
by captured_at, which defaults to now() -- transaction start time in PostgreSQL,
identical for every row written in the same transaction. A withdrawal recorded
alongside a grant tied, DISTINCT ON broke the tie arbitrarily, and a candidate
who had withdrawn could stay matchable.
"""

from __future__ import annotations

from sqlalchemy import text

from app.operations.registry import record_consent


def current(session, candidate_id, purpose="placement"):
    return session.execute(
        text(
            "SELECT granted FROM v_current_consent "
            "WHERE candidate_id = :cid AND purpose = :purpose"
        ),
        {"cid": candidate_id, "purpose": purpose},
    ).scalar_one_or_none()


def test_withdrawal_in_the_same_transaction_wins(session, make_candidate, staff_id):
    """The regression. Both rows share captured_at; recorded_seq breaks the tie."""
    cid = make_candidate(consented=False)
    record_consent(session, cid, "placement", True, "paper", staff_id)
    record_consent(session, cid, "placement", False, "whatsapp", staff_id)
    assert current(session, cid) is False


def test_re_granting_after_a_withdrawal_wins_again(session, make_candidate, staff_id):
    cid = make_candidate(consented=False)
    record_consent(session, cid, "placement", True, "paper", staff_id)
    record_consent(session, cid, "placement", False, "whatsapp", staff_id)
    record_consent(session, cid, "placement", True, "whatsapp", staff_id)
    assert current(session, cid) is True


def test_purposes_are_tracked_independently(session, make_candidate, staff_id):
    cid = make_candidate(consented=False)
    record_consent(session, cid, "placement", True, "paper", staff_id)
    record_consent(session, cid, "reporting", False, "paper", staff_id)
    assert current(session, cid, "placement") is True
    assert current(session, cid, "reporting") is False


def test_history_is_preserved_not_overwritten(session, make_candidate, staff_id):
    cid = make_candidate(consented=False)
    record_consent(session, cid, "placement", True, "paper", staff_id)
    record_consent(session, cid, "placement", False, "whatsapp", staff_id)

    rows = session.execute(
        text(
            "SELECT granted, captured_via FROM consent_records "
            "WHERE candidate_id = :cid ORDER BY recorded_seq"
        ),
        {"cid": cid},
    ).all()
    assert [(r[0], r[1]) for r in rows] == [(True, "paper"), (False, "whatsapp")]


def test_consent_rows_cannot_be_updated_or_deleted(session, make_candidate, staff_id):
    cid = make_candidate(consented=False)
    record_consent(session, cid, "placement", True, "paper", staff_id)

    session.execute(text("UPDATE consent_records SET granted = false"))
    session.execute(text("DELETE FROM consent_records"))

    remaining = session.execute(
        text("SELECT granted FROM consent_records WHERE candidate_id = :cid"),
        {"cid": cid},
    ).scalar_one()
    assert remaining is True


def test_the_policy_version_is_recorded(session, make_candidate, staff_id):
    """We must be able to prove what someone agreed to, not just that they did."""
    cid = make_candidate(consented=False)
    record_consent(session, cid, "placement", True, "paper", staff_id,
                   policy_version="v2.1")
    version = session.execute(
        text(
            "SELECT policy_version FROM consent_records WHERE candidate_id = :cid"
        ),
        {"cid": cid},
    ).scalar_one()
    assert version == "v2.1"
