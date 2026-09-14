"""Airspace volumes: a footprint on the ground plus a slab of altitude.

Real zone data looks like this and not like a circle. The FAA's UAS Facility Maps are a
grid of cells each carrying its own ceiling; EUROCAE ED-269 zones carry `lowerLimit`,
`upperLimit` and a vertical reference. So one neighbourhood is not one rule: a river
corridor, an apartment block and a school can each sit under a different ceiling.

Heights are metres. AGL is height above the ground under the aircraft, AMSL is height
above sea level. Converting between them needs terrain, which is why a volume says which
one it means instead of pretending they are the same.
"""

import math
from dataclasses import dataclass, field

INDEX_CELL_DEG = 0.002   # one index grid cell, about 220 m of latitude
EMPTY: list = []
# Lateral distance to keep from forbidden areas. A segment grazing a building corner by 0.5 m
# passes judgement, yet the 18 m corridor on screen cuts through the building. Larger than the
# corridor half-width (9 m).
# Grid cells and closed areas are 900 m cells; grazing them by 10 m draws a knife-edge path
# between two red cells. Regulated zones get a wider berth.
SEPARATION_M = 10.0
ZONE_SEPARATION_M = 40.0
# Height to keep above a roof when flying over a building. For crewed aircraft 14 CFR 91.119
# requires 1,000 ft/500 ft above obstacles; Part 107 sets no minimum for drones, so 50 m is our
# operating standard. It rides on the building Volume's clearance_m — for judgement, the
# building extends to roof + clearance.
VERTICAL_CLEARANCE_M = 50.0
# In low-ceiling FAA cells (200 ft = 61 m, 26 cells across Manhattan), 50 m above a 30 m
# building is impossible.
# In those cells, buildings with roofs under 40 m are crossed with 20 m clearance; 40 m and up
# keep 50 m (= must go around).
# When this was pinned at 40 m, every landing site north of Central Park and in Harlem became
# unreachable.
REDUCED_CLEARANCE_M = 20.0
LOW_ROOF_M = 40.0


def building_clearance_m(roof_m: float | None, cell_ceiling_m: float | None) -> float:
    """Clearance to keep above this building: 50 m by default, 20 m only for low buildings
    where the ceiling doesn't allow 50 m."""
    if roof_m is None or cell_ceiling_m is None:
        return VERTICAL_CLEARANCE_M
    if roof_m + VERTICAL_CLEARANCE_M + 0.5 <= cell_ceiling_m - 1.0:
        return VERTICAL_CLEARANCE_M
    return REDUCED_CLEARANCE_M if roof_m < LOW_ROOF_M else VERTICAL_CLEARANCE_M
# A landing spot needs this much building-free radius. A spot 10 m clear sideways is fine to
# cruise past, but for a 90 m vertical descent it is a canyon between towers. The landing point
# is the route's end point, and the runtime checks it when it judges the route.
LANDING_SEPARATION_M = 50.0
# Low buildings (roof under 40 m) get a tighter landing radius. When the data was widened to
# everything 20 m and up, 12 of 30 landing sites had 20-26 m buildings within 50 m and nowhere
# left to land. The descent column really takes navigation error (10 m) + arrival radius (6 m),
# so a low building 15 m away is outside it. Tall buildings and zones keep 50 m.
LANDING_TALL_M = 40.0
LANDING_LOW_SEPARATION_M = 15.0
# Minimum separation between aircraft. Two aircraft within 30 m laterally and 25 m vertically
# in the same tick is a loss of separation. These are the values EU U-space (CORUS) and the
# NASA UTM TCL demos used for small drones — crewed 3 NM/1,000 ft scaled down to drone size and
# speed. The runtime's intent (4D) corridor width and the simulator's loss-of-separation metric
# use the same numbers; if they differed, judgement and measurement would disagree.
TRAFFIC_LATERAL_M = 30.0
TRAFFIC_VERTICAL_M = 25.0
# Sample spacing when judging vertical legs (takeoff column, climbs and descents at waypoints,
# landing column). A building blocks up to roof + clearance, so sampling every 5 m misses no
# building band (a band is at least 50 m).
COLUMN_STEP_M = 5.0
# Most index cells one segment may span. A 50 km diagonal is about 130,000 cells, so this is
# ample. A segment with merely finite coordinates (1e300) spans 1e600 cells and judgement never
# finished — meanwhile the runtime thread held the GIL and starved the world and arbiter
# threads. Segments too long to count are refused judgement. The form check (runtime.service)
# filters first; this is the last line of defence.
MAX_LEG_CELLS = 1_000_000


def separation_for(volume: "Volume") -> float:
    return SEPARATION_M if volume.id.startswith("bldg-") else ZONE_SEPARATION_M


def ground_clamped(alt_m: float) -> float:
    """Below ground is ground. Buildings start at 0 m, so -1 m is inside a building, not
    'under' it.

    Treating floor and ceiling as a plain interval put negative altitudes outside every zone,
    and a route through the middle of a building at -1 m passed judgement.
    """
    return alt_m if alt_m > 0.0 else 0.0


METRES_PER_DEG_LAT = 110_570.0
METRES_PER_DEG_LON = 84_400.0    # at latitude 40.7°


@dataclass
class Volume:
    id: str
    name: str
    polygon: list[tuple[float, float]]      # [(lat, lon), ...], need not be closed
    floor_m: float = 0.0
    ceiling_m: float | None = None          # None: unbounded above
    reference: str = "AGL"                  # "AGL" or "AMSL"
    rule: str = "forbidden"                 # forbidden | ceiling | permitted
    reason: str = ""
    source: str = ""                        # which agency's data it came from
    tags: dict = field(default_factory=dict)
    clearance_m: float = 0.0                # extra height kept clear above (roof clearance)
    # Validity window (ticks). A zone from a notice has a start and an end. Judgement looks only
    # at 'now', so the runtime adds the zone when the window opens and removes it when it
    # closes — these fields are the basis for that.
    from_tick: int | None = None
    until_tick: int | None = None

    @property
    def top_m(self) -> float | None:
        """Height this zone actually blocks up to; unbounded if there is no ceiling."""
        return None if self.ceiling_m is None else self.ceiling_m + self.clearance_m

    def covers(self, lat: float, lon: float) -> bool:
        return bool(self.polygon) and point_in_polygon(lat, lon, self.polygon)

    def contains(self, lat: float, lon: float, alt_m: float) -> bool:
        if not self.covers(lat, lon):
            return False
        alt_m = ground_clamped(alt_m)
        if alt_m < self.floor_m:
            return False
        return self.top_m is None or alt_m <= self.top_m

    def breach(self, lat: float, lon: float, alt_m: float) -> str | None:
        """Does this position and altitude break the zone's rule? If so, one line saying why."""
        alt_m = ground_clamped(alt_m)
        if self.polygon and not self.covers(lat, lon):
            return None
        if not self.polygon:
            # A zone without a polygon is a rule that applies everywhere, like the default cap
            if self.rule == "ceiling" and self.ceiling_m is not None and alt_m > self.ceiling_m:
                return f"{self.name} exceeded ({alt_m:.0f} m > {self.ceiling_m:.0f} m)"
            return None
        if self.rule == "forbidden":
            if self.top_m is None or self.floor_m <= alt_m <= self.top_m:
                band = self.band()
                if self.clearance_m and self.ceiling_m is not None and alt_m > self.ceiling_m:
                    # The name ends in a height ("BUILDING 88 m"), so a verb keeps the two
                    # numbers apart — same shape as the near-miss line ("approached to 34 m").
                    return (f"{self.name} crossed {alt_m - self.ceiling_m:.0f} m above the roof "
                            f"(needs {self.clearance_m:.0f} m clearance)")
                return f"{self.name} no entry ({band})"
            return None
        if self.rule == "ceiling" and self.ceiling_m is not None and alt_m > self.ceiling_m:
            return (f"{self.name} above the ceiling "
                    f"({alt_m:.0f} m > {self.ceiling_m:.0f} m {self.reference})")
        return None

    def band(self) -> str:
        """The zone's altitude band, separate so the screen doesn't have to parse the sentence."""
        top = "no limit" if self.ceiling_m is None else f"{self.ceiling_m:.0f} m"
        return f"{self.floor_m:.0f}~{top} {self.reference}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "polygon": [[lat, lon] for lat, lon in self.polygon],
            "floor_m": self.floor_m, "ceiling_m": self.ceiling_m,
            "reference": self.reference, "rule": self.rule,
            "reason": self.reason, "source": self.source, "tags": dict(self.tags),
            "clearance_m": self.clearance_m,
            "from_tick": self.from_tick, "until_tick": self.until_tick,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Volume":
        return cls(
            id=raw["id"], name=raw.get("name", raw["id"]),
            polygon=[(float(a), float(b)) for a, b in raw["polygon"]],
            floor_m=float(raw.get("floor_m", 0.0)),
            ceiling_m=None if raw.get("ceiling_m") is None else float(raw["ceiling_m"]),
            reference=raw.get("reference", "AGL"),
            rule=raw.get("rule", "forbidden"),
            reason=raw.get("reason", ""), source=raw.get("source", ""),
            tags=raw.get("tags", {}),
            clearance_m=float(raw.get("clearance_m", 0.0)),
            from_tick=None if raw.get("from_tick") is None else int(raw["from_tick"]),
            until_tick=None if raw.get("until_tick") is None else int(raw["until_tick"]),
        )


def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray casting. Zones are small, so treating them as flat is fine.

    At city-district scale (a few km), treating lat/lon as planar coordinates is off by metres.
    A zone covering a whole country would need geodesic maths.
    """
    if len(polygon) < 3:
        return False
    inside = False
    count = len(polygon)
    for index in range(count):
        lat_a, lon_a = polygon[index]
        lat_b, lon_b = polygon[(index + 1) % count]
        if (lat_a > lat) != (lat_b > lat):
            crossing = (lon_b - lon_a) * (lat - lat_a) / (lat_b - lat_a) + lon_a
            if lon < crossing:
                inside = not inside
    return inside


def box(lat_min: float, lon_min: float, lat_max: float, lon_max: float):
    return [(lat_min, lon_min), (lat_min, lon_max), (lat_max, lon_max), (lat_max, lon_min)]


# Part 107 default cap. Where there is no grid, there is still a rule: this value.
# 400 ft AGL = 121.92 m. Leave it out and the airspace is modelled as if anything goes.
DEFAULT_CEILING_M = 121.9


class Airspace:
    """The set of active zones. Both filings and positions are checked against it."""

    def __init__(self, volumes: list[Volume] | None = None,
                 default_ceiling_m: float | None = DEFAULT_CEILING_M):
        self._volumes: dict[str, Volume] = {v.id: v for v in (volumes or [])}
        self.default_ceiling_m = default_ceiling_m
        # One zone opening and another closing leaves the count unchanged, so invalidating
        # caches by count misses that moment. Hence a number that goes up on every change.
        self.revision = 0
        self._grid: dict[tuple[int, int], list[Volume]] = {}
        self._everywhere: list[Volume] = []
        self._index_for = -1

    def add(self, volume: Volume) -> None:
        self._volumes[volume.id] = volume
        self.revision += 1

    def add_all(self, volumes: list[Volume]) -> None:
        """Add many at once. Judgement runs on another thread (HTTP), so adding 30,000 one by
        one lets filings in between be judged against a half-filled airspace (compose: approved
        at revision 14480). The new dict is built on the side and swapped in with a single
        assignment, and only then is the revision bumped — bumping first could let an index
        built from the old dict carry the new revision. The revision rises by as much as adding
        them one at a time would."""
        if not volumes:
            return
        merged = dict(self._volumes)
        merged.update((volume.id, volume) for volume in volumes)
        self._volumes = merged
        self.revision += len(volumes)

    def remove(self, volume_id: str) -> None:
        if self._volumes.pop(volume_id, None) is not None:
            self.revision += 1

    def all(self) -> list[Volume]:
        return list(self._volumes.values())

    def get(self, volume_id: str) -> Volume | None:
        return self._volumes.get(volume_id)

    def near(self, lat: float, lon: float) -> list[Volume]:
        """Only the zones that could apply at this point; the rest needn't be looked at.

        Zones overlapping each grid cell (0.002°, about 220 m) are indexed in advance. Buildings
        take the zone count from 200 to over 3,000, and scanning all of them per sample costs
        hundreds of thousands of polygon tests to check one route. Same answer, fewer zones
        looked at.
        """
        if self._index_for != self.revision:
            self._rebuild_index()
        cell = (int(math.floor(lat / INDEX_CELL_DEG)), int(math.floor(lon / INDEX_CELL_DEG)))
        boxes = self._grid.get(cell)
        if not boxes:
            return list(self._everywhere)
        return [box[0] for box in boxes] + self._everywhere

    def landing_breach(self, lat: float, lon: float) -> tuple["Volume", float] | None:
        """Can we land here? At ground level, no forbidden zone or building may lie within the
        landing radius (50 m)."""
        point = {"lat": lat, "lon": lon}
        worst = None
        for volume in self.near(lat, lon):
            if volume.rule != "forbidden" or not volume.polygon or volume.floor_m > 0.0:
                continue
            gap = 0.0 if volume.covers(lat, lon) else _clearance_m(point, point, volume.polygon)[0]
            low_building = (volume.id.startswith("bldg-") and volume.ceiling_m is not None
                            and volume.ceiling_m < LANDING_TALL_M)
            needed = (max(LANDING_LOW_SEPARATION_M, separation_for(volume)) if low_building
                      else max(LANDING_SEPARATION_M, separation_for(volume)))
            if gap < needed and (worst is None or gap < worst[1]):
                worst = (volume, gap)
        return worst

    def too_close(self, lat: float, lon: float, alt_m: float) -> bool:
        """Inside a forbidden zone or within its clearance? The same test as first_breach."""
        if self.forbidden_at(lat, lon, alt_m):
            return True
        alt_m = ground_clamped(alt_m)
        point = {"lat": lat, "lon": lon}
        for volume in self.near(lat, lon):
            if (volume.rule != "forbidden" or not volume.polygon or alt_m < volume.floor_m
                    or (volume.top_m is not None and alt_m > volume.top_m)):
                continue
            if _clearance_m(point, point, volume.polygon)[0] < separation_for(volume):
                return True
        return False

    def forbidden_at(self, lat: float, lon: float, alt_m: float) -> bool:
        """Is this point off-limits at this altitude? Shared by the planner and the runtime.

        Altitude matters. A building blocks only up to its roof and is open above; ignore
        altitude and even a 20 m building becomes a wall to go around forever.
        A bounding box filters before the polygon test — this is called hundreds of thousands
        of times per search.
        """
        if self._index_for != self.revision:
            self._rebuild_index()
        alt_m = ground_clamped(alt_m)
        cell = (int(math.floor(lat / INDEX_CELL_DEG)), int(math.floor(lon / INDEX_CELL_DEG)))
        for volume, south, north, west, east in self._grid.get(cell, EMPTY):
            if (volume.rule == "forbidden" and south <= lat <= north
                    and west <= lon <= east
                    and (volume.top_m is None or volume.floor_m <= alt_m <= volume.top_m)
                    and volume.covers(lat, lon)):
                return True
        return any(v.rule == "forbidden" and v.breach(lat, lon, alt_m)
                   for v in self._everywhere)

    def _rebuild_index(self) -> None:
        grid: dict[tuple[int, int], list[Volume]] = {}
        everywhere: list[Volume] = []
        for volume in self._volumes.values():
            if not volume.polygon:
                everywhere.append(volume)   # a rule without a polygon applies everywhere
                continue
            lats = [point[0] for point in volume.polygon]
            lons = [point[1] for point in volume.polygon]
            box = (volume, min(lats), max(lats), min(lons), max(lons))
            # Widened by the clearance distance: near() looks at one cell but also has to
            # measure the distance to zones in the neighbouring cells.
            widest = max(ZONE_SEPARATION_M, LANDING_SEPARATION_M)
            pad_lat = widest / METRES_PER_DEG_LAT
            pad_lon = widest / METRES_PER_DEG_LON
            for row in range(int(math.floor((min(lats) - pad_lat) / INDEX_CELL_DEG)),
                             int(math.floor((max(lats) + pad_lat) / INDEX_CELL_DEG)) + 1):
                for col in range(int(math.floor((min(lons) - pad_lon) / INDEX_CELL_DEG)),
                                 int(math.floor((max(lons) + pad_lon) / INDEX_CELL_DEG)) + 1):
                    grid.setdefault((row, col), []).append(box)
        self._grid = grid
        self._everywhere = everywhere
        self._index_for = self.revision

    def breach(self, lat: float | None, lon: float | None, alt_m: float) -> Volume | None:
        """First zone breached. Forbidden zones come before altitude limits."""
        if lat is None or lon is None:
            return None
        ordered = sorted(self.near(lat, lon), key=lambda v: v.rule != "forbidden")
        for volume in ordered:
            if volume.breach(lat, lon, alt_m):
                return volume
        if self.default_ceiling_m is not None and alt_m > self.default_ceiling_m:
            return Volume(
                id="part107-default", name="Part 107 default ceiling",
                polygon=[], ceiling_m=self.default_ceiling_m, rule="ceiling",
                reason="default ceiling 400 ft AGL where there is no grid",
                source="14 CFR 107.51",
            )
        return None

    def ceiling_at(self, lat: float, lon: float) -> float | None:
        """Highest altitude allowed here. Where zones overlap, the lowest ceiling wins."""
        ceilings = [
            v.ceiling_m for v in self.near(lat, lon)
            if v.rule == "ceiling" and v.covers(lat, lon) and v.ceiling_m is not None
        ]
        if self.default_ceiling_m is not None:
            ceilings.append(self.default_ceiling_m)
        return min(ceilings) if ceilings else None


def _leg_volumes(airspace: Airspace, here: dict, nxt: dict):
    """Collect only the zones touching the segment's bounding box (widened by the clearance
    distance). Judgement is first_breach's alone."""
    if airspace._index_for != airspace.revision:
        airspace._rebuild_index()
    south, north = sorted((here["lat"], nxt["lat"]))
    west, east = sorted((here["lon"], nxt["lon"]))
    widest = max(SEPARATION_M, ZONE_SEPARATION_M)
    south -= widest / METRES_PER_DEG_LAT
    north += widest / METRES_PER_DEG_LAT
    west -= widest / METRES_PER_DEG_LON
    east += widest / METRES_PER_DEG_LON
    rows = range(math.floor(south / INDEX_CELL_DEG), math.floor(north / INDEX_CELL_DEG) + 1)
    cols = range(math.floor(west / INDEX_CELL_DEG), math.floor(east / INDEX_CELL_DEG) + 1)
    if len(rows) * len(cols) > MAX_LEG_CELLS:
        raise ValueError(f"leg too long to judge ({len(rows)}x{len(cols)} cells)")
    candidates = {v.id: v for v in airspace._everywhere}
    for row in rows:
        for col in cols:
            for volume, lo_lat, hi_lat, lo_lon, hi_lon in airspace._grid.get((row, col), EMPTY):
                if lo_lat <= north and hi_lat >= south and lo_lon <= east and hi_lon >= west:
                    candidates[volume.id] = volume
    return candidates.values()


def _crossing_fractions(here: dict, nxt: dict, polygon: list[tuple[float, float]]):
    """Fractions where the segment crosses polygon edges; inside/outside holds between them."""
    lat, lon = here["lat"], here["lon"]
    dy, dx = nxt["lat"] - lat, nxt["lon"] - lon
    cuts = {0.0, 1.0}
    for i, (ay, ax) in enumerate(polygon):
        by, bx = polygon[(i + 1) % len(polygon)]
        ey, ex = by - ay, bx - ax
        denominator = dx * ey - dy * ex
        if abs(denominator) < 1e-20:
            # An edge on the same line can still flip inside/outside at its end points.
            if abs((ax - lon) * dy - (ay - lat) * dx) < 1e-20:
                for py, px in ((ay, ax), (by, bx)):
                    if abs(dx) > abs(dy):
                        t = (px - lon) / dx
                    else:
                        t = (py - lat) / dy if dy else 0
                    if 0 <= t <= 1:
                        cuts.add(t)
            continue
        t = ((ax - lon) * ey - (ay - lat) * ex) / denominator
        u = ((ax - lon) * dy - (ay - lat) * dx) / denominator
        if 0 <= t <= 1 and 0 <= u <= 1:
            cuts.add(t)
    return sorted(cuts)


def _point_segment_m(lat: float, lon: float, a: tuple[float, float],
                     b: tuple[float, float]) -> tuple[float, float]:
    """Distance (m) from the point to the segment, and the fraction of the nearest point on it."""
    ax, ay = (a[1] - lon) * METRES_PER_DEG_LON, (a[0] - lat) * METRES_PER_DEG_LAT
    bx, by = (b[1] - lon) * METRES_PER_DEG_LON, (b[0] - lat) * METRES_PER_DEG_LAT
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
    return math.hypot(ax + t * dx, ay + t * dy), t


def _clearance_m(here: dict, nxt: dict, polygon: list[tuple[float, float]]) -> tuple[float, float]:
    """Minimum distance (m) between the segment and the polygon boundary, and the segment
    fraction where it occurs.

    If two segments don't intersect, the closest point is an end point of one of them, so four
    checks suffice: each end of the segment to the edge, each end of the edge to the segment.
    Intersections are caught first by first_breach's crossing test.
    """
    a, b = (here["lat"], here["lon"]), (nxt["lat"], nxt["lon"])
    best = (float("inf"), 0.0)
    for i, edge_a in enumerate(polygon):
        edge_b = polygon[(i + 1) % len(polygon)]
        for point, fraction in ((a, 0.0), (b, 1.0)):
            distance, _ = _point_segment_m(point[0], point[1], edge_a, edge_b)
            if distance < best[0]:
                best = (distance, fraction)
        for corner in (edge_a, edge_b):
            distance, t = _point_segment_m(corner[0], corner[1], a, b)
            if distance < best[0]:
                best = (distance, t)
    return best


def nearest_exit(volume: Volume, lat: float, lon: float,
                 margin_m: float | None = None) -> tuple[float, float] | None:
    """Nearest spot outside the zone for a point inside it; None if the point is outside.

    When a zone closes, aircraft inside must leave. Hovering in place means staying inside a
    closed zone, and replanning finds no route because the start is in a forbidden zone. The
    answer is out through the nearest boundary, plus the clearance distance beyond it.
    """
    if not volume.polygon or not volume.covers(lat, lon):
        return None
    if margin_m is None:
        margin_m = separation_for(volume) + 5.0
    best = (float("inf"), None)
    for i, edge_a in enumerate(volume.polygon):
        edge_b = volume.polygon[(i + 1) % len(volume.polygon)]
        distance, t = _point_segment_m(lat, lon, edge_a, edge_b)
        if distance < best[0]:
            best = (distance, (edge_a[0] + (edge_b[0] - edge_a[0]) * t,
                               edge_a[1] + (edge_b[1] - edge_a[1]) * t))
    distance, (door_lat, door_lon) = best
    north = (door_lat - lat) * METRES_PER_DEG_LAT
    east = (door_lon - lon) * METRES_PER_DEG_LON
    length = math.hypot(north, east) or 1.0
    push = (distance + margin_m) / length
    return (lat + north * push / METRES_PER_DEG_LAT, lon + east * push / METRES_PER_DEG_LON)


def required_top_along(airspace: "Airspace", here: dict, nxt: dict,
                       margin_m: float = SEPARATION_M) -> float:
    """Highest 'roof + that building's clearance' below this segment (including within
    lateral clearance); 0 if none.

    The operator uses it to set leg altitudes. Clearance can differ per building (20 m for low
    buildings in low cells), so this reads the blocked top (top_m), not the roof. Judgement
    (first_breach) doesn't trust this value and checks on its own.
    """
    top = 0.0
    for volume in _leg_volumes(airspace, here, nxt):
        if not volume.id.startswith("bldg-") or volume.top_m is None or not volume.polygon:
            continue
        if volume.top_m <= top:
            continue
        crosses = len(_crossing_fractions(here, nxt, volume.polygon)) > 2
        if crosses or _clearance_m(here, nxt, volume.polygon)[0] < margin_m:
            top = volume.top_m
    return top


def first_breach(airspace: "Airspace", legs: list[dict], samples: int | None = None):
    """First leg that breaks the rules. The runtime and the planner use this same function.

    Fixed-interval sampling misses a brief cut through a building corner, so segments are
    split at polygon boundaries and each piece is checked. samples is kept for existing callers
    and does not lower accuracy.
    """
    for index in range(len(legs) - 1):
        here, nxt = legs[index], legs[index + 1]
        altitude = ground_clamped(float(nxt.get("alt_m", here.get("alt_m", 0.0))))
        first = None
        for volume in _leg_volumes(airspace, here, nxt):
            cuts = _crossing_fractions(here, nxt, volume.polygon)
            probes = sorted(set(cuts + [(a + b) / 2 for a, b in zip(cuts, cuts[1:], strict=False)]))
            hit = False
            for fraction in probes:
                if first is not None and fraction >= first[0]:
                    break
                lat = here["lat"] + (nxt["lat"] - here["lat"]) * fraction
                lon = here["lon"] + (nxt["lon"] - here["lon"]) * fraction
                reason = volume.breach(lat, lon, altitude)
                if reason:
                    first = (fraction, volume, reason, (lat, lon))
                    hit = True
                    break
            # Too close is a breach even without entering — for forbidden zones only, and only
            # at an altitude within the zone's height.
            if (not hit and volume.polygon and volume.rule == "forbidden"
                    and volume.floor_m <= altitude
                    and (volume.top_m is None or altitude <= volume.top_m)):
                gap, fraction = _clearance_m(here, nxt, volume.polygon)
                needed = separation_for(volume)
                if gap < needed and (first is None or fraction < first[0]):
                    lat = here["lat"] + (nxt["lat"] - here["lat"]) * fraction
                    lon = here["lon"] + (nxt["lon"] - here["lon"]) * fraction
                    first = (fraction, volume,
                             f"{volume.name} approached to {gap:.0f} m "
                             f"(needs {needed:.0f} m clearance)",
                             (lat, lon))
        # The polygon-less default ceiling is judged at the same entry point.
        start_breach = airspace.breach(here["lat"], here["lon"], altitude)
        if start_breach is not None:
            return (index + 1, start_breach,
                    start_breach.breach(here["lat"], here["lon"], altitude),
                    (here["lat"], here["lon"]))
        if first is not None:
            return index + 1, first[1], first[2], first[3]
    return None


def leg_breaches(airspace: "Airspace", here: dict, nxt: dict) -> list:
    """Everything one leg breaks, in order along it: [(fraction, volume, why, (lat, lon))].

    first_breach answers with the first only (enough for judgement). When the operator asks the
    model to redraw, it must list everything the leg runs into — report only the first and the
    model never sees the must-go-around building hiding behind a low one. Same criteria as
    first_breach.
    """
    altitude = ground_clamped(float(nxt.get("alt_m", here.get("alt_m", 0.0))))
    found = []
    for volume in _leg_volumes(airspace, here, nxt):
        cuts = _crossing_fractions(here, nxt, volume.polygon)
        probes = sorted(set(cuts + [(a + b) / 2 for a, b in zip(cuts, cuts[1:], strict=False)]))
        hit = None
        for fraction in probes:
            lat = here["lat"] + (nxt["lat"] - here["lat"]) * fraction
            lon = here["lon"] + (nxt["lon"] - here["lon"]) * fraction
            reason = volume.breach(lat, lon, altitude)
            if reason:
                hit = (fraction, volume, reason, (lat, lon))
                break
        if (hit is None and volume.polygon and volume.rule == "forbidden"
                and volume.floor_m <= altitude
                and (volume.top_m is None or altitude <= volume.top_m)):
            gap, fraction = _clearance_m(here, nxt, volume.polygon)
            needed = separation_for(volume)
            if gap < needed:
                lat = here["lat"] + (nxt["lat"] - here["lat"]) * fraction
                lon = here["lon"] + (nxt["lon"] - here["lon"]) * fraction
                why = (f"{volume.name} approached to {gap:.0f} m "
                       f"(needs {needed:.0f} m clearance)")
                hit = (fraction, volume, why, (lat, lon))
        if hit is not None:
            found.append(hit)
    start_breach = airspace.breach(here["lat"], here["lon"], altitude)
    if start_breach is not None:
        found.append((0.0, start_breach, start_breach.breach(here["lat"], here["lon"], altitude),
                      (here["lat"], here["lon"])))
    found.sort(key=lambda item: item[0])
    return found


def vertical_column(lat: float, lon: float, from_m: float, to_m: float,
                    step_m: float = COLUMN_STEP_M) -> list[dict]:
    """A climb or descent in place, shaped for the judgement function.

    A multirotor climbs and descends in place at waypoints (sim World._at_cruise). That
    vertical line also passes through airspace and must be judged — a 60 m building beside the
    start doesn't block the 120 m cruise leg but does block the column from 0 m to 120 m.
    Chaining zero-length legs that differ only in altitude lets first_breach judge each at its
    altitude, so no new judgement code is needed (G7).
    """
    lo, hi = sorted((ground_clamped(from_m), ground_clamped(to_m)))
    altitudes = [lo]
    while altitudes[-1] + step_m < hi:
        altitudes.append(altitudes[-1] + step_m)
    if hi > lo:
        altitudes.append(hi)
    if from_m > to_m:
        altitudes.reverse()
    if len(altitudes) == 1:
        altitudes.append(altitudes[0])
    return [{"lat": lat, "lon": lon, "alt_m": altitude} for altitude in altitudes]
