"""Who takes which delivery, and in what order. The operator's call, never the runtime's.

The runtime judges routes. It does not decide which drone serves which customer — that is
fleet dispatch, and dispatch is the operator's business (here the simulator plays the
operator's order desk). Putting it in the runtime would make the runtime the operator, and
then the party handing out the work would also be the party saying the work is safe.

Two dispatchers share one interface, `next_stop(world, vehicle) -> landing area | None`:

- RuleDispatcher — the rule this world always used, moved here unchanged from
  World._assign_job, so seed 7 plays exactly the same round as before.
- CuOptDispatcher — posts the fleet and the open orders to an NVIDIA cuOpt server (open
  source, Apache-2.0) and follows the vehicle routes it returns. cuOpt solves on a GPU, and
  there is none on this laptop, so it runs on a Nebius AI Cloud instance: it stays off unless
  CUOPT_URL (or configs/fleet.yaml dispatch.solver: cuopt) says where the server is. Every
  failure — no server, a timeout, an infeasible or malformed answer, an unknown id — falls
  back to the rule for that stop and backs off before asking again, because the simulator's
  clock thread is what calls this and a stalled dispatcher would stop the world.

Either way the aircraft still has to ask the runtime for a route to whatever stop it is
given. A better dispatch plan changes the order book, never the clearance.
"""

import math
import os
import random
import threading
import time
import weakref
from pathlib import Path

from shared.http import get_json, post_json_status
from sim.world import (
    CRUISE_MPS,
    DEPOT,
    DROP_TICKS,
    LANDING_AREAS,
    PARCELS_PER_STOP,
    SIM_SECONDS_PER_TICK,
    STOPS_PER_TRIP,
    to_latlon,
)

CONFIG_FILE = os.getenv("CONFIG",
                        str(Path(__file__).resolve().parent.parent / "configs/fleet.yaml"))
# Longest the world thread waits for cuOpt (s). Ticks stop for that long, so keep it short.
CUOPT_TIMEOUT_S = 2.0
# Solve time on the server (s). With 30 stops and 4 aircraft, 1 s is plenty.
CUOPT_TIME_LIMIT_S = 1.0
# After a failure, use the rule for this long without asking. Waiting 2 s at every stop on a
# dead server would stall the screen by as much.
CUOPT_BACKOFF_S = 60.0
POLL_S = 0.05
# Header the cuOpt server requires. The value is the client version; the server only checks
# that it is there.
CLIENT_VERSION = "custom"


class RuleDispatcher:
    """The old World._assign_job, not one line changed — seed 7 must play the same round.

    The first stop is any landing site; the second is one of the six nearest the first. Two far
    stops in a row (Harlem → Battery Park) make one trip longer than a round, and real dispatch
    also keeps a trip within one neighbourhood. Sites another aircraft is heading to are avoided
    — one aircraft per site at a time (a runtime rule); when two took the same site, the second
    sat 1300 ticks at its first stop until the first one left.
    """

    name = "rules"

    def next_stop(self, world, vehicle) -> dict | None:
        taken = _taken_by_others(world, vehicle)
        pool = [area for area in LANDING_AREAS
                if area["name"] != vehicle.job_label and area["name"] not in taken]
        if not pool:
            pool = [area for area in LANDING_AREAS if area["name"] != vehicle.job_label]
        if not pool:
            return None
        if vehicle.stops_left < STOPS_PER_TRIP and vehicle.job_x is not None:
            here_lat, here_lon = to_latlon(vehicle.job_x, vehicle.job_y)
            pool = sorted(pool, key=lambda a: math.hypot((a["lat"] - here_lat) * 110_570,
                                                         (a["lon"] - here_lon) * 84_400))[:6]
        return world._rng.choice(pool)


class CuOptDispatcher:
    """Asks cuOpt for the fleet's next trip: which aircraft visits which site, in what order.

    It plans the whole trip at once rather than one stop at a time (that is what a VRP solver is
    good at). The plan is held as a queue per aircraft and handed out one stop at a time; an
    empty queue means solving again. If anything is off, the rule picks that stop — a stalled
    dispatcher grounds the fleet, and dispatch is work, not safety, so it has no reason to stall.
    """

    name = "cuopt"

    def __init__(self, url: str, timeout_s: float = CUOPT_TIMEOUT_S,
                 time_limit_s: float = CUOPT_TIME_LIMIT_S, backoff_s: float = CUOPT_BACKOFF_S,
                 seed: int = 7, fallback=None):
        self.url = (url or "").rstrip("/")
        self.timeout_s = float(timeout_s)
        self.time_limit_s = float(time_limit_s)
        self.backoff_s = float(backoff_s)
        # Dice for drawing the order book. Not the world's (world._rng): the rule dispatcher
        # uses that one, so sharing it would change the rule dispatcher's round just by turning
        # cuOpt on or off.
        self.rng = random.Random(seed + 4242)
        self.fallback = fallback or RuleDispatcher()
        self.queues: dict[str, list[str]] = {}
        self.skip_until = 0.0
        self.stats = {"solved": 0, "served": 0, "fell_back": 0, "last_error": None}
        self._lock = threading.Lock()
        # The worker thread talking to cuOpt. While one abandoned at its deadline is still
        # running, no new request is made.
        self._asking: threading.Thread | None = None

    def next_stop(self, world, vehicle) -> dict | None:
        with self._lock:
            area = self._from_queue(world, vehicle)
            if area is not None:
                self.stats["served"] += 1
                return area
            if time.monotonic() < self.skip_until:
                return self._fall_back(world, vehicle, None)
            plan = self._solve(world, vehicle)
            if plan is None:
                self.skip_until = time.monotonic() + self.backoff_s
                return self._fall_back(world, vehicle, None)
            self.queues.update(plan)
            self.stats["solved"] += 1
            area = self._from_queue(world, vehicle)
            if area is None:
                return self._fall_back(world, vehicle, "no stop for this aircraft")
            self.stats["served"] += 1
            return area

    # ---------- planning ----------

    def _from_queue(self, world, vehicle) -> dict | None:
        """The first usable stop in this aircraft's queue, skipping sites another aircraft has
        since taken."""
        taken = _taken_by_others(world, vehicle)
        queue = self.queues.get(vehicle.id) or []
        while queue:
            name = queue.pop(0)
            if name in taken or name == vehicle.job_label:
                continue
            area = _area_named(name)
            if area is not None:
                return area
        return None

    def _solve(self, world, vehicle):
        """{aircraft: [site name, ...]} or None. Failure here only means the rule takes over."""
        crew = [vehicle] + [other for other in world.vehicles.values()
                            if other is not vehicle and not self.queues.get(other.id)
                            and not other.job_label]
        stops = {member.id: _stops_for(member) for member in crew}
        orders = self._orders(world, vehicle, sum(stops.values()))
        if not orders:
            self.stats["last_error"] = "no open landing areas to dispatch"
            return None
        body = self._problem(crew, stops, orders)
        answer = self._ask(body)
        if answer is None:
            return None
        return self._plan_from(answer, crew, stops, orders)

    def _orders(self, world, vehicle, count: int) -> list[dict]:
        """The order book to plan. Sites someone is heading to are left out (one per site)."""
        taken = _taken_by_others(world, vehicle) | {vehicle.job_label}
        pool = [area for area in LANDING_AREAS if area["name"] not in taken]
        if len(pool) < count:
            return []
        return self.rng.sample(pool, count)

    def _problem(self, crew, stops: dict, orders: list[dict]) -> dict:
        """Into cuOpt's data model. Location 0 is the depot — every trip ends there."""
        places = [to_latlon(*DEPOT)] + [(order["lat"], order["lon"]) for order in orders]
        starts = []
        for member in crew:
            places.append(_vehicle_place(member))
            starts.append(len(places) - 1)
        matrix = [[round(_metres(a, b)) for b in places] for a in places]
        seconds = [[round(cost / CRUISE_MPS) for cost in row] for row in matrix]
        return {
            "cost_matrix_data": {"data": {"0": matrix}},
            "travel_time_matrix_data": {"data": {"0": seconds}},
            "fleet_data": {
                # [start location, return location]. A trip ends at the depot.
                "vehicle_locations": [[start, 0] for start in starts],
                "vehicle_ids": [member.id for member in crew],
                "capacities": [[stops[member.id] * PARCELS_PER_STOP for member in crew]],
            },
            "task_data": {
                "task_locations": list(range(1, len(orders) + 1)),
                "task_ids": [order["id"] for order in orders],
                "demand": [[PARCELS_PER_STOP] * len(orders)],
                "service_times": [round(DROP_TICKS * SIM_SECONDS_PER_TICK)] * len(orders),
            },
            "solver_config": {"time_limit": self.time_limit_s},
        }

    def _ask(self, body: dict) -> dict | None:
        """Asks cuOpt once. The caller (the world's clock thread) waits at most timeout_s.

        The whole exchange runs on a worker thread. urllib's timeout applies per socket read, so
        a server that trickles its answer a byte at a time holds on several times longer —
        measured: a 2 s budget froze both worlds for 21-30 s. Past the deadline the rule takes
        over and a late answer is dropped (whoever was waiting has moved on).
        """
        if self._asking is not None and self._asking.is_alive():
            self.stats["last_error"] = "the last cuOpt request is still hanging"
            return None
        deadline = time.monotonic() + self.timeout_s
        box: dict = {}
        worker = threading.Thread(target=self._exchange, args=(body, deadline, box),
                                  daemon=True, name="cuopt-ask")
        self._asking = worker
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive() or "solution" not in box:
            self.stats["last_error"] = box.get("error") or "cuOpt did not answer in time"
            return None
        return box["solution"]

    def _exchange(self, body: dict, deadline: float, box: dict) -> None:
        """POST /cuopt/request → {"reqId"}, then GET /cuopt/solution/{id} until it answers.

        Until it has solved, the server returns only {"reqId": ...} (200); once response is
        attached it is done. Runs on the worker thread and writes its result only to box — stats
        belongs to the world thread, and an abandoned thread writing late would overwrite the
        next stop's record.
        """
        try:
            status, answer = post_json_status(f"{self.url}/cuopt/request", body,
                                              timeout=max(0.1, deadline - time.monotonic()),
                                              headers={"CLIENT-VERSION": CLIENT_VERSION})
            if status != 200 or not isinstance(answer, dict):
                box["error"] = f"POST /cuopt/request → {status}"
                return
            solution = answer.get("response")
            request_id = answer.get("reqId")
            while solution is None and request_id and time.monotonic() < deadline:
                time.sleep(POLL_S)
                answer = get_json(f"{self.url}/cuopt/solution/{request_id}",
                                  timeout=max(0.1, deadline - time.monotonic())) or {}
                solution = answer.get("response")
            if not isinstance(solution, dict):
                box["error"] = "cuOpt did not answer in time"
                return
            box["solution"] = solution
        except Exception as error:  # noqa: BLE001 - whatever breaks, that stop goes to the rule
            box["error"] = f"cuOpt request failed: {error!r}"[:200]

    def _plan_from(self, solution: dict, crew, stops: dict, orders: list[dict]):
        """cuOpt's answer as per-aircraft stop queues. Any unknown name discards the lot."""
        found = solution.get("solver_response") or {}
        if str(found.get("status")) != "0":
            self.stats["last_error"] = f"solver status {found.get('status')}"
            return None
        by_id = {order["id"]: order for order in orders}
        ids = [member.id for member in crew]
        plan: dict[str, list[str]] = {}
        for key, route in (found.get("vehicle_data") or {}).items():
            vehicle_id = key if key in ids else _by_index(ids, key)
            if vehicle_id is None or not isinstance(route, dict):
                self.stats["last_error"] = f"unknown vehicle {key!r}"
                return None
            names = []
            kinds = route.get("type") or []
            for index, task in enumerate(route.get("task_id") or []):
                kind = kinds[index] if index < len(kinds) else ""
                if task == "Depot" or kind in ("Depot", "Break", "w"):
                    continue
                order = by_id.get(task)
                if order is None:
                    self.stats["last_error"] = f"unknown task {task!r}"
                    return None
                names.append(order["name"])
            plan[vehicle_id] = names[:stops.get(vehicle_id, STOPS_PER_TRIP)]
        if not any(plan.values()):
            self.stats["last_error"] = "cuOpt returned no stops"
            return None
        return plan

    def _fall_back(self, world, vehicle, why: str | None) -> dict | None:
        self.stats["fell_back"] += 1
        if why:
            self.stats["last_error"] = why
        return self.fallback.next_stop(world, vehicle)


# ---------- which dispatcher ----------


def settings() -> dict:
    """The dispatch section of configs/fleet.yaml. Environment variables (DISPATCH, CUOPT_*) win.

    The runtime does not read this section — dispatch is the operator's job. If the file cannot
    be read, dispatch uses the rule: one setting must never keep the world from starting.
    """
    raw = {}
    try:
        import yaml
        text = Path(CONFIG_FILE).read_text(encoding="utf-8")
        raw = (yaml.safe_load(text) or {}).get("dispatch") or {}
    except FileNotFoundError:
        pass    # the simulator image has only configs/airspace; then only the environment counts
    except Exception as error:  # noqa: BLE001 - an unreadable config must not stop the world
        print(f"dispatch: settings unreadable ({error!r}) — using rule dispatch", flush=True)
    return {
        "solver": (os.getenv("DISPATCH") or raw.get("solver") or "rules").strip().lower(),
        "url": (os.getenv("CUOPT_URL") or raw.get("cuopt_url") or "").strip(),
        "timeout_s": _number("CUOPT_TIMEOUT_S", raw.get("timeout_s"), CUOPT_TIMEOUT_S),
        "time_limit_s": _number("CUOPT_TIME_LIMIT_S", raw.get("time_limit_s"),
                                CUOPT_TIME_LIMIT_S),
        "backoff_s": _number("CUOPT_BACKOFF_S", raw.get("backoff_s"), CUOPT_BACKOFF_S),
    }


def build_dispatcher(seed: int = 7):
    found = settings()
    if found["solver"] == "cuopt" and found["url"]:
        print(f"dispatch: cuOpt at {found['url']} (the rule stands by as fallback)",
              flush=True)
        return CuOptDispatcher(found["url"], timeout_s=found["timeout_s"],
                               time_limit_s=found["time_limit_s"],
                               backoff_s=found["backoff_s"], seed=seed)
    if found["solver"] == "cuopt":
        print("DISPATCH=cuopt but CUOPT_URL is not set — using rule dispatch", flush=True)
    return RuleDispatcher()


_DISPATCHERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def dispatcher_for(world):
    """This world's dispatcher. One per world — two worlds must never share a plan."""
    found = _DISPATCHERS.get(world)
    if found is None:
        found = build_dispatcher(seed=_seed())
        _DISPATCHERS[world] = found
    return found


def _seed() -> int:
    try:
        return int(os.getenv("SEED", "7"))
    except ValueError:
        return 7


def _number(name: str, configured, fallback: float) -> float:
    for value in (os.getenv(name), configured):
        try:
            if value not in (None, ""):
                return float(value)
        except (TypeError, ValueError):
            continue
    return fallback


def _stops_for(vehicle) -> int:
    """How many more stops this aircraft makes on this trip."""
    if 0 < vehicle.stops_left < STOPS_PER_TRIP:
        return vehicle.stops_left
    return STOPS_PER_TRIP


def _taken_by_others(world, vehicle) -> set:
    return {other.job_label for other in world.vehicles.values()
            if other is not vehicle and other.job_label}


def _area_named(name: str) -> dict | None:
    return next((area for area in LANDING_AREAS if area["name"] == name), None)


def _vehicle_place(vehicle) -> tuple[float, float]:
    """Where this aircraft's next leg starts: the drop it is heading to, else where it is now."""
    if vehicle.job_x is not None:
        return to_latlon(vehicle.job_x, vehicle.job_y)
    return to_latlon(vehicle.x, vehicle.y)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * 110_570.0, (b[1] - a[1]) * 84_400.0)


def _by_index(ids: list[str], key) -> str | None:
    """Some cuOpt servers answer with an index instead of the aircraft name."""
    text = str(key)
    if text.isdigit() and int(text) < len(ids):
        return ids[int(text)]
    return None
