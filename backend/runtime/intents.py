"""Operational intents: where an approved route will be, and when.

ASTM F3548-21 calls this an operational intent — a set of 4D volumes (a footprint, an
altitude band, a time window) that an operator shares before flying. Two intents conflict
if and only if a volume of each intersects in space and time (3.2.8); anything more than a
centimetre apart is clear. Strategic deconfliction is refusing the second filing while it
is still a filing, which is much cheaper than separating two aircraft that are already
airborne.

The operator never states the volumes. The runtime derives them from the filed legs and the
performance the operator declared for the aircraft, so a window cannot be understated to
hide a crossing. The runtime still only verifies: it does not move the route, it says which
aircraft, where and until when.
"""

import math
import uuid
from dataclasses import dataclass, field

from shared.config import Performance
from shared.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    TRAFFIC_LATERAL_M,
    TRAFFIC_VERTICAL_M,
    _point_segment_m,
    ground_clamped,
)

# Time margin. Approval checks and loading can run a few seconds late or early, so the window is
# widened on both sides. 30 ticks = 24 s = 530 m at cruise. A wider window is safer, but the
# same airspace can be shared that much less.
TIME_PAD_TICKS = 30
# A new filing's route is sampled at this spacing to find its first point inside another
# aircraft's corridor. Sampling a 30 m corridor every 5 m misses only a path grazing its edge by
# under 5 m, and such a path is 29.9 m away.
SAMPLE_M = 5.0
# F3548 3.2.8: anything more than 1 cm apart does not intersect.
CLEAR_M = 0.01
# Exit tick of a volume with no known end. A recalled aircraft hovering has no known landing
# time, so the volume covers that spot until it lands or a new approval replaces it.
OPEN_ENDED_TICK = 10 ** 9
# How long a lost-link aircraft's reservation is held past its nominal touchdown. The volume's
# own TIME_PAD_TICKS comes on top (nominal touchdown + 60 ticks). With no telemetry, this must
# cover an aircraft that took off late landing a few ticks after nominal. After that it is taken
# to have landed as declared and the path is released, but the landing site stays that
# aircraft's until the link comes back (its intent is still live, so landing_conflict blocks).
LOST_LINK_MARGIN_TICKS = TIME_PAD_TICKS

ACCEPTED = "accepted"     # approved, not yet airborne
ACTIVATED = "activated"   # telemetry says airborne
ENDED = "ended"           # landed, recalled, or replaced by a new approval

# Intent kinds: an approved route (route); where an airborne aircraft that lost its route will
# hold (contingency); the current position of an airborne aircraft with no intent (presence —
# built from telemetry each time, never in the registry).
ROUTE = "route"
CONTINGENCY = "contingency"
PRESENCE = "presence"


@dataclass
class Volume4D:
    """A corridor piece: lateral_m around a→b, altitude band [floor, ceiling], ticks [from, to)."""

    leg: int                          # leg number in the filing (counted like blocked_leg)
    a: tuple[float, float]
    b: tuple[float, float]
    alt_lo: float                     # nominal altitude band; lo == hi on a cruise leg
    alt_hi: float
    t_enter: int                      # nominal entry and exit ticks (before the margin)
    t_exit: int
    lateral_m: float = TRAFFIC_LATERAL_M
    vertical_m: float = TRAFFIC_VERTICAL_M
    pad_ticks: int = TIME_PAD_TICKS

    @property
    def floor_m(self) -> float:
        return ground_clamped(self.alt_lo - self.vertical_m)

    @property
    def ceiling_m(self) -> float:
        return self.alt_hi + self.vertical_m

    @property
    def from_tick(self) -> int:
        return self.t_enter - self.pad_ticks

    @property
    def to_tick(self) -> int:
        return self.t_exit + self.pad_ticks

    @property
    def is_column(self) -> bool:
        return self.a == self.b

    def contains(self, lat: float, lon: float, alt_m: float, tick: int) -> bool:
        """Is the point inside this volume? Checks time → altitude → distance, cheapest first."""
        if not (self.from_tick <= tick < self.to_tick):
            return False
        return self.covers(lat, lon, alt_m)

    def covers(self, lat: float, lon: float, alt_m: float) -> bool:
        """Space only, no time. For a lost-link aircraft the question is where it could be, not
        where it will be and when, so the lost-link reservation and the conformance check after
        recovery use this.
        """
        if not (self.floor_m - CLEAR_M <= alt_m <= self.ceiling_m + CLEAR_M):
            return False
        return _point_segment_m(lat, lon, self.a, self.b)[0] <= self.lateral_m + CLEAR_M

    def polygon(self) -> list[tuple[float, float]]:
        """The segment inflated by lateral_m into a rectangle. Display only; contains judges."""
        north = (self.b[0] - self.a[0]) * METRES_PER_DEG_LAT
        east = (self.b[1] - self.a[1]) * METRES_PER_DEG_LON
        length = math.hypot(north, east)
        if length < 1e-9:
            north, east, length = 1.0, 0.0, 1.0
        ux, uy = north / length, east / length          # unit vector along travel (north, east)
        px, py = -uy, ux                                  # left perpendicular
        r = self.lateral_m

        def at(point, along, side):
            return (point[0] + (ux * along + px * side) / METRES_PER_DEG_LAT,
                    point[1] + (uy * along + py * side) / METRES_PER_DEG_LON)

        return [at(self.a, -r, r), at(self.b, r, r), at(self.b, r, -r), at(self.a, -r, -r)]

    def to_dict(self) -> dict:
        return {
            "leg": self.leg, "polygon": [[lat, lon] for lat, lon in self.polygon()],
            "floor_m": round(self.floor_m, 1), "ceiling_m": round(self.ceiling_m, 1),
            "from_tick": self.from_tick, "to_tick": self.to_tick,
        }


@dataclass
class Intent:
    asset: str
    proposal_id: str
    volumes: list[Volume4D]
    start: tuple[float, float]
    landing: tuple[float, float]
    depart_tick: int
    arrive_tick: int                  # nominal touchdown tick
    filed_tick: int
    state: str = ACCEPTED
    ended_reason: str = ""
    kind: str = ROUTE
    # Lost-link contingency judged at approval (configs/fleet.yaml performance.lost_link).
    # For continue_and_land the contingency volume is the approved route plus the landing
    # column, which is exactly these volumes.
    contingency: str = ""
    # Once a lost link has extended the reservation: the first tick without fresh telemetry.
    # None if the link is not lost.
    dark_since: int | None = None
    id: str = field(default_factory=lambda: f"i_{uuid.uuid4().hex[:10]}")
    # Windows before the extension [(t_enter, t_exit)]; restored when the link comes back.
    _windows: list | None = field(default=None, repr=False, compare=False)

    @property
    def from_tick(self) -> int:
        return min(v.from_tick for v in self.volumes)

    @property
    def to_tick(self) -> int:
        return max(v.to_tick for v in self.volumes)

    @property
    def live(self) -> bool:
        return self.state in (ACCEPTED, ACTIVATED)

    def covers(self, lat: float, lon: float, alt_m: float) -> bool:
        """Is this position inside any approved volume (ignoring time)?

        The conformance check after the link comes back.
        """
        return any(volume.covers(lat, lon, alt_m) for volume in self.volumes)

    def reserve_dark(self, last_seen_tick: int, at: tuple[float, float, float] | None,
                     margin_ticks: int = LOST_LINK_MARGIN_TICKS) -> int:
        """The link is lost. Block the whole remaining route from the last-seen tick until
        nominal touchdown plus the margin.

        With no telemetry we do not know where along the remaining route the aircraft is. The
        declared contingency (continue_and_land) flies the approved route as filed and lands at
        the destination, so every remaining volume may be in use at any tick in between. Volumes
        already passed (behind the last-seen position and exited on schedule too) are left as
        they are: re-blocking the take-off column it left would keep every aircraft at the
        neighbouring spot on the ground. Returns the tick the reservation is released.
        """
        if self._windows is None:
            self._windows = [(volume.t_enter, volume.t_exit) for volume in self.volumes]
        start = self._remaining_from(last_seen_tick, at)
        landing = last_seen_tick if self.arrive_tick >= OPEN_ENDED_TICK else self.arrive_tick
        until = max(landing, last_seen_tick) + margin_ticks
        for volume in self.volumes[start:]:
            volume.t_enter = min(volume.t_enter, last_seen_tick)
            if volume.t_exit < OPEN_ENDED_TICK:
                volume.t_exit = max(volume.t_exit, until)
        self.dark_since = last_seen_tick + 1
        return max(volume.to_tick for volume in self.volumes[start:])

    def release_dark(self) -> None:
        """The link is back. Put the extended windows back as approved: it is visible again."""
        if self._windows is not None:
            for volume, (enter, leave) in zip(self.volumes, self._windows, strict=True):
                volume.t_enter, volume.t_exit = enter, leave
        self._windows = None
        self.dark_since = None

    def _remaining_from(self, tick: int, at: tuple[float, float, float] | None) -> int:
        """Index of the volume where the remaining route starts: the earlier of the first volume
        not yet exited on schedule and the first volume covering the last-seen position. For a
        late aircraft the position points earlier; for an early one, the schedule does.
        """
        by_time = next((index for index, volume in enumerate(self.volumes)
                        if volume.to_tick > tick), len(self.volumes) - 1)
        if at is None:
            return by_time
        by_place = next((index for index, volume in enumerate(self.volumes)
                         if volume.covers(*at)), None)
        return by_time if by_place is None else min(by_time, by_place)

    def reanchor(self, depart_tick: int) -> int:
        """Move the time windows to the actual departure tick. Returns the shift in ticks
        (negative if it took off early).

        An aircraft that took off before its approved window is not in that window. Left as it
        is, the intent would have the judgement blocking empty sky while the real aircraft sits
        in no window at all: registering what is actually there comes first.
        """
        shift = depart_tick - self.depart_tick
        for volume in self.volumes:
            volume.t_enter += shift
            if volume.t_exit < OPEN_ENDED_TICK:
                volume.t_exit += shift
        self.depart_tick += shift
        if self.arrive_tick < OPEN_ENDED_TICK:
            self.arrive_tick += shift
        return shift

    def to_dict(self) -> dict:
        return {"asset": self.asset, "state": self.state, "id": self.id, "kind": self.kind,
                "from_tick": self.from_tick, "to_tick": self.to_tick,
                "depart_tick": self.depart_tick, "arrive_tick": self.arrive_tick,
                "proposal_id": self.proposal_id, "ended": self.ended_reason or None,
                "contingency": self.contingency or None, "dark_since": self.dark_since}

@dataclass
class Conflict:
    asset: str                        # the other aircraft
    intent_id: str | None             # None for an aircraft with no intent (from telemetry)
    kind: str                         # traffic | landing
    at: tuple[float, float]           # first point where the new route enters the other volume
    leg: int
    until_tick: int                   # tick the other volume clears; take-off is fine from then
    alt_m: float = 0.0
    tick: int = 0


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def corridor_widths(performance: Performance) -> tuple[float, float]:
    """Half-widths of the other aircraft's corridor (lateral, vertical).

    The separation minimum plus the navigation error the operator declared. Drawn at the minimum
    alone, two approvals 31 m apart lose separation when one strays by just 1 m, and the
    simulator's waypoint radius (6 m, cutting corners) is enough to do that. Vertically, one
    tick of climb or descent: the error while altitude is matched at a vertex.
    """
    step = max(performance.climb_mps, performance.descent_mps) * performance.seconds_per_tick
    return TRAFFIC_LATERAL_M + performance.nav_tolerance_m, TRAFFIC_VERTICAL_M + step


def schedule(legs: list[dict], depart_tick: int, start_alt_m: float,
             performance: Performance) -> tuple[list[Volume4D], int]:
    """When each leg is entered and left, from the declared performance.

    Returns (volumes, nominal touchdown tick). A vertical climb at the start to the first leg's
    altitude, legs at cruise speed, vertical climb or descent at each vertex, and descent to the
    ground at the end: exactly the order the simulator flies (World._advance). Vertical parts get
    their own zero-length volumes. While climbing from 40 m to 118 m the aircraft is in neither
    cruise band, so with only the two bands another aircraft passing between them goes unseen.
    """
    step_m = performance.cruise_mps * performance.seconds_per_tick
    climb_m = performance.climb_mps * performance.seconds_per_tick
    descent_m = performance.descent_mps * performance.seconds_per_tick
    lateral, vertical = corridor_widths(performance)

    def change_ticks(from_m: float, to_m: float) -> int:
        rate = climb_m if to_m > from_m else descent_m
        return int(math.ceil(abs(to_m - from_m) / rate)) if rate > 0 else 0

    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
    altitudes = [ground_clamped(float(leg.get("alt_m") or 0.0)) for leg in legs]
    volumes: list[Volume4D] = []
    tick = depart_tick
    height = ground_clamped(start_alt_m)
    for index in range(len(points) - 1):
        target = altitudes[index + 1]
        # climb or descend in place at the vertex (the take-off column at the first point)
        rising = change_ticks(height, target)
        if rising or index == 0:
            volumes.append(Volume4D(index + 1, points[index], points[index],
                                    min(height, target), max(height, target), tick, tick + rising,
                                    lateral, vertical))
            tick += rising
        height = target
        # cruise along the leg
        moving = int(math.ceil(_distance_m(points[index], points[index + 1]) / step_m))
        volumes.append(Volume4D(index + 1, points[index], points[index + 1],
                                height, height, tick, tick + moving, lateral, vertical))
        tick += moving
    arrive = tick + change_ticks(height, 0.0)
    volumes.append(Volume4D(len(points) - 1, points[-1], points[-1], 0.0, height, tick, arrive,
                            lateral, vertical))
    return volumes, arrive


def hold(asset: str, at: tuple[float, float], alt_m: float, now: int, performance: Performance,
         exit_point: tuple[float, float] | None = None, kind: str = CONTINGENCY,
         proposal_id: str = "") -> Intent:
    """The space an airborne aircraft without a route covers. It has no end tick.

    An aircraft whose intent ended in a recall, a withdrawal or a declined job hovers where it
    is and waits (after flying to the nearest point outside, if it was inside a zone). Dropping
    it from the judgement for lack of an intent would clear the next filing through that spot
    as is: an airborne aircraft is always somewhere, and strategic deconfliction has to see
    that spot. It ends when the aircraft lands (observe: arrived) or a new route is approved
    (accept: replaced).
    """
    lateral, vertical = corridor_widths(performance)
    volumes: list[Volume4D] = []
    tick, here = now, at
    if exit_point is not None and _distance_m(at, exit_point) > CLEAR_M:
        step_m = performance.cruise_mps * performance.seconds_per_tick
        moving = int(math.ceil(_distance_m(at, exit_point) / step_m))
        volumes.append(Volume4D(1, at, exit_point, alt_m, alt_m, now, now + moving,
                                lateral, vertical))
        tick, here = now + moving, exit_point
    volumes.append(Volume4D(len(volumes) + 1, here, here, alt_m, alt_m, tick, OPEN_ENDED_TICK,
                            lateral, vertical))
    return Intent(asset=asset, proposal_id=proposal_id, volumes=volumes, start=at, landing=here,
                  depart_tick=now, arrive_tick=OPEN_ENDED_TICK, filed_tick=now, state=ACTIVATED,
                  kind=kind)


def samples(volumes: list[Volume4D]):
    """Points along the new route as (lat, lon, alt, tick, leg), in flight order."""
    for volume in volumes:
        span = max(1, volume.t_exit - volume.t_enter)
        if volume.is_column:
            height = volume.alt_hi - volume.alt_lo
            count = max(1, int(math.ceil(height / SAMPLE_M)))
            for k in range(count + 1):
                fraction = k / count
                yield (volume.a[0], volume.a[1], volume.alt_lo + height * fraction,
                       volume.t_enter + int(span * fraction), volume.leg)
            continue
        length = _distance_m(volume.a, volume.b)
        count = max(1, int(math.ceil(length / SAMPLE_M)))
        for k in range(count + 1):
            fraction = k / count
            yield (volume.a[0] + (volume.b[0] - volume.a[0]) * fraction,
                   volume.a[1] + (volume.b[1] - volume.a[1]) * fraction,
                   volume.alt_lo, volume.t_enter + int(span * fraction), volume.leg)


def first_conflict(volumes: list[Volume4D], others: list[Intent]) -> Conflict | None:
    """The first point where the new route enters another aircraft's volume, or None.

    The new route uses points on its un-inflated centreline; the other side uses its inflated
    volumes. Inflating both would make even a route moved 30 m aside conflict, and a fix like
    'altitude +30 m' would never work.
    """
    live = [(intent, intent.volumes) for intent in others if intent.live]
    if not live:
        return None
    for lat, lon, alt, tick, leg in samples(volumes):
        for intent, theirs in live:
            for volume in theirs:
                if volume.contains(lat, lon, alt, tick):
                    # a presence is not a registered intent, so it has no id
                    return Conflict(intent.asset, None if intent.kind == PRESENCE else intent.id,
                                    "traffic", (lat, lon), leg, volume.to_tick, alt, tick)
    return None


def ground_conflict(landing: tuple[float, float], arrive_tick: int, last_leg: int,
                    occupants: list[tuple[str, tuple[float, float], Intent | None]],
                    now: int) -> Conflict | None:
    """Is another aircraft standing on the landing spot now? Judged by telemetry, not intents.

    A landed aircraft's intent has ended, so by intents alone the spot looks empty, and the gap
    until its next filing is not a few ticks but can be any length (refusals, retries, a depot
    spot). If the standing aircraft has an approved departure (accepted) and the new arrival
    comes after it, it will be gone by then, so that is fine.
    occupants is (aircraft, position, live intent or None).
    """
    radius = TRAFFIC_LATERAL_M + CLEAR_M
    for asset, at, intent in occupants:
        if _distance_m(landing, at) > radius:
            continue
        leaving = intent is not None and intent.state == ACCEPTED
        if leaving and arrive_tick >= intent.depart_tick + TIME_PAD_TICKS:
            continue
        until = intent.depart_tick + TIME_PAD_TICKS if leaving else now + TIME_PAD_TICKS
        return Conflict(asset, intent.id if intent is not None else None, "landing", landing,
                        last_leg, until, 0.0, arrive_tick)
    return None


def landing_conflict(landing: tuple[float, float], arrive_tick: int, last_leg: int,
                     others: list[Intent], now: int) -> Conflict | None:
    """Does another intent use the landing spot? A landing site never takes two aircraft.

    Checks both the other's landing point and, if it has not taken off yet, its departure point
    (it stands there until it does). A landed aircraft stands there until its next approval
    replaces the intent, so once the other has landed its landing point has no end: blocking
    only until its landing column ends would let a later filing land on top of the aircraft
    standing there. The reverse, the other landing after me on a spot I land on first, is the
    same thing, so two live intents landing on the same spot conflict regardless of order.
    Corridor volumes only cover the spot from 30 ticks before departure, so landing next to it
    before then is caught by the departure-point rule.
    """
    radius = TRAFFIC_LATERAL_M + CLEAR_M
    for intent in others:
        if not intent.live:
            continue
        if intent.kind == ROUTE and _distance_m(landing, intent.landing) <= radius:
            # When it clears is unknown (only the other's next approved route would tell), so
            # this gives only the earliest possibility.
            return Conflict(intent.asset, intent.id, "landing", landing, last_leg,
                            max(intent.to_tick, now + TIME_PAD_TICKS), 0.0, arrive_tick)
        if (intent.state == ACCEPTED and _distance_m(landing, intent.start) <= radius
                and now <= arrive_tick < intent.depart_tick + TIME_PAD_TICKS):
            return Conflict(intent.asset, intent.id, "landing", landing, last_leg,
                            intent.depart_tick + TIME_PAD_TICKS, 0.0, arrive_tick)
    return None


class IntentRegistry:
    """The latest intent per aircraft. States only move accepted → activated → ended."""

    def __init__(self):
        self._latest: dict[str, Intent] = {}
        # Aircraft that took off before their approved window: (intent, approved departure tick).
        # The runtime drains these into the ledger.
        self.nonconforming: list[tuple[Intent, int]] = []

    def get(self, asset: str) -> Intent | None:
        return self._latest.get(asset)

    def others(self, asset: str) -> list[Intent]:
        """Other aircraft's live intents. Its own is left out: a new approval replaces it."""
        return [i for a, i in self._latest.items() if a != asset and i.live]

    def live(self) -> list[Intent]:
        return [i for i in self._latest.values() if i.live]

    def accept(self, intent: Intent) -> Intent | None:
        """A new approval. The aircraft's previous intent ends and is returned."""
        previous = self._latest.get(intent.asset)
        if previous is not None and previous.live:
            previous.state, previous.ended_reason = ENDED, "replaced"
        self._latest[intent.asset] = intent
        return previous

    def end(self, asset: str, reason: str) -> Intent | None:
        intent = self._latest.get(asset)
        if intent is None or not intent.live:
            return None
        intent.state, intent.ended_reason = ENDED, reason
        return intent

    def observe(self, telemetry: dict, tick: int) -> list[tuple[str, str, str]]:
        """Move states on from telemetry. Returns a list of (aircraft, before, after).

        Airborne: activated; airborne and then landed: ended (arrived). Not airborne by the end
        of the window: ended (expired), or a dead intent would block other filings forever.
        Taking off before the approved window (departure minus the margin) is nonconforming. The
        intent is moved to the actual departure tick and kept in nonconforming: when the
        autopilot did not keep a deferred departure, the judgement must not be left blocking
        empty sky.
        """
        changes = []
        for asset, intent in self._latest.items():
            if not intent.live:
                continue
            state = telemetry.get(asset) or {}
            airborne = float(state.get("alt_m") or 0.0) > 1.0
            was = intent.state
            if intent.state == ACCEPTED and airborne:
                if tick < intent.depart_tick - TIME_PAD_TICKS:
                    planned = intent.depart_tick
                    intent.reanchor(tick)
                    self.nonconforming.append((intent, planned))
                intent.state = ACTIVATED
            elif intent.state == ACTIVATED and not airborne and state:
                intent.state, intent.ended_reason = ENDED, "arrived"
            elif intent.state == ACCEPTED and tick >= intent.to_tick:
                intent.state, intent.ended_reason = ENDED, "expired"
            if intent.state != was:
                changes.append((asset, was, intent.state))
        return changes

    def drain_nonconforming(self) -> list[tuple[Intent, int]]:
        found, self.nonconforming = self.nonconforming, []
        return found

    def clear(self) -> None:
        self._latest.clear()
        self.nonconforming.clear()

    def snapshot(self) -> list[dict]:
        return [intent.to_dict() for intent in self._latest.values()]


# ---------- Telemetry heartbeat ----------

LINK_OK = "ok"
LINK_LOST = "lost"


@dataclass
class Link:
    status: str = LINK_OK
    since_tick: int = 0               # tick this state began (lost: first tick with no new record)
    last_seen_tick: int = 0           # tick of the last new record (the telemetry's tick stamp)
    declared_tick: int | None = None  # tick the runtime declared the link lost (only when lost)

    def to_dict(self) -> dict:
        return {"status": self.status, "since_tick": self.since_tick,
                "last_seen_tick": self.last_seen_tick, "declared_tick": self.declared_tick}


@dataclass
class LinkEvent:
    asset: str
    kind: str                         # lost | restored
    since_tick: int                   # first tick with no new record
    last_seen_tick: int               # tick of the last new record before the gap
    tick: int                         # tick the loss was declared, or a new record came back


class LinkWatch:
    """When each aircraft's telemetry was last fresh. This alone decides a lost link.

    An airborne aircraft whose record has not refreshed for timeout_ticks is lost; a fresh record
    again means restored. Freshness is counted by the record's tick stamp (telemetry_tick):
    counting by unchanged position would make a hovering aircraft look lost. A record with no
    stamp (from an adapter that gives none) means the heartbeat is unknown, and unknown is not
    lost (the same principle as the endpoint check not comparing when it does not know the
    position). An aircraft on the ground is never declared lost: there is no sky to hold.
    """

    def __init__(self, timeout_ticks: int):
        self.timeout_ticks = max(1, int(timeout_ticks))
        self.links: dict[str, Link] = {}

    def lost(self, asset: str) -> bool:
        link = self.links.get(asset)
        return link is not None and link.status == LINK_LOST

    def observe(self, telemetry: dict, tick: int) -> list[LinkEvent]:
        """Move states on from this poll's telemetry. Returns only the changes."""
        events = []
        for asset, state in telemetry.items():
            seen = _stamp(state, tick)
            link = self.links.get(asset)
            if link is None:
                link = self.links[asset] = Link(since_tick=seen, last_seen_tick=seen)
            if link.status == LINK_LOST:
                if seen > link.last_seen_tick:
                    events.append(LinkEvent(asset, "restored", link.since_tick,
                                            link.last_seen_tick, seen))
                    link.status, link.since_tick, link.last_seen_tick = LINK_OK, seen, seen
                    link.declared_tick = None
                continue
            link.last_seen_tick = max(link.last_seen_tick, seen)
            airborne = float(state.get("alt_m") or 0.0) > 1.0
            if airborne and tick - link.last_seen_tick >= self.timeout_ticks:
                link.status, link.since_tick = LINK_LOST, link.last_seen_tick + 1
                link.declared_tick = tick
                events.append(LinkEvent(asset, "lost", link.since_tick, link.last_seen_tick, tick))
        return events

    def clear(self) -> None:
        self.links.clear()

    def snapshot(self) -> dict:
        return {asset: link.to_dict() for asset, link in self.links.items()}


def _stamp(state: dict, tick: int) -> int:
    """A record's tick stamp; the current tick if missing or not a number (unknown is fresh)."""
    try:
        return tick if state.get("telemetry_tick") is None else int(state["telemetry_tick"])
    except (TypeError, ValueError):
        return tick
