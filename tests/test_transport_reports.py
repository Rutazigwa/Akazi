"""What the commute actually cost, and what it changes.

The fare model has described itself as a placeholder awaiting real receipts
since it was written. Nothing collected any -- TransportEstimate even carried
is_estimate, a flag nothing ever set to false. Two load-bearing things rested
on the guess: matching filter 2, which refuses a placement when transport
exceeds 30% of daily pay, and "net earnings after transport", a headline
metric that was derived from straight-line distance and reported as measured.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from app.matching.repository import find_matches
from app.matching.transport import (
    MIN_CALIBRATION_REPORTS,
    TransportEstimate,
    resolve_transport,
)
from app.operations.transport import (
    TransportReportError,
    calibration,
    record_transport_report,
    route_history,
)


def guess(rwf=1000, minutes=20):
    return TransportEstimate(daily_rwf=rwf, commute_min=minutes,
                             straight_line_km=4.0)


# --- which number wins -----------------------------------------------------

def test_a_reported_fare_displaces_the_guess():
    resolved = resolve_transport(guess(1000), observed_rwf=1600,
                                 observed_reports=3)
    assert resolved.daily_rwf == 1600
    assert resolved.is_estimate is False
    assert "reported by 3 workers" in resolved.basis


def test_one_report_is_enough_for_that_route():
    """It is the route they will actually travel. One real fare beats a line."""
    resolved = resolve_transport(guess(1000), observed_rwf=1500,
                                 observed_reports=1)
    assert resolved.daily_rwf == 1500
    assert "1 worker on this route" in resolved.basis


def test_the_guess_is_corrected_once_there_is_enough_evidence():
    resolved = resolve_transport(
        guess(1000), calibration_factor=1.4,
        calibration_reports=MIN_CALIBRATION_REPORTS,
    )
    assert resolved.daily_rwf == 1400
    assert resolved.is_estimate is True
    assert "corrected x1.40" in resolved.basis


def test_a_correction_from_too_few_reports_is_not_applied():
    """Applying it would give a guess an authority it has not earned."""
    resolved = resolve_transport(
        guess(1000), calibration_factor=1.4,
        calibration_reports=MIN_CALIBRATION_REPORTS - 1,
    )
    assert resolved.daily_rwf == 1000
    assert "uncalibrated" in resolved.basis


def test_a_route_report_outranks_the_general_correction():
    """The specific beats the general, because it is the actual journey."""
    resolved = resolve_transport(
        guess(1000), observed_rwf=800, observed_reports=2,
        calibration_factor=2.0, calibration_reports=50,
    )
    assert resolved.daily_rwf == 800


def test_no_coordinates_and_no_reports_stays_unknown():
    """A missing estimate is not a free commute."""
    assert resolve_transport(None, calibration_reports=50) is None


def test_a_reported_fare_works_without_any_coordinates():
    """Somebody registered without a home location can still be measured."""
    resolved = resolve_transport(None, observed_rwf=1200, observed_reports=2)
    assert resolved.daily_rwf == 1200
    assert resolved.is_estimate is False


# --- recording -------------------------------------------------------------

def test_a_reported_fare_is_recorded_against_the_route(
    session, make_placement, staff_id
):
    placement_id = make_placement()
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=1400, reported_min=25,
                            recorded_by=staff_id)
    history = route_history(session, placement_id)
    assert history[0]["reported_rwf"] == 1400
    assert history[0]["reported_min"] == 25


def test_what_we_predicted_is_kept_alongside_it(session, make_placement):
    """So the calibration ratio survives a change to the geometry."""
    placement_id = make_placement(transport_rwf=900)
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=1400)
    assert route_history(session, placement_id)[0]["estimated_rwf"] == 900


def test_asking_twice_about_the_same_day_corrects_it(session, make_placement):
    """A worker asked again should correct the record, not double-weight it."""
    placement_id = make_placement()
    day = date.today()
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=1400, work_date=day)
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=1600, work_date=day)
    history = route_history(session, placement_id)
    assert len(history) == 1
    assert history[0]["reported_rwf"] == 1600


def test_an_implausible_daily_fare_is_refused(session, make_placement):
    """Almost always a monthly figure, and the median it feeds decides who
    gets offered work."""
    with pytest.raises(TransportReportError, match="implausible"):
        record_transport_report(session, placement_id=make_placement(),
                                reported_rwf=45_000)


def test_a_negative_fare_is_refused(session, make_placement):
    with pytest.raises(TransportReportError, match="cannot be negative"):
        record_transport_report(session, placement_id=make_placement(),
                                reported_rwf=-100)


def test_a_free_commute_is_allowed(session, make_placement):
    """Walking to work is a real answer, and a valuable one."""
    placement_id = make_placement()
    record_transport_report(session, placement_id=placement_id, reported_rwf=0)
    assert route_history(session, placement_id)[0]["reported_rwf"] == 0


# --- calibration -----------------------------------------------------------

def test_calibration_is_one_when_nothing_has_been_reported(session):
    assert calibration(session) == {"reports": 0, "factor": 1.0,
                                    "raw_factor": 1.0}


def test_calibration_is_the_median_ratio(session, make_placement,
                                         make_candidate):
    """Median, not mean: one worker stranded in the rain should inform the
    number, not define it."""
    for reported in (2000, 2000, 9000):
        placement_id = make_placement(candidate_id=make_candidate(),
                                      transport_rwf=1000)
        record_transport_report(session, placement_id=placement_id,
                                reported_rwf=reported)
    assert calibration(session)["factor"] == pytest.approx(2.0)


def test_the_factor_is_held_to_a_sane_band(session, make_placement,
                                           make_candidate):
    """A handful of odd reports early on must not make every estimate absurd.

    This feeds the filter that decides who is offered work.
    """
    for _ in range(3):
        placement_id = make_placement(candidate_id=make_candidate(),
                                      transport_rwf=100)
        record_transport_report(session, placement_id=placement_id,
                                reported_rwf=15000)
    calib = calibration(session)
    assert calib["raw_factor"] == pytest.approx(150.0)
    assert calib["factor"] == pytest.approx(2.5)


# --- and what it changes ---------------------------------------------------

def test_a_reported_fare_can_exclude_someone_the_guess_would_have_matched(
    session, make_request, make_candidate, staff_id
):
    """The whole point. Underestimating puts someone in a job that costs them
    money, which is the failure filter 2 exists to prevent.
    """
    request_id = make_request(pay_rwf=5000)
    candidate_id = make_candidate()
    session.execute(
        text("UPDATE candidates SET home_lat = -1.9480, home_lng = 30.1050, "
             "max_commute_rwf = 5000 WHERE candidate_id = :c"),
        {"c": str(candidate_id)},
    )
    session.execute(
        text("UPDATE work_requests SET shift_start = '08:00', "
             "shift_end = '16:00' WHERE request_id = :r"),
        {"r": str(request_id)},
    )
    session.execute(
        text("INSERT INTO availability (candidate_id, day_of_week, start_time, "
             "end_time) SELECT :c, d, '06:00', '20:00' FROM generate_series(0,6) d"),
        {"c": str(candidate_id)},
    )
    session.execute(
        text("INSERT INTO consent_records (candidate_id, purpose, granted, "
             "policy_version, captured_via, captured_by) "
             "VALUES (:c, 'placement', TRUE, 'v1', 'paper', :s)"),
        {"c": str(candidate_id), "s": str(staff_id)},
    )

    before = find_matches(session, request_id)
    assert any(m.candidate.candidate_id == candidate_id for m in before.matches), [
        (r.filter_name, r.reason) for r in before.rejections
    ]

    # The worker reports what it really costs: over 30% of 5,000.
    placement_id = session.execute(
        text("INSERT INTO placements (request_id, candidate_id, status, "
             "agreed_pay_rwf, pay_unit, est_transport_rwf) "
             "VALUES (:r, :c, 'completed', 5000, 'day', 400) "
             "RETURNING placement_id"),
        {"r": str(request_id), "c": str(candidate_id)},
    ).scalar_one()
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=2400, recorded_by=staff_id)

    after = find_matches(session, request_id)
    rejection = next(
        r for r in after.rejections if r.candidate.candidate_id == candidate_id
    )
    assert rejection.filter_name == "transport_viability"
    assert "2,400" in rejection.reason or "2400" in rejection.reason


# --- collected where the coordinator already is ----------------------------

def test_the_fare_is_asked_for_at_the_check_in(web, session, make_placement):
    """The coordinator is already on the telephone and the journey is fresh."""
    from tests.conftest import csrf

    placement_id = make_placement()
    session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', CURRENT_DATE) RETURNING follow_up_id"),
        {"p": str(placement_id)},
    )
    follow_up_id = session.execute(
        text("SELECT follow_up_id FROM follow_ups WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one()

    page = web.get(f"/ui/placements/{placement_id}").text
    assert 'name="transport_rwf"' in page

    done = web.post(
        f"/ui/follow-ups/{follow_up_id}/complete",
        data={"csrf_token": csrf(page), "still_working": "true",
              "transport_rwf": "1400"},
        follow_redirects=True,
    )
    assert "fare RWF 1,400" in done.text
    assert route_history(session, placement_id)[0]["reported_rwf"] == 1400


def test_a_check_in_without_a_fare_still_records(web, session, make_placement):
    """Not every call gets an answer, and the check-in matters either way."""
    from tests.conftest import csrf

    placement_id = make_placement()
    session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', CURRENT_DATE)"),
        {"p": str(placement_id)},
    )
    follow_up_id = session.execute(
        text("SELECT follow_up_id FROM follow_ups WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one()

    page = web.get(f"/ui/placements/{placement_id}").text
    done = web.post(
        f"/ui/follow-ups/{follow_up_id}/complete",
        data={"csrf_token": csrf(page), "still_working": "true"},
        follow_redirects=True,
    )
    assert "Check-in recorded" in done.text
    assert route_history(session, placement_id) == []


def test_an_implausible_fare_does_not_lose_the_check_in(web, session,
                                                        make_placement):
    """The check-in is the more important of the two, and it already happened."""
    from tests.conftest import csrf

    placement_id = make_placement()
    session.execute(
        text("INSERT INTO follow_ups (placement_id, checkpoint, due_on) "
             "VALUES (:p, 'day_1', CURRENT_DATE)"),
        {"p": str(placement_id)},
    )
    follow_up_id = session.execute(
        text("SELECT follow_up_id FROM follow_ups WHERE placement_id = :p"),
        {"p": str(placement_id)},
    ).scalar_one()

    page = web.get(f"/ui/placements/{placement_id}").text
    done = web.post(
        f"/ui/follow-ups/{follow_up_id}/complete",
        data={"csrf_token": csrf(page), "still_working": "true",
              "transport_rwf": "60000"},
        follow_redirects=True,
    )
    assert "implausible" in done.text
    assert session.execute(
        text("SELECT completed_at FROM follow_ups WHERE follow_up_id = :f"),
        {"f": str(follow_up_id)},
    ).scalar_one() is not None


def test_reported_fares_are_shown_beside_what_we_guessed(web, session,
                                                         make_placement):
    placement_id = make_placement(transport_rwf=900)
    record_transport_report(session, placement_id=placement_id,
                            reported_rwf=1400)
    page = web.get(f"/ui/placements/{placement_id}").text
    assert "What the journey actually costs" in page
    assert "1,400" in page and "900" in page


def test_a_fare_can_be_recorded_through_the_api(client, make_placement):
    """The browser form was the only way in. A bulk import of paper follow-up
    sheets needs one too, and a capability in one surface and not the other is
    a divergence that grows."""
    placement_id = make_placement(transport_rwf=800)
    recorded = client.post(f"/placements/{placement_id}/transport",
                           json={"reported_rwf": 1400, "reported_min": 25})
    assert recorded.status_code == 201, recorded.text

    reports = client.get(f"/placements/{placement_id}/transport").json()
    assert reports["reports"][0]["reported_rwf"] == 1400
    assert reports["calibration"]["reports"] == 1


def test_the_api_refuses_an_implausible_fare_too(client, make_placement):
    refused = client.post(f"/placements/{make_placement()}/transport",
                          json={"reported_rwf": 45000})
    assert refused.status_code == 422
    assert "implausible" in refused.json()["detail"]
