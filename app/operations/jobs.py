"""Heartbeat for scheduled work, and the backlog it is supposed to clear.

Messages are queued by the application and sent by a cron. Nothing knew
whether that cron was still alive, and the failure is silent: /health reports
"ok" because the web application is fine -- it is the part nobody watches that
stopped. The cost is not abstract. An unreminded worker is a no-show, a
no-show invokes the guarantee, and the guarantee is priced into the fee.

Two signals, because they fail differently:

  record_run() is the heartbeat. Only it can tell a stalled cron from a quiet
  evening with nothing to send -- an empty outbox looks identical either way.

  overdue_messages() is the symptom, read from the outbox. It stays true when
  the dispatcher runs perfectly and fails at every send.
"""
from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# The dispatcher is meant to run every five minutes. Three missed runs is a
# real outage rather than one slow tick, and a threshold that cries wolf gets
# turned off, which is worse than not having it.
DISPATCH_INTERVAL_MINUTES = 5
STALE_AFTER_MINUTES = DISPATCH_INTERVAL_MINUTES * 3

# A message a few minutes late is the dispatcher's normal cadence. An hour
# late means it is not moving.
OVERDUE_AFTER_MINUTES = 60

# Backups run nightly. One missed run is a real gap rather than a slow night,
# but a threshold at exactly 24 hours would fire every time the cron drifts by
# a minute, and an alert that fires daily is an alert nobody reads.
BACKUP_INTERVAL_HOURS = 24
BACKUP_STALE_AFTER_MINUTES = 30 * 60


def start_run(session: Session, job_name: str) -> UUID:
    return session.execute(
        text("INSERT INTO job_runs (job_name) VALUES (:j) RETURNING run_id"),
        {"j": job_name},
    ).scalar_one()


def finish_run(
    session: Session, run_id: UUID, *, ok: bool,
    detail: dict | None = None, error: str | None = None,
) -> None:
    import json

    session.execute(
        text("UPDATE job_runs SET finished_at = clock_timestamp(), ok = :ok, "
             "detail = CAST(:detail AS jsonb), error = :error "
             "WHERE run_id = :rid"),
        {"ok": ok, "detail": json.dumps(detail) if detail else None,
         "error": error, "rid": str(run_id)},
    )


@contextmanager
def recorded_run(session: Session, job_name: str):
    """Record a run whether or not it succeeds.

    A job that crashes every time is the case worth catching, so the failure
    has to be written down before the exception is re-raised. The row is
    committed on the way out because the caller's transaction may be rolled
    back by whatever went wrong.
    """
    run_id = start_run(session, job_name)
    session.commit()
    result: dict = {}
    try:
        yield result
    except Exception as exc:
        finish_run(session, run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        session.commit()
        raise
    finish_run(session, run_id, ok=True, detail=result or None)
    session.commit()


def job_health(session: Session) -> list[dict]:
    """The last run of each job.

    minutes_since is cast at the boundary: SQL ROUND returns Decimal, which
    will not divide by a float, and every caller wants a number rather than a
    lesson in numeric types.
    """
    rows = []
    for row in session.execute(text("SELECT * FROM v_job_health")).mappings():
        run = dict(row)
        run["minutes_since"] = float(run["minutes_since"])
        rows.append(run)
    return rows


def overdue_messages(
    session: Session, older_than_minutes: int = OVERDUE_AFTER_MINUTES
) -> list[dict]:
    return [
        dict(row)
        for row in session.execute(
            text("SELECT * FROM v_overdue_messages WHERE minutes_late >= :m"),
            {"m": older_than_minutes},
        ).mappings()
    ]


def backup_status(session: Session) -> dict:
    """When the last backup ran, and whether it was any good.

    A backup cron that stopped is discovered at restore time, which is the
    worst possible moment to discover anything. backup.sh already refuses to
    write an unencrypted dump and verifies what it wrote -- but a script that
    is not being run verifies nothing.

    A failed backup counts as no backup. "It ran" is not the question.
    """
    health = {row["job_name"]: row for row in job_health(session)}
    backup = health.get("backup")

    if backup is None:
        return {
            "state": "unknown",
            "reason": "no backup has ever recorded a run",
            "hours_ago": None,
        }

    hours = backup["minutes_since"] / 60.0
    if backup["last_ok"] is False:
        state = "failing"
        reason = f"the last backup failed: {backup['last_error']}"
    elif backup["minutes_since"] > BACKUP_STALE_AFTER_MINUTES:
        state = "stale"
        reason = (
            f"the last successful backup was {hours:.0f} hours ago; "
            f"backups are meant to run every {BACKUP_INTERVAL_HOURS}"
        )
    else:
        state = "ok"
        reason = f"last backup {hours:.0f} hours ago, verified"

    return {"state": state, "reason": reason, "hours_ago": round(hours, 1)}


def messaging_status(session: Session) -> dict:
    """One answer to "is anything reaching people".

    Reported rather than turned into an HTTP failure. A stalled cron does not
    mean this web container is unwell, and returning 503 would have an
    orchestrator restart the one part that is working.
    """
    health = {row["job_name"]: row for row in job_health(session)}
    dispatcher = health.get("dispatch_messages")
    backlog = overdue_messages(session)

    if dispatcher is None:
        state, reason = "unknown", "the dispatcher has never recorded a run"
    elif dispatcher["minutes_since"] > STALE_AFTER_MINUTES:
        state = "stalled"
        reason = (
            f"the dispatcher last ran {dispatcher['minutes_since']:.0f} minutes "
            f"ago; it is meant to run every {DISPATCH_INTERVAL_MINUTES}"
        )
    elif dispatcher["last_ok"] is False:
        state, reason = "failing", f"the last run failed: {dispatcher['last_error']}"
    elif backlog:
        state = "behind"
        reason = (
            f"{len(backlog)} message(s) are more than "
            f"{OVERDUE_AFTER_MINUTES} minutes late"
        )
    else:
        state, reason = "ok", "messages are moving"

    return {
        "state": state,
        "reason": reason,
        "overdue": len(backlog),
        "last_run_minutes_ago": dispatcher["minutes_since"] if dispatcher else None,
    }
