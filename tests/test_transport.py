"""Transport estimation.

These numbers decide who gets offered work, so they are business logic. The
fare model itself is a placeholder pending calibration against real receipts --
what is tested here is the behaviour around it, which must hold whatever the
rates turn out to be.
"""

from __future__ import annotations

from app.matching.transport import estimate_transport, haversine_km

SITE = (-1.9550, 30.1150)


def test_distance_is_symmetric_and_zero_at_a_point():
    assert haversine_km(*SITE, *SITE) == 0
    assert haversine_km(-1.95, 30.11, -1.90, 30.00) == haversine_km(
        -1.90, 30.00, -1.95, 30.11
    )


def test_missing_coordinates_give_no_estimate_not_a_free_commute():
    """None, never zero. Zero would silently disable the transport filter."""
    assert estimate_transport(None, None, *SITE) is None
    assert estimate_transport(*SITE, None, None) is None


def test_a_walkable_distance_costs_nothing_but_still_takes_time():
    estimate = estimate_transport(-1.9550, 30.1150, -1.9555, 30.1155)
    assert estimate.daily_rwf == 0
    assert estimate.commute_min > 0


def test_cost_and_time_rise_with_distance():
    near = estimate_transport(*SITE, -1.9480, 30.1050)
    far = estimate_transport(*SITE, -1.8900, 29.9800)
    assert far.daily_rwf > near.daily_rwf
    assert far.commute_min > near.commute_min


def test_the_estimate_is_a_round_trip():
    """People come home. A one-way figure would halve the real cost."""
    estimate = estimate_transport(*SITE, -1.9480, 30.1050)
    one_way_km = estimate.straight_line_km
    assert estimate.commute_min > one_way_km / 20 * 60


def test_fares_are_rounded_to_something_quotable():
    estimate = estimate_transport(*SITE, -1.9000, 30.0000)
    assert estimate.daily_rwf % 50 == 0
