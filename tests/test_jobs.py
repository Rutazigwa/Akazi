"""Whether anything is still sending messages.

Messages are queued by the application and sent by a cron. Nothing knew
whether that cron was alive, and the failure is silent: /health says "ok"
because the web application is fine -- it is the part nobody watches that
stopped. The cost is not abstract. An unreminded worker is a no-show, a
no-show invokes the guarantee, and the guarantee is priced into the fee.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.operations.jobs import (
    OVERDUE_AFTER_MINUTES,
    STALE_AFTER_MINUTES,
    finish_run,
    job_health,
    messaging_status,
    overdue_messages,
    recorded_run,
    start_run,
)


def ran_minutes_ago(session, minutes: int, job="dispatch_messages", ok=True):
    run_id = start_run(session, job)
    finish_run(session, run_id, ok=ok, detail={"sent": 3})
    session.execute(
        text("UPDATE job_runs SET started_at = now() - make_interval(mins => :m), "
             "finished_at = now() - make_interval(mins => :m) WHERE run_id = :r"),
        {"m": minutes, "r": str(run_id)},
    )
    return run_id


def queue_message(session, make_candidate, minutes_late: int, status="queued"):
    return session.execute(
        text("INSERT INTO messages (candidate_id, template_key, body, status, "
             "scheduled_for) VALUES (:c, 'shift_reminder', 'see you tomorrow', "
             "CAST(:s AS message_status), now() - make_interval(mins => :m)) "
             "RETURNING message_id"),
        {"c": str(make_candidate()), "s": status, "m": minutes_late},
    ).scalar_one()


# --- the heartbeat ---------------------------------------------------------

def test_a_job_that_has_never_run_is_not_reported_as_healthy(session):
    """An empty outbox looks the same whether the cron is running or dead.

    Only the heartbeat separates them, so no heartbeat means unknown, never
    "fine".
    """
    assert messaging_status(session)["state"] == "unknown"


def test_a_recent_successful_run_with_an_empty_queue_is_ok(session):
    ran_minutes_ago(session, 2)
    assert messaging_status(session)["state"] == "ok"


def test_a_dispatcher_that_stopped_is_reported_as_stalled(session):
    ran_minutes_ago(session, STALE_AFTER_MINUTES + 5)
    status = messaging_status(session)
    assert status["state"] == "stalled"
    assert "last ran" in status["reason"]


def test_one_slow_tick_is_not_an_outage(session):
    """A threshold that cries wolf gets turned off, which is worse."""
    ran_minutes_ago(session, STALE_AFTER_MINUTES - 1)
    assert messaging_status(session)["state"] == "ok"


def test_a_failing_run_is_reported_even_when_recent(session):
    run_id = start_run(session, "dispatch_messages")
    finish_run(session, run_id, ok=False, error="provider timed out")
    status = messaging_status(session)
    assert status["state"] == "failing"
    assert "provider timed out" in status["reason"]


def test_recorded_run_writes_a_row_when_the_job_crashes(session):
    """A dispatcher that crashes every time is the case worth catching."""
    with pytest.raises(RuntimeError):
        with recorded_run(session, "dispatch_messages"):
            raise RuntimeError("provider exploded")

    last = job_health(session)[0]
    assert last["last_ok"] is False
    assert "provider exploded" in last["last_error"]


def test_recorded_run_keeps_what_the_job_reported(session):
    with recorded_run(session, "dispatch_messages") as detail:
        detail.update({"sent": 7, "failed": 1})
    assert job_health(session)[0]["last_detail"]["sent"] == 7


# --- the symptom -----------------------------------------------------------

def test_messages_stuck_past_their_time_are_reported(session, make_candidate):
    """True even when the dispatcher runs happily and fails at every send."""
    ran_minutes_ago(session, 1)
    queue_message(session, make_candidate, OVERDUE_AFTER_MINUTES + 10)
    status = messaging_status(session)
    assert status["state"] == "behind"
    assert status["overdue"] == 1


def test_a_message_a_few_minutes_late_is_the_normal_cadence(
    session, make_candidate
):
    ran_minutes_ago(session, 1)
    queue_message(session, make_candidate, 3)
    assert messaging_status(session)["state"] == "ok"


def test_a_message_scheduled_for_the_future_is_not_overdue(
    session, make_candidate
):
    ran_minutes_ago(session, 1)
    session.execute(
        text("INSERT INTO messages (candidate_id, template_key, body, "
             "scheduled_for) VALUES (:c, 'shift_reminder', 'tomorrow', "
             "now() + interval '6 hours')"),
        {"c": str(make_candidate())},
    )
    assert overdue_messages(session) == []


def test_a_sent_message_is_not_a_backlog(session, make_candidate):
    ran_minutes_ago(session, 1)
    queue_message(session, make_candidate, OVERDUE_AFTER_MINUTES + 10,
                  status="sent")
    assert messaging_status(session)["state"] == "ok"


def test_a_stalled_dispatcher_outranks_a_backlog(session, make_candidate):
    """Both are true at once; the coordinator needs the cause, not the symptom."""
    ran_minutes_ago(session, STALE_AFTER_MINUTES + 5)
    queue_message(session, make_candidate, OVERDUE_AFTER_MINUTES + 10)
    assert messaging_status(session)["state"] == "stalled"


# --- how it is surfaced ----------------------------------------------------

def test_health_reports_messaging_without_failing_the_check(
    api, session, monkeypatch
):
    """A stalled cron does not mean this container is unwell.

    Returning 503 would have an orchestrator restart the one part that is
    still working. /health opens its own session rather than taking the
    injected one, so it is pointed at the test transaction here -- the same
    way the existing database-reachability tests do it.
    """
    import contextlib

    import app.main

    ran_minutes_ago(session, STALE_AFTER_MINUTES + 5)

    @contextlib.contextmanager
    def test_session(*_a, **_kw):
        yield session

    monkeypatch.setattr(app.main, "session_scope", test_session)
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json()["messaging"]["state"] == "stalled"


def test_health_says_nothing_about_messaging_when_the_database_is_down(
    api, monkeypatch
):
    """Do not claim the queue is fine when nothing could be read."""
    import app.main

    def broken(*_a, **_kw):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(app.main, "session_scope", broken)
    response = api.get("/health")
    assert response.status_code == 503
    assert response.json()["messaging"]["state"] == "unknown"


def test_the_dashboard_says_so_when_nothing_is_going_out(web, session):
    ran_minutes_ago(session, STALE_AFTER_MINUTES + 5)
    page = web.get("/ui/")
    assert "Messages are not going out" in page.text


def test_the_dashboard_stays_quiet_when_messages_are_moving(web, session):
    ran_minutes_ago(session, 2)
    assert "Messages are not going out" not in web.get("/ui/").text


def test_old_runs_are_pruned(session):
    ran_minutes_ago(session, 60 * 24 * 45)
    ran_minutes_ago(session, 5)
    removed = session.execute(text("SELECT prune_job_runs(30)")).scalar_one()
    assert removed == 1
    assert session.execute(
        text("SELECT count(*) FROM job_runs")
    ).scalar_one() == 1


# --- the check that keeps catching me --------------------------------------

def test_every_template_balances_its_blocks():
    """A missing endif breaks every page, not just the one edited.

    This has caught the same mistake twice while adding dashboard sections,
    both times only because it was run by hand. Run it here instead.
    """
    unbalanced = []
    for path in Path("app/web/templates").rglob("*.html"):
        source = path.read_text()
        for tag, closing in (("if", "endif"), ("for", "endfor"),
                             ("block", "endblock")):
            opens = len(re.findall(r"\{%-?\s*" + tag + r"\b", source))
            closes = len(re.findall(r"\{%-?\s*" + closing + r"\b", source))
            if opens != closes:
                unbalanced.append(f"{path.name}: {tag} {opens} vs {closes}")
    assert unbalanced == [], unbalanced


def test_every_template_balances_its_html_tags():
    """Jinja blocks are not the only thing I have left unclosed.

    An unbalanced table or div renders without error and lays the page out
    wrongly, which is harder to notice than a template that refuses to load.
    """
    unbalanced = []
    for path in Path("app/web/templates").rglob("*.html"):
        source = path.read_text()
        for tag in ("table", "tr", "td", "th", "form", "div", "select"):
            opens = len(re.findall(r"<" + tag + r"[ >]", source))
            closes = len(re.findall(r"</" + tag + r">", source))
            if opens != closes:
                unbalanced.append(f"{path.name}: <{tag}> {opens} vs {closes}")
    assert unbalanced == [], unbalanced


def test_the_template_check_looks_at_real_templates():
    """Guards the guard: an empty glob would pass silently."""
    assert len(list(Path("app/web/templates").rglob("*.html"))) > 10


# --- backups: the same failure, discovered at the worst moment -------------

def test_a_database_with_no_backup_recorded_is_not_reported_as_fine(session):
    from app.operations.jobs import backup_status
    assert backup_status(session)["state"] == "unknown"


def test_a_recent_verified_backup_is_ok(session):
    from app.operations.jobs import backup_status
    ran_minutes_ago(session, 120, job="backup")
    assert backup_status(session)["state"] == "ok"


def test_a_backup_that_has_not_run_for_two_days_is_stale(session):
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES, backup_status
    ran_minutes_ago(session, BACKUP_STALE_AFTER_MINUTES + 60, job="backup")
    status = backup_status(session)
    assert status["state"] == "stale"
    assert "hours ago" in status["reason"]


def test_a_backup_running_a_little_late_is_not_an_alarm(session):
    """Nightly crons drift. An alert that fires daily is one nobody reads."""
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES, backup_status
    ran_minutes_ago(session, BACKUP_STALE_AFTER_MINUTES - 60, job="backup")
    assert backup_status(session)["state"] == "ok"


def test_a_failed_backup_counts_as_no_backup(session):
    """"It ran" is not the question."""
    from app.operations.jobs import backup_status
    run_id = start_run(session, "backup")
    finish_run(session, run_id, ok=False, error="verification failed")
    status = backup_status(session)
    assert status["state"] == "failing"
    assert "verification failed" in status["reason"]


def test_health_reports_backups_too(api, session, monkeypatch):
    import contextlib

    import app.main
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES

    ran_minutes_ago(session, BACKUP_STALE_AFTER_MINUTES + 60, job="backup")

    @contextlib.contextmanager
    def test_session(*_a, **_kw):
        yield session

    monkeypatch.setattr(app.main, "session_scope", test_session)
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json()["backups"]["state"] == "stale"


def test_the_dashboard_says_so_when_backups_stopped(web, session):
    from app.operations.jobs import BACKUP_STALE_AFTER_MINUTES
    ran_minutes_ago(session, BACKUP_STALE_AFTER_MINUTES + 60, job="backup")
    assert "Backups are not running" in web.get("/ui/").text


def test_the_dashboard_stays_quiet_when_backups_are_current(web, session):
    ran_minutes_ago(session, 120, job="backup")
    assert "Backups are not running" not in web.get("/ui/").text
