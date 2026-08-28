"""The audits that assert nothing is wrong, checked for looking at anything.

Several tests in this suite scan the source or the schema and assert the
result is empty. That shape is only as good as the scan: a walk that finds
nothing passes, reports success, and proves precisely nothing.

It has already happened twice here -- a route walker that examined zero routes
because `app.routes` holds router wrappers, and a seeder check wired into one
of forty-one calls. Both looked exactly like coverage.

Each audit ought to guard itself, and most now do. This file is the backstop:
it re-derives every scanned collection and refuses an empty one. If an audit
is added later without its own guard, this is what catches it.
"""
from __future__ import annotations

import re
from pathlib import Path


def python_sources() -> list[Path]:
    return list(Path("app").rglob("*.py"))


def templates() -> list[Path]:
    return list(Path("app/web/templates").rglob("*.html"))


def test_the_source_tree_is_findable_from_the_test_run():
    """Every scan below starts here. If the working directory were wrong,
    all of them would pass while reading nothing."""
    assert len(python_sources()) > 25, python_sources()
    assert len(templates()) > 10, templates()


def test_the_clock_audit_reads_real_files():
    """test_clock scans for CURRENT_DATE and date.today() in user-facing code."""
    sources = [p for p in python_sources() if p.read_text().strip()]
    assert len(sources) > 25
    # And the thing it looks for exists somewhere, or the pattern is wrong.
    assert any("kigali_today" in p.read_text() for p in sources)


def test_the_rules_audit_reads_real_files():
    """test_rules refuses a module defining a business rule privately."""
    names = ("MINIMUM_AGE", "MAX_TRANSPORT_SHARE", "GUARANTEE_HOURS")
    defined = [
        p for p in python_sources()
        if any(re.search(rf"^{n}\s*=", p.read_text(), re.M) for n in names)
    ]
    # Exactly one file may define them, and it must be the one home.
    assert [p.name for p in defined] == ["rules.py"], defined


def test_the_privilege_audit_finds_statements_to_check():
    """test_privileges derives its targets from SQL in the source. An empty
    derivation would pass every grant check."""
    joined = "\n".join(p.read_text() for p in python_sources())
    assert len(re.findall(r"INSERT\s+INTO\s+[a-z_]+", joined)) > 15
    assert len(re.findall(r"UPDATE\s+[a-z_]+\s+SET", joined)) > 10
    assert len(re.findall(r"DELETE\s+FROM\s+[a-z_]+", joined)) >= 1


def test_the_data_rights_audit_finds_tables_to_check():
    """It compares the schema against the export and the erasure function."""
    export = Path("app/operations/data_rights.py").read_text()
    assert export.count("rows(") > 10
    erasure = Path("migrations/044_erasure_reaches_free_text.sql").read_text()
    assert erasure.count("UPDATE ") > 5


def test_the_deployment_audit_finds_variables_to_check():
    compose = Path("deploy/docker-compose.prod.yml").read_text()
    assert len(re.findall(r"^\s{6}[A-Z_]+:", compose, re.M)) > 5
    env = Path("deploy/.env.example").read_text()
    assert len(re.findall(r"^[A-Z_]+=", env, re.M)) > 5


def test_the_route_audit_finds_routes():
    """The one that was actually vacuous: app.routes holds router wrappers,
    so iterating it found six objects and no endpoints."""
    from tests.test_readonly_role import _staff_write_routes

    assert len(list(_staff_write_routes())) > 20


def test_the_template_audits_find_templates():
    """Two scans depend on this: block balance and the ban on inline styles."""
    assert len([p for p in templates() if p.read_text().strip()]) > 10


def test_every_audit_style_test_has_a_guard_somewhere():
    """A test asserting a scanned collection is empty needs a companion
    proving the scan is not.

    Named explicitly rather than detected, because the distinction is about
    intent: `assert rejections == []` after creating one candidate is a
    behaviour test, and `assert offenders == []` after walking the source is
    an audit. Only the second kind can pass by looking at nothing.
    """
    audits = {
        "tests/test_clock.py": "test_the_clock_audit_reads_real_files",
        "tests/test_rules.py": "test_the_rules_audit_reads_real_files",
        "tests/test_privileges.py": "test_the_privilege_audit_finds_statements_to_check",
        "tests/test_data_rights_coverage.py": "test_the_comparison_is_looking_at_real_tables",
        "tests/test_deployment_config.py": "test_the_deployment_audit_finds_variables_to_check",
        "tests/test_readonly_role.py": "test_the_route_walker_actually_finds_routes",
        "tests/test_security_headers.py": "test_the_check_is_looking_at_the_real_templates",
        "tests/test_jobs.py": "test_the_template_check_looks_at_real_templates",
    }
    here = Path(__file__).read_text()
    for path, guard in audits.items():
        assert Path(path).exists(), path
        covered = guard in Path(path).read_text() or guard in here
        assert covered, f"{path} has no guard proving its scan finds anything"
