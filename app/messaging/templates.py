"""Message templates.

Kept in code rather than the database so they are versioned with the
application and reviewed like any other change. What went out is recorded on
the message row -- rendering a template later with today's data is not evidence
of what someone was told.

Two rules run through all of them:

**Pay and transport are stated before acceptance.** That is a legal
requirement, not a courtesy, and the offer message is where it is met. The
figure quoted is net of the estimated fare, because gross pay is not what the
person takes home.

**Short.** These land on low-storage phones over patchy connections and are
often read on a lock screen. Anything that needs a second screen has failed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    key: str
    # Whether this needs a consent record for the placement purpose. Operational
    # messages to a placed worker do; nothing here is marketing.
    requires_consent: bool
    text: str

    def render(self, **values) -> str:
        return self.text.format(**values).strip()


TEMPLATES: dict[str, Template] = {
    "placement_offer": Template(
        key="placement_offer",
        requires_consent=True,
        text=(
            "Akazi: work offer from {business_name}.\n"
            "{title} on {starts_on}{shift}.\n"
            "Pay: RWF {pay_rwf} per {pay_unit}.\n"
            "{transport_line}\n"
            "Reply YES to accept or NO to decline. No fee to apply."
        ),
    ),
    "shift_reminder": Template(
        key="shift_reminder",
        requires_consent=True,
        text=(
            "Akazi: reminder — {title} at {business_name} tomorrow"
            "{shift}.\n"
            "If you cannot go, reply NO now so we can send someone else."
        ),
    ),
    "followup_day_1": Template(
        key="followup_day_1",
        requires_consent=True,
        text=(
            "Akazi: how was your first day at {business_name}?\n"
            "Reply OK if all is well, or tell us if there is a problem with "
            "pay, hours, transport or safety."
        ),
    ),
    "followup_week_1": Template(
        key="followup_week_1",
        requires_consent=True,
        text=(
            "Akazi: one week at {business_name}. Still working there?\n"
            "Reply YES or NO. Tell us if anything is wrong."
        ),
    ),
    "followup_day_30": Template(
        key="followup_day_30",
        requires_consent=True,
        text=(
            "Akazi: 30 days at {business_name}. Are you still working there?\n"
            "Reply YES or NO."
        ),
    ),
    "followup_day_90": Template(
        key="followup_day_90",
        requires_consent=True,
        text=(
            "Akazi: 90 days at {business_name}. Are you still working there?\n"
            "Reply YES or NO."
        ),
    ),
    "placement_cancelled": Template(
        key="placement_cancelled",
        requires_consent=True,
        text=(
            "Akazi: the {title} shift at {business_name} on {starts_on} has "
            "been cancelled by the employer. This is not your doing and it "
            "does not affect your record.\n"
            "We will let you know about other work."
        ),
    ),
    "placement_contract": Template(
        key="placement_contract",
        requires_consent=True,
        text=(
            "{contract}\n\n"
            "Keep this message. Quote {contract_ref} if you need to talk to "
            "us about this work."
        ),
    ),
    # To an employer contact, so no candidate consent applies.
    "employer_worker_assigned": Template(
        key="employer_worker_assigned",
        requires_consent=False,
        text=(
            "Akazi: {display_name} is assigned to {title} on {starts_on}"
            "{shift}.\n"
            "Confirm attendance at the link we sent you. If they do not "
            "arrive, tell us and we cover the slot free of charge."
        ),
    ),
    # Internal, to a member of staff. No consent gate: staff are not data
    # subjects of the messaging consent regime, they are on duty. It carries
    # no name and no detail of what was reported -- a missed response time is
    # a prompt to open the escalation, not a channel for the report itself.
    "escalation_breach": Template(
        key="escalation_breach",
        requires_consent=False,
        text=(
            "Akazi: a {kind} escalation raised {raised} has not been "
            "acknowledged. It was due a response by {respond_by}.\n"
            "Open it now: {link}"
        ),
    ),
    "employer_cover_sent": Template(
        key="employer_cover_sent",
        requires_consent=False,
        text=(
            "Akazi: {display_name} did not arrive. We are sending "
            "{cover_name} as cover at no charge — expected by {fill_by}."
        ),
    ),
}


def render(key: str, **values) -> str:
    if key not in TEMPLATES:
        raise KeyError(f"unknown message template {key!r}")
    return TEMPLATES[key].render(**values)


def transport_line(pay_rwf: int, transport_rwf: int, covered: bool) -> str:
    """The line that makes the offer honest.

    A wage quoted gross is not what the person takes home, and a role that
    costs a third of its pay to reach is the one that fails in week two. If we
    know the fare, we say what is left.
    """
    if covered:
        return "Transport: paid by the employer."
    if transport_rwf <= 0:
        return "Transport: no fare estimated — check the route before you accept."
    return (
        f"Transport: about RWF {transport_rwf} per day, "
        f"leaving about RWF {pay_rwf - transport_rwf}."
    )


def shift_window(shift_start, shift_end) -> str:
    if shift_start is None or shift_end is None:
        return ""
    return f", {shift_start:%H:%M}–{shift_end:%H:%M}"
