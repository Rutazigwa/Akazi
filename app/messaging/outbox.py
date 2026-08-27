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

import dataclasses

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.clock import KIGALI as _KIGALI
from app.messaging.providers import MessageProvider, RecordingProvider

logger = logging.getLogger("akazi.messaging")

# One definition, in app/clock.py, so the offset cannot drift between modules.
KIGALI = _KIGALI
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
    # Moved from WhatsApp to SMS and requeued -- neither sent nor failed yet.
    fell_back: int = 0

    def __str__(self) -> str:
        return (
            f"sent={self.sent} failed={self.failed} "
            f"suppressed={self.suppressed} deferred={self.deferred} "
            f"fell_back={self.fell_back}"
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


def preferred_channel(session: Session, candidate_id: UUID | None) -> str:
    """WhatsApp where we know they can receive it, SMS otherwise.

    has_smartphone is asked at registration and was never used. A meaningful
    share of this cohort is on low-storage handsets where WhatsApp is not
    installed -- sending an offer they cannot receive is the same as not
    telling them the work exists.

    Employer contacts default to WhatsApp; they are on laptops and good phones
    by assumption, and the blueprint rules out anything more for them.
    """
    if candidate_id is None:
        return "whatsapp"
    has_smartphone = session.execute(
        text("SELECT has_smartphone FROM candidates WHERE candidate_id = :cid"),
        {"cid": str(candidate_id)},
    ).scalar_one_or_none()
    return "whatsapp" if has_smartphone else "sms"


def queue(
    session: Session,
    *,
    template_key: str,
    body: str,
    candidate_id: UUID | None = None,
    contact_id: UUID | None = None,
    staff_id: UUID | None = None,
    placement_id: UUID | None = None,
    channel: str | None = None,
    scheduled_for: datetime | None = None,
) -> UUID | None:
    """Add a message to the outbox.

    Returns None when an identical once-only message already exists. Queueing
    the same shift reminder twice because a coordinator clicked twice should be
    a no-op, not two messages that make us look disorganised.
    """
    recipients = [r for r in (candidate_id, contact_id, staff_id) if r]
    if len(recipients) != 1:
        raise MessagingError("a message needs exactly one recipient")

    if channel is None:
        # Staff get SMS: an internal alert must not depend on someone having
        # WhatsApp open, and this is the path used when a response time has
        # already been missed.
        channel = "sms" if staff_id else preferred_channel(session, candidate_id)

    row = session.execute(
        text(
            """
            INSERT INTO messages (candidate_id, contact_id, staff_id,
                                  placement_id, channel, template_key, body,
                                  scheduled_for)
            VALUES (:candidate_id, :contact_id, :staff_id, :placement_id,
                    CAST(:channel AS message_channel), :template_key, :body,
                    COALESCE(:scheduled_for, clock_timestamp()))
            ON CONFLICT DO NOTHING
            RETURNING message_id
            """
        ),
        {
            "candidate_id": str(candidate_id) if candidate_id else None,
            "contact_id": str(contact_id) if contact_id else None,
            "staff_id": str(staff_id) if staff_id else None,
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
        # Quiet hours exist so we do not wake a worker at 23:00 about a shift.
        # They are not a reason to sit on an internal alert: a harassment
        # escalation that missed its response time at 22:00 must reach the
        # owner at 22:00, and staff are on duty in a way candidates are not.
        deferred = session.execute(
            text(
                """
                UPDATE messages SET scheduled_for = :next
                 WHERE status = 'queued' AND scheduled_for <= :now
                   AND staff_id IS NULL
                """
            ),
            {"next": next_send_window(moment), "now": moment},
        ).rowcount
        # The report is frozen, so the deferral count is folded in rather
        # than added to it.
        internal = _dispatch_due(session, provider, limit, moment, staff_only=True)
        return dataclasses.replace(internal, deferred=internal.deferred + deferred)

    return _dispatch_due(session, provider, limit, moment)


def _dispatch_due(
    session: Session,
    provider,
    limit: int,
    moment: datetime,
    staff_only: bool = False,
) -> DispatchReport:
    """Send the messages that are due, optionally only the internal ones."""

    due = session.execute(
        text(
            """
            SELECT message_id, candidate_id, contact_id, channel::text AS channel,
                   body, attempts, template_key
              FROM messages
             WHERE status = 'queued' AND scheduled_for <= :now
               AND (NOT :staff_only OR staff_id IS NOT NULL)
             ORDER BY scheduled_for
             LIMIT :limit
             FOR UPDATE SKIP LOCKED
            """
        ),
        {"now": moment, "limit": limit, "staff_only": staff_only},
    ).mappings().all()

    report = DispatchReport()
    for message in due:
        outcome = _dispatch_one(session, provider, message, moment)
        report = DispatchReport(
            sent=report.sent + (outcome == "sent"),
            failed=report.failed + (outcome == "failed"),
            suppressed=report.suppressed + (outcome == "suppressed"),
            deferred=report.deferred,
            fell_back=report.fell_back + (outcome == "fell_back"),
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

    # The blueprint's SMS fallback. A permanent WhatsApp failure usually means
    # the number is not on WhatsApp, which SMS does not care about -- so the
    # message goes back in the queue on the other channel rather than being
    # written off. Once, because a message already on SMS has nowhere left to
    # fall back to.
    if exhausted and message["channel"] == "whatsapp":
        session.execute(
            text(
                """
                UPDATE messages
                   SET channel = 'sms', attempts = 0, status = 'queued',
                       scheduled_for = :now, last_attempt_at = :now,
                       last_error = :error
                 WHERE message_id = :mid
                """
            ),
            {
                "mid": str(message_id), "now": moment,
                "error": f"whatsapp failed ({result.error}); falling back to SMS",
            },
        )
        logger.info(
            "message %s falling back to SMS after whatsapp failure: %s",
            message_id, result.error,
        )
        return "fell_back"

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


def record_delivery(
    session: Session, provider_ref: str, delivered: bool,
    error: str | None = None,
) -> bool:
    """Apply a provider's delivery receipt. Returns False if we do not know it.

    'Sent' means the provider accepted it; 'delivered' means it reached the
    handset. The gap between them is where a wrong number or a phone that has
    been off for a week lives -- and a shift reminder that was accepted but
    never delivered is indistinguishable from one that arrived, unless this is
    recorded.
    """
    updated = session.execute(
        text(
            """
            UPDATE messages
               SET status = CASE WHEN :delivered THEN 'delivered'::message_status
                                 ELSE 'failed'::message_status END,
                   delivered_at = CASE WHEN :delivered THEN clock_timestamp() END,
                   last_error = COALESCE(:error, last_error)
             WHERE provider_ref = :ref
               AND status IN ('sent','delivered','failed')
            RETURNING message_id
            """
        ),
        {"ref": provider_ref, "delivered": delivered, "error": error},
    ).scalar_one_or_none()
    return updated is not None


def undelivered(session: Session, older_than_minutes: int = 60) -> list[dict]:
    """Accepted by the provider, never confirmed as delivered.

    Worth a coordinator's attention on anything time-critical: a shift
    reminder in this state may simply not have arrived.
    """
    rows = session.execute(
        text(
            """
            SELECT m.message_id, m.template_key, m.channel::text AS channel,
                   m.sent_at, c.display_name
              FROM messages m
              LEFT JOIN candidates c ON c.candidate_id = m.candidate_id
             WHERE m.status = 'sent'
               AND m.sent_at < now() - make_interval(mins => :mins)
             ORDER BY m.sent_at
            """
        ),
        {"mins": older_than_minutes},
    ).mappings()
    return [dict(r) for r in rows]


def outbox_summary(session: Session) -> list[dict]:
    rows = session.execute(
        text(
            "SELECT status::text AS status, count(*) AS n FROM messages "
            "GROUP BY status ORDER BY status"
        )
    ).mappings()
    return [dict(r) for r in rows]
