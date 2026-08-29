"""Every table holding personal data, against the rights that must reach it.

The subject access export and the erasure function were both written when the
schema was smaller. Six tables arrived afterwards -- her own messages, what
she told us about an employer, the concerns raised about her -- and neither
path was updated. Nobody noticed, because nothing compares the schema to the
rights.

These tests do the comparison, derived from the database rather than a list,
so the next table to arrive cannot quietly escape both.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text


DATA_RIGHTS = Path("app/operations/data_rights.py")
ERASURE = Path("migrations/044_erasure_reaches_free_text.sql")


def candidate_tables(session) -> set[str]:
    """Every base table that links to a candidate, directly or through one hop.

    A literal candidate_id column is not the whole set. attendance, follow_ups,
    pay_records, pay_deductions and placement_contracts all hold data about a
    person and reach her through placement_id or pay_id -- so for a long time
    this comparison could not see five tables, two of which really did keep her
    name after an erasure.
    """
    return set(session.execute(
        text(
            """
            SELECT c.table_name
              FROM information_schema.columns c
              JOIN pg_tables t ON t.tablename = c.table_name
                              AND t.schemaname = 'public'
             WHERE c.table_schema = 'public'
               AND c.column_name IN ('candidate_id', 'placement_id', 'pay_id')
            """
        )
    ).scalars())


# Tables deliberately outside the export, each for a stated reason. Anything
# not here has to be returned: an access request that answers with most of
# what is held is a failed one.
EXPORT_EXCLUSIONS = {
    # The subject's own record, returned in full under "identity".
    "candidate_identity": "returned as identity",
    "candidates": "returned as profile",
    # Requests she made about her own data. Returning the request alongside
    # the response would be circular, and the response is the response.
    "erasure_requests": "the request that produced this export",
}

# Tables erasure need not touch, each for a stated reason.
ERASURE_EXCLUSIONS = {
    "candidates": "redacted directly",
    "candidate_identity": "redacted directly",
    "erasure_requests": "records that the erasure happened",
    # audit_log is not listed: it references a candidate through record_id
    # rather than candidate_id, so it is out of scope here. It is also
    # append-only and hash-chained on purpose -- its rows are the evidence
    # that a read or an erasure occurred, and overwriting them would defeat
    # having it. See migration 044.
    # Structural links carrying no free text about the person.
    "availability": "day and time windows only",
    "consent_records": "the proof of what she agreed to, kept deliberately",
    # A contract names its parties by necessity, and its terms are immutable by
    # trigger. Retained for establishing or defending a legal claim -- recorded
    # as an open question in CLAUDE.md, because that boundary is a legal call.
    "placement_contracts": "the agreement she was a party to",
    # What money moved and how. pay_records.method is 'momo' or 'cash'; the
    # written reason a deduction was taken lives in pay_deductions, which is
    # redacted.
    "pay_records": "amounts and method, no free text about her",
}

# Three reasons that used to sit in ERASURE_EXCLUSIONS were simply untrue, and
# nothing checked them: "assessment_results: scores only, no free text" beside
# a notes column, "cohort_members: membership and outcome only" beside another,
# and "placements: carries no personal free text" beside employer_note and
# match_reason. All three held a person's name after she had been erased. A
# stated reason is only worth as much as the thing that checks it -- which is
# now tests/test_erasure_leaves_nothing.py, not this list.


def test_the_export_returns_every_table_that_holds_her_data(session):
    source = DATA_RIGHTS.read_text()
    missing = [
        table for table in sorted(candidate_tables(session))
        if table not in EXPORT_EXCLUSIONS and table not in source
    ]
    assert missing == [], (
        f"a subject access request would not return: {missing}. "
        "Add them to export_candidate_data, or to EXPORT_EXCLUSIONS with a "
        "reason."
    )


def erasure_function_body(session) -> str:
    """The installed function, not a file.

    This used to read migrations/044 as text, which was wrong twice over: a
    table named in a comment satisfied it, and a redaction added by any later
    migration was invisible to it -- so widening the table list above reported
    five tables as unredacted that migration 051 redacts.
    """
    return session.execute(
        text("SELECT pg_get_functiondef('erase_candidate_identity(uuid,uuid)'"
             "::regprocedure)")
    ).scalar_one()


def test_erasure_reaches_every_table_that_holds_her_words(session):
    import re

    body = erasure_function_body(session)
    # An actual UPDATE of that table, not a mention of its name.
    updated = set(re.findall(r"UPDATE\s+(\w+)", body))
    missing = [
        table for table in sorted(candidate_tables(session))
        if table not in ERASURE_EXCLUSIONS and table not in updated
    ]
    assert missing == [], (
        f"erasure would leave her data in: {missing}. Add them to "
        "erase_candidate_identity, or to ERASURE_EXCLUSIONS with a reason."
    )


def test_the_comparison_is_looking_at_real_tables(session):
    """Guards the guard. An empty set would pass both tests silently."""
    tables = candidate_tables(session)
    assert len(tables) >= 10, tables
    assert "employer_safety_reports" in tables


def test_every_exclusion_names_a_table_that_exists(session):
    """A stale exclusion is how a table slips out of scope unnoticed."""
    tables = candidate_tables(session)
    for listed in (EXPORT_EXCLUSIONS, ERASURE_EXCLUSIONS):
        for table in listed:
            assert table in tables, f"{table} is excluded but no longer exists"


# --- what erasure keeps, and why ------------------------------------------

def seeded_person(session, make_placement, make_candidate, staff_id):
    """A candidate with words of hers in every table that holds them."""
    from app.operations.safety import record_safety_report
    from app.operations.transport import record_transport_report

    candidate_id = make_candidate()
    placement_id = make_placement(candidate_id=candidate_id)

    session.execute(
        text("INSERT INTO inbound_messages (from_phone, channel, body, "
             "provider_ref, candidate_id) VALUES ('+250788111222', 'whatsapp', "
             "'this is Aline, the supervisor keeps shouting at me', 'p1', :c)"),
        {"c": str(candidate_id)},
    )
    session.execute(
        text("INSERT INTO messages (candidate_id, template_key, body) "
             "VALUES (:c, 'shift_reminder', 'Aline, your shift is tomorrow')"),
        {"c": str(candidate_id)},
    )
    record_safety_report(session, placement_id=placement_id, felt_safe=False,
                         concern="harassment", note="the supervisor, by name")
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=1400, note="two motos, she said")
    return candidate_id, placement_id


def erase(session, candidate_id, staff_id):
    from app.operations.data_rights import complete_erasure, request_erasure

    erasure_id = request_erasure(session, candidate_id=candidate_id,
                                 requested_via="phone", received_by=staff_id)
    session.execute(text("SELECT set_config('app.staff_id', :s, true)"),
                    {"s": str(staff_id)})
    complete_erasure(session, erasure_id)


def test_erasure_reaches_her_own_words(session, make_placement, make_candidate,
                                       staff_id):
    """A redaction that leaves "this is Aline" in inbound_messages has not
    erased anybody."""
    candidate_id, _ = seeded_person(session, make_placement, make_candidate,
                                    staff_id)
    erase(session, candidate_id, staff_id)

    remaining = session.execute(
        text("SELECT body FROM inbound_messages WHERE candidate_id = :c "
             "UNION ALL SELECT body FROM messages WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    ).scalars().all()
    assert all(body == "[erased]" for body in remaining), remaining


def test_erasure_removes_the_free_text_from_her_safety_report(
    session, make_placement, make_candidate, staff_id
):
    candidate_id, _ = seeded_person(session, make_placement, make_candidate,
                                    staff_id)
    erase(session, candidate_id, staff_id)
    assert session.execute(
        text("SELECT note FROM employer_safety_reports WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    ).scalar_one() is None


def test_the_warning_she_left_survives_her_erasure(
    session, make_placement, make_candidate, staff_id
):
    """The hard one. If erasing her record also erased her warning, the next
    woman placed there loses the protection -- and the employer gains from her
    leaving. Her words go; the fact somebody felt unsafe stays.
    """
    candidate_id, _ = seeded_person(session, make_placement, make_candidate,
                                    staff_id)
    erase(session, candidate_id, staff_id)

    report = session.execute(
        text("SELECT felt_safe, concern FROM employer_safety_reports "
             "WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    ).mappings().one()
    assert report["felt_safe"] is False
    assert report["concern"] == "harassment"


def test_the_escalation_pattern_survives_but_not_its_detail(
    session, make_placement, make_candidate, staff_id
):
    """"This employer had a harassment escalation" is what protects the next
    person."""
    candidate_id, _ = seeded_person(session, make_placement, make_candidate,
                                    staff_id)
    erase(session, candidate_id, staff_id)

    escalation = session.execute(
        text("SELECT kind::text AS kind, detail FROM escalations "
             "WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    ).mappings().first()
    assert escalation["kind"] == "harassment"
    assert escalation["detail"] == "[erased]"


def test_the_audit_trail_is_not_rewritten(session, make_placement,
                                          make_candidate, staff_id):
    """Its rows are the evidence an auditor asks for."""
    candidate_id, _ = seeded_person(session, make_placement, make_candidate,
                                    staff_id)
    before = session.execute(
        text("SELECT count(*) FROM audit_log WHERE record_id = :c"),
        {"c": str(candidate_id)},
    ).scalar_one()
    erase(session, candidate_id, staff_id)
    after = session.execute(
        text("SELECT count(*) FROM audit_log WHERE record_id = :c"),
        {"c": str(candidate_id)},
    ).scalar_one()
    assert after > before


def test_the_consent_record_is_kept(session, make_placement, make_candidate,
                                    staff_id):
    """The proof of what she agreed to, and when. Deleting it would leave us
    unable to show the processing was lawful while it happened."""
    candidate_id, _ = seeded_person(session, make_placement, make_candidate,
                                    staff_id)
    before = session.execute(
        text("SELECT count(*) FROM consent_records WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    ).scalar_one()
    erase(session, candidate_id, staff_id)
    assert session.execute(
        text("SELECT count(*) FROM consent_records WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    ).scalar_one() == before
