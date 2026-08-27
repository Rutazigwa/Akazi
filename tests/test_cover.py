"""Same-day cover, which is the guarantee.

The general matcher answers "who should do this job" and ranks on prior work,
retention, assessment score, then commute. That is right for a shift starting
next Tuesday and wrong at 08:40 when the 08:00 cleaner has not arrived: then
the binding constraint is physical, and an excellent candidate 45 minutes away
is worth less than an adequate one 10 minutes away.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from uuid import uuid4

from app.clock import KIGALI
from app.matching.cover import MOBILISATION_MINUTES, find_cover
from app.matching.engine import AvailabilityWindow, Candidate, WorkRequest


TODAY = date.today()


def shift(start="08:00", end="16:00", pay=6000, transport_covered=True):
    h1, m1 = (int(x) for x in start.split(":"))
    h2, m2 = (int(x) for x in end.split(":"))
    return WorkRequest(
        request_id=uuid4(), employer_id=uuid4(), starts_on=TODAY,
        pay_rwf=pay, pay_unit="day",
        shift_start=time(h1, m1), shift_end=time(h2, m2),
        transport_covered=transport_covered,
    )


def worker(name="A", commute=10, prior=0, retention=0.0, score=0,
           transport_rwf=500, busy=False, gender="F", lat=-1.95, lng=30.11):
    return Candidate(
        candidate_id=uuid4(), display_name=name, gender=gender,
        age_eligible=True, has_placement_consent=True,
        availability=[AvailabilityWindow(d, time(6, 0), time(22, 0))
                      for d in range(7)],
        est_commute_min=commute, est_transport_rwf=transport_rwf,
        max_commute_rwf=3000,
        prior_completed_with_employer=prior,
        retention_30day_rate=retention,
        assessment_score=score,
        has_conflicting_commitment=busy,
    )


def at(clock="08:40"):
    h, m = (int(x) for x in clock.split(":"))
    return datetime.combine(TODAY, time(h, m), tzinfo=KIGALI)


def names(result):
    return [o.candidate.display_name for o in result.options]


def refused(result, name):
    return next(r for r in result.rejections if r.candidate.display_name == name)


# --- the physical question -------------------------------------------------

def test_the_nearest_useful_worker_is_offered_first(session=None):
    """Not the best worker. The one who covers most of the gap."""
    result = find_cover(
        [worker("Far", commute=90, prior=5, retention=1.0, score=5),
         worker("Near", commute=10)],
        shift(), now=at("08:40"),
    )
    assert names(result)[0] == "Near"


def test_prior_work_breaks_a_tie_on_arrival(session=None):
    """When two arrive together, the employer's own hands win.

    A same-day cover who has done the job here before needs no induction, and
    the employer is already unhappy.
    """
    result = find_cover(
        [worker("Stranger", commute=10),
         worker("Known", commute=10, prior=3)],
        shift(), now=at("08:40"),
    )
    assert names(result)[0] == "Known"


def test_someone_who_cannot_arrive_before_the_end_is_refused(session=None):
    result = find_cover([worker("Distant", commute=300)],
                        shift(end="16:00"), now=at("14:00"))
    assert names(result) == []
    assert "cannot arrive before the shift ends" in refused(result, "Distant").reason


def test_arriving_with_minutes_left_is_not_cover(session=None):
    """Sending someone for twenty minutes is theatre, and costs them a fare."""
    # 80 minutes away plus mobilisation arrives at 15:40, leaving 20 minutes.
    # Far enough to be useless, near enough that the earlier filter passes.
    result = find_cover([worker("Late", commute=80)],
                        shift(end="16:00"), now=at("14:00"))
    assert "would arrive with only 20 minutes left" in refused(result, "Late").reason


def test_mobilisation_time_is_counted_not_just_travel(session=None):
    """Somebody has to answer the phone, agree, and get out of the door.

    Treating travel as the whole answer promises arrivals that never happen,
    and this is the one promise the business is built on.
    """
    result = find_cover([worker("Near", commute=10)], shift(), now=at("08:40"))
    expected = (at("08:40") + timedelta(minutes=MOBILISATION_MINUTES + 10)).time()
    assert result.options[0].arrives_at == expected
    assert expected == time(9, 10)


def test_someone_already_on_a_shift_is_refused(session=None):
    result = find_cover([worker("Busy", commute=5, busy=True)],
                        shift(), now=at("08:40"))
    assert names(result) == []
    assert refused(result, "Busy").filter_name == "already working"


def test_someone_with_no_home_location_cannot_be_promised(session=None):
    """A general match tolerates this; a promised arrival time cannot."""
    result = find_cover([worker("Unknown", commute=None)],
                        shift(), now=at("08:40"))
    assert "arrival time cannot be estimated" in refused(result, "Unknown").reason


# --- the shift itself ------------------------------------------------------

def test_a_shift_with_too_little_left_is_not_covered_at_all(session=None):
    """The answer is a replacement day, not a worker sent for half an hour."""
    result = find_cover([worker("Near", commute=5)],
                        shift(end="16:00"), now=at("15:40"))
    assert result.viable is False
    assert "replacement day" in result.note
    assert result.options == []


def test_a_shift_that_already_ended_is_not_covered(session=None):
    """The clock wrapping past midnight must not read as a free day."""
    result = find_cover([worker("Near", commute=5)],
                        shift(end="16:00"), now=at("21:00"))
    assert result.viable is False


def test_a_request_with_no_shift_end_has_no_window(session=None):
    request = WorkRequest(
        request_id=uuid4(), employer_id=uuid4(), starts_on=TODAY,
        pay_rwf=6000, pay_unit="day",
    )
    result = find_cover([worker()], request)
    assert result.viable is False
    assert "no shift end" in result.note


def test_the_remaining_window_is_reported(session=None):
    result = find_cover([worker("Near", commute=10)],
                        shift(end="16:00"), now=at("08:40"))
    assert result.minutes_remaining == 7 * 60 + 20
    assert result.options[0].minutes_covered == 7 * 60 + 20 - MOBILISATION_MINUTES - 10


# --- the rules that still apply --------------------------------------------

def test_transport_viability_still_applies(session=None):
    """An emergency is not a reason to send someone on an unaffordable fare."""
    result = find_cover(
        [worker("Costly", commute=10, transport_rwf=2500)],
        shift(pay=5000, transport_covered=False), now=at("08:40"),
    )
    assert names(result) == []
    assert refused(result, "Costly").filter_name == "transport viability"


def test_the_after_dark_safety_rule_still_applies(session=None):
    """Urgency is exactly when a safety rule gets quietly skipped."""
    late = shift(start="14:00", end="22:00", transport_covered=False)
    result = find_cover([worker("Woman", commute=10, gender="F")],
                        late, now=at("14:30"))
    assert refused(result, "Woman").filter_name == "safety"


def test_consent_and_age_still_apply(session=None):
    no_consent = worker("NoConsent", commute=5)
    object.__setattr__(no_consent, "has_placement_consent", False)
    result = find_cover([no_consent], shift(), now=at("08:40"))
    assert refused(result, "NoConsent").filter_name == "hard exclusion"


# --- what the coordinator says out loud ------------------------------------

def test_the_reason_is_something_you_can_read_to_an_employer(session=None):
    result = find_cover(
        [worker("Aline", commute=10, prior=2, retention=0.8)],
        shift(), now=at("08:40"),
    )
    reason = result.options[0].reason
    assert "can be there by 09:10" in reason
    assert "has worked here 2 times" in reason
    assert "10-min commute" in reason


# --- the page a coordinator opens with an employer on the telephone --------

def cover_setup(session, make_placement, employer_id):
    """A placement whose worker did not arrive, plus somebody nearby."""
    from sqlalchemy import text

    placement_id = make_placement()
    session.execute(
        text("UPDATE work_requests SET shift_start = '08:00', "
             "shift_end = '18:00', starts_on = CURRENT_DATE, "
             "transport_covered = TRUE"),
    )
    session.execute(
        text("UPDATE placements SET status = 'no_show' "
             "WHERE placement_id = :p"),
        {"p": str(placement_id)},
    )
    session.execute(
        text("INSERT INTO attendance (placement_id, work_date, present, "
             "confirmed_by, confirmed_at, absence_reason) "
             "VALUES (:p, CURRENT_DATE, FALSE, 'employer', now(), 'no show')"),
        {"p": str(placement_id)},
    )
    return placement_id


def test_the_cover_page_opens_on_a_no_show(web, session, make_placement,
                                           employer_id):
    placement_id = cover_setup(session, make_placement, employer_id)
    page = web.get(f"/ui/placements/{placement_id}/cover")
    assert page.status_code == 200
    assert "Cover" in page.text


def test_the_cover_page_refuses_a_placement_nobody_was_absent_from(
    web, make_placement
):
    """Otherwise it invites a replacement for a shift that is going fine."""
    page = web.get(f"/ui/placements/{make_placement()}/cover",
                   follow_redirects=True)
    assert "marked absent" in page.text


def test_the_dashboard_links_to_it(web, session, make_placement, employer_id):
    cover_setup(session, make_placement, employer_id)
    assert "/cover" in web.get("/ui/").text
