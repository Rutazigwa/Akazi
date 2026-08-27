"""Employer and candidate registration endpoints."""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.deps import IdentityStaffDep, SessionDep, StaffDep
from app.operations.catalogue import assessment_for_scoring
from app.operations.registry import (
    AvailabilitySlot,
    RegistryError,
    add_employer_contact,
    record_assessment_result,
    record_consent,
    register_candidate,
    register_employer,
    set_employer_tier,
)

router = APIRouter(tags=["registry"])


class NewEmployer(BaseModel):
    business_name: str
    sector: str
    district: str
    tin: str | None = None
    site_lat: float | None = None
    site_lng: float | None = None
    is_cooperative: bool = False


class EmployerTier(BaseModel):
    tier: str = Field(pattern="^(prospect|pilot|active|suspended)$")
    safety_verified: bool | None = None


class NewContact(BaseModel):
    full_name: str
    phone: str
    role_title: str | None = None
    email: str | None = None
    is_primary: bool = False


class Slot(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start: time
    end: time


class NewCandidate(BaseModel):
    # Identity -- residency-sensitive, isolated in candidate_identity.
    legal_first_name: str
    legal_last_name: str
    date_of_birth: date
    phone_primary: str
    national_id: str | None = None
    phone_alt: str | None = None
    emergency_contact: str | None = None
    # Operational
    display_name: str
    district: str
    sector: str
    gender: str | None = Field(default=None, pattern="^[FMX]$")
    cell: str | None = None
    home_lat: float | None = None
    home_lng: float | None = None
    education_level: str | None = None
    languages: list[str] = Field(default_factory=list)
    has_smartphone: bool = False
    momo_registered: bool = False
    max_commute_rwf: int | None = Field(default=None, ge=0)
    max_commute_min: int | None = Field(default=None, ge=0)
    accepts_after_dark: bool = False
    availability: list[Slot] = Field(default_factory=list)
    # Consent is captured at intake, not later.
    consent_captured_via: str = Field(pattern="^(paper|whatsapp|app)$")


class NewConsent(BaseModel):
    purpose: str = Field(pattern="^(placement|training|reporting)$")
    granted: bool
    captured_via: str = Field(pattern="^(paper|whatsapp|app)$")


class NewAssessmentResult(BaseModel):
    assessment_id: UUID
    score: int = Field(ge=0)
    notes: str | None = None


@router.post("/employers", status_code=201)
def create_employer(body: NewEmployer, session: SessionDep, staff: StaffDep):
    employer_id = register_employer(
        session,
        business_name=body.business_name,
        sector=body.sector,
        district=body.district,
        account_owner=staff.staff_id,
        tin=body.tin,
        site_lat=body.site_lat,
        site_lng=body.site_lng,
        is_cooperative=body.is_cooperative,
    )
    return {"employer_id": employer_id}


@router.post("/employers/{employer_id}/contacts", status_code=201)
def create_contact(
    employer_id: UUID, body: NewContact, session: SessionDep, staff: StaffDep
):
    contact_id = add_employer_contact(
        session,
        employer_id,
        body.full_name,
        body.phone,
        body.role_title,
        body.email,
        body.is_primary,
    )
    return {"contact_id": contact_id}


@router.post("/employers/{employer_id}/contacts/{contact_id}/invite")
def invite_employer_contact(
    employer_id: UUID, contact_id: UUID, session: SessionDep, staff: StaffDep
):
    """Give an employer contact a login to the employer dashboard.

    Returns a single-use password, shown once. Generated rather than chosen so
    the coordinator setting it up does not know the employer's password.
    """
    from app.employer_auth import invite_contact

    owns = session.execute(
        text(
            "SELECT 1 FROM employer_contacts "
            "WHERE contact_id = :cid AND employer_id = :eid"
        ),
        {"cid": str(contact_id), "eid": str(employer_id)},
    ).first()
    if not owns:
        raise HTTPException(status_code=404, detail="no such contact")

    return {
        "contact_id": contact_id,
        "temporary_password": invite_contact(session, contact_id),
        "note": "shown once; the contact signs in at /employer/login",
    }


@router.patch("/employers/{employer_id}")
def update_employer_tier(
    employer_id: UUID, body: EmployerTier, session: SessionDep, staff: StaffDep
):
    try:
        set_employer_tier(session, employer_id, body.tier, body.safety_verified)
    except RegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"employer_id": employer_id, "tier": body.tier}


@router.get("/employers")
def list_employers(session: SessionDep, staff: StaffDep):
    rows = session.execute(
        text(
            """
            SELECT employer_id, business_name, sector, district,
                   tier::text AS tier, is_cooperative, safety_verified
              FROM employers ORDER BY business_name
            """
        )
    ).mappings()
    return {"employers": [dict(r) for r in rows]}


# Registration writes a national ID number, so it needs the identity grant --
# the same gate that guards reading one.
@router.post("/candidates", status_code=201)
def create_candidate(
    body: NewCandidate, session: SessionDep, staff: IdentityStaffDep
):
    try:
        candidate_id = register_candidate(
            session,
            legal_first_name=body.legal_first_name,
            legal_last_name=body.legal_last_name,
            date_of_birth=body.date_of_birth,
            phone_primary=body.phone_primary,
            display_name=body.display_name,
            district=body.district,
            sector=body.sector,
            registered_by=staff.staff_id,
            consent_captured_via=body.consent_captured_via,
            national_id=body.national_id,
            phone_alt=body.phone_alt,
            emergency_contact=body.emergency_contact,
            gender=body.gender,
            cell=body.cell,
            home_lat=body.home_lat,
            home_lng=body.home_lng,
            education_level=body.education_level,
            languages=body.languages,
            has_smartphone=body.has_smartphone,
            momo_registered=body.momo_registered,
            max_commute_rwf=body.max_commute_rwf,
            max_commute_min=body.max_commute_min,
            accepts_after_dark=body.accepts_after_dark,
            availability=[
                AvailabilitySlot(s.day_of_week, s.start, s.end)
                for s in body.availability
            ],
        )
    except RegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"candidate_id": candidate_id}


@router.get("/candidates")
def list_candidates(session: SessionDep, staff: StaffDep):
    """Operational profiles only. No names from candidate_identity here."""
    rows = session.execute(
        text(
            """
            SELECT c.candidate_id, c.display_name, c.gender, c.district,
                   c.sector, c.status::text AS status,
                   COALESCE(v.granted, false) AS placement_consent
              FROM candidates c
              LEFT JOIN v_current_consent v
                     ON v.candidate_id = c.candidate_id
                    AND v.purpose = 'placement'
             ORDER BY c.display_name
            """
        )
    ).mappings()
    return {"candidates": [dict(r) for r in rows]}


@router.post("/candidates/{candidate_id}/consent", status_code=201)
def add_consent(
    candidate_id: UUID, body: NewConsent, session: SessionDep, staff: StaffDep
):
    consent_id = record_consent(
        session,
        candidate_id,
        purpose=body.purpose,
        granted=body.granted,
        captured_via=body.captured_via,
        captured_by=staff.staff_id,
    )
    return {"consent_id": consent_id}


@router.post("/candidates/{candidate_id}/assessments", status_code=201)
def add_assessment_result(
    candidate_id: UUID,
    body: NewAssessmentResult,
    session: SessionDep,
    staff: StaffDep,
):
    result_id = record_assessment_result(
        session, candidate_id, body.assessment_id, body.score,
        staff.staff_id, body.notes,
    )
    # Echo the scale back. "4" means nothing on its own, and this is the number
    # matching filters on and a coordinator reads aloud to an employer asking
    # why this person -- so the response says 4 out of 5, passed.
    scored = assessment_for_scoring(session, body.assessment_id)
    return {
        "result_id": result_id,
        "score": body.score,
        "max_score": scored["max_score"],
        "passed": body.score >= scored["pass_score"],
        "skill_code": scored["skill_code"],
    }
