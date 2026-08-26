"""Cohort management.

The seventh of the seven things the blueprint lists for weeks 1-6, and the last
to be built. Candidates are prepared in groups before placement: a short
orientation for a sector, run by a named facilitator.

`women_only` is the part that carries weight. The blueprint asks for all-female
cohort options as a concrete measure for women's participation, alongside
shift-time limits and employer safety ratings -- because female unemployment
runs 15.5% against 11.6% male, and tracking the gap is not a plan. A woman who
will not attend a mixed session is not served by a system that offers her one
anyway. The rule is enforced by a database trigger as well as here: it is a
promise made to the people in the room, and it should not depend on every
future code path remembering it.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

OUTCOMES = ("completed", "withdrew", "did_not_finish")


class CohortError(Exception):
    pass


def create_cohort(
    session: Session,
    *,
    name: str,
    starts_on: date,
    facilitator: UUID,
    sector: str | None = None,
    ends_on: date | None = None,
    women_only: bool = False,
    capacity: int | None = None,
    location: str | None = None,
) -> UUID:
    if ends_on is not None and ends_on < starts_on:
        raise CohortError("a cohort cannot end before it starts")
    if capacity is not None and capacity < 1:
        raise CohortError("capacity must be at least one place")

    return session.execute(
        text(
            """
            INSERT INTO cohorts (name, sector, starts_on, ends_on, facilitator,
                                 women_only, capacity, location)
            VALUES (:name, :sector, :starts, :ends, :facilitator,
                    :women_only, :capacity, :location)
            RETURNING cohort_id
            """
        ),
        {
            "name": name, "sector": sector, "starts": starts_on,
            "ends": ends_on, "facilitator": str(facilitator),
            "women_only": women_only, "capacity": capacity,
            "location": location,
        },
    ).scalar_one()


def add_member(session: Session, cohort_id: UUID, candidate_id: UUID) -> None:
    """Enrol someone.

    The women-only and capacity rules are enforced by trigger; this turns the
    database's exception into something a coordinator can act on.
    """
    exists = session.execute(
        text("SELECT status::text FROM cohorts WHERE cohort_id = :cid"),
        {"cid": str(cohort_id)},
    ).scalar_one_or_none()
    if exists is None:
        raise CohortError("no such cohort")
    if exists in ("completed", "cancelled"):
        raise CohortError(f"this cohort is {exists}")

    # Inside a savepoint: a trigger rejection is a PostgreSQL error, which
    # aborts the whole transaction. Without this the caller could catch
    # CohortError and find every later statement failing -- which is exactly
    # what a coordinator enrolling a list of people would hit on the first
    # refusal, losing the ones after it too.
    savepoint = session.begin_nested()
    try:
        session.execute(
            text(
                "INSERT INTO cohort_members (cohort_id, candidate_id) "
                "VALUES (:co, :ca) ON CONFLICT DO NOTHING"
            ),
            {"co": str(cohort_id), "ca": str(candidate_id)},
        )
        savepoint.commit()
    except Exception as exc:  # noqa: BLE001 -- re-raised as a domain error
        savepoint.rollback()
        message = str(exc)
        if "women-only" in message:
            raise CohortError(
                "this cohort is women-only and that candidate is not recorded "
                "as female"
            ) from exc
        if "is full" in message:
            raise CohortError("this cohort is full") from exc
        raise


def record_outcome(
    session: Session,
    cohort_id: UUID,
    candidate_id: UUID,
    outcome: str,
    notes: str | None = None,
) -> None:
    """How it went for one person.

    Completing training moves the candidate to 'trained', which is what that
    status has been waiting for. Withdrawing does not: someone who left partway
    has not been trained, and recording otherwise would flatter both the cohort
    numbers and the candidate's readiness.
    """
    if outcome not in OUTCOMES:
        raise CohortError(f"outcome must be one of {OUTCOMES}")

    updated = session.execute(
        text(
            """
            UPDATE cohort_members
               SET outcome = CAST(:outcome AS cohort_outcome),
                   completed_at = clock_timestamp(),
                   notes = COALESCE(:notes, notes)
             WHERE cohort_id = :co AND candidate_id = :ca
            RETURNING candidate_id
            """
        ),
        {
            "co": str(cohort_id), "ca": str(candidate_id),
            "outcome": outcome, "notes": notes,
        },
    ).scalar_one_or_none()
    if updated is None:
        raise CohortError("that candidate is not in this cohort")

    from app.operations.attendance import refresh_candidate_status

    refresh_candidate_status(session, candidate_id)


def set_status(session: Session, cohort_id: UUID, status: str) -> None:
    if status not in ("planned", "running", "completed", "cancelled"):
        raise CohortError("unknown cohort status")
    updated = session.execute(
        text(
            "UPDATE cohorts SET status = CAST(:s AS cohort_status) "
            "WHERE cohort_id = :cid RETURNING cohort_id"
        ),
        {"s": status, "cid": str(cohort_id)},
    ).scalar_one_or_none()
    if updated is None:
        raise CohortError("no such cohort")


def list_cohorts(session: Session, include_finished: bool = False) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT co.cohort_id, co.name, co.sector, co.starts_on, co.ends_on,
                   co.women_only, co.capacity, co.location,
                   co.status::text AS status,
                   s.full_name AS facilitator_name,
                   count(cm.candidate_id) AS members,
                   count(*) FILTER (WHERE cm.outcome = 'completed') AS finished
              FROM cohorts co
              JOIN staff s ON s.staff_id = co.facilitator
              LEFT JOIN cohort_members cm ON cm.cohort_id = co.cohort_id
             WHERE (:include_finished OR co.status IN ('planned','running'))
             GROUP BY co.cohort_id, s.full_name
             ORDER BY co.starts_on DESC
            """
        ),
        {"include_finished": include_finished},
    ).mappings()
    return [dict(r) for r in rows]


def cohort_members(session: Session, cohort_id: UUID) -> list[dict]:
    """Display names only -- a training register does not need identity data."""
    rows = session.execute(
        text(
            """
            SELECT cm.candidate_id, c.display_name, c.gender, c.district,
                   cm.joined_at, cm.outcome::text AS outcome, cm.notes
              FROM cohort_members cm
              JOIN candidates c ON c.candidate_id = cm.candidate_id
             WHERE cm.cohort_id = :cid
             ORDER BY c.display_name
            """
        ),
        {"cid": str(cohort_id)},
    ).mappings()
    return [dict(r) for r in rows]


def training_effect(session: Session) -> dict:
    """Whether finishing a cohort is associated with getting placed.

    Reported honestly as an association, not a causal claim: people who finish
    training are also the people who showed up, and that is not something this
    number can separate. It is still the first thing to look at before spending
    another week of anyone's time on orientation.
    """
    row = session.execute(
        text(
            """
            WITH trained AS (
                SELECT DISTINCT candidate_id FROM cohort_members
                 WHERE outcome = 'completed'
            ),
            placed AS (
                SELECT DISTINCT candidate_id FROM placements
                 WHERE status IN ('active','completed')
            )
            SELECT
              (SELECT count(*) FROM trained)                        AS trained,
              (SELECT count(*) FROM trained t JOIN placed p USING (candidate_id))
                                                                    AS trained_placed,
              (SELECT count(*) FROM candidates c
                WHERE c.status NOT IN ('withdrawn','inactive')
                  AND NOT EXISTS (SELECT 1 FROM trained t
                                   WHERE t.candidate_id = c.candidate_id))
                                                                    AS untrained,
              (SELECT count(*) FROM placed p
                WHERE NOT EXISTS (SELECT 1 FROM trained t
                                   WHERE t.candidate_id = p.candidate_id))
                                                                    AS untrained_placed
            """
        )
    ).mappings().one()
    return dict(row)
