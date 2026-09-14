"""Find a way there that stays inside the rules.

This is not obstacle avoidance — the autopilot does that, and it must, because it is the
layer that has to work when everything else is down. This is the other kind of routing:
given a map of who is allowed where and how high, hand back a path that a regulator would
sign off on, or say plainly that there is none.

Refusing is easy and useless. An operator told only "no" flies anyway or stops delivering.
The useful answer is "not that way, this way, and stay under 61 m for the middle leg".
"""

import heapq
import math
import time
from dataclasses import dataclass

from shared.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    Airspace,
    _point_segment_m,
    first_breach,
    required_top_along,
)

# Each leg flies at its lowest safe altitude: tallest roof below + 50 m clearance, at least
# FLOOR. If that exceeds the local ceiling (FAA grid, at most CRUISE), the leg can't be flown
# and the route goes around. So altitude changes leg by leg — 40 m over the river, 70 m over
# low-rises, a detour beside a tower. Fly one height everywhere and the screen never shows that
# altitude is being judged.
CRUISE_ALT_M = 120.0      # highest we climb; just under the FAA default cap of 400 ft (121.9 m)
# Buildings missing from the judgement data (under 20 m) still need 50 m above them. At 40 m, a
# route crossing 33-37 m buildings at 40 m was approved, and on screen its corridor cut right
# through them. FAA grid cells with a ceiling under 70 m (15/30/61 m) become impassable at this
# value — going around them is correct.
FLOOR_ALT_M = 70.0        # cruise floor over river/park = 20 m data cutoff + 50 m clearance

# Detours may stray this far from the destination. A route that swings wide around a whole
# district has to be possible.
SEARCH_REACH_M = 12_000.0

# ---------- candidate routes ----------
# The planner offers up to three legal routes; the operator's model picks one
# (drone/agent/chooser.py).
# When the model wrote coordinates itself, drafts crossing a long stretch of Manhattan almost
# never passed judgement — geometry is a search problem. Choosing is a judgement call, so the
# model does it, and whatever it picks, the runtime judges by the same rules.
CANDIDATE_LABELS = {"a": "shortest", "b": "lowest altitude", "c": "clear of traffic"}
VARIANT_TAGS = {"b": "lowest-altitude", "c": "clear-of-traffic"}
# Two routes whose every point lies within this distance of the other count as one route.
# Hand the model the same route three times under different names and it draws lots instead
# of choosing.
DISTINCT_M = 100.0
# (b) altitude penalty. A leg whose cruise altitude rises from the floor (70 m) to the max
# (120 m) costs (1 + this) times as much. At 1.0, 1 km at 120 m equals 2 km at 70 m — enough to
# route low over the river and parks, not enough to double the distance to skip one block.
ALTITUDE_WEIGHT = 1.0
# (c) separation penalty. Grid points within this distance of another aircraft's approved
# corridor, an incident circle or a notice zone cost more the closer they get (1 + CLEAR_WEIGHT
# times when touching). About six times the corridor half-width (30 m + 10 m navigation) — a
# crossing refusal needs the times to overlap as well, but to choose a route with no overlap
# worry at all, one route has to be apart in space. The zones themselves are already forbidden
# (40 m clearance); this value is margin on top of that.
CLEAR_MARGIN_M = 250.0
CLEAR_WEIGHT = 4.0
# Time limit (s) for one (b)/(c) search. (a) is the existing A* unchanged and has no limit (no
# (a) means no route at all). The others are only options, so a slow one is dropped. They share
# the edge memo (_edge_memo), so after (a) they usually finish well inside the limit. The search
# looks at the clock every DEADLINE_CHECK_EVERY nodes (looking every time costs too).
VARIANT_BUDGET_S = 12.0
DEADLINE_CHECK_EVERY = 512
# Heuristic scale for the penalised searches (weighted A*). The distance heuristic is far below
# the penalised cost, so left as is the search degrades to near-Dijkstra and can't reach Harlem
# within 12 s (measured: timeouts left only candidate (a)). Inflating it to roughly the real cost
# multiplier trades the shortest-path guarantee for speed — what we need here is 'another legal
# route', not the optimum, and first_breach checks legality separately.
HEURISTIC_SCALE = {"b": 1.0 + ALTITUDE_WEIGHT / 2.0, "c": 1.2}
# String-pulling window for penalised candidates (grid cells, about 1.2 km). (a) uses 80, but (b)
# has to recompute the safe altitude of every shortcut segment (the longer, the costlier), and at
# 80 cells one Harlem run took 37 s (measured).
VARIANT_PULL_WINDOW = 24
# Extra time (s) for string-pulling, so a route found just before the limit isn't thrown away.
PULL_GRACE_S = 2.0
# Penalised searches stay within this many metres of (a). An alternative is a neighbour of the
# shortest route, not the far side of the city — and on trips that swing wide around 0 ft grid
# cells, like Harlem, a penalised search over the whole window (12 km) used up the node budget
# (120,000) in 4 s without finding anything (measured: no (b) or (c), only candidate (a)). The
# width leaves (c) plenty of room to stand off a corridor by the penalty distance (250 m). The
# deadline bounds time separately.
VARIANT_TUBE_M = 1200.0
VARIANT_NODE_BUDGET = 400_000
# Drop candidates longer than this multiple of (a). A route nearly twice as long isn't a choice,
# it's a battery problem.
MAX_VARIANT_STRETCH = 1.8
# Spacing (m) when sampling a route into points. These points decide whether two routes are the
# same and what a route passes near.
SAMPLE_STEP_M = 25.0


@dataclass
class Leg:
    lat: float
    lon: float
    alt_m: float

    def to_dict(self) -> dict:
        return {"lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "alt_m": round(self.alt_m, 1)}


@dataclass
class Route:
    legs: list[Leg]
    detoured: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "detoured": self.detoured,
            "reason": self.reason,
        }


class Router:
    def __init__(self, airspace: Airspace, cell_deg: float = 0.00045,
                 min_alt_m: float = 20.0, cruise_alt_m: float = 0.0):
        """cell_deg 0.00045 is about 50 m, the size of one building.

        With 200 m cells, only the FAA grid (about 900 m) had to be avoided. Once buildings came
        in, every 200 m edge grazed a building almost everywhere in Manhattan and the graph fell
        apart. At 50 m the side streets open up, and it is faster than both 100 m and 28 m — the
        former blocks edges so often that the search wanders, the latter walks the same
        distance in smaller steps.

        cruise_alt_m is not the allowed ceiling but the height this aircraft wants to fly. Real
        delivery drones fly at 40-60 m (Wing at about 45 m). Climb as high as the ceiling allows
        and most of the city's buildings sit below, so the route never sees them.
        """
        self.airspace = airspace
        self.cell = cell_deg
        self.min_alt_m = min_alt_m
        self.cruise_alt_m = cruise_alt_m or CRUISE_ALT_M
        self.floor_alt_m = min(FLOOR_ALT_M, self.cruise_alt_m)
        self._blocked_memo: dict[tuple[int, int], bool] = {}
        self._edge_memo: dict[tuple, float | None] = {}
        self._route_memo: dict[tuple, Route | None] = {}
        # Memo for (b)/(c) candidates. Only successes are kept — a search that hit the limit may
        # succeed when retried with a fuller memo.
        self._variant_memo: dict[tuple, list[dict]] = {}
        self._memo_for = -1
        # Per-candidate time (s) of the last candidates() call, for measurement and logs.
        self.last_timings: dict[str, float] = {}

    @staticmethod
    def cruise_alt_default() -> float:
        """Default height an aircraft wants to fly. Written in two places, it would drift."""
        return CRUISE_ALT_M

    # ---------- grid ----------

    def _node(self, lat: float, lon: float) -> tuple[int, int]:
        return (round(lat / self.cell), round(lon / self.cell))

    def _coords(self, node: tuple[int, int]) -> tuple[float, float]:
        return (node[0] * self.cell, node[1] * self.cell)

    def _forbidden_at(self, lat: float, lon: float) -> bool:
        """Is this point off-limits at the altitude we would fly it?

        That altitude is the lower of the cruise height and the local ceiling — the same rule
        _to_legs applies to each leg. If the two used different heights, the runtime would
        refuse routes the planner considered clear.
        """
        return self.airspace.too_close(lat, lon, self._altitude_at(lat, lon))

    def _altitude_at(self, lat: float, lon: float) -> float:
        ceiling = self.airspace.ceiling_at(lat, lon)
        allowed = self.cruise_alt_m if ceiling is None else ceiling - 1.0
        return max(self.min_alt_m, min(self.cruise_alt_m, allowed))

    def _blocked(self, node: tuple[int, int]) -> bool:
        """A grid point's answer never changes; the search revisits a point dozens of times."""
        known = self._blocked_memo.get(node)
        if known is None:
            known = self._forbidden_at(*self._coords(node))
            self._blocked_memo[node] = known
        return known

    def leg_altitude(self, start: tuple[float, float], goal: tuple[float, float]) -> float | None:
        """Lowest safe altitude for this segment, or None if it can't be flown.

        Tallest roof below + clearance (50 m), at least FLOOR. If that exceeds the lowest ceiling
        along the segment (minus 1 m, at most CRUISE), the route has to go around. A final check
        runs the same first_breach the runtime uses — lateral clearance, the grid and zones are
        caught there.
        """
        here = {"lat": start[0], "lon": start[1]}
        nxt = {"lat": goal[0], "lon": goal[1]}
        allowed = min(self._altitude_at(*start), self._altitude_at(*goal),
                      self._ceiling_allowance(start, goal))
        top = required_top_along(self.airspace, here, nxt)
        # The cruise floor (70 m) applies only where the ceiling allows; in a low cell (61 m) it
        # is ceiling - 1. A building Volume blocks up to roof + clearance inclusive (closed
        # interval); exactly that height is still inside, so add 0.5 m.
        floor_here = min(self.floor_alt_m, allowed)
        needed = max(floor_here, self.min_alt_m, top + 0.5 if top > 0 else 0.0)
        if needed > allowed:
            return None
        legs = [{**here, "alt_m": needed}, {**nxt, "alt_m": needed}]
        return None if first_breach(self.airspace, legs) is not None else needed

    def _ceiling_allowance(self, start, goal, samples: int = 24) -> float:
        """Lowest ceiling along the segment minus 1 m; max cruise if there is no ceiling."""
        lowest = self.cruise_alt_m
        for step in range(samples + 1):
            fraction = step / samples
            ceiling = self.airspace.ceiling_at(start[0] + (goal[0] - start[0]) * fraction,
                                               start[1] + (goal[1] - start[1]) * fraction)
            if ceiling is not None:
                lowest = min(lowest, ceiling - 1.0)
        return max(self.min_alt_m, lowest)

    def _edge(self, a: tuple[int, int], b: tuple[int, int]) -> float | None:
        """Grid edge altitude, None if impassable. The search revisits edges dozens of times."""
        key = (a, b) if a <= b else (b, a)
        if key in self._edge_memo:
            return self._edge_memo[key]
        altitude = self.leg_altitude(self._coords(a), self._coords(b))
        self._edge_memo[key] = altitude
        return altitude

    def _crosses(self, a: tuple[int, int], b: tuple[int, int], samples: int = 0) -> bool:
        """Is the segment between two grid points impassable at every altitude?

        Checking only grid points cuts corners. The runtime checks segments, so the planner
        does too.
        """
        return self._edge(a, b) is None

    # ---------- pathfinding ----------

    def plan(self, start: tuple[float, float], goal: tuple[float, float]) -> Route | None:
        if self._memo_for != self.airspace.revision:
            self._blocked_memo.clear()          # airspace changed: drop the memoised answers
            self._edge_memo.clear()
            self._route_memo.clear()
            self._variant_memo.clear()
            self._memo_for = self.airspace.revision
        # The same trip from the same spot to the same landing site comes up run after run. A
        # search through 34,000 buildings around low-ceiling cells takes 1-3 min each time, so
        # answers are memoised within one airspace revision (within 20 m counts as the same spot).
        key = (round(start[0], 4), round(start[1], 4), round(goal[0], 4), round(goal[1], 4))
        if key in self._route_memo:
            return self._route_memo[key]
        route = self._plan(start, goal)
        self._route_memo[key] = route
        return route

    def _plan(self, start: tuple[float, float], goal: tuple[float, float]) -> Route | None:
        if self.airspace.landing_breach(*goal) is not None:
            return None  # can't land there, so a route would be useless
        starts = self._free_nodes_near(start)
        goals = self._free_nodes_near(goal)
        if not starts or not goals:
            return None  # no open grid point around the start or the destination

        direct = self._straight(starts[0], goals[0])
        if direct is not None:
            legs = self._attach(self._to_legs([starts[0], goals[0]]), start, goal)
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=False)

        path = self._search(starts, goals)
        if path is None:
            return None
        # Try the string-pulled path first, then the turns-only version, then the raw
        # cell-by-cell path. Whichever it is, the runtime's check runs on it again at the end.
        for nodes in (self._pull(path), self._simplify(path), path):
            if not self._legal_chain(nodes):
                continue
            legs = self._attach(self._to_legs(nodes), start, goal)
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=True, reason="detour around a forbidden zone")
        return None

    def _free_nodes_near(self, point: tuple[float, float]) -> list[tuple[int, int]]:
        """Open grid points this point can connect to, nearest first.

        A grid point can be up to 35 m off the real point. Addresses sit beside buildings and a
        recalled aircraft hovers just outside a zone boundary, so the nearest grid point often
        falls inside a building or within the clearance distance — and a perfectly good
        destination came back as 'no route'. So every point within two cells whose straight
        line to the point is passable is used: the single nearest one can be trapped in a
        pocket between buildings (that is why the way back from north of Central Park failed),
        so the search starts from all of them at once and ends on reaching any one. _pin joins
        the real point.
        """
        centre = self._node(*point)
        candidates = sorted(
            ((centre[0] + di, centre[1] + dj) for di in range(-2, 3) for dj in range(-2, 3)),
            key=lambda node: math.dist(self._coords(node), point),
        )
        open_nodes = []
        for node in candidates:
            if self._blocked(node):
                continue
            if self.leg_altitude(point, self._coords(node)) is not None:
                open_nodes.append(node)
        return open_nodes

    def _attach(self, legs: list[Leg], start: tuple[float, float],
                goal: tuple[float, float]) -> list[Leg]:
        """Join the real start and destination to the two ends of the grid path.

        The search runs on grid points, so the path ends up to 35 m from the destination. We
        used to swap the end grid point for the real point, but then the last leg became a new,
        never-judged segment that could graze a building corner and get the whole route refused
        (Union Square). Now the grid point stays and a short leg is appended to the real point —
        a segment _free_nodes_near has already cleared. Altitude: the lower of the two sides.
        """
        if not legs:
            return legs
        head_alt = self.leg_altitude(start, (legs[0].lat, legs[0].lon))
        tail_alt = self.leg_altitude((legs[-1].lat, legs[-1].lon), goal)
        if head_alt is None or tail_alt is None:
            return legs      # unreachable: _free_nodes_near already cleared these legs
        legs[0] = Leg(legs[0].lat, legs[0].lon, head_alt)
        return ([Leg(start[0], start[1], head_alt)] + legs
                + [Leg(goal[0], goal[1], tail_alt)])

    def _pull(self, path: list[tuple[int, int]], window: int = 80,
              keep=None, deadline: float | None = None) -> list[tuple[int, int]]:
        """Pull the string: stretches with nothing in the way become straight.

        A* scores equal-length paths the same whichever way they turn, so even over the open
        river it produces a staircase of single grid steps. Keeping only the turns reverted the
        whole path to the original if any one stretch failed, so obstacle-free stretches became
        staircases too. Here the path runs straight as far as it can and turns only where it
        is blocked.

        With keep(i, j), the i→j shortcut is taken only if it keeps the candidate's character
        (low, or clear). Otherwise a low detour straightens out over a tower and a route that
        stood off cuts across the corridor. Past the deadline the rest is not pulled, only
        reduced to its turns ((a) passes no deadline).
        """
        if len(path) < 3:
            return path
        kept = [path[0]]
        index = 0
        while index < len(path) - 1:
            if deadline is not None and time.monotonic() > deadline:
                kept.extend(self._simplify(path[index:])[1:])
                return kept
            furthest = index + 1
            for candidate in range(min(len(path) - 1, index + window), index + 1, -1):
                if keep is not None and not keep(index, candidate):
                    continue
                if self._legal_chain([path[index], path[candidate]]):
                    furthest = candidate
                    break
            kept.append(path[furthest])
            index = furthest
        return kept

    def _legal_chain(self, nodes: list[tuple[int, int]]) -> bool:
        """Do the joined legs stay out of forbidden areas? Checks segments, not grid points."""
        return all(
            not self._blocked(a) and not self._blocked(b)
            and not self._crosses(a, b)
            for a, b in zip(nodes, nodes[1:], strict=False)
        )

    def _straight(self, a: tuple[int, int], b: tuple[int, int]) -> bool | None:
        return True if self._legal_chain([a, b]) else None

    def _search(self, starts, goals, budget: int = 120_000, step_cost=None,
                deadline: float | None = None, heuristic_scale: float = 1.0, allowed=None):
        """Start from several grid points at once; stop on reaching any goal grid point.

        step_cost(node, nxt, step), if given, prices each edge (the (b)/(c) penalties).
        heuristic_scale inflates the heuristic to match those penalties (weighted A*). Past the
        deadline (monotonic) it returns None, and it never steps on a grid point where
        allowed(node) is False (the tube for alternatives). (a) passes none of the four, so it
        behaves exactly as before, step for step.
        """
        starts = [starts] if isinstance(starts, tuple) else list(starts)
        goals = {goals} if isinstance(goals, tuple) else set(goals)
        goal = min(goals, key=lambda node: math.dist(node, starts[0]))
        origin = starts[0]
        reach = max(40, int(SEARCH_REACH_M / (self.cell * 110_570.0)))

        def heuristic(node):
            return math.dist(node, goal) * heuristic_scale

        open_set = [(heuristic(start), 0.0, start) for start in starts]
        heapq.heapify(open_set)
        came_from: dict = {}
        best = {start: 0.0 for start in starts}
        seen = 0
        while open_set:
            _, cost, node = heapq.heappop(open_set)
            if node in goals:
                path = [node]
                while node in came_from:
                    node = came_from[node]
                    path.append(node)
                return list(reversed(path))
            seen += 1
            if seen > budget:
                return None
            if (deadline is not None and seen % DEADLINE_CHECK_EVERY == 0
                    and time.monotonic() > deadline):
                return None
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nxt = (node[0] + di, node[1] + dj)
                # The search range is a distance. As a node count, a finer grid would shrink the
                # reachable range with it and far destinations would never be found. Looking
                # only around the destination yields routes that work out but not back — a wide
                # detour inside the destination's window on the way out lies outside the window
                # on the way back. So both windows count.
                if ((abs(nxt[0] - goal[0]) > reach or abs(nxt[1] - goal[1]) > reach)
                        and (abs(nxt[0] - origin[0]) > reach or abs(nxt[1] - origin[1]) > reach)):
                    continue
                if allowed is not None and not allowed(nxt):
                    continue
                if self._blocked(nxt) or self._crosses(node, nxt):
                    continue
                step = math.hypot(di, dj)
                fresh = cost + (step if step_cost is None else step_cost(node, nxt, step))
                if fresh < best.get(nxt, float("inf")):
                    best[nxt] = fresh
                    came_from[nxt] = node
                    heapq.heappush(open_set, (fresh + heuristic(nxt), fresh, nxt))
        return None

    @staticmethod
    def _simplify(path: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Merge runs of cells in the same direction, leaving only the turning points."""
        if len(path) < 3:
            return path
        kept = [path[0]]
        for previous, node, following in zip(path, path[1:], path[2:], strict=False):
            before = (node[0] - previous[0], node[1] - previous[1])
            after = (following[0] - node[0], following[1] - node[1])
            if before != after:
                kept.append(node)
        kept.append(path[-1])
        return kept

    def _to_legs(self, nodes: list[tuple[int, int]]) -> list[Leg]:
        """Give each leg its lowest safe altitude (_edge); one altitude per leg."""
        legs = []
        for index, node in enumerate(nodes):
            lat, lon = self._coords(node)
            if index == 0:
                altitude = self._edge(node, nodes[1]) if len(nodes) > 1 else self.floor_alt_m
            else:
                altitude = self._edge(nodes[index - 1], node)
            legs.append(Leg(lat, lon, self.floor_alt_m if altitude is None else altitude))
        return legs

    # ---------- candidates ----------

    def candidates(self, start: tuple[float, float], goal: tuple[float, float],
                   context: dict | None = None, budget_s: float | None = None) -> list[dict]:
        """Up to three legal routes: (a) shortest, (b) lowest, (c) clear of other aircraft and
        incident zones.

        Only routes that pass our copy of first_breach come out — legality is settled when a
        route is built, not after it is chosen. Near-identical routes (within DISTINCT_M) count
        as one. context is what to keep clear of (input to keep_clear_shapes); with nothing to
        keep clear of, (c) would be the same route as (a), so it isn't built.
        This file doesn't choose — the operator's model does, and whatever it picks, the runtime
        judges.
        One candidate: {id, label, legs, length_m, max_alt_m, min_alt_m, reason_tags}.
        """
        began = time.monotonic()
        shortest = self.plan(start, goal)
        self.last_timings = {"a": round(time.monotonic() - began, 3)}
        if shortest is None:
            return []   # no (a) means no route; a penalised search would hit the same wall
        shapes = keep_clear_shapes(context)
        budget = VARIANT_BUDGET_S if budget_s is None else max(0.0, float(budget_s))
        found = [self._candidate("a", [leg.to_dict() for leg in shortest.legs], shapes,
                                 ["detour" if shortest.detoured else "straight"])]
        if budget <= 0.0:
            return found
        for variant in ("b", "c"):
            if variant == "c" and not shapes:
                continue
            began = time.monotonic()
            legs = self._variant(variant, start, goal, shapes, budget, found[0]["legs"])
            self.last_timings[variant] = round(time.monotonic() - began, 3)
            if legs is None:
                continue
            candidate = self._candidate(variant, legs, shapes, [VARIANT_TAGS[variant]])
            if candidate["length_m"] > MAX_VARIANT_STRETCH * max(1, found[0]["length_m"]):
                continue
            if any(same_route(candidate["legs"], other["legs"]) for other in found):
                continue
            found.append(candidate)
        return found

    def _variant(self, variant: str, start: tuple[float, float], goal: tuple[float, float],
                 shapes: list["KeepClear"], budget_s: float,
                 around: list[dict]) -> list[dict] | None:
        """Find (b) or (c) with a penalised A*. Returns legs that pass, or None.

        From there it matches (a): string-pulling (only where the character holds) → turns
        only → raw path, with first_breach last.
        """
        key = (variant, round(start[0], 4), round(start[1], 4), round(goal[0], 4),
               round(goal[1], 4), tuple(shape.key for shape in shapes) if variant == "c" else ())
        if key in self._variant_memo:
            return self._variant_memo[key]
        starts = self._free_nodes_near(start)
        goals = self._free_nodes_near(goal)
        if not starts or not goals:
            return None
        deadline = time.monotonic() + budget_s
        tube = self._tube(around, VARIANT_TUBE_M)
        if variant == "b":
            path = self._search(starts, goals, VARIANT_NODE_BUDGET, self._altitude_cost,
                                deadline, HEURISTIC_SCALE["b"], tube)
            keep = None if path is None else self._keeps_low(path)
        else:
            penalty = self._penalty_for(shapes)

            def clearance_cost(node, nxt, step):
                return step * (1.0 + penalty(nxt))

            path = self._search(starts, goals, VARIANT_NODE_BUDGET, clearance_cost, deadline,
                                HEURISTIC_SCALE["c"], tube)
            keep = None if path is None else self._keeps_clear(path, penalty, shapes)
        if path is None:
            # Not found within the limit. Not memoised — next time the edge memo will be fuller
            return None
        pull_by = max(deadline, time.monotonic() + PULL_GRACE_S)
        pulled = self._pull(path, window=VARIANT_PULL_WINDOW, keep=keep, deadline=pull_by)
        for nodes in (pulled, self._simplify(path), path):
            if not self._legal_chain(nodes):
                continue
            legs = [leg.to_dict() for leg in self._attach(self._to_legs(nodes), start, goal)]
            if first_breach(self.airspace, legs) is None:
                self._variant_memo[key] = legs
                return legs
        return None

    def _tube(self, legs: list[dict], width_m: float):
        """Is a grid point within width_m of legs? The search checks the same point many
        times, so answers are memoised for this search only."""
        points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
        segments = list(zip(points, points[1:], strict=False))
        known: dict[tuple[int, int], bool] = {}

        def inside(node: tuple[int, int]) -> bool:
            value = known.get(node)
            if value is None:
                lat, lon = self._coords(node)
                value = min(_point_segment_m(lat, lon, a, b)[0] for a, b in segments) <= width_m
                known[node] = value
            return value

        return inside

    def _altitude_cost(self, node, nxt, step: float) -> float:
        """Edge cost for (b): the higher its lowest safe altitude, the more it costs (_edge is
        memoised)."""
        altitude = self._edge(node, nxt)
        if altitude is None:
            return step        # unreachable: _search filters out blocked edges first
        span = max(1.0, self.cruise_alt_m - self.floor_alt_m)
        return step * (1.0 + ALTITUDE_WEIGHT * max(0.0, altitude - self.floor_alt_m) / span)

    def _penalty_for(self, shapes: list["KeepClear"]):
        """Grid-point penalty for (c). The search revisits points, so it is memoised for this
        search only."""
        known: dict[tuple[int, int], float] = {}

        def penalty(node: tuple[int, int]) -> float:
            value = known.get(node)
            if value is None:
                value = clear_penalty(*self._coords(node), shapes)
                known[node] = value
            return value

        return penalty

    def _keeps_low(self, path: list[tuple[int, int]]):
        """Pull only if the shortcut flies no higher than the highest leg it replaces."""
        altitudes = [self._edge(a, b) for a, b in zip(path, path[1:], strict=False)]
        altitudes = [self.cruise_alt_m if alt is None else alt for alt in altitudes]

        def keep(index: int, candidate: int) -> bool:
            shortcut = self._edge(path[index], path[candidate])
            return shortcut is not None and shortcut <= max(altitudes[index:candidate]) + 0.5

        return keep

    def _keeps_clear(self, path: list[tuple[int, int]], penalty, shapes: list["KeepClear"]):
        """Pull only if no point on the shortcut is closer to a keep-clear shape than the
        stretch of path it replaces."""
        along = [penalty(node) for node in path]

        def keep(index: int, candidate: int) -> bool:
            limit = max(along[index:candidate + 1]) + 0.05
            a, b = self._coords(path[index]), self._coords(path[candidate])
            steps = max(1, int(_flat_m(a, b) / SAMPLE_STEP_M))
            return all(clear_penalty(a[0] + (b[0] - a[0]) * k / steps,
                                     a[1] + (b[1] - a[1]) * k / steps, shapes) <= limit
                       for k in range(steps + 1))

        return keep

    def _candidate(self, variant: str, legs: list[dict], shapes: list["KeepClear"],
                   tags: list[str]) -> dict:
        """One candidate row, tagged with what it passes near (within CLEAR_MARGIN_M) — the
        model and the rules read the tags."""
        altitudes = [float(leg["alt_m"]) for leg in legs]
        near = [f"near-{kind}:{name}" for kind, name in exposure(legs, shapes)]
        return {"id": variant, "label": CANDIDATE_LABELS[variant], "legs": legs,
                "length_m": round(route_length_m(legs)),
                "max_alt_m": round(max(altitudes), 1), "min_alt_m": round(min(altitudes), 1),
                "reason_tags": list(tags) + near}


# ---------- what to keep clear of (candidate (c)) ----------


@dataclass(frozen=True)
class KeepClear:
    """One thing candidate (c) keeps clear of: another aircraft's approved corridor (segments)
    or a circle (incident or notice zone)."""

    id: str
    kind: str                                  # traffic | keepout
    segments: tuple = ()                       # (((lat, lon), (lat, lon)), ...)
    centre: tuple | None = None                # circle centre (lat, lon)
    radius_m: float = 0.0
    box: tuple = (0.0, 0.0, 0.0, 0.0)          # (south, north, west, east) + penalty distance

    def in_box(self, lat: float, lon: float) -> bool:
        south, north, west, east = self.box
        return south <= lat <= north and west <= lon <= east

    def distance_m(self, lat: float, lon: float) -> float:
        if self.centre is not None:
            return max(0.0, _flat_m((lat, lon), self.centre) - self.radius_m)
        return min((_point_segment_m(lat, lon, a, b)[0] for a, b in self.segments),
                   default=math.inf)

    @property
    def key(self) -> tuple:
        """Same shape, same value (coordinates rounded to about 10 m). Key for the (c) memo."""
        centre = tuple(round(value, 4) for value in (self.centre or ()))
        segments = tuple((round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4))
                         for a, b in self.segments)
        return (self.id, round(self.radius_m), centre, segments)


def keep_clear_shapes(context: dict | None) -> list[KeepClear]:
    """Shapes to keep clear of. context = {"traffic": [{"id", "legs": [{"lat", "lon"}, ...]}],
    "keepouts": [{"id", "lat", "lon", "radius_m"} or {"id", "polygon": [[lat, lon], ...]}]}.

    A polygon becomes a circle from its centroid to its farthest vertex. This is a penalty, not
    a judgement, so erring wide is right — the zone itself is already a forbidden volume in the
    airspace copy and the planner never crosses it. Unreadable items are skipped (if the
    runtime's /state changes shape, only candidate (c) is lost).
    """
    context = context or {}
    shapes: list[KeepClear] = []
    for item in context.get("traffic") or []:
        points = _points_of(item.get("legs") if isinstance(item, dict) else None)
        if not points:
            continue
        if len(points) == 1:
            points = points * 2
        shapes.append(KeepClear(str(item.get("id") or "traffic"), "traffic",
                                segments=tuple(zip(points, points[1:], strict=False)),
                                box=_box(points, 0.0)))
    for item in context.get("keepouts") or []:
        centre, radius = _circle_of(item if isinstance(item, dict) else {})
        if centre is None:
            continue
        shapes.append(KeepClear(str(item.get("id") or "keepout"), "keepout", centre=centre,
                                radius_m=radius, box=_box([centre], radius)))
    return shapes


def clear_penalty(lat: float, lon: float, shapes: list[KeepClear]) -> float:
    """Penalty by distance to the nearest shape: 0 beyond the penalty distance, CLEAR_WEIGHT
    when touching."""
    nearest = math.inf
    for shape in shapes:
        if shape.in_box(lat, lon):
            nearest = min(nearest, shape.distance_m(lat, lon))
    if nearest >= CLEAR_MARGIN_M:
        return 0.0
    return CLEAR_WEIGHT * (1.0 - nearest / CLEAR_MARGIN_M)


def exposure(legs: list[dict], shapes: list[KeepClear]) -> list[tuple[str, str]]:
    """Shapes the route passes within the penalty distance of, as [(kind, id)]."""
    points = route_samples(legs)
    near = []
    for shape in shapes:
        if any(shape.in_box(lat, lon) and shape.distance_m(lat, lon) < CLEAR_MARGIN_M
               for lat, lon in points):
            near.append((shape.kind, shape.id))
    return near


def route_samples(legs: list[dict], step_m: float = SAMPLE_STEP_M) -> list[tuple[float, float]]:
    """Sample the route into points step_m apart (end point included)."""
    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
    samples: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:], strict=False):
        steps = max(1, int(_flat_m(a, b) / step_m))
        samples.extend((a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)
                       for k in range(steps))
    if points:
        samples.append(points[-1])
    return samples


def route_length_m(legs: list[dict]) -> float:
    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
    return sum(_flat_m(a, b) for a, b in zip(points, points[1:], strict=False))


def same_route(first: list[dict], second: list[dict], limit_m: float = DISTINCT_M) -> bool:
    """Same route if every point of each lies within limit_m of the other (two-way Hausdorff)."""
    return _within(first, second, limit_m) and _within(second, first, limit_m)


def _within(first: list[dict], second: list[dict], limit_m: float) -> bool:
    segments = [((float(a["lat"]), float(a["lon"])), (float(b["lat"]), float(b["lon"])))
                for a, b in zip(second, second[1:], strict=False)]
    if not segments:
        return False
    for lat, lon in route_samples(first, 2 * SAMPLE_STEP_M):
        if min(_point_segment_m(lat, lon, a, b)[0] for a, b in segments) > limit_m:
            return False
    return True


def _flat_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def _points_of(raw) -> list[tuple[float, float]]:
    points = []
    for item in raw or []:
        try:
            lat, lon = (float(item["lat"]), float(item["lon"])) if isinstance(item, dict) \
                else (float(item[0]), float(item[1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon):
            points.append((lat, lon))
    return points


def _circle_of(item: dict) -> tuple[tuple[float, float] | None, float]:
    """As one circle: centre and radius as given, or polygon centroid + farthest vertex."""
    try:
        if item.get("lat") is not None and item.get("lon") is not None:
            return (float(item["lat"]), float(item["lon"])), max(0.0, float(item.get("radius_m")
                                                                             or 0.0))
    except (TypeError, ValueError):
        return None, 0.0
    polygon = _points_of(item.get("polygon"))
    if not polygon:
        return None, 0.0
    centre = (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
    return centre, max(_flat_m(centre, point) for point in polygon)


def _box(points: list[tuple[float, float]], radius_m: float) -> tuple[float, float, float, float]:
    reach = radius_m + CLEAR_MARGIN_M
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats) - reach / METRES_PER_DEG_LAT, max(lats) + reach / METRES_PER_DEG_LAT,
            min(lons) - reach / METRES_PER_DEG_LON, max(lons) + reach / METRES_PER_DEG_LON)
