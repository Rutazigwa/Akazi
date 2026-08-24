"""Queueing and dispatching messages.

Queueing happens inside the transaction that caused it. Dispatch happens
separately, so a failed send is a row to retry rather than a lost message.

The dispatcher applies three guards before anything leaves:

  consent      a candidate with no current placement consent is not messaged
  quiet hours  nothing between 21:00 and 07:00 Kigali time
  recipient    no phone on file, or an erased record, means suppressed

Suppressed is a distinct outcome from failed. A message nobody can receive is
not a fault to retry -- it is a fact about the recipient, and burying it in the
failure count hides the real errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.messaging.providers import MessageProvider, RecordingProvider

logger = logging.getLogger("akazi.messaging")

# Kigali is UTC+2 year-round -- no daylight saving to track.
KIGALI = timezone(timedelta(hours=2))
QUIET_FROM = time(21, 0)
QUIET_UNTIL = time(7, 0)

MAX_ATTEMPTS = 5
# Backoff between attempts, in minutes. A person who did not get a shift
# reminder needs it within the hour, not eventually.
BACKOFF_MINUTES = [1, 5, 15, 60, 180]


class MessagingError(Exception):
    pass


@dataclass(frozen=True)
class DispatchReport:
    sent: int = 0
    failed: int = 0
    suppressed: int = 0
    deferred: int = 0

    def __str__(self) -> str:
        return (
            f"sent={self.sent} failed={self.failed} "
            f"suppressed={self.suppressed} deferred={self.deferred}"
        )


def in_quiet_hours(moment: datetime | None = None) -> bool:
    """Nothing lands on someone's phone at 03:00 because a cron job woke up."""
    local = (moment or datetime.now(timezone.utc)).astimezone(KIGALI).time()
    return local >= QUIET_FROM or local < QUIET_UNTIL


def next_send_window(moment: datetime | None = None) -> datetime:
    """The next moment outside quiet hours."""
    now = (moment or datetime.now(timezone.utc)).astimezone(KIGALI)
    if not in_quiet_hours(now):
        return now.astimezone(timezone.utc)
    target = now.date() if now.time() < QUIET_UNTIL else now.date() + timedelta(days=1)
    return datetime.combine(target, QUIET_UNTIL, tzinfo=KIGALI).astimezone(
        timezone.utc
    )


def queue(
    session: Session,
    *,
    template_key: str,
    body: str,
    candidate_id: UUID | None = None,
    contact_id: UUID | None = None,
    placement_id: UUID | None = None,
    channel: str = "whatsapp",
    scheduled_for: datetime | None = None,
) -> UUID | None:
    """Add a message to the outbox.

    Returns None when an identical once-only message already exists. Queueing
    the same shift reminder twice because a coordinator clicked twice should be
    a no-op, not two messages that make us look disorganised.
    """
    if (candidate_id is None) == (contact_id is None):
        raise MessagingError("a message needs exactly one recipient")

    row = session.execute(
        text(
            """
            INSERT INTO messages (candidate_id, contact_id, placement_id,
                                  channel, template_key, body, scheduled_for)
            VALUES (:candidate_id, :contact_id, :placement_id,
                    CAST(:channel AS message_channel), :template_key, :body,
                    COALESCE(:scheduled_for, clock_timestamp()))
            ON CONFLICT DO NOTHING
            RETURNING message_id
            """
        ),
        {
            "candidate_id": str(candidate_id) if candidate_id else None,
            "contact_id": str(contact_id) if contact_id else None,
            "placement_id": str(placement_id) if placement_id else None,
            "channel": channel,
            "template_key": template_key,
            "body": body,
            "scheduled_for": scheduled_for,
        },
    ).first()
    return row[0] if row else None


def cancel_for_placement(
    session: Session, placement_id: UUID, reason: str
) -> int:
    """Stop queued messages for a placement that is no longer happening.

    A shift reminder for a placement that was declined or replaced is worse
    than no reminder: it sends someone to a job that is not theirs.
    """
    return session.execute(
        text(
            """
            UPDATE messages
               SET status = 'cancelled', last_error = :reason
             WHERE placement_id = :pid AND status = 'queued'
            """
        ),
        {"pid": str(placement_id), "reason": reason},
    ).rowcount


def _has_consent(session: Session, candidate_id: UUID) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT granted FROM v_current_consent "
                "WHERE candidate_id = :cid AND purpose = 'placement'"
            ),
            {"cid": str(candidate_id)},
        ).scalar_one_or_none()
    )


def dispatch(
    session: Session,
    provider: MessageProvider | None = None,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> DispatchReport:
    """Send what is due. Safe to run repeatedly; safe to run on a cron."""
    provider = provider or RecordingProvider()
    moment = now or datetime.now(timezone.utc)

    if in_quiet_hours(moment):
        deferred = session.execute(
            text(
                """
                UPDATE messages SET scheduled_for = :next
                 WHERE status = 'queued' AND scheduled_for <= :now
                """
            ),
            {"next": next_send_window(moment), "now": moment},
        ).rowcount
        return DispatchReport(deferred=deferred)

    due = session.execute(
        text(
            """
            SELECT message_id, candidate_id, contact_id, channel::text AS channel,
                   body, attempts, template_key
              FROM messages
             WHERE status = 'queued' AND scheduled_for <= :now
             ORDER BY scheduled_for
             LIMIT :limit
             FOR UPDATE SKIP LOCKED
            """
        ),
        {"now": moment, "limit": limit},
    ).mappings().all()

    report = DispatchReport()
    for message in due:
        outcome = _dispatch_one(session, provider, message, moment)
        report = DispatchReport(
            sent=report.sent + (outcome == "sent"),
            failed=report.failed + (outcome == "failed"),
            suppressed=report.suppressed + (outcome == "suppressed"),
            deferred=report.deferred,
        )
    return report


def _suppress(session: Session, message_id: UUID, reason: str) -> str:
    session.execute(
        text(
            "UPDATE messages SET status = 'suppressed', last_error = :reason "
            "WHERE message_id = :mid"
        ),
        {"mid": str(message_id), "reason": reason},
    )
    return "suppressed"


def _dispatch_one(
    session: Session, provider: MessageProvider, message, moment: datetime
) -> str:
    from app.messaging.templates import TEMPLATES

    message_id = message["message_id"]

    template = TEMPLATES.get(message["template_key"])
    if (
        template is not None
        and template.requires_consent
        and message["candidate_id"] is not None
        and not _has_consent(session, message["candidate_id"])
    ):
        return _suppress(session, message_id, "no current placement consent")

    recipient = session.execute(
        text("SELECT phone, is_candidate FROM message_recipient_phone(:mid)"),
        {"mid": str(message_id)},
    ).mappings().first()

    if recipient is None or not recipient["phone"]:
        return _suppress(session, message_id, "no phone number on file")

    result = provider.send(recipient["phone"], message["body"], message["channel"])

    if result.ok:
        session.execute(
            text(
                """
                UPDATE messages
                   SET status = 'sent', sent_at = :now, attempts = attempts + 1,
                       last_attempt_at = :now, provider_ref = :ref,
                       last_error = NULL
                 WHERE message_id = :mid
                """
            ),
            {"mid": str(message_id), "now": moment, "ref": result.provider_ref},
        )
        return "sent"

    attempts = message["attempts"] + 1
    exhausted = not result.retryable or attempts >= MAX_ATTEMPTS
    if exhausted:
        session.execute(
            text(
                """
                UPDATE messages
                   SET status = 'failed', attempts = :attempts,
                       last_attempt_at = :now, last_error = :error
                 WHERE message_id = :mid
                """
            ),
            {
                "mid": str(message_id), "attempts": attempts,
                "now": moment, "error": result.error,
            },
        )
        logger.warning(
            "message %s failed permanently after %s attempt(s): %s",
            message_id, attempts, result.error,
        )
        return "failed"

    backoff = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
    session.execute(
        text(
            """
            UPDATE messages
               SET attempts = :attempts, last_attempt_at = :now,
                   last_error = :error,
                   scheduled_for = :now + CAST(:backoff AS interval)
             WHERE message_id = :mid
            """
        ),
        {
            "mid": str(message_id), "attempts": attempts, "now": moment,
            "error": result.error, "backoff": f"{backoff} minutes",
        },
    )
    return "failed"


def outbox_summary(session: Session) -> list[dict]:
    rows = session.execute(
        text(
            "SELECT status::text AS status, count(*) AS n FROM messages "
            "GROUP BY status ORDER BY status"
        )
    ).mappings()
    return [dict(r) for r in rows]
