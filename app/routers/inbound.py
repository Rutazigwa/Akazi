"""Provider webhook for inbound messages, and the escalation queue.

The webhook is the one endpoint reachable without a staff session, because the
messaging provider has no account here. It is protected by a shared secret
instead, compared in constant time.

That is weaker than the rest of the system and it is worth being clear about
why it is acceptable: the endpoint only stores a message for later handling. It
cannot read anything, and an attacker who forges one gets a row in a queue that
a coordinator will look at. Handling happens separately, where the sender is
resolved against numbers we already hold.
"""

from __future__ import annotations

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import SessionDep, StaffDep
from app.messaging.inbound import (
    handle,
    handle_pending,
    needs_attention,
    record_inbound,
)
from app.messaging.outbox import record_delivery, undelivered
from app.operations.escalations import (
    EscalationError,
    acknowledge,
    open_escalations,
    resolve,
    response_performance,
)

router = APIRouter(tags=["inbound"])


class InboundPayload(BaseModel):
    from_phone: str = Field(max_length=20)
    body: str = Field(max_length=4000)
    channel: str = Field(default="whatsapp", pattern="^(whatsapp|sms)$")
    provider_ref: str | None = Field(default=None, max_length=120)


class DeliveryReceipt(BaseModel):
    provider_ref: str = Field(max_length=120)
    delivered: bool
    error: str | None = Field(default=None, max_length=500)


class Resolution(BaseModel):
    resolution: str = Field(min_length=1)
    no_action: bool = False


@router.post("/webhooks/inbound", status_code=202)
def inbound_webhook(
    body: InboundPayload,
    session: SessionDep,
    x_webhook_secret: Annotated[str | None, Header()] = None,
):
    settings = get_settings()
    if not settings.inbound_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound webhook is not configured",
        )
    if not x_webhook_secret or not secrets.compare_digest(
        x_webhook_secret, settings.inbound_webhook_secret
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid webhook secret")

    inbound_id = record_inbound(
        session, body.from_phone, body.body, body.channel, body.provider_ref
    )
    if inbound_id is None:
        # A duplicate delivery. Providers retry; acting twice would decline an
        # offer twice or raise two escalations for one report.
        return {"accepted": True, "duplicate": True}

    return {"accepted": True, "outcome": handle(session, inbound_id)}


@router.post("/webhooks/delivery", status_code=202)
def delivery_webhook(
    body: DeliveryReceipt,
    session: SessionDep,
    x_webhook_secret: Annotated[str | None, Header()] = None,
):
    """A provider reporting whether a message reached the handset.

    Same shared secret as the inbound webhook, and the same reasoning: this
    endpoint only annotates a message we already sent. An unknown reference is
    reported back rather than guessed at -- silently accepting receipts for
    messages we have no record of would make the delivery numbers meaningless.
    """
    settings = get_settings()
    if not settings.inbound_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="delivery webhook is not configured",
        )
    if not x_webhook_secret or not secrets.compare_digest(
        x_webhook_secret, settings.inbound_webhook_secret
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid webhook secret")

    known = record_delivery(
        session, body.provider_ref, body.delivered, body.error
    )
    return {"accepted": True, "known_message": known}


@router.get("/messages/undelivered")
def list_undelivered(session: SessionDep, staff: StaffDep,
                     older_than_minutes: int = 60):
    """Accepted by the provider, never confirmed as reaching the handset."""
    return {"undelivered": undelivered(session, older_than_minutes)}


@router.post("/inbound/process")
def process_pending(session: SessionDep, staff: StaffDep):
    return {"handled": handle_pending(session)}


@router.get("/inbound/attention")
def attention(session: SessionDep, staff: StaffDep):
    """Replies nobody could interpret. A short queue, not a dead letter box."""
    return {"messages": needs_attention(session)}


@router.get("/escalations")
def list_escalations(session: SessionDep, staff: StaffDep):
    return {"open": open_escalations(session)}


@router.get("/escalations/performance")
def performance(session: SessionDep, staff: StaffDep):
    """Whether we met our own response times. Measured, or it decays."""
    return {"by_kind": response_performance(session)}


@router.post("/escalations/{escalation_id}/acknowledge")
def do_acknowledge(
    escalation_id: UUID, session: SessionDep, staff: StaffDep
):
    try:
        acknowledge(session, escalation_id, staff.staff_id)
    except EscalationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"escalation_id": escalation_id, "status": "acknowledged"}


@router.post("/escalations/{escalation_id}/resolve")
def do_resolve(
    escalation_id: UUID,
    body: Resolution,
    session: SessionDep,
    staff: StaffDep,
):
    try:
        resolve(
            session, escalation_id, staff.staff_id, body.resolution,
            body.no_action,
        )
    except EscalationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"escalation_id": escalation_id, "status": "resolved"}
