"""Whether the database could actually be restored.

A backup that stopped running is discovered at the worst possible moment. The
check is cheap and the failure is silent, which is the same shape as the job
heartbeat -- but a different subsystem, so it lives in its own file.
"""
from __future__ import annotations


from app.operations.jobs import backup_status, finish_run, start_run


def test_a_database_with_no_backup_recorded_is_not_reported_as_fine(session):
    assert backup_status(session)["state"] == "unknown"


def test_a_recent_verified_backup_is_ok(session, ran_minutes_ago):
    ran_minutes_ago(120, job="backup")
    assert backup_status(session)["state"] == "ok"


def test_a_backup_that_has_not_run_for_two_days_is_stale(session, ran_minutes_ago):
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES, backup_status
    ran_minutes_ago(BACKUP_STALE_AFTER_MINUTES + 60, job="backup")
    status = backup_status(session)
    assert status["state"] == "stale"
    assert "hours ago" in status["reason"]


def test_a_backup_running_a_little_late_is_not_an_alarm(session, ran_minutes_ago):
    """Nightly crons drift. An alert that fires daily is one nobody reads."""
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES, backup_status
    ran_minutes_ago(BACKUP_STALE_AFTER_MINUTES - 60, job="backup")
    assert backup_status(session)["state"] == "ok"


def test_a_failed_backup_counts_as_no_backup(session):
    """"It ran" is not the question."""
    run_id = start_run(session, "backup")
    finish_run(session, run_id, ok=False, error="verification failed")
    status = backup_status(session)
    assert status["state"] == "failing"
    assert "verification failed" in status["reason"]


def test_health_reports_backups_too(api, session, monkeypatch, ran_minutes_ago):
    import contextlib

    import app.main
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES

    ran_minutes_ago(BACKUP_STALE_AFTER_MINUTES + 60, job="backup")

    @contextlib.contextmanager
    def test_session(*_a, **_kw):
        yield session

    monkeypatch.setattr(app.main, "session_scope", test_session)
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json()["backups"]["state"] == "stale"


def test_the_dashboard_says_so_when_backups_stopped(web, session, ran_minutes_ago):
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES
    ran_minutes_ago(BACKUP_STALE_AFTER_MINUTES + 60, job="backup")
    assert "Backups are not running" in web.get("/ui/").text


def test_the_dashboard_stays_quiet_when_backups_are_current(web, session, ran_minutes_ago):
    ran_minutes_ago(120, job="backup")
    assert "Backups are not running" not in web.get("/ui/").text
