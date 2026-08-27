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
    return [
        dict(row)
        for row in session.execute(text("SELECT * FROM v_job_health")).mappings()
    ]


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
