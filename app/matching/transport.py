"""Transport cost and commute time estimation.

Filter 2 of the matching engine rejects a placement when transport eats too much
of the wage, so these numbers decide who gets offered work. That makes the
estimator a business input, not a utility function.

**The default fare model is a placeholder and must be calibrated.** It is a
straight-line distance converted to a moto fare with a base plus per-kilometre
rate. Real Kigali fares vary by route, time of day, weather and negotiation, and
straight-line distance understates a hilly city. Replace `MotoFareEstimate`
with a table built from actual receipts collected during the first cohort --
until then, treat every estimate as provisional and confirm the fare with the
candidate before offering.

Overestimating is the safer error: it rejects a placement that might have
worked. Underestimating puts someone in a job that costs them money, which is
the failure mode the filter exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

# Placeholder fare model -- calibrate against real receipts.
MOTO_BASE_RWF = 300
MOTO_PER_KM_RWF = 150
# Straight-line distance understates road distance, more so in a hilly city.
ROAD_WINDING_FACTOR = 1.4
# Average moto speed through Kigali traffic, km/h.
MOTO_SPEED_KMH = 20.0
# Below this, walking is realistic and the fare is zero.
WALKABLE_KM = 1.5
WALKING_SPEED_KMH = 4.5


@dataclass(frozen=True)
class TransportEstimate:
    """A round-trip daily estimate. Both legs -- people come home."""

    daily_rwf: int
    commute_min: int
    straight_line_km: float
    is_estimate: bool = True


def haversine_km(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    """Great-circle distance in kilometres."""
    earth_radius_km = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    return 2 * earth_radius_km * asin(sqrt(a))


def estimate_transport(
    home_lat: float | None,
    home_lng: float | None,
    site_lat: float | None,
    site_lng: float | None,
) -> TransportEstimate | None:
    """Estimate one day's round-trip transport, or None if coordinates are missing.

    Returning None rather than zero is deliberate. A missing estimate is not a
    free commute, and letting it default to zero would silently disable the one
    filter that prevents most 30-day dropouts.
    """
    if None in (home_lat, home_lng, site_lat, site_lng):
        return None

    straight_km = haversine_km(home_lat, home_lng, site_lat, site_lng)
    road_km = straight_km * ROAD_WINDING_FACTOR

    if road_km <= WALKABLE_KM:
        minutes = round(2 * road_km / WALKING_SPEED_KMH * 60)
        return TransportEstimate(0, minutes, round(straight_km, 3))

    one_way_rwf = MOTO_BASE_RWF + MOTO_PER_KM_RWF * road_km
    minutes = round(2 * road_km / MOTO_SPEED_KMH * 60)
    # Round to RWF 50: nobody quotes a moto fare to the franc.
    daily = int(round(2 * one_way_rwf / 50) * 50)
    return TransportEstimate(daily, minutes, round(straight_km, 3))
