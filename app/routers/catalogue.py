"""Defining what we test and what counts as a pass.

Authoring is gated on admin/owner: pass_score decides who is eligible for
work, which is policy rather than data entry. Reading the catalogue is open to
any staff member, because a coordinator scoring a candidate needs the rubric
in front of them.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.deps import AdminDep, SessionDep, StaffDep
from app.operations.catalogue import (
    CATEGORIES,
    METHODS,
    CatalogueError,
    assessment_for_scoring,
    create_assessment,
    create_skill,
    list_assessments,
    list_skills,
    rename_skill,
    update_rubric,
)

router = APIRouter(tags=["catalogue"])


def _bad_request(exc: CatalogueError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    )


class NewSkill(BaseModel):
    skill_code: str = Field(max_length=40)
    skill_name: str = Field(max_length=120)
    category: str = Field(pattern="^(" + "|".join(CATEGORIES) + ")$")


class RenameSkill(BaseModel):
    skill_name: str = Field(max_length=120)


class NewAssessment(BaseModel):
    skill_id: UUID
    title: str = Field(max_length=140)
    method: str = Field(pattern="^(" + "|".join(METHODS) + ")$")
    pass_score: int = Field(ge=0)
    max_score: int = Field(default=5, ge=1)
    rubric: str | None = None


class UpdateRubric(BaseModel):
    rubric: str


@router.get("/skills")
def get_skills(session: SessionDep, staff: StaffDep):
    return {"skills": list_skills(session)}


@router.post("/skills", status_code=201)
def post_skill(body: NewSkill, session: SessionDep, staff: AdminDep):
    try:
        skill_id = create_skill(
            session,
            skill_code=body.skill_code,
            skill_name=body.skill_name,
            category=body.category,
        )
    except CatalogueError as exc:
        raise _bad_request(exc) from exc
    return {"skill_id": skill_id}


@router.patch("/skills/{skill_id}")
def patch_skill(
    skill_id: UUID, body: RenameSkill, session: SessionDep, staff: AdminDep
):
    try:
        rename_skill(session, skill_id, body.skill_name)
    except CatalogueError as exc:
        raise _bad_request(exc) from exc
    return {"skill_id": skill_id, "skill_name": body.skill_name}


@router.get("/assessments")
def get_assessments(
    session: SessionDep, staff: StaffDep, skill_id: UUID | None = None
):
    return {"assessments": list_assessments(session, skill_id)}


@router.get("/assessments/{assessment_id}")
def get_assessment(assessment_id: UUID, session: SessionDep, staff: StaffDep):
    """What an assessor needs while scoring, rubric included."""
    try:
        return assessment_for_scoring(session, assessment_id)
    except CatalogueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/assessments", status_code=201)
def post_assessment(body: NewAssessment, session: SessionDep, staff: AdminDep):
    try:
        assessment_id = create_assessment(
            session,
            skill_id=body.skill_id,
            title=body.title,
            method=body.method,
            pass_score=body.pass_score,
            max_score=body.max_score,
            rubric=body.rubric,
        )
    except CatalogueError as exc:
        raise _bad_request(exc) from exc
    return {"assessment_id": assessment_id}


@router.patch("/assessments/{assessment_id}/rubric")
def patch_rubric(
    assessment_id: UUID, body: UpdateRubric, session: SessionDep,
    staff: AdminDep,
):
    """Bounds are frozen once results exist; the rubric wording is not.

    Sharpening how a criterion is described does not change who passed.
    """
    try:
        update_rubric(session, assessment_id, body.rubric)
    except CatalogueError as exc:
        raise _bad_request(exc) from exc
    return {"assessment_id": assessment_id}
