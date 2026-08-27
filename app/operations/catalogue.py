"""The skill and assessment catalogue: what we test, and what counts as a pass.

Nothing could create either before this. skills and assessments were empty on
a fresh deployment and no code inserted into them, so require_skill() raised
'unknown skill' for every code there was, no assessment result could reference
a real assessment, and matching filter 1 and rank criterion 3 never engaged.

Defining a pass mark is a policy decision -- it decides who is eligible for
work -- so the API gates authoring on admin/owner while recording a result
stays with coordinators. The integrity rules live in the database (migration
035) because a bulk import of paper assessment sheets is exactly the path that
skips the application.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

CATEGORIES = ("hospitality", "retail", "trades", "soft", "cleaning",
              "logistics", "agriculture", "other")
METHODS = ("practical", "observed", "written")


class CatalogueError(Exception):
    """A skill or assessment that cannot be defined as asked."""


def _normalise_code(skill_code: str) -> str:
    """Codes are matched exactly by require_skill(), so they are normalised
    once here rather than left to whoever types them."""
    code = skill_code.strip().lower().replace(" ", "_").replace("-", "_")
    if not code:
        raise CatalogueError("a skill needs a code")
    if len(code) > 40:
        raise CatalogueError("skill_code is limited to 40 characters")
    if not all(c.isalnum() or c == "_" for c in code):
        raise CatalogueError(
            "skill_code may contain letters, digits and underscores only"
        )
    return code


def create_skill(
    session: Session, *, skill_code: str, skill_name: str, category: str
) -> UUID:
    code = _normalise_code(skill_code)
    if category not in CATEGORIES:
        raise CatalogueError(f"category must be one of {CATEGORIES}")
    if not skill_name.strip():
        raise CatalogueError("a skill needs a name")

    existing = session.execute(
        text("SELECT skill_id FROM skills WHERE skill_code = :code"),
        {"code": code},
    ).scalar_one_or_none()
    if existing is not None:
        raise CatalogueError(f"skill {code!r} already exists")

    return session.execute(
        text(
            """
            INSERT INTO skills (skill_code, skill_name, category)
            VALUES (:code, :name, :category)
            RETURNING skill_id
            """
        ),
        {"code": code, "name": skill_name.strip(), "category": category},
    ).scalar_one()


def rename_skill(session: Session, skill_id: UUID, skill_name: str) -> None:
    """The display name only. skill_code is the stable handle and is frozen
    by trigger -- see migration 035."""
    if not skill_name.strip():
        raise CatalogueError("a skill needs a name")
    updated = session.execute(
        text("UPDATE skills SET skill_name = :name WHERE skill_id = :sid "
             "RETURNING skill_id"),
        {"name": skill_name.strip(), "sid": str(skill_id)},
    ).scalar_one_or_none()
    if updated is None:
        raise CatalogueError(f"no such skill {skill_id}")


def create_assessment(
    session: Session,
    *,
    skill_id: UUID,
    title: str,
    method: str,
    pass_score: int,
    max_score: int = 5,
    rubric: str | None = None,
) -> UUID:
    """Define how a skill is tested and what counts as passing it.

    The rubric is strongly encouraged rather than required: two coordinators
    scoring "retail greeting" from memory will not agree, and matching filter 1
    then ranks on noise. It is surfaced to whoever records a result.
    """
    if method not in METHODS:
        raise CatalogueError(f"method must be one of {METHODS}")
    if not title.strip():
        raise CatalogueError("an assessment needs a title")
    if max_score < 1:
        raise CatalogueError("max_score must be at least 1")
    if not 0 <= pass_score <= max_score:
        raise CatalogueError(
            f"pass_score must be between 0 and max_score ({max_score})"
        )

    known = session.execute(
        text("SELECT skill_id FROM skills WHERE skill_id = :sid"),
        {"sid": str(skill_id)},
    ).scalar_one_or_none()
    if known is None:
        raise CatalogueError(f"no such skill {skill_id}")

    return session.execute(
        text(
            """
            INSERT INTO assessments (skill_id, title, method, max_score,
                                     pass_score, rubric)
            VALUES (:sid, :title, :method, :max_score, :pass_score, :rubric)
            RETURNING assessment_id
            """
        ),
        {
            "sid": str(skill_id), "title": title.strip(), "method": method,
            "max_score": max_score, "pass_score": pass_score,
            "rubric": (rubric or "").strip() or None,
        },
    ).scalar_one()


def update_rubric(session: Session, assessment_id: UUID, rubric: str) -> None:
    """Sharpening the wording of a rubric does not change who passed, so
    unlike the bounds it stays editable for the life of the assessment."""
    updated = session.execute(
        text("UPDATE assessments SET rubric = :rubric "
             "WHERE assessment_id = :aid RETURNING assessment_id"),
        {"rubric": rubric.strip() or None, "aid": str(assessment_id)},
    ).scalar_one_or_none()
    if updated is None:
        raise CatalogueError(f"no such assessment {assessment_id}")


def list_skills(session: Session) -> list[dict]:
    """The catalogue, with how many assessments each skill has.

    A skill with no assessment cannot be scored, so it cannot be required on a
    work request in any useful way -- the count is what makes that visible.
    """
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT s.skill_id, s.skill_code, s.skill_name, s.category,
                       count(a.assessment_id) AS assessment_count
                  FROM skills s
                  LEFT JOIN assessments a ON a.skill_id = s.skill_id
                 GROUP BY s.skill_id, s.skill_code, s.skill_name, s.category
                 ORDER BY s.category, s.skill_name
                """
            )
        ).mappings()
    ]


def list_assessments(
    session: Session, skill_id: UUID | None = None
) -> list[dict]:
    """Every assessment with its rubric and whether its bounds are still open.

    results_recorded is what tells an administrator why a pass mark can no
    longer be edited, rather than leaving them to discover it from an error.
    """
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT a.assessment_id, a.title, a.method, a.max_score,
                       a.pass_score, a.rubric,
                       s.skill_id, s.skill_code, s.skill_name,
                       count(r.result_id) AS results_recorded,
                       count(r.result_id) = 0 AS bounds_editable
                  FROM assessments a
                  JOIN skills s ON s.skill_id = a.skill_id
                  LEFT JOIN assessment_results r
                         ON r.assessment_id = a.assessment_id
                 WHERE (CAST(:sid AS uuid) IS NULL
                        OR a.skill_id = CAST(:sid AS uuid))
                 GROUP BY a.assessment_id, a.title, a.method, a.max_score,
                          a.pass_score, a.rubric, s.skill_id, s.skill_code,
                          s.skill_name
                 ORDER BY s.skill_name, a.title
                """
            ),
            {"sid": str(skill_id) if skill_id else None},
        ).mappings()
    ]


def assessment_for_scoring(session: Session, assessment_id: UUID) -> dict:
    """What an assessor needs in front of them while scoring.

    The rubric was stored and never shown anywhere, which is how two
    coordinators end up scoring the same performance differently -- and the
    score is both a matching filter and what gets read aloud to an employer
    defending the choice.
    """
    row = session.execute(
        text(
            """
            SELECT a.assessment_id, a.title, a.method, a.max_score,
                   a.pass_score, a.rubric, s.skill_code, s.skill_name
              FROM assessments a
              JOIN skills s ON s.skill_id = a.skill_id
             WHERE a.assessment_id = :aid
            """
        ),
        {"aid": str(assessment_id)},
    ).mappings().first()
    if row is None:
        raise CatalogueError(f"no such assessment {assessment_id}")
    return dict(row)
