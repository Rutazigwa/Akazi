"""Message transports.

The dispatcher does not know or care which of these it is holding. That matters
because the WhatsApp Business API cannot be exercised without credentials and a
number in good standing, so the pilot has to be able to run end to end with a
provider that records instead of sends.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("akazi.messaging")


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_ref: str | None = None
    error: str | None = None
    # Whether trying again could plausibly work. A network blip is retryable;
    # "this number is not on WhatsApp" is not, and retrying it forever just
    # buries the real failures.
    retryable: bool = False


class MessageProvider(Protocol):
    name: str

    def send(self, phone: str, body: str, channel: str) -> SendResult: ...


class RecordingProvider:
    """Records instead of sending. The default, and not only for tests.

    A pilot can run its whole first week on this: every message is written to
    the outbox and logged, so the operator can read exactly what would have
    gone out before any of it reaches a real person. Switching to a live
    provider is a configuration change, not a code change.
    """

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, phone: str, body: str, channel: str) -> SendResult:
        self.sent.append((phone, body, channel))
        # Truncated: message bodies can carry pay figures and shift locations,
        # and application logs are not held to the same standard as the
        # database. The outbox row has the full text.
        logger.info(
            "would send %s to %s: %.60s%s",
            channel, _mask(phone), body.replace("\n", " "),
            "…" if len(body) > 60 else "",
        )
        return SendResult(ok=True, provider_ref=f"recording:{len(self.sent)}")


class FailingProvider:
    """Fails every send. For exercising retry and backoff."""

    name = "failing"

    def __init__(self, retryable: bool = True, error: str = "provider down"):
        self.retryable = retryable
        self.error = error
        self.attempts = 0

    def send(self, phone: str, body: str, channel: str) -> SendResult:
        self.attempts += 1
        return SendResult(ok=False, error=self.error, retryable=self.retryable)


def _mask(phone: str) -> str:
    """Keep enough to identify a number in a log without printing it."""
    return phone[:6] + "…" + phone[-2:] if len(phone) > 8 else "…"


# A live WhatsApp Business / SMS provider goes here. It is deliberately not
# written yet: the API shape depends on which BSP the operation signs with, and
# guessing would produce code that looks finished and has never run. What is
# needed from it is exactly the MessageProvider protocol above -- one method,
# returning SendResult with a retryable flag.
