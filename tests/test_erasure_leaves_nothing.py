"""Erasure, tested the only way that means anything: put her name everywhere,
erase her, then search the whole database for it.

tests/test_data_rights_coverage.py compares the schema to the rights, and it
has two holes that let real survivals through. It derives candidate tables from
a literal `candidate_id` column, so five candidate-linked tables -- attendance,
follow_ups, pay_records, pay_deductions, placement_contracts -- are invisible to
it. And its assertion is `table not in source`, a substring test against the
migration's file text, so naming a table in a comment satisfies it.

This file does not read source. It writes a marker through the application's
own functions, runs erasure, and then generates a UNION over every text and
jsonb column in the live schema. Anything that comes back is either a defect or
a deliberate exception with a reason written next to it.

The cost is a slow test. The alternative is a fast one that passes while a
person's name sits in eight columns.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.clock import kigali_today

# Distinctive enough that a match is never a coincidence, and shaped like a
# real Rwandan name so anything doing name-ish validation accepts it.
MARKER = "Zephyrine-Nkurunziza-Marker"

# Survivals that are correct, each with the reason it is correct. Anything not
# listed here is a defect. A bare table name is not enough -- the column is
# what survives.
ALLOWED = {
    # A contract names its parties by necessity; one that does not is not
    # evidence of an agreement, and it protects her as much as the employer.
    # fn_contract_terms_immutable() refuses any change to agreed terms, which
    # is what makes a contract worth anything. Retention for establishing or
    # defending a legal claim is a recognised basis under Law No. 058/2021.
    #
    # This is a legal judgement, not an engineering one, and it is recorded as
    # an open question in CLAUDE.md. Every other entry added here needs an
    # argument of the same kind: why a person's name may remain in this exact
    # column after they have asked to be forgotten.
    ("placement_contracts", "terms"): "the agreement she was a party to",
}


def text_columns(session) -> list[tuple[str, str]]:
    """Every column in the schema that could hold a name."""
    return [
        (r[0], r[1]) for r in session.execute(
            text(
                """
                SELECT c.table_name, c.column_name
                  FROM information_schema.columns c
                  JOIN pg_tables t ON t.tablename = c.table_name
                                  AND t.schemaname = 'public'
                 WHERE c.table_schema = 'public'
                   AND (c.data_type IN ('text', 'character varying', 'character')
                        OR c.udt_name IN ('jsonb', 'json'))
                 ORDER BY 1, 2
                """
            )
        )
    ]


def search_everywhere(session, marker: str) -> list[tuple[str, str, int]]:
    """Every column still holding the marker, with a count."""
    columns = text_columns(session)
    assert len(columns) > 40, f"only {len(columns)} text columns found"

    union = " UNION ALL ".join(
        f"SELECT '{table}' AS t, '{column}' AS c, count(*) AS n "
        f"FROM {table} WHERE {column}::text LIKE :marker"
        for table, column in columns
    )
    rows = session.execute(
        text(f"SELECT * FROM ({union}) hits WHERE n > 0 ORDER BY t, c"),
        {"marker": f"%{marker}%"},
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


@pytest.fixture
def her(session, staff_id, employer_id, make_request, make_candidate):
    """A candidate whose name has reached every column a person can type into.

    Written through the application's own functions rather than by INSERT: a
    column nothing can write to is not a leak, and seeding one by hand would
    invent a finding.
    """
    from app.operations.attendance import log_attendance
    from app.operations.cohorts import add_member, create_cohort, record_outcome
    from app.operations.contracts import issue_contract
    from app.operations.employer_portal import rate_worker
    from app.operations.pay import record_pay_period
    from app.operations.registry import record_assessment_result
    from app.operations.safety import record_safety_report
    from app.operations.transport import record_transport_report

    candidate_id = make_candidate(name=MARKER)
    request_id = make_request()
    session.execute(
        text("UPDATE work_requests SET safety_notes = :n WHERE request_id = :r"),
        {"n": f"ask for {MARKER} at the gate", "r": str(request_id)},
    )
    placement_id = session.execute(
        text("INSERT INTO placements (request_id, candidate_id, status, "
             "agreed_pay_rwf, pay_unit, est_transport_rwf, match_reason) "
             "VALUES (:r, :c, 'accepted', 5000, 'day', 500, :m) "
             "RETURNING placement_id"),
        {"r": str(request_id), "c": str(candidate_id),
         "m": f"coordinator picked {MARKER} personally"},
    ).scalar_one()

    # The contract snapshots her name into terms, for every placement.
    issue_contract(session, placement_id, issued_by=staff_id)

    session.execute(
        text("UPDATE placements SET status = 'active' WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    from datetime import timedelta
    # A day worked, so the employer may rate her, and a day missed, because
    # the reason for an absence is free text somebody types.
    log_attendance(session, placement_id, work_date=kigali_today() - timedelta(days=1),
                   present=True, confirmed_by="employer", hours_worked=8)
    log_attendance(session, placement_id, work_date=kigali_today(), present=False,
                   confirmed_by="employer",
                   absence_reason=f"{MARKER} was ill, her mother rang")
    rate_worker(session, employer_id, placement_id, rating=5,
                note=f"{MARKER} is reliable, ask for her again")

    today = kigali_today()
    pay_id = record_pay_period(
        session, placement_id, period_start=today, period_end=today,
        gross_rwf=5000, due_on=today, deductions_rwf=500,
        deductions=[{"kind": "damage", "amount_rwf": 500,
                     "note": f"{MARKER} broke a tray on the Tuesday shift"}],
    )

    cohort_id = create_cohort(session, name="Audit cohort", starts_on=today,
                              facilitator=staff_id)
    add_member(session, cohort_id, candidate_id)
    record_outcome(session, cohort_id, candidate_id, outcome="completed",
                   notes=f"{MARKER} finished top of the group")

    skill_id = session.execute(
        text("INSERT INTO skills (skill_code, skill_name, category) "
             "VALUES ('audit_greeting', 'Greeting', 'retail') RETURNING skill_id")
    ).scalar_one()
    assessment_id = session.execute(
        text("INSERT INTO assessments (skill_id, title, method, max_score, "
             "pass_score) VALUES (:s, 'Observed', 'observed', 5, 3) "
             "RETURNING assessment_id"),
        {"s": skill_id},
    ).scalar_one()
    record_assessment_result(
        session, candidate_id=candidate_id, assessment_id=assessment_id, score=4,
        assessed_by=staff_id, notes=f"{MARKER} greeted every customer",
    )
    record_safety_report(session, placement_id=placement_id, felt_safe=False,
                         concern="harassment", note=f"{MARKER} was shouted at")
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=1400, note=f"{MARKER} took two motos")
    session.execute(
        text("INSERT INTO inbound_messages (from_phone, channel, body, "
             "provider_ref, candidate_id) VALUES ('+250788111333', 'whatsapp', "
             ":b, :ref, :c)"),
        {"b": f"this is {MARKER}, I have not been paid", "c": str(candidate_id),
         "ref": str(uuid.uuid4())},
    )
    return {"candidate_id": candidate_id, "placement_id": placement_id,
            "pay_id": pay_id, "request_id": request_id}


def test_the_marker_really_did_reach_the_database(session, her):
    """Guards the guard, and it is the whole file.

    If the seeding above silently stopped writing -- a renamed argument, a
    changed signature -- erasure would have nothing to miss and this file would
    pass while testing nothing at all.
    """
    hits = search_everywhere(session, MARKER)
    tables = {t for t, _, _ in hits}
    assert len(hits) >= 10, f"only seeded {len(hits)} columns: {hits}"
    for expected in ("placement_contracts", "pay_deductions", "attendance",
                     "assessment_results", "cohort_members", "placements"):
        assert expected in tables, f"nothing written to {expected}: {sorted(tables)}"


def test_erasure_leaves_her_name_nowhere(session, her, staff_id):
    """The property, stated once.

    Not "erasure updates these nine tables" -- that is a description of the
    implementation. This is the obligation: after an erasure, searching every
    column in the database for her name returns nothing.
    """
    from app.operations.data_rights import complete_erasure, request_erasure

    session.execute(
        text("UPDATE placements SET status = 'completed' WHERE placement_id = :p"),
        {"p": str(her["placement_id"])},
    )
    erasure_id = request_erasure(session, candidate_id=her["candidate_id"],
                                 requested_via="phone", received_by=staff_id)
    session.execute(text("SELECT set_config('app.staff_id', :s, true)"),
                    {"s": str(staff_id)})
    complete_erasure(session, erasure_id)

    survived = [(t, c, n) for t, c, n in search_everywhere(session, MARKER)
                if (t, c) not in ALLOWED]
    assert survived == [], (
        "her name survived erasure in:\n  "
        + "\n  ".join(f"{t}.{c} ({n} row(s))" for t, c, n in survived)
        + "\nEither redact it in erase_candidate_identity, or add it to "
          "ALLOWED with the reason it may remain."
    )


# --- the other half: what she is entitled to see --------------------------

def export(session, candidate_id) -> dict:
    from app.operations.data_rights import export_candidate_data

    return export_candidate_data(session, candidate_id)


def test_she_can_see_why_money_came_off_her_wage(session, her, staff_id):
    """_add_deduction_line demands at least ten characters of reason for a
    damage deduction, "in enough words that the worker could dispute it".

    She was never shown them. The pay section returned deductions_rwf, a
    number. A reason written so that somebody can dispute it, and withheld from
    the only person who would, is not a safeguard.
    """
    session.execute(text("SELECT set_config('app.staff_id', :s, true)"),
                    {"s": str(staff_id)})
    sections = export(session, her["candidate_id"])

    assert "pay_deductions" in sections, sorted(sections)
    notes = [d["note"] for d in sections["pay_deductions"]]
    assert any(MARKER in (n or "") for n in notes), notes


def test_the_export_covers_what_erasure_removes(session, her, staff_id):
    """The two lists must agree.

    Anything erasure treats as her words she must be able to see first;
    anything she cannot see, erasure has no business calling hers. The gap
    found here ran both ways -- transport_reports.note was erased and never
    exported, pay_deductions.note was neither.
    """
    from app.operations.data_rights import complete_erasure, request_erasure

    session.execute(text("SELECT set_config('app.staff_id', :s, true)"),
                    {"s": str(staff_id)})
    before = export(session, her["candidate_id"])
    seen = [s for s in ("pay_deductions", "contracts",
                        "transport_you_reported_notes") if s in before]
    assert len(seen) == 3, f"missing export sections: {seen}"

    session.execute(
        text("UPDATE placements SET status = 'completed' WHERE placement_id = :p"),
        {"p": str(her["placement_id"])},
    )
    erasure_id = request_erasure(session, candidate_id=her["candidate_id"],
                                 requested_via="phone", received_by=staff_id)
    complete_erasure(session, erasure_id)

    after = export(session, her["candidate_id"])
    remaining = [d["note"] for d in after["pay_deductions"]]
    assert not any(MARKER in (n or "") for n in remaining), remaining
    # ...and the deduction itself is still there, because the money did move.
    assert len(after["pay_deductions"]) == len(before["pay_deductions"])
    assert after["pay_deductions"][0]["amount_rwf"] == 500
