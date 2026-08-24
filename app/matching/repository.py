"""Loading the matching engine's inputs from the database.

The engine itself is pure functions over dataclasses and knows nothing about
SQL. This module is the seam: it assembles candidates, computes a transport
estimate per candidate/employer pair, runs the filters, and hands back both the
matches and the reasons everyone else was excluded.

Deliberately no pre-filtering in SQL beyond an active-candidate scan. At pilot
volume the whole registry fits in memory, and pushing filters into the query
would hide the rejection reasons -- which are the useful half of the output when
a request fills nobody.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.matching.engine import (
    AvailabilityWindow,
    Candidate,
    MatchResult,
    WorkRequest,
    match_candidates,
)
from app.matching.transport import estimate_transport


@dataclass(frozen=True)
class RequestContext:
    request: WorkRequest
    employer_id: UUID
    site_lat: float | None
    site_lng: float | None


def load_request(session: Session, request_id: UUID) -> RequestContext:
    row = session.execute(
        text(
            """
            SELECT wr.request_id, wr.employer_id, wr.starts_on, wr.pay_rwf,
                   wr.pay_unit, wr.shift_start, wr.shift_end,
                   wr.transport_covered, e.site_lat, e.site_lng
              FROM work_requests wr
              JOIN employers e ON e.employer_id = wr.employer_id
             WHERE wr.request_id = :rid
            """
        ),
        {"rid": str(request_id)},
    ).mappings().first()

    if row is None:
        raise LookupError(f"work request {request_id} not found")

    required = {
        r["skill_code"]: r["min_score"]
        for r in session.execute(
            text(
                """
                SELECT s.skill_code, rs.min_score
                  FROM request_skills rs
                  JOIN skills s ON s.skill_id = rs.skill_id
                 WHERE rs.request_id = :rid
                """
            ),
            {"rid": str(request_id)},
        ).mappings()
    }

    return RequestContext(
        request=WorkRequest(
            request_id=row["request_id"],
            employer_id=row["employer_id"],
            starts_on=row["starts_on"],
            pay_rwf=row["pay_rwf"],
            pay_unit=row["pay_unit"],
            shift_start=row["shift_start"],
            shift_end=row["shift_end"],
            transport_covered=row["transport_covered"],
            required_skills=required,
        ),
        employer_id=row["employer_id"],
        site_lat=float(row["site_lat"]) if row["site_lat"] is not None else None,
        site_lng=float(row["site_lng"]) if row["site_lng"] is not None else None,
    )


# One row per candidate, with the reliability history the ranking depends on.
_CANDIDATE_SQL = """
SELECT c.candidate_id,
       c.display_name,
       c.gender,
       ci.date_of_birth,
       c.home_lat,
       c.home_lng,
       c.max_commute_rwf,
       c.max_commute_min,
       c.accepts_after_dark,
       COALESCE(consent.granted, false) AS has_placement_consent,
       COALESCE(prior.completed_with_employer, 0) AS completed_with_employer,
       COALESCE(retention.rate, 0.0)              AS retention_30day_rate,
       COALESCE(scores.best_score, 0)             AS assessment_score,
       COALESCE(skills.scores, '{}'::jsonb)       AS skill_scores,
       COALESCE(windows.slots, '[]'::jsonb)       AS availability
  FROM candidates c
  -- date_of_birth is the one identity field matching needs, for the age filter.
  JOIN candidate_identity ci ON ci.candidate_id = c.candidate_id
  LEFT JOIN LATERAL (
      SELECT vc.granted FROM v_current_consent vc
       WHERE vc.candidate_id = c.candidate_id AND vc.purpose = 'placement'
  ) consent ON true
  LEFT JOIN LATERAL (
      SELECT count(*) AS completed_with_employer
        FROM placements p
        JOIN work_requests w ON w.request_id = p.request_id
       WHERE p.candidate_id = c.candidate_id
         AND w.employer_id = :employer_id
         AND p.status = 'completed'
  ) prior ON true
  LEFT JOIN LATERAL (
      SELECT avg(f.still_working::int)::float AS rate
        FROM follow_ups f
        JOIN placements p ON p.placement_id = f.placement_id
       WHERE p.candidate_id = c.candidate_id
         AND f.checkpoint = 'day_30'
         AND f.completed_at IS NOT NULL
         AND f.still_working IS NOT NULL
  ) retention ON true
  LEFT JOIN LATERAL (
      SELECT max(ar.score) AS best_score
        FROM assessment_results ar
       WHERE ar.candidate_id = c.candidate_id
  ) scores ON true
  LEFT JOIN LATERAL (
      -- Best score per skill: a retake supersedes an earlier attempt.
      SELECT jsonb_object_agg(sk.skill_code, sk.best) AS scores
        FROM (SELECT s.skill_code, max(ar.score) AS best
                FROM assessment_results ar
                JOIN assessments a ON a.assessment_id = ar.assessment_id
                JOIN skills s      ON s.skill_id = a.skill_id
               WHERE ar.candidate_id = c.candidate_id
               GROUP BY s.skill_code) sk
  ) skills ON true
  LEFT JOIN LATERAL (
      SELECT jsonb_agg(jsonb_build_array(av.day_of_week,
                                         av.start_time::text,
                                         av.end_time::text)) AS slots
        FROM availability av
       WHERE av.candidate_id = c.candidate_id
  ) windows ON true
 WHERE c.status IN ('registered','assessed','trained')
"""


def _parse_time(value: str):
    from datetime import time as _time

    hour, minute, *rest = value.split(":")
    return _time(int(hour), int(minute), int(float(rest[0])) if rest else 0)


def load_candidates(
    session: Session, context: RequestContext
) -> list[Candidate]:
    """Assemble candidate records, including a transport estimate for this site.

    A candidate with no home coordinates gets no estimate. Their transport cost
    stays at zero and only their own declared ceiling applies -- which is why
    capturing home location at registration matters: without it, the filter that
    prevents most 30-day dropouts cannot run for that person.
    """
    rows = session.execute(
        text(_CANDIDATE_SQL), {"employer_id": str(context.employer_id)}
    ).mappings()

    candidates: list[Candidate] = []
    for row in rows:
        estimate = estimate_transport(
            float(row["home_lat"]) if row["home_lat"] is not None else None,
            float(row["home_lng"]) if row["home_lng"] is not None else None,
            context.site_lat,
            context.site_lng,
        )
        candidates.append(
            Candidate(
                candidate_id=row["candidate_id"],
                display_name=row["display_name"],
                gender=row["gender"],
                date_of_birth=row["date_of_birth"],
                availability=[
                    AvailabilityWindow(dow, _parse_time(start), _parse_time(end))
                    for dow, start, end in row["availability"]
                ],
                skill_scores=dict(row["skill_scores"]),
                max_commute_rwf=row["max_commute_rwf"],
                max_commute_min=row["max_commute_min"],
                accepts_after_dark=row["accepts_after_dark"],
                has_placement_consent=row["has_placement_consent"],
                est_transport_rwf=estimate.daily_rwf if estimate else 0,
                est_commute_min=estimate.commute_min if estimate else None,
                prior_completed_with_employer=row["completed_with_employer"],
                retention_30day_rate=row["retention_30day_rate"],
                assessment_score=row["assessment_score"],
            )
        )
    return candidates


def find_matches(session: Session, request_id: UUID) -> MatchResult:
    context = load_request(session, request_id)
    return match_candidates(context.request, load_candidates(session, context))
