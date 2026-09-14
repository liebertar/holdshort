"""Whether a filed route complies with the airspace, the columns and its own endpoints.

Reads airspace, pad_coords and telemetry; writes nothing.
"""

import math

from backend.runtime.form import ROUTED, _form_problem
from shared.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    TRAFFIC_LATERAL_M,
    Volume,
    first_breach,
    vertical_column,
)
from shared.models import Proposal


class RouteCheckMixin:
    def check_route(self, proposal: Proposal, checks: list[str] | None = None) -> str | None:
        """Does the filed route comply? Drawing routes is not our job.

        The operator knows its aircraft and its schedule and draws the path. All we do is answer
        whether that path is allowed, and if not, say which leg and which zone are the reason.
        The moment we draw the path for them we become the operator, and a bad path becomes our
        responsibility. Authority and execution must stay separate.
        """
        checks = checks if checks is not None else []
        if proposal.action not in ROUTED:
            return None
        legs = proposal.params.get("legs")
        if not legs:
            return None if not self.airspace.all() else "a route must be filed with it"
        checks.append("form")
        malformed = _form_problem(legs)
        if malformed:
            # A form problem, caught before judgement. Non-numeric coordinates passed to the
            # judgement functions killed the request with a 500 and the operator never learned
            # why it was refused. If it is not valid form, say so.
            return f"route is not in valid form ({malformed})"
        # The route's two ends must be where the aircraft is now and where it is going. The
        # autopilot drops the first point and flies from its current position to the second,
        # and after the last point it carries on to the delivery site unjudged — a first point
        # filed somewhere else makes the judged path and the flown path different paths.
        checks.append("endpoints")
        astray = self._endpoint_problem(proposal, legs)
        if astray:
            return astray

        checks.append("route")
        found = first_breach(self.airspace, legs)
        if found is not None:
            segment, volume, why, at = found
            # Record what was blocked and why as values. If the UI parsed the sentence back,
            # every wording change would quietly break the UI.
            self._note_block(proposal, volume, segment, volume.rule, at)
            return f"leg {segment} breaks the rules — {why}"
        # Vertical segments. The takeoff column, waypoint climbs/descents and the landing
        # column are lines through the airspace too. A 60 m building beside the route does not
        # block a 120 m cruise leg, but it does block a column climbing from 0 m to 120 m.
        checks.append("columns")
        column = self._column_breach(proposal, legs)
        if column is not None:
            kind, segment, volume, why, at = column
            self._note_block(proposal, volume, segment, kind, at)
            what = {"takeoff": "takeoff column", "column": f"climb at waypoint {segment}",
                    "landing": "landing column"}[kind]
            return f"{what} breaks the rules — {why}"
        # The end of the route is where it touches down. A path that can pass alongside and a
        # spot that can be descended onto are different standards, so the ring around the end
        # point (LANDING_SEPARATION_M) is checked separately for buildings and restricted zones.
        checks.append("landing")
        last = legs[-1]
        landing = self.airspace.landing_breach(float(last["lat"]), float(last["lon"]))
        if landing is not None:
            volume, gap = landing
            self._note_block(proposal, volume, len(legs) - 1, "landing",
                             (float(last["lat"]), float(last["lon"])))
            return f"{volume.name} around the landing spot ({gap:.0f} m) — cannot touch down"
        return None

    def _endpoint_problem(self, proposal: Proposal, legs: list[dict]) -> str | None:
        """Whether the first point is farther than TRAFFIC_LATERAL_M from the aircraft, or the
        last point from the destination (delivery site or pad).

        Without a position (no telemetry) there is no comparison — that is unknown, not wrong.
        """
        state = self.telemetry.get(proposal.asset_id) or {}
        first = (float(legs[0]["lat"]), float(legs[0]["lon"]))
        last = (float(legs[-1]["lat"]), float(legs[-1]["lon"]))
        if state.get("lat") is not None and state.get("lon") is not None:
            gap = _distance_m((float(state["lat"]), float(state["lon"])), first)
            if gap > TRAFFIC_LATERAL_M:
                self._note_endpoint(proposal, "origin", 1, first, gap)
                return (f"the route's first point is {gap:.0f} m from the aircraft — "
                        "the judged path and the flown path differ")
        goal = None
        if proposal.action == "fly_route" and state.get("job_lat") is not None:
            goal = (float(state["job_lat"]), float(state["job_lon"]))
        elif proposal.action == "reserve_pad":
            goal = self.pad_coords.get(proposal.resource or proposal.params.get("pad"))
        if goal is not None:
            gap = _distance_m((float(goal[0]), float(goal[1])), last)
            if gap > TRAFFIC_LATERAL_M:
                self._note_endpoint(proposal, "destination", len(legs) - 1, last, gap)
                return (f"the route's last point is {gap:.0f} m from the destination — "
                        "what follows is flown unjudged")
        return None

    @staticmethod
    def _note_endpoint(proposal: Proposal, kind: str, segment: int, at: tuple[float, float],
                       gap_m: float) -> None:
        proposal.params = {
            **proposal.params, "blocked_kind": kind, "blocked_leg": segment,
            "blocked_at": {"lat": round(at[0], 6), "lon": round(at[1], 6)},
            "blocked_gap_m": round(gap_m, 1),
        }

    @staticmethod
    def _note_block(proposal: Proposal, volume: Volume, segment: int, kind: str,
                    at: tuple[float, float]) -> None:
        proposal.params = {
            **proposal.params,
            "blocked_volume": volume.id,
            "blocked_leg": segment,
            "blocked_name": volume.name,
            "blocked_floor_m": volume.floor_m,
            "blocked_kind": kind,
            "blocked_at": {"lat": round(at[0], 6), "lon": round(at[1], 6)},
            "blocked_polygon": [[lat, lon] for lat, lon in volume.polygon],
            "blocked_ceiling_m": volume.ceiling_m,
        }

    def _column_breach(self, proposal: Proposal, legs: list[dict]):
        """The first breach among the takeoff column, waypoint climbs/descents and the landing
        column, as (kind, segment, volume, why, at).

        Judgement is first_breach alone (G7) — the columns are just cut into several
        zero-length segments. For an airborne aircraft re-filing, the takeoff column runs from
        its current altitude to the first leg's altitude. But if its current altitude at that
        spot already breaks the rules (where a recall left it), that is a fact, not a plan, so
        it is skipped — otherwise the aircraft could not even file a way down and would be
        stuck there.
        """
        points = [(float(leg["lat"]), float(leg["lon"]), float(leg.get("alt_m") or 0.0))
                  for leg in legs]
        state = self.telemetry.get(proposal.asset_id, {})
        current_alt = float(state.get("alt_m") or 0.0)
        airborne = current_alt > 1.0
        columns = []
        lat0, lon0, _ = points[0]
        if not (airborne and self.airspace.breach(lat0, lon0, current_alt) is not None):
            columns.append(("takeoff", 1, lat0, lon0, current_alt if airborne else 0.0,
                            points[1][2]))
        for index in range(1, len(points) - 1):
            lat, lon, alt = points[index]
            columns.append(("column", index + 1, lat, lon, alt, points[index + 1][2]))
        lat_n, lon_n, alt_n = points[-1]
        columns.append(("landing", len(points) - 1, lat_n, lon_n, alt_n, 0.0))
        for kind, segment, lat, lon, from_m, to_m in columns:
            found = first_breach(self.airspace, vertical_column(lat, lon, from_m, to_m))
            if found is not None:
                _, volume, why, at = found
                return kind, segment, volume, why, at
        return None


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)
