"""Employer and candidate registration.

Candidate intake in phase one is WhatsApp and assisted in-person registration --
a coordinator types this in, there is no candidate app. That shapes the API:
everything is one call with everything known at the desk, rather than a
multi-step wizard nobody is standing in front of.

Consent is captured in the same transaction as the identity record. It is not a
later step and not optional: without a consent record the candidate is excluded
by matching filter 1, so a half-registered person would sit in the database
invisible and unplaceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from uuid import UUID

from app.clock import kigali_today
from app.rules import MINIMUM_AGE
from sqlalchemy import text
from sqlalchemy.orm import Session

# Bump when the wording of what candidates agree to changes. Never edit an
# existing version's meaning -- consent_records stores the version someone
# actually agreed to, and rewriting history defeats the point.
CURRENT_CONSENT_VERSION = "v1.0"

# Enforced here so the API can answer clearly, and by chk_minimum_age on
# candidate_identity so it holds even for writes that bypass this code.
# Narrow apprenticeship exceptions for 13-15 are not supported: they need an
# authorised-exception record and a deliberate schema change.



class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class AvailabilitySlot:
    day_of_week: int
    start: time
    end: time


def _age_on(born: date, when: date) -> int:
    years = when.year - born.year
    if (when.month, when.day) < (born.month, born.day):
        years -= 1
    return years


def register_employer(
    session: Session,
    business_name: str,
    sector: str,
    district: str,
    account_owner: UUID,
    tin: str | None = None,
    site_lat: float | None = None,
    site_lng: float | None = None,
    is_cooperative: bool = False,
) -> UUID:
    return session.execute(
        text(
            """
            INSERT INTO employers (business_name, tin, sector, district,
                                   site_lat, site_lng, is_cooperative,
                                   account_owner)
            VALUES (:name, :tin, :sector, :district, :lat, :lng, :coop, :owner)
            RETURNING employer_id
            """
        ),
        {
            "name": business_name,
            "tin": tin,
            "sector": sector,
            "district": district,
            "lat": site_lat,
            "lng": site_lng,
            "coop": is_cooperative,
            "owner": str(account_owner),
        },
    ).scalar_one()


EMPLOYER_TIERS = ("prospect", "pilot", "active", "suspended")


def set_employer_tier(
    session: Session,
    employer_id: UUID,
    tier: str,
    safety_verified: bool | None = None,
) -> None:
    """Move an employer between prospect / pilot / active / suspended.

    New employers start as prospects: an interview is not a relationship, and
    the "active employers" pilot metric would be meaningless if every business
    card counted. Promotion is a decision somebody makes.
    """
    if tier not in EMPLOYER_TIERS:
        raise RegistryError(f"tier must be one of {EMPLOYER_TIERS}")

    session.execute(
        text(
            """
            UPDATE employers
               SET tier = CAST(:tier AS employer_tier),
                   safety_verified = COALESCE(:verified, safety_verified),
                   verified_at = CASE
                       WHEN :verified IS TRUE THEN COALESCE(verified_at, kigali_today())
                       WHEN :verified IS FALSE THEN NULL
                       ELSE verified_at END
             WHERE employer_id = :eid
            """
        ),
        {"eid": str(employer_id), "tier": tier, "verified": safety_verified},
    )


def add_employer_contact(
    session: Session,
    employer_id: UUID,
    full_name: str,
    phone: str,
    role_title: str | None = None,
    email: str | None = None,
    is_primary: bool = False,
) -> UUID:
    return session.execute(
        text(
            """
            INSERT INTO employer_contacts (employer_id, full_name, role_title,
                                           phone, email, is_primary)
            VALUES (:eid, :name, :role, :phone, :email, :primary)
            RETURNING contact_id
            """
        ),
        {
            "eid": str(employer_id),
            "name": full_name,
            "role": role_title,
            "phone": phone,
            "email": email,
            "primary": is_primary,
        },
    ).scalar_one()


def register_candidate(
    session: Session,
    *,
    legal_first_name: str,
    legal_last_name: str,
    date_of_birth: date,
    phone_primary: str,
    display_name: str,
    district: str,
    sector: str,
    registered_by: UUID,
    consent_captured_via: str,
    national_id: str | None = None,
    phone_alt: str | None = None,
    emergency_contact: str | None = None,
    gender: str | None = None,
    cell: str | None = None,
    home_lat: float | None = None,
    home_lng: float | None = None,
    education_level: str | None = None,
    languages: list[str] | None = None,
    has_smartphone: bool = False,
    momo_registered: bool = False,
    max_commute_rwf: int | None = None,
    max_commute_min: int | None = None,
    accepts_after_dark: bool = False,
    availability: list[AvailabilitySlot] | None = None,
) -> UUID:
    """Register a candidate: identity, operational profile, consent, availability.

    All in one transaction. The minimum-age constraint lives on the identity
    table, so an under-16 registration fails here rather than surfacing later as
    a mysteriously unmatched candidate.
    """
    if _age_on(date_of_birth, kigali_today()) < MINIMUM_AGE:
        raise RegistryError(
            f"candidate is under the minimum working age of {MINIMUM_AGE}"
        )
    if consent_captured_via not in ("paper", "whatsapp", "app"):
        raise RegistryError(
            f"consent must be captured via paper, whatsapp or app, "
            f"not {consent_captured_via!r}"
        )

    candidate_id = session.execute(
        text(
            """
            INSERT INTO candidate_identity
                (legal_first_name, legal_last_name, national_id, date_of_birth,
                 phone_primary, phone_alt, emergency_contact)
            VALUES (:first, :last, :nid, :dob, :phone, :alt, :emergency)
            RETURNING candidate_id
            """
        ),
        {
            "first": legal_first_name,
            "last": legal_last_name,
            "nid": national_id,
            "dob": date_of_birth,
            "phone": phone_primary,
            "alt": phone_alt,
            "emergency": emergency_contact,
        },
    ).scalar_one()

    session.execute(
        text(
            """
            INSERT INTO candidates
                (candidate_id, display_name, gender, district, sector, cell,
                 home_lat, home_lng, education_level, languages, has_smartphone,
                 momo_registered, max_commute_rwf, max_commute_min,
                 accepts_after_dark, registered_by)
            VALUES (:cid, :display, :gender, :district, :sector, :cell,
                    :lat, :lng, :education, :languages, :smartphone,
                    :momo, :max_rwf, :max_min, :after_dark, :by)
            """
        ),
        {
            "cid": candidate_id,
            "display": display_name,
            "gender": gender,
            "district": district,
            "sector": sector,
            "cell": cell,
            "lat": home_lat,
            "lng": home_lng,
            "education": education_level,
            "languages": languages or [],
            "smartphone": has_smartphone,
            "momo": momo_registered,
            "max_rwf": max_commute_rwf,
            "max_min": max_commute_min,
            "after_dark": accepts_after_dark,
            "by": str(registered_by),
        },
    )

    record_consent(
        session,
        candidate_id,
        purpose="placement",
        granted=True,
        captured_via=consent_captured_via,
        captured_by=registered_by,
    )

    for slot in availability or []:
        add_availability(session, candidate_id, slot)

    return candidate_id


def add_availability(
    session: Session, candidate_id: UUID, slot: AvailabilitySlot
) -> None:
    session.execute(
        text(
            """
            INSERT INTO availability (candidate_id, day_of_week, start_time,
                                      end_time)
            VALUES (:cid, :dow, :start, :end)
            ON CONFLICT (candidate_id, day_of_week, start_time) DO UPDATE
                SET end_time = EXCLUDED.end_time
            """
        ),
        {
            "cid": str(candidate_id),
            "dow": slot.day_of_week,
            "start": slot.start,
            "end": slot.end,
        },
    )


def record_consent(
    session: Session,
    candidate_id: UUID,
    purpose: str,
    granted: bool,
    captured_via: str,
    captured_by: UUID,
    policy_version: str = CURRENT_CONSENT_VERSION,
) -> UUID:
    """Append a consent record.

    Withdrawal is a new row with granted=False, never an update -- the table
    rejects updates outright. The history is the evidence.
    """
    return session.execute(
        text(
            """
            INSERT INTO consent_records (candidate_id, policy_version, purpose,
                                         granted, captured_via, captured_by)
            VALUES (:cid, :version, :purpose, :granted, :via, :by)
            RETURNING consent_id
            """
        ),
        {
            "cid": str(candidate_id),
            "version": policy_version,
            "purpose": purpose,
            "granted": granted,
            "via": captured_via,
            "by": str(captured_by),
        },
    ).scalar_one()


def record_assessment_result(
    session: Session,
    candidate_id: UUID,
    assessment_id: UUID,
    score: int,
    assessed_by: UUID,
    notes: str | None = None,
) -> UUID:
    result_id = session.execute(
        text(
            """
            INSERT INTO assessment_results (candidate_id, assessment_id, score,
                                            assessed_by, notes)
            VALUES (:cid, :aid, :score, :by, :notes)
            RETURNING result_id
            """
        ),
        {
            "cid": str(candidate_id),
            "aid": str(assessment_id),
            "score": score,
            "by": str(assessed_by),
            "notes": notes,
        },
    ).scalar_one()

    # A candidate who has been assessed is no longer merely registered.
    session.execute(
        text(
            "UPDATE candidates SET status = 'assessed' "
            "WHERE candidate_id = :cid AND status = 'registered'"
        ),
        {"cid": str(candidate_id)},
    )
    return result_id
