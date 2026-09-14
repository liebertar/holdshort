"""What a filing must look like before anyone judges it.

The leaf of backend/runtime: it imports nothing from the package, so the five modules that
need ROUTED never take it from a sibling mixin.
"""

import math

from shared.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON

# Longest allowed leg. The service radius is 11 km, so no route inside it has a longer leg.
# A half-globe leg with merely finite coordinates made the judgement walk 1e10 index-grid
# cells and never finish, while the runtime thread held the GIL and the world and arbitration
# froze. It is a form problem, caught before judgement.
MAX_LEG_M = 50_000.0
# Actions that carry a route. Only these are judged against airspace and intents.
ROUTED = ("reserve_pad", "fly_route")
# Refusals the advisory does not count as consecutive refusals. The path is not blocked: the
# same filing was sent twice (duplicate), or the runtime is not ready to judge yet
# (airspace_not_loaded).
NOT_REFUSALS = ("duplicate", "airspace_not_loaded")


def _form_problem(legs) -> str | None:
    """Whether legs are in a form judgement can take, and if not, what's wrong. A form
    check, not a judgement.

    Coordinates must be on Earth (|lat| ≤ 90, |lon| ≤ 180), altitude above ground (≥ 0), and
    each leg at most MAX_LEG_M. Merely finite values don't qualify: a negative altitude slipped
    'under' every zone and flew through buildings, and a 1e300 coordinate made judgement never
    finish.
    """
    if not isinstance(legs, list) or len(legs) < 2:
        return "legs must be a list of two or more points"
    previous = None
    for index, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict):
            return f"point {index} is not an object"
        try:
            lat, lon, alt = float(leg["lat"]), float(leg["lon"]), float(leg.get("alt_m", 0.0))
        except (KeyError, TypeError, ValueError):
            return f"point {index} has no numeric lat/lon/alt_m"
        if not all(math.isfinite(value) for value in (lat, lon, alt)):
            return f"point {index} is not a finite number"
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return f"point {index} is not a coordinate on Earth"
        if alt < 0.0:
            return f"point {index} altitude is below ground ({alt:.0f} m)"
        if previous is not None:
            length = math.hypot((lat - previous[0]) * METRES_PER_DEG_LAT,
                                (lon - previous[1]) * METRES_PER_DEG_LON)
            if length > MAX_LEG_M:
                return (f"leg {index - 1} is too long "
                        f"({length / 1000:.0f}km > {MAX_LEG_M / 1000:.0f}km)")
        previous = (lat, lon)
    return None
