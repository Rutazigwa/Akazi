"""Reading replies.

The outbound templates ask people to reply. Until this existed, nothing read
those replies -- which is worse than not asking, because it teaches a worker
that the channel is decorative, and the one time it matters they will not use
it.

Parsing is deliberately conservative. Anything not clearly understood is left
unhandled for a coordinator rather than guessed at: acting on a misread reply
means cancelling someone's work or ignoring a report of harassment, and both
are worse than a short queue.

Kinyarwanda and French keywords are included alongside English. A worker
replying "YEGO" has answered the question; failing to understand it is our
problem, not theirs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("akazi.messaging")

# Whole-word matches, so "notice" does not read as "no".
AFFIRMATIVE = {"yes", "y", "ok", "okay", "yego", "oui", "sawa"}
NEGATIVE = {"no", "n", "oya", "non", "cancel"}
STOP_WORDS = {"stop", "unsubscribe", "hagarika", "arret", "arreter"}

# Phrases where a negative word means the opposite of a decline. "No problem"
# is someone saying they are fine; reading it as a refusal would cancel their
# work. Checked before the word sets, and they win.
BENIGN_NEGATIVES = (
    "no problem", "no problems", "no issue", "no issues", "no complaint",
    "no complaints", "nta kibazo", "nta bibazo", "pas de probleme",
    "pas de problème", "all good", "all is well", "everything is fine",
)

# Multi-word reports the single-word sets would miss. Patterns rather than
# literal phrases, because tense and phrasing vary and a missed harassment
# report is the worst failure this system has: "touched me", "keeps touching
# me" and "he touches me" are one report written three ways.
#
# Deliberately broad. A false positive costs a coordinator two minutes reading
# a message; a false negative costs someone a response to being assaulted.
ISSUE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("harassment", (
        r"\btouch\w*\s+(me|her|him)\b",
        r"\bshout\w*\s+at\s+(me|her|him)\b",
        r"\byell\w*\s+at\s+(me|her|him)\b",
        r"\bfollow\w*\s+(me|her|him)\b",
        r"\bgrab\w*\s+(me|her|him)\b",
        r"\b(wont|won't|would\s+not|will\s+not)\s+leave\s+me\b",
        r"\bmade?\s+me\s+(feel\s+)?(uncomfortable|afraid|scared)\b",
        r"\bsexual\w*\b",
        r"\binappropriate\b",
    )),
    ("safety", (
        r"\bnot\s+safe\b", r"\bno\s+safety\b",
        r"\b(got|was|am|been)\s+(hurt|injured)\b",
        r"\bno\s+(gloves|helmet|boots|mask)\b",
    )),
    ("pay", (
        r"\bnot\s+(been\s+)?paid\b", r"\bno\s+pay\b",
        r"\b(did|has|have|hasnt|havent)\s*n[o']?t\s+pa[iy]d?\b",
        r"\bstill\s+waiting\s+for\b",
        r"\b(paid|gave)\s+(me\s+)?less\b", r"\bunderpaid\b",
        r"\bshort\s*paid\b",
    )),
    ("transport", (
        r"\btoo\s+far\b", r"\bcan'?not?\s+afford\b",
        r"\bfare\s+is\s+too\b", r"\bno\s+transport\b",
    )),
    ("hours", (
        r"\btoo\s+many\s+hours\b", r"\bno\s+break\b",
        r"\bworking\s+late\b", r"\bextra\s+hours\b",
    )),
]

# Reported problems. Ordered by severity: a message mentioning both harassment
# and transport is a harassment report.
ISSUE_KEYWORDS: list[tuple[str, set[str]]] = [
    ("harassment", {
        "harassment", "harassed", "harass", "abuse", "abused", "assault",
        "touched", "ihohoterwa", "gukorwaho", "harcelement", "harcèlement",
    }),
    ("safety", {"unsafe", "danger", "dangerous", "injured", "injury",
                "umutekano", "accident"}),
    ("pay", {"unpaid", "notpaid", "salary", "wages", "owed", "amafaranga",
             "sinahembwe", "paiement"}),
    ("transport", {"transport", "fare", "moto", "bus", "gutwara"}),
    ("hours", {"hours", "overtime", "late", "amasaha"}),
]


@dataclass(frozen=True)
class Interpretation:
    intent: str | None
    issue_kind: str | None = None


def interpret(body: str) -> Interpretation:
    """Work out what a reply meant, or admit we could not tell.

    Order matters and is deliberate:

      1. reported problems, phrases first  -- "yes but he touched me" is a report
      2. benign negatives                  -- "no problem" is not a refusal
      3. opt-out, then yes, then no

    Anything unmatched returns no intent, which queues it for a human. Guessing
    would cancel work or ignore a report of harassment.
    """
    normalised = " ".join(body.strip().lower().split())
    words = set(re.findall(r"[a-zà-ÿ]+", normalised))

    # An issue beats a yes/no: "yes but he shouted at me" is a report.
    for kind, patterns in ISSUE_PATTERNS:
        if any(re.search(pattern, normalised) for pattern in patterns):
            return Interpretation("issue_reported", kind)
    for kind, keywords in ISSUE_KEYWORDS:
        if words & keywords:
            return Interpretation("issue_reported", kind)

    # "No problem" is someone saying they are fine.
    if any(phrase in normalised for phrase in BENIGN_NEGATIVES):
        return Interpretation("affirmative")

    if words & STOP_WORDS:
        return Interpretation("opt_out")
    if words & AFFIRMATIVE:
        return Interpretation("affirmative")
    if words & NEGATIVE:
        return Interpretation("negative")
    return Interpretation(None)


def record_inbound(
    session: Session,
    from_phone: str,
    body: str,
    channel: str = "whatsapp",
    provider_ref: str | None = None,
) -> UUID | None:
    """Store an incoming message. Returns None if it is a duplicate delivery.

    Providers retry webhooks, so the same reply can arrive more than once.
    Acting on a duplicate would decline an offer twice or raise two escalations
    for one report.
    """
    row = session.execute(
        text(
            """
            -- The phone parameter is both an inserted value and a comparison
            -- operand, so it is cast once explicitly: without it PostgreSQL
            -- deduces text in one place and varchar in the other and refuses.
            WITH incoming AS (SELECT CAST(:phone AS varchar(20)) AS phone)
            INSERT INTO inbound_messages (from_phone, channel, body, provider_ref,
                                          candidate_id, contact_id)
            SELECT incoming.phone, CAST(:channel AS message_channel), :body, :ref,
                   (SELECT ci.candidate_id FROM candidate_identity ci
                     WHERE ci.phone_primary = incoming.phone
                       AND ci.erased_at IS NULL LIMIT 1),
                   (SELECT ec.contact_id FROM employer_contacts ec
                     WHERE ec.phone = incoming.phone AND ec.is_active LIMIT 1)
              FROM incoming
            ON CONFLICT (provider_ref) DO NOTHING
            RETURNING inbound_id
            """
        ),
        {"phone": from_phone, "channel": channel, "body": body,
         "ref": provider_ref},
    ).first()
    return row[0] if row else None


def handle(session: Session, inbound_id: UUID) -> str:
    """Interpret one stored reply and act on it. Returns what was done."""
    from app.operations.escalations import EscalationError, raise_escalation

    message = session.execute(
        text(
            "SELECT inbound_id, from_phone, body, candidate_id, contact_id "
            "FROM inbound_messages WHERE inbound_id = :iid AND handled_at IS NULL"
        ),
        {"iid": str(inbound_id)},
    ).mappings().first()
    if message is None:
        return "already handled"

    reading = interpret(message["body"])

    if message["candidate_id"] is None and message["contact_id"] is None:
        return _finish(
            session, inbound_id, reading.intent,
            "from a number we do not recognise -- needs a human",
            handled=False,
        )

    candidate_id = message["candidate_id"]

    if reading.intent == "issue_reported" and candidate_id:
        placement_id = _latest_placement(session, candidate_id)
        try:
            raise_escalation(
                session,
                reading.issue_kind,
                candidate_id=candidate_id,
                placement_id=placement_id,
                inbound_id=inbound_id,
                detail=message["body"],
            )
        except EscalationError as exc:
            # Never swallow this. An unraisable escalation must stay visible.
            return _finish(
                session, inbound_id, reading.intent,
                f"could not raise escalation: {exc}", handled=False,
            )
        return _finish(
            session, inbound_id, reading.intent,
            f"{reading.issue_kind} escalation raised",
        )

    if reading.intent == "opt_out" and candidate_id:
        _withdraw_consent(session, candidate_id, inbound_id)
        return _finish(
            session, inbound_id, reading.intent,
            "placement consent withdrawn; messaging stopped",
        )

    if reading.intent in ("affirmative", "negative") and candidate_id:
        placement_id = _pending_offer(session, candidate_id)
        if placement_id is not None:
            from app.operations.requests import RequestError, respond_to_offer

            try:
                respond_to_offer(
                    session, placement_id, reading.intent == "affirmative"
                )
            except RequestError as exc:
                return _finish(
                    session, inbound_id, reading.intent, str(exc), handled=False
                )
            return _finish(
                session, inbound_id, reading.intent,
                f"offer {'accepted' if reading.intent == 'affirmative' else 'declined'}",
            )
        # A yes or no with no offer outstanding is probably a check-in answer.
        # A coordinator confirms it -- guessing which checkpoint it belongs to
        # would corrupt the retention numbers.
        return _finish(
            session, inbound_id, reading.intent,
            "reply with no offer outstanding -- likely a check-in answer",
            handled=False,
        )

    return _finish(
        session, inbound_id, reading.intent, "could not interpret", handled=False
    )


def _finish(
    session: Session, inbound_id: UUID, intent: str | None, note: str,
    handled: bool = True,
) -> str:
    session.execute(
        text(
            """
            UPDATE inbound_messages
               SET intent = :intent, handling_note = :note,
                   handled_at = CASE WHEN :handled THEN clock_timestamp() END
             WHERE inbound_id = :iid
            """
        ),
        {"iid": str(inbound_id), "intent": intent, "note": note,
         "handled": handled},
    )
    return note


def _latest_placement(session: Session, candidate_id: UUID):
    return session.execute(
        text(
            "SELECT placement_id FROM placements WHERE candidate_id = :cid "
            "ORDER BY offered_at DESC LIMIT 1"
        ),
        {"cid": str(candidate_id)},
    ).scalar_one_or_none()


def _pending_offer(session: Session, candidate_id: UUID):
    return session.execute(
        text(
            "SELECT placement_id FROM placements "
            "WHERE candidate_id = :cid AND status = 'offered' "
            "ORDER BY offered_at DESC LIMIT 1"
        ),
        {"cid": str(candidate_id)},
    ).scalar_one_or_none()


def _withdraw_consent(session: Session, candidate_id: UUID, inbound_id: UUID):
    """A STOP is a withdrawal of consent, recorded as an append-only row.

    captured_by is null: the candidate did this, not a staff member, and
    attributing it to whoever happened to run the dispatcher would be false.
    """
    session.execute(
        text(
            """
            INSERT INTO consent_records (candidate_id, policy_version, purpose,
                                         granted, captured_via, captured_by)
            VALUES (:cid, :version, 'placement', false, 'whatsapp', NULL)
            """
        ),
        {"cid": str(candidate_id), "version": _current_policy_version()},
    )
    session.execute(
        text(
            "UPDATE messages SET status = 'cancelled', "
            "    last_error = 'recipient opted out' "
            "WHERE candidate_id = :cid AND status = 'queued'"
        ),
        {"cid": str(candidate_id)},
    )


def _current_policy_version() -> str:
    from app.operations.registry import CURRENT_CONSENT_VERSION

    return CURRENT_CONSENT_VERSION


def handle_pending(session: Session, limit: int = 100) -> dict[str, int]:
    """Work through unhandled inbound messages."""
    pending = session.execute(
        text(
            "SELECT inbound_id FROM inbound_messages WHERE handled_at IS NULL "
            "ORDER BY received_at LIMIT :limit"
        ),
        {"limit": limit},
    ).scalars().all()

    results: dict[str, int] = {}
    for inbound_id in pending:
        note = handle(session, inbound_id)
        results[note] = results.get(note, 0) + 1
    return results


def needs_attention(session: Session) -> list[dict]:
    """Replies a human has to look at."""
    rows = session.execute(
        text(
            """
            SELECT i.inbound_id, i.from_phone, i.body, i.received_at,
                   i.intent, i.handling_note, c.display_name
              FROM inbound_messages i
              LEFT JOIN candidates c ON c.candidate_id = i.candidate_id
             WHERE i.handled_at IS NULL
             ORDER BY i.received_at
            """
        )
    ).mappings()
    return [dict(r) for r in rows]
