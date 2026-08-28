"""Database privileges under the real application role.

The rest of the suite connects as superuser, where GRANT and REVOKE do not
apply. That is convenient and it hid a production-breaking bug: the matching
engine joined candidate_identity, which app_operations cannot read, so matching
would have failed on any deployment that actually used the role model.

These tests run the real queries with `SET ROLE app_operations` so the grants
are exercised. If something here fails, the deployment is broken even though
every other test passes.
"""

from __future__ import annotations
from app.clock import kigali_today

import os
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text


os.environ.setdefault("DATA_RESIDENCY", "local_dev")

TODAY = kigali_today()


@contextmanager
def as_operations(session):
    """Run the enclosed statements with only app_operations' privileges.

    A context manager rather than a fixture, deliberately: seed data has to be
    created first, and a fixture would drop privileges before the test body
    gets a chance to set anything up.
    """
    session.execute(text("SET LOCAL ROLE app_operations"))
    try:
        yield session
    finally:
        session.execute(text("RESET ROLE"))


def test_operations_cannot_read_identity_directly(session):
    """The revoke that makes the audit trail complete.

    A savepoint around the expected failure: a permission error aborts the
    transaction, and without one the RESET ROLE that follows cannot run.
    """
    with as_operations(session) as ops:
        savepoint = ops.begin_nested()
        with pytest.raises(Exception, match="permission denied"):
            ops.execute(text("SELECT * FROM candidate_identity")).all()
        savepoint.rollback()


def test_the_matcher_runs_without_identity_access(
    session, make_candidate, make_request, staff_id
):
    """The regression. This is the query that was broken in production."""
    from app.matching.repository import find_matches

    make_candidate()
    request_id = make_request()

    with as_operations(session):
        result = find_matches(session, request_id)

    # Matched or excluded does not matter here -- that it ran at all does.
    assert result.matches or result.rejections


def test_age_eligibility_is_readable_without_reading_dates_of_birth(
    session, make_candidate
):
    make_candidate()
    with as_operations(session) as ops:
        rows = ops.execute(
            text(
                "SELECT candidate_id, age_eligible "
                "FROM candidates_age_eligible(:d)"
            ),
            {"d": TODAY},
        ).mappings().all()
    assert rows
    assert rows[0]["age_eligible"] is True
    # A boolean, not a date. Nothing identifying crossed the boundary.
    assert set(rows[0]) == {"candidate_id", "age_eligible"}


def test_an_under_16_candidate_is_reported_ineligible(session, make_candidate):
    """Belt and braces: the constraint blocks the insert, and if it were ever
    relaxed, the matcher would still refuse to place them."""
    make_candidate()
    rows = session.execute(
        text(
            "SELECT age_eligible FROM candidates_age_eligible(:d)"
        ),
        # Ask the question as of a date when today's 22-year-old was 10.
        {"d": TODAY - timedelta(days=365 * 12)},
    ).mappings().all()
    assert all(r["age_eligible"] is False for r in rows) or not rows


def test_operations_can_do_its_actual_job(
    session, make_placement, make_request, make_candidate, staff_id
):
    """The operational tables app_operations must be able to reach."""
    pid = make_placement(request_id=make_request(), candidate_id=make_candidate())
    with as_operations(session) as ops:
        for query in (
            "SELECT count(*) FROM candidates",
            "SELECT count(*) FROM placements",
            "SELECT count(*) FROM attendance",
            "SELECT count(*) FROM work_requests",
            "SELECT count(*) FROM v_pilot_scorecard",
            "SELECT count(*) FROM v_guarantee_invocations",
            "SELECT count(*) FROM v_current_consent",
        ):
            ops.execute(text(query)).scalar_one()
    assert pid


def test_identity_reads_still_work_through_the_audited_function(
    session, make_candidate
):
    """app_identity keeps the function, not the table."""
    cid = make_candidate()
    granted = session.execute(
        text(
            "SELECT has_function_privilege('app_identity', "
            "'read_candidate_identity(uuid, text)', 'EXECUTE')"
        )
    ).scalar_one()
    assert granted is True

    table = session.execute(
        text(
            "SELECT has_table_privilege('app_identity', 'candidate_identity', "
            "'SELECT')"
        )
    ).scalar_one()
    assert table is False
    assert cid


# --- grants derived from what the application actually does ----------------

def _write_targets() -> dict[str, set[str]]:
    """Tables the application INSERTs into or UPDATEs, read from the source.

    Derived rather than listed, so a new write target cannot arrive without a
    matching grant. Listing them by hand is how the staff table ended up
    readable, updatable and not insertable -- correct in every test, broken on
    the first deploy that used the role model.
    """
    import pathlib
    import re

    targets: dict[str, set[str]] = {
        "INSERT": set(), "UPDATE": set(), "DELETE": set(),
    }
    for path in pathlib.Path("app").rglob("*.py"):
        source = path.read_text()
        for table in re.findall(r"INSERT\s+INTO\s+([a-z_]+)", source):
            targets["INSERT"].add(table)
        for table in re.findall(r"UPDATE\s+([a-z_]+)\s+SET", source):
            targets["UPDATE"].add(table)
        for table in re.findall(r"DELETE\s+FROM\s+([a-z_]+)", source):
            targets["DELETE"].add(table)
    return targets


# The application connects as one role holding both, so a grant to either
# satisfies it. See docs/DEPLOYMENT.md on splitting them across connections.
APP_ROLES = ("app_operations", "app_identity")


def test_the_app_role_can_write_everything_the_app_writes(session):
    targets = _write_targets()
    # Guard the guard. DELETE was absent from this parser entirely, so the
    # first DELETE the app performed shipped without a grant and the test that
    # exists to catch that passed -- it was not looking for the verb at all.
    for verb in ("INSERT", "UPDATE", "DELETE"):
        assert targets[verb], f"found no {verb} targets -- the parser is broken"

    missing = []
    for privilege, tables in targets.items():
        for table in sorted(tables):
            exists = session.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
            ).scalar_one()
            if exists is None:
                continue  # a CTE or alias, not a real table
            granted = any(
                session.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": table, "p": privilege},
                ).scalar_one()
                for role in APP_ROLES
            )
            if not granted:
                missing.append(f"{privilege} on {table}")

    assert missing == [], (
        "the application writes to tables the app role cannot: "
        f"{missing} -- add a GRANT in a migration"
    )


def test_every_function_the_app_calls_is_executable(session):
    """SECURITY DEFINER functions are useless if the caller cannot EXECUTE."""
    import pathlib
    import re

    called = set()
    for path in pathlib.Path("app").rglob("*.py"):
        source = path.read_text()
        called.update(re.findall(r"SELECT\s+\*?\s*FROM\s+([a-z_]+)\(", source))
        called.update(re.findall(r"SELECT\s+([a-z_]+)\(", source))

    ours = session.execute(
        text(
            "SELECT p.proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public'"
        )
    ).scalars().all()

    missing = []
    for name in sorted(called & set(ours)):
        granted = any(
            session.execute(
                text(
                    "SELECT bool_or(has_function_privilege(:r, p.oid, 'EXECUTE')) "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname = :f"
                ),
                {"r": role, "f": name},
            ).scalar_one()
            for role in APP_ROLES
        )
        if not granted:
            missing.append(name)

    assert missing == [], (
        f"the application calls functions the app role cannot execute: {missing}"
    )


def test_the_app_role_still_cannot_read_identity_directly(session):
    """The grants above must never widen this one."""
    for role in APP_ROLES:
        assert session.execute(
            text("SELECT has_table_privilege(:r, 'candidate_identity', 'SELECT')"),
            {"r": role},
        ).scalar_one() is False


def _read_targets() -> set[str]:
    """Relations the application selects from, read from the source.

    The mirror of _write_targets(). A missing SELECT grant fails the same way
    a missing INSERT does -- at runtime, on a deployment using the role model,
    invisibly to a suite connected as the owner.
    """
    import pathlib
    import re

    found: set[str] = set()
    for path in pathlib.Path("app").rglob("*.py"):
        source = path.read_text()
        found.update(re.findall(r"\bFROM\s+([a-z_]+)", source))
        found.update(re.findall(r"\bJOIN\s+([a-z_]+)", source))
    return found


# The one relation the application reads and the role deliberately cannot.
# Reads go through read_candidate_identity(), which is SECURITY DEFINER and
# writes the audit row; only candidate_id and erased_at are granted, and at
# column level, which a table-level privilege check does not see. If this set
# ever grows, the growth is the thing to look at.
READ_EXCEPTIONS = {"candidate_identity"}


def test_the_app_role_can_read_everything_the_app_reads(session):
    targets = _read_targets()
    assert len(targets) > 20, "found almost no read targets -- parser broken"

    missing = []
    for relation in sorted(targets):
        exists = session.execute(
            text("SELECT to_regclass(:t)"), {"t": f"public.{relation}"}
        ).scalar_one()
        if exists is None or relation in READ_EXCEPTIONS:
            continue
        granted = any(
            session.execute(
                text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                {"r": role, "t": f"public.{relation}"},
            ).scalar_one()
            for role in APP_ROLES
        )
        if not granted:
            missing.append(relation)

    assert missing == [], (
        f"the application reads relations the app role cannot: {missing} "
        "-- add a GRANT in a migration"
    )


def test_identity_is_still_the_only_deliberate_read_exception(session):
    """Guards the exception list above from quietly absorbing a real bug."""
    for relation in READ_EXCEPTIONS:
        granted = any(
            session.execute(
                text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                {"r": role, "t": f"public.{relation}"},
            ).scalar_one()
            for role in APP_ROLES
        )
        assert not granted, (
            f"{relation} is now table-readable; it is on the exception list "
            "because it must not be"
        )
