"""The drone operator's own route planner.

Drawing the route is the operator's job. They know their aircraft, their schedule and
their customers. What they cannot do is decide whether the route is allowed — that is
somebody else's call, and this file never makes it.

So the planner draws the cheapest line it can, submits it, and if the runtime says no it
draws another one that avoids what it was told about. The operator's own copy of the
airspace may be stale or read differently; the disagreement is resolved by asking, not by
assuming.
"""

import os

from shared.geo import Airspace, Volume, first_breach
from shared.route import Router


class OperatorPlanner:
    def __init__(self, airspace: Airspace | None = None):
        # Our company's copy of the airspace. It may not be the latest.
        self.airspace = airspace or Airspace()
        self.router = Router(self.airspace)
        self.learned: set[str] = set()   # what the runtime has told us

    def load(self, raw_volumes: list[dict]) -> None:
        for raw in raw_volumes:
            self.airspace.add(Volume.from_dict(raw))

    def note_refusal(self, volume_id: str | None) -> None:
        """Record a zone named in a refusal reason on our map too."""
        if volume_id:
            self.learned.add(volume_id)

    def draw(self, start: tuple[float, float], goal: tuple[float, float]) -> list[dict] | None:
        """Straight line if possible, else a detour. None if neither works."""
        route = self.router.plan(start, goal)
        if route is None:
            return None
        return [leg.to_dict() for leg in route.legs]

    def candidates(self, start: tuple[float, float], goal: tuple[float, float],
                   context: dict | None = None, budget_s: float | None = None) -> list[dict]:
        """Up to three legal candidates (route.Router.candidates). This file doesn't choose.

        budget_s is the search limit for one (b)/(c). Without it, ROUTE_CANDIDATE_BUDGET_S;
        without that, the default.
        """
        if budget_s is None and os.getenv("ROUTE_CANDIDATE_BUDGET_S"):
            budget_s = float(os.getenv("ROUTE_CANDIDATE_BUDGET_S"))
        return self.router.candidates(start, goal, context, budget_s)

    def start_blocked(self, start: tuple[float, float], telemetry: dict | None = None) -> bool:
        """Is the start itself inside a forbidden zone (or within its clearance)? Then the
        destination isn't the problem."""
        altitude = float((telemetry or {}).get("alt_m") or self.router.cruise_alt_m)
        return self.airspace.too_close(start[0], start[1], altitude)

    def lift(self, legs: list[dict], shift_m: float) -> list[dict] | None:
        """The same route, every leg raised by shift_m. None if any leg exceeds the ceiling.

        The first fix for a crossing refusal. Another aircraft's corridor spans ±25 m
        vertically, so going 30 m higher clears it. The ceiling (grid, Part 107 default cap,
        1 m margin) is checked on our copy first — filing it knowing it's over would just flash
        one more airspace refusal on screen, a meaningless display.
        """
        lifted = [{**leg, "alt_m": round(float(leg.get("alt_m") or 0.0) + shift_m, 1)}
                  for leg in legs]
        for here, nxt in zip(lifted, lifted[1:], strict=False):
            allowed = self.router._ceiling_allowance((here["lat"], here["lon"]),
                                                     (nxt["lat"], nxt["lon"]))
            if nxt["alt_m"] > allowed:
                return None
        if first_breach(self.airspace, lifted) is not None:
            return None
        return lifted

    def straight(self, start: tuple[float, float], goal: tuple[float, float],
                 alt_m: float | None = None) -> list[dict]:
        """The cheapest route: one straight line, at the lowest safe altitude per our copy. If
        there is none (roof + clearance exceeds the ceiling), file at the highest altitude
        under the ceiling and let the runtime say why it fails."""
        if alt_m is None:
            alt_m = self.router.leg_altitude(start, goal)
            if alt_m is None:
                alt_m = min(self.router._altitude_at(*start), self.router._altitude_at(*goal))
        return self.straight_at(start, goal, alt_m)

    @staticmethod
    def straight_at(start: tuple[float, float], goal: tuple[float, float],
                    alt_m: float) -> list[dict]:
        """A plan that ignores airspace — the very route the direct wiring files."""
        return [
            {"lat": start[0], "lon": start[1], "alt_m": alt_m},
            {"lat": goal[0], "lon": goal[1], "alt_m": alt_m},
        ]
