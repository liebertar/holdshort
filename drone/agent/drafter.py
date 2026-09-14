"""The operator lets a small model sketch the route. The runtime still decides.

This is the strongest claim the project makes: who draws the line changes nothing about
the guarantee. A route drafted by Nemotron goes through exactly the same judge as one
from A*, and gets refused for exactly the same reasons. So the model may be wrong, slow,
or absent, and nothing it produces is executed until `first_breach` and the runtime have
said yes.

What the model gets: origin, goal, the rules in one paragraph, and a reading of the map —
what the straight line hits and at what distance, which of those cannot be crossed at any
legal altitude (go around, and which side is open), where the ceiling drops, and the
reason the last attempt was refused. What it returns: a list of legs, or nothing. What
this file does with it: schema check, bounds check, snap the ends, length check, the
operator's own altitude rule per leg (the same one the straight line uses), then the
operator's own pre-judgement with the same `first_breach` the runtime uses. Fails once →
one more ask that names every breach: the obstacle, its roof, the altitude that would
have been needed, whether that is above the limit (then it must be flown around), and
which side is clear. Fails twice → the caller draws with A*.
"""

import json
import math
import os
import time

from shared.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    Volume,
    first_breach,
    leg_breaches,
)
from shared.llm.client import LlmTier, TieredLlm, parse_json_object

MAX_LEGS = 12
# Allowed range for altitudes the model writes. Below the cruise floor (FLOOR_ALT_M 70 m) the
# draft isn't dropped; _apply_altitude_rule raises it by the operator's rule — where to go is
# the model's call, how high is the rule's.
ALT_MIN_M = 40.0
ALT_MAX_M = 120.0
# A detour may be at most this multiple of the straight line. Longer means the model wandered,
# and A* draws it better.
MAX_STRETCH = 2.5
# The service area extends this far (about 2 km) beyond the landing sites' bounding box.
BBOX_MARGIN_DEG = 0.02
# How many obstacles on the straight line to tell the model about. Tell it only the first and
# the draft runs into the next one.
OBSTACLE_LIMIT = 5
# Everything no altitude clears (must go around) is reported. Leave one out and the draft hits
# it.
GO_AROUND_LIMIT = 12
# Sideways distances (m) probed to find where an obstacle opens up; the first open one is
# reported.
SIDE_PROBES_M = (80, 150, 250, 400, 600, 900, 1300, 2000, 3000)
MAX_ASKS = 2
# Budget for one draft, unlike a filing (a few hundred tokens, 6 s): sending a 1,200-token map
# brief and getting 7 legs back took 8.7-8.9 s even on an idle Ollama, and recorded passing
# answers took 5-19 s. With the aircraft-wide timeout (6 s), all 15 drafts were cut off live
# and nano drew no routes at all. The aircraft is waiting on the ground, so this time shows on
# screen as 'redrawing after a refusal'.
# Live run (Ollama, 4 drones): drafts queued on one slot for 19-28 s; at 30 s, 7 of 9 were cut.
DRAFT_TIMEOUT_S = 60.0
# If the server just failed to answer, skip drafts for this long and go to A*. Waiting 5.6 s
# after a timeout and then giving the same server another 30 s keeps the aircraft standing that
# much longer behind a call that won't finish.
DRAFT_BACKOFF_S = 30.0
# Don't ask with less than this left before the deadline: sending the map brief and getting the
# first token alone takes this long.
MIN_ASK_S = 2.0
# Crossing a building takes roof + clearance (50 m). Exactly that height is still inside the
# zone (closed interval), so add 0.5 m — the same arithmetic as the operator's planner
# (route.Router.leg_altitude). If the two differed, the model would hear numbers that differ
# from the planner's.
OVER_ROOF_MARGIN_M = 0.5

SYSTEM = (
    "You draft a flight route for one uncrewed delivery aircraft. You do not fly it and you "
    "do not approve it: a runtime judges every route against the rules below and refuses "
    "anything that breaks them, so draw carefully rather than optimistically. "
    "Reply with one JSON object and nothing else: "
    '{"legs":[{"lat":<deg>,"lon":<deg>,"alt_m":<m>}, ...]}. '
    "A leg is a waypoint; alt_m is the cruise altitude of the segment that ends at that "
    "waypoint. Rules: cruise between 70 and 120 m AGL. To cross a building you must be 50 m "
    "above its roof; if roof + 50 m is above 120 m you must go around it. Stay 10 m laterally "
    "clear of buildings 20 m or taller and 40 m clear of no-fly cells (they are forbidden at "
    "every altitude, so always go around them). Some cells cap the altitude; never plan above "
    "the cap there. The last waypoint is the landing point and needs 50 m clear of buildings "
    "and no-fly cells around it. Use at most 12 legs. The first leg must be the origin and "
    "the last leg the goal, both exactly as given. Every lat/lon must stay inside the service "
    "box. Use decimal degrees with 5 decimals. Open water and parks are the safe corridors "
    "in this city: prefer them, give every obstacle a wide berth (100 m or more, not 15 m), "
    "and detour early rather than clipping the corner of an obstacle."
)


def service_bbox(points: list[tuple[float, float]],
                 margin_deg: float = BBOX_MARGIN_DEG) -> tuple[float, float, float, float] | None:
    """(lat_min, lon_min, lat_max, lon_max), or None without points."""
    if not points:
        return None
    lats = [float(p[0]) for p in points]
    lons = [float(p[1]) for p in points]
    return (min(lats) - margin_deg, min(lons) - margin_deg,
            max(lats) + margin_deg, max(lons) + margin_deg)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def _offset(point: tuple[float, float], north_m: float, east_m: float) -> tuple[float, float]:
    return (point[0] + north_m / METRES_PER_DEG_LAT, point[1] + east_m / METRES_PER_DEG_LON)


def _heading(start: tuple[float, float], goal: tuple[float, float]) -> float:
    """Heading (radians, north 0, east +)."""
    north = (goal[0] - start[0]) * METRES_PER_DEG_LAT
    east = (goal[1] - start[1]) * METRES_PER_DEG_LON
    return math.atan2(east, north)


def _footprint(volume: Volume) -> str:
    lats = [p[0] for p in volume.polygon]
    lons = [p[1] for p in volume.polygon]
    if not lats:
        return "everywhere"
    return f"lat {min(lats):.5f}..{max(lats):.5f}, lon {min(lons):.5f}..{max(lons):.5f}"


# Ways the model misspells the altitude key. In a live run the 4B wrote "alt_ma" in four of ten
# answers (the same typo on retry too), and every one of those drafts was dropped. A key name is
# form, not rule — the value still gets the same range checks and the same judgement — so
# these are accepted.
ALTITUDE_KEYS = ("alt_m", "alt_ma", "altitude_m", "altitude", "alt")


def _altitude_field(leg: dict):
    for key in ALTITUDE_KEYS:
        if key in leg:
            return leg[key]
    raise KeyError("alt_m")


def is_building(volume: Volume) -> bool:
    return volume.id.startswith("bldg-") and volume.ceiling_m is not None


def needed_over(volume: Volume) -> int | None:
    """Altitude needed to cross the building (m, rounded up). None if it isn't a building."""
    if not is_building(volume):
        return None
    return math.ceil(volume.ceiling_m + volume.clearance_m + OVER_ROOF_MARGIN_M)


def breach_words(volume: Volume) -> str:
    """What the pre-check caught, in brief, for the screen card, e.g.
    "crossed bldg-t02452, roof 114 m"."""
    if is_building(volume):
        return f"crossed {volume.id}, roof {volume.ceiling_m:.0f} m"
    if volume.rule == "ceiling" and volume.ceiling_m is not None:
        return f"above the {volume.ceiling_m:.0f} m cap in {volume.id}"
    return f"entered {volume.id}"


def describe(volume: Volume, allowed_m: float = ALT_MAX_M) -> str:
    """One obstacle line for the model to read. Map reading, not grounds for judgement.

    allowed_m is the highest one may climb there (below 120 under a ceiling cell). If the
    needed altitude is higher, there is no way over, so the line says 'go around'.
    """
    needed = needed_over(volume)
    if needed is not None:
        how = (f"cross it at {needed} m or higher" if needed <= allowed_m
               else f"would need {needed} m, above the {allowed_m:.0f} m limit — go around it")
        return (f"building {volume.id} ({volume.name}, roof {volume.ceiling_m:.0f} m; {how}), "
                f"footprint {_footprint(volume)}")
    if volume.rule == "forbidden":
        band = "at every altitude" if volume.ceiling_m is None else f"up to {volume.top_m:.0f} m"
        return (f"no-fly cell {volume.id} ({volume.name}, forbidden {band}, keep 40 m away, "
                f"go around it), footprint {_footprint(volume)}")
    if volume.rule == "ceiling" and volume.ceiling_m is not None:
        return (f"altitude cap {volume.name}: at most {volume.ceiling_m:.0f} m, "
                f"footprint {_footprint(volume)}")
    return f"{volume.name}, footprint {_footprint(volume)}"


class ModelDrafter:
    def __init__(self, llm: TieredLlm, planner, tier: LlmTier = LlmTier.NANO,
                 bbox: tuple[float, float, float, float] | None = None,
                 timeout_s: float | None = None, backoff_s: float | None = None):
        self.llm = llm
        self.planner = planner          # the operator's airspace copy and straight-line plan
        self.tier = tier
        self.bbox = bbox
        self.model = llm.model_for(tier)
        self.timeout_s = (float(os.getenv("DRAFT_TIMEOUT_S", str(DRAFT_TIMEOUT_S)))
                          if timeout_s is None else float(timeout_s))
        self.backoff_s = (float(os.getenv("DRAFT_BACKOFF_S", str(DRAFT_BACKOFF_S)))
                          if backoff_s is None else float(backoff_s))
        # After a draft call is cut off, don't ask again until this time. Separate from filing
        # (6 s) timeouts — with one shared server state, every filing cut off on a busy Ollama
        # skipped drafts for 30 s, and a live run got not a single nano draft.
        self.skip_until = 0.0
        self.last_attempts = 0
        self.last_failures: list[str] = []
        self.last_raised = 0            # legs raised by the operator's altitude rule
        # For the screen card (model_trace.route.draft): what the last draft hit, and the
        # model's total time.
        self.last_breach: str | None = None
        self.last_latency_ms = 0
        self._volumes_by_id: dict[str, Volume] = {}
        self._volumes_for = -1

    @property
    def enabled(self) -> bool:
        return bool(self.llm.enabled and self.model)

    @property
    def name(self) -> str:
        return f"{self.tier.value}:{self.model}"

    # ---------- drawing ----------

    def draft(self, start: tuple[float, float], goal: tuple[float, float],
              context: dict | None = None, deadline: float | None = None) -> list[dict] | None:
        """Ask the model up to twice and return only a draft that passes. Otherwise None —
        A*'s turn.

        deadline is a monotonic time. If given, both asks together stop at it — the caller
        waits only one draft budget (timeout_s) from the refusal before going to A*, so an ask
        still pending on the server after that is useless even if it answers. Without it, each
        ask gets its own budget.
        """
        self.last_attempts = 0
        self.last_failures = []
        self.last_raised = 0
        self.last_breach = None
        self.last_latency_ms = 0
        if not self.enabled:
            return None
        if time.monotonic() < self.skip_until:
            # A draft call was just cut off. Asking again just means waiting again, so A* takes
            # this one.
            self.last_failures.append("server unreachable a moment ago, skipped")
            return None
        airspace = self.planner.airspace
        if airspace.landing_breach(goal[0], goal[1]) is not None:
            return None      # can't land there: no draft helps, and A* gives the same answer

        bbox = self.bbox or service_bbox([start, goal])
        brief = self._brief(start, goal, bbox, context)
        user = brief
        took_s = 0.0          # time the last ask took; a retry won't finish any faster
        for _ in range(MAX_ASKS):
            budget = self._budget(deadline)
            if budget < MIN_ASK_S:
                self.last_failures.append("draft budget exhausted, not asked")
                return None
            if deadline is not None and budget < took_s:
                # The first answer took longer than the budget left. Another ask of the same
                # size on the same server just waits for an answer past the deadline (live run:
                # 11 retries cut off, 0 passed).
                self.last_failures.append(
                    f"retry skipped: {budget:.0f} s left, the first ask took {took_s:.0f} s")
                return None
            self.last_attempts += 1
            reply = self.llm.ask(self.tier, SYSTEM, user, max_tokens=700, json_object=True,
                                 timeout_s=budget)
            if reply is None:
                self.last_failures.append("no reply")
                self.skip_until = time.monotonic() + self.backoff_s
                return None   # server missing or slow; asking again just waits again
            took_s = reply.latency_ms / 1000.0
            self.last_latency_ms += reply.latency_ms
            form = parse_json_object(reply.text)
            legs, problem = self.validate(form, start, goal, bbox)
            if legs is None:
                self.llm.discard(self.tier)
                self.last_failures.append(problem)
                self.last_breach = problem
                user = self._retry_brief(brief, reply.text[:1500], [problem])
                continue
            legs = self._apply_altitude_rule(legs)
            breaches = self.breaches_along(legs)
            if not breaches:
                return legs
            self.llm.discard(self.tier)
            self.last_breach = breach_words(breaches[0][1])
            lines = self.feedback_lines(legs, breaches)
            self.last_failures.append("; ".join(lines))
            user = self._retry_brief(brief, json.dumps({"legs": legs}), lines)
        return None

    def _budget(self, deadline: float | None) -> float:
        """Time for this ask: the budget, or the time left before the deadline if less."""
        if deadline is None:
            return self.timeout_s
        return min(self.timeout_s, deadline - time.monotonic())

    # ---------- form checks (done by code; the model can't change them) ----------

    @staticmethod
    def validate(form, start: tuple[float, float], goal: tuple[float, float],
                 bbox: tuple[float, float, float, float]) -> tuple[list[dict] | None, str | None]:
        """Form, count, box, altitude, end points, length. Any violation → (None, reason)."""
        if not isinstance(form, dict) or not isinstance(form.get("legs"), list):
            return None, "not a {\"legs\": [...]} object"
        raw = form["legs"]
        if len(raw) < 2:
            return None, f"{len(raw)} legs, need at least 2"
        if len(raw) > MAX_LEGS:
            return None, f"{len(raw)} legs, at most {MAX_LEGS}"
        legs = []
        for index, leg in enumerate(raw, start=1):
            if not isinstance(leg, dict):
                return None, f"leg {index} is not an object"
            try:
                lat, lon = float(leg["lat"]), float(leg["lon"])
                alt = float(_altitude_field(leg))
            except (KeyError, TypeError, ValueError):
                return None, f"leg {index} lacks numeric lat/lon/alt_m"
            if not all(math.isfinite(v) for v in (lat, lon, alt)):
                return None, f"leg {index} is not finite"
            if not ALT_MIN_M <= alt <= ALT_MAX_M:
                return None, f"leg {index} alt_m {alt:.0f} outside {ALT_MIN_M:.0f}..{ALT_MAX_M:.0f}"
            if not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
                return None, f"leg {index} ({lat:.5f},{lon:.5f}) outside the service box"
            legs.append({"lat": round(lat, 6), "lon": round(lon, 6), "alt_m": round(alt, 1)})
        # We know both ends. If the model writes them slightly off, the facts win for the start
        # and the destination.
        legs[0] = {**legs[0], "lat": round(start[0], 6), "lon": round(start[1], 6)}
        legs[-1] = {**legs[-1], "lat": round(goal[0], 6), "lon": round(goal[1], 6)}
        # The same point twice makes a zero-length leg: judgeable, but meaningless.
        distinct = [legs[0]]
        for leg in legs[1:]:
            if distance_m((distinct[-1]["lat"], distinct[-1]["lon"]),
                          (leg["lat"], leg["lon"])) >= 1.0:
                distinct.append(leg)
        if len(distinct) < 2:
            return None, "all legs at the same place"
        straight = distance_m(start, goal)
        total = sum(distance_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                    for a, b in zip(distinct, distinct[1:], strict=False))
        if total > MAX_STRETCH * straight + 50.0:
            return None, (f"route {total:.0f} m is longer than {MAX_STRETCH}x the straight "
                          f"{straight:.0f} m")
        return distinct, None

    def _apply_altitude_rule(self, legs: list[dict]) -> list[dict]:
        """Where the model's altitude won't do for a leg, replace it by the operator's rule
        (the lowest safe altitude).

        The model drew where to go; how high follows the same rule as a straight-line filing
        (planner.straight sets altitudes with leg_altitude too). If no altitude can fly the
        leg, the model's value stays and judgement states the reason.
        """
        router = self.planner.router
        fixed = [dict(legs[0])]
        for here, nxt in zip(legs, legs[1:], strict=False):
            a, b = (here["lat"], here["lon"]), (nxt["lat"], nxt["lon"])
            segment = [{"lat": a[0], "lon": a[1], "alt_m": nxt["alt_m"]},
                       {"lat": b[0], "lon": b[1], "alt_m": nxt["alt_m"]}]
            altitude = nxt["alt_m"]
            floor = min(router.floor_alt_m, ALT_MAX_M)
            if altitude < floor:
                # Low buildings missing from the judgement data (under 20 m) still need 50 m
                # above them. A model value below that is raised to the rule's floor. If the
                # ceiling cell is lower still, judgement says so below.
                segment = [{**segment[0], "alt_m": floor}, {**segment[1], "alt_m": floor}]
                altitude = floor
                self.last_raised += 1
            if first_breach(self.planner.airspace, segment) is not None:
                safe = router.leg_altitude(a, b)
                if safe is not None and ALT_MIN_M <= safe <= ALT_MAX_M:
                    altitude = round(safe, 1)
                    self.last_raised += 1
            fixed.append({**nxt, "alt_m": altitude})
        return fixed

    # ---------- what the model is shown ----------

    def _volume(self, volume_id: str | None) -> Volume | None:
        airspace = self.planner.airspace
        if self._volumes_for != airspace.revision:
            self._volumes_by_id = {v.id: v for v in airspace.all()}
            self._volumes_for = airspace.revision
        return self._volumes_by_id.get(volume_id or "")

    def breaches_along(self, legs: list[dict], limit: int = OBSTACLE_LIMIT) -> list:
        """What the route runs into, in order: [(leg, zone, reason, position)].

        Collects everything each leg runs into, in order along it (geo.leg_breaches). It used
        to re-ask from just past the first hit, which stopped behind a single low building at
        the end of a short leg and left the must-go-around building beyond it off the list.
        Each zone once.
        """
        found = []
        seen: set[str] = set()
        for index in range(len(legs) - 1):
            for _fraction, volume, why, at in leg_breaches(self.planner.airspace, legs[index],
                                                           legs[index + 1]):
                if volume.id in seen:
                    continue
                seen.add(volume.id)
                found.append((index + 1, volume, why, at))
                if len(found) >= limit:
                    return found
        return found

    def obstacles(self, start: tuple[float, float], goal: tuple[float, float],
                  alt_m: float, limit: int = OBSTACLE_LIMIT) -> list:
        """What the straight line runs into, in order: [(leg, zone, reason, position)]."""
        return self.breaches_along([{"lat": start[0], "lon": start[1], "alt_m": alt_m},
                                    {"lat": goal[0], "lon": goal[1], "alt_m": alt_m}], limit)

    def go_arounds(self, start: tuple[float, float], goal: tuple[float, float]) -> list:
        """What no legal altitude clears on the line — go around these: [(zone, position)].

        Measuring the line at max altitude (120 m) skips buildings that can be crossed lower,
        leaving only buildings whose roof + 50 m exceeds 120 m, cells forbidden at every
        altitude, and lateral clearance. Under a ceiling cell the ceiling is the limit, so
        while crossing that cell the line is measured again just below the ceiling — the 120 m
        scan hits the cell itself and skips its whole footprint, so a 101 m building inside it
        (152 m needed, in a 90 m cell) was missed and the model zigzagged right through it.
        """
        found: dict[str, tuple[Volume, tuple[float, float]]] = {}
        self._collect_go_arounds(start, goal, ALT_MAX_M, found, depth=0)
        ordered = sorted(found.values(), key=lambda pair: distance_m(start, pair[1]))
        return ordered[:GO_AROUND_LIMIT]

    def _collect_go_arounds(self, cursor: tuple[float, float], goal: tuple[float, float],
                            alt_m: float, found: dict, depth: int) -> None:
        """Measure cursor→goal at alt_m and collect what must be gone around into found.

        On hitting a ceiling cell, the stretch across its footprint is measured again at
        (ceiling - 1 m) (recursive, up to three deep — lower cells inside the cell). Anything
        else is filtered with must_go_around.
        """
        airspace = self.planner.airspace
        while len(found) < GO_AROUND_LIMIT and distance_m(cursor, goal) >= 1.0:
            legs = [{"lat": cursor[0], "lon": cursor[1], "alt_m": alt_m},
                    {"lat": goal[0], "lon": goal[1], "alt_m": alt_m}]
            breach = first_breach(airspace, legs)
            if breach is None:
                return
            _, volume, _, at = breach
            beyond = self._past(volume, cursor, goal, at)
            if volume.rule == "ceiling" and volume.ceiling_m is not None and depth < 3:
                cap = volume.ceiling_m - 1.0
                if cap < alt_m:
                    self._collect_go_arounds(at, beyond or goal, cap, found, depth + 1)
            elif volume.id not in found and self.must_go_around(volume, at):
                found[volume.id] = (volume, at)
            if beyond is None:
                return
            cursor = beyond

    def allowed_over(self, at: tuple[float, float]) -> float:
        """Highest altitude allowed at that point (ceiling - 1 m, at most 120 m)."""
        ceiling = self.planner.airspace.ceiling_at(at[0], at[1])
        return ALT_MAX_M if ceiling is None else min(ALT_MAX_M, ceiling - 1.0)

    def must_go_around(self, volume: Volume, at: tuple[float, float]) -> bool:
        """Is there no legal altitude to cross this obstacle at that point?"""
        needed = needed_over(volume)
        if needed is None:
            return volume.rule == "forbidden"     # a forbidden cell ignores altitude
        return needed > self.allowed_over(at)

    @staticmethod
    def _past(volume: Volume, cursor, goal, at) -> tuple[float, float] | None:
        lats = [p[0] for p in volume.polygon] or [at[0]]
        lons = [p[1] for p in volume.polygon] or [at[1]]
        length = distance_m(cursor, goal)
        if length < 1.0:
            return None
        # Step ahead by the diagonal of the zone's bounding box, past every footprint inside it.
        span = distance_m((min(lats), min(lons)), (max(lats), max(lons))) + 30.0
        fraction = min(1.0, (distance_m(cursor, at) + span) / length)
        if fraction >= 1.0:
            return None
        return (cursor[0] + (goal[0] - cursor[0]) * fraction,
                cursor[1] + (goal[1] - cursor[1]) * fraction)

    @staticmethod
    def _compass(north: float, east: float) -> str:
        names = ("north", "north-east", "east", "south-east", "south", "south-west", "west",
                 "north-west")
        return names[int((math.degrees(math.atan2(east, north)) % 360 + 22.5) // 45) % 8]

    def openings(self, at: tuple[float, float], heading_rad: float) -> dict[str, dict]:
        """How far left or right of the obstacle the way opens, measured at the altitude
        allowed there.

        {"left": {"compass", "metres", "point"}, "right": {...}}. metres is None if blocked.
        """
        airspace = self.planner.airspace
        router = self.planner.router
        found = {}
        for label, sign in (("left", 1.0), ("right", -1.0)):
            # Left of the heading (north cos h, east sin h) is it turned 90° counter-clockwise:
            # (north sin h, east -cos h). The signs were first written backwards, and 'left' of
            # a northbound line came out east.
            north = math.sin(heading_rad) * sign
            east = -math.cos(heading_rad) * sign
            opening = {"compass": self._compass(north, east), "metres": None, "point": None}
            for metres in SIDE_PROBES_M:
                point = _offset(at, north * metres, east * metres)
                if not airspace.too_close(point[0], point[1], router._altitude_at(*point)):
                    opening.update(metres=metres, point=point)
                    break
            found[label] = opening
        return found

    def _clear_sides(self, at: tuple[float, float], heading_rad: float) -> str:
        words = []
        for label, opening in self.openings(at, heading_rad).items():
            side = f"{label} ({opening['compass']})"
            if opening["metres"] is None:
                words.append(f"blocked for {SIDE_PROBES_M[-1]} m to the {side}")
            else:
                point = opening["point"]
                words.append(f"clear {opening['metres']} m to the {side} "
                             f"at {point[0]:.5f},{point[1]:.5f}")
        return "; ".join(words)

    @staticmethod
    def pick_side(openings: dict[str, dict], previous: str | None) -> str | None:
        """Pick exactly one side to go around. None if neither side opens.

        The nearer side is the default, but the side chosen at the previous obstacle is kept
        if it opens within twice the distance. In recorded drafts the model alternated 'left
        opens at 80 m' and 'right opens at 80 m' as waypoints and zigzagged through the
        buildings — give it both sides and it uses both.
        """
        open_sides = {label: o for label, o in openings.items() if o["metres"] is not None}
        if not open_sides:
            return None
        nearest = min(open_sides, key=lambda label: open_sides[label]["metres"])
        if previous in open_sides and \
                open_sides[previous]["metres"] <= 2 * open_sides[nearest]["metres"]:
            return previous
        return nearest

    def side_advice(self, at: tuple[float, float], heading_rad: float,
                    previous: str | None = None) -> tuple[str, str | None]:
        """(sentence for the model, side chosen), like 'pass to the south, e.g. via this point'."""
        openings = self.openings(at, heading_rad)
        chosen = self.pick_side(openings, previous)
        if chosen is None:
            return (f"both sides blocked for {SIDE_PROBES_M[-1]} m — detour far earlier", None)
        opening = openings[chosen]
        point = opening["point"]
        words = (f"pass {opening['compass'].upper()} of it ({chosen}), e.g. via "
                 f"{point[0]:.5f},{point[1]:.5f} ({opening['metres']} m off the line)")
        other = "right" if chosen == "left" else "left"
        elsewhere = openings[other]
        if elsewhere["metres"] is not None:
            words += f"; {elsewhere['compass']} is also clear at {elsewhere['metres']} m"
        return words, chosen

    def _ceilings_along(self, start, goal, step_m: float = 100.0) -> list[str]:
        """Stretches of the line with a ceiling under 120 m. A route drawn above it is refused."""
        airspace = self.planner.airspace
        length = distance_m(start, goal)
        samples = max(1, int(length / step_m))
        spans: list[list] = []      # [cap, from_km, to_km]
        for step in range(samples + 1):
            fraction = step / samples
            point = (start[0] + (goal[0] - start[0]) * fraction,
                     start[1] + (goal[1] - start[1]) * fraction)
            ceiling = airspace.ceiling_at(*point)
            cap = None if ceiling is None or ceiling >= ALT_MAX_M else round(ceiling - 1.0)
            km = fraction * length / 1000.0
            if cap is not None and spans and spans[-1][0] == cap and spans[-1][2] >= km - 0.15:
                spans[-1][2] = km
            elif cap is not None:
                spans.append([cap, km, km])
        return [f"altitude capped at {cap} m from {a:.1f} km to {b:.1f} km along the line"
                for cap, a, b in spans]

    def _brief(self, start, goal, bbox, context: dict | None) -> str:
        context = context or {}
        straight = self.planner.straight(start, goal)
        alt_m = float(straight[-1]["alt_m"]) if straight else ALT_MIN_M
        heading = _heading(start, goal)
        length = distance_m(start, goal)
        lines = [
            f"origin {start[0]:.5f},{start[1]:.5f} -> goal {goal[0]:.5f},{goal[1]:.5f} "
            f"(straight {length / 1000:.1f} km, heading {math.degrees(heading) % 360:.0f} deg).",
            f"service box lat {bbox[0]:.4f}..{bbox[2]:.4f}, lon {bbox[1]:.4f}..{bbox[3]:.4f}.",
        ]
        # First, everything no altitude clears. Height can't fix these; they must be bypassed
        # sideways.
        around = self.go_arounds(start, goal)
        if around:
            lines.append("GO AROUND — no legal altitude over these on the straight line "
                         "(roof + 50 m clearance is above the limit there, or forbidden at "
                         "every altitude); climbing cannot help, pass beside them. Pick ONE side "
                         "for a run of neighbouring obstacles and stay on it — never alternate "
                         "between left and right points, that crosses the obstacle:")
            side = None
            for volume, at in around:
                km = distance_m(start, at) / 1000.0
                advice, side = self.side_advice(at, heading, side)
                lines.append(f"- {self._go_around_words(volume, at)} at {km:.1f} km: {advice}")
        hits = self.obstacles(start, goal, alt_m)
        if hits:
            lines.append(f"The straight line at {alt_m:.0f} m is refused. Along it, in order:")
            for _, volume, _, at in hits:
                km = distance_m(start, at) / 1000.0
                # A ceiling cell is flown under, not sidestepped; sides are measured only for
                # forbidden zones.
                sides = f"; {self._clear_sides(at, heading)}" if volume.rule == "forbidden" else ""
                lines.append(f"- at {km:.1f} km: {describe(volume, self.allowed_over(at))}{sides}")
        lines += ["- " + line for line in self._ceilings_along(start, goal)]
        refused = self._volume(context.get("forbids"))
        reason = context.get("reason")
        if reason:
            listed = {volume.id for _, volume, _, _ in hits} | {volume.id for volume, _ in around}
            extra = "" if refused is None or refused.id in listed else f" ({describe(refused)})"
            lines.append(f"The runtime's refusal of the straight line: {reason}{extra}")
        lines.append("Draft a route that avoids all of that, with room to spare. JSON only.")
        return "\n".join(lines)

    def _go_around_words(self, volume: Volume, at: tuple[float, float]) -> str:
        needed = needed_over(volume)
        if needed is None:
            return f"no-fly cell {volume.id} ({volume.name}, forbidden at every altitude)"
        return (f"building {volume.id} ({volume.name}, roof {volume.ceiling_m:.0f} m, would need "
                f"{needed} m, limit there {self.allowed_over(at):.0f} m)")

    def feedback_lines(self, legs: list[dict], breaches: list) -> list[str]:
        """One line per hit. The side to go around stays that of the line before (pick_side)."""
        lines, side = [], None
        for breach in breaches:
            line, side = self.feedback_line(legs, breach, side)
            lines.append(line)
        return lines

    def feedback_line(self, legs: list[dict], breach,
                      previous_side: str | None = None) -> tuple[str, str | None]:
        """For a retry: one concrete line for the model about one hit, and the side chosen.

        What was hit (id, name) and where, how tall the roof is and what altitude was needed,
        whether that exceeds the local limit so the route must go around, and which side is
        open. Told only 'fix it', the model jiggles the same line and refiles. It needs
        numbers: what to move, which way, and how far.
        """
        segment, volume, why, at = breach
        here, nxt = legs[segment - 1], legs[segment]
        heading = _heading((here["lat"], here["lon"]), (nxt["lat"], nxt["lon"]))
        flown = float(nxt["alt_m"])
        where = (f"leg {segment} ({here['lat']:.5f},{here['lon']:.5f} -> "
                 f"{nxt['lat']:.5f},{nxt['lon']:.5f} at {flown:.0f} m)")
        needed = needed_over(volume)
        allowed = self.allowed_over(at)
        if needed is None and volume.rule == "forbidden":
            fix = (f"no-fly cell {volume.id} ({volume.name}) is forbidden at every altitude, "
                   f"so you MUST go around it")
        elif needed is None:
            fix = f"{volume.name}: stay at or below the cap there"
        elif needed > allowed:
            fix = (f"building {volume.id} ({volume.name}): roof {volume.ceiling_m:.0f} m, you "
                   f"would need {needed} m over it, which is above the {allowed:.0f} m limit "
                   f"there, so you MUST fly around it, not over it")
        else:
            fix = (f"building {volume.id} ({volume.name}): roof {volume.ceiling_m:.0f} m, you "
                   f"need {needed} m or higher over it (you flew {flown:.0f} m); climb to "
                   f"{needed} m or fly around it")
        sides, side = "", previous_side
        if volume.rule == "forbidden":
            advice, side = self.side_advice(at, heading, previous_side)
            sides = f"; {advice}"
        return (f"{where} hits {fix} at {at[0]:.5f},{at[1]:.5f}{sides} (judge said: {why})",
                side)

    @staticmethod
    def _retry_brief(brief: str, previous: str, failures: list[str]) -> str:
        listed = "\n".join(f"- {line}" for line in failures)
        return (brief
                + "\n\nYour previous draft was refused by the operator's own pre-check:\n"
                + listed
                + f"\nPrevious draft: {previous}\n"
                "Where it says MUST go around, climbing cannot fix it: move those waypoints to "
                "the clear side named above, by at least that distance, and detour early. "
                "Elsewhere move the legs well away from what they hit (100 m or more). Keep the "
                "rest, and reply with the full JSON object again.")
