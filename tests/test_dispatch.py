"""Dispatch is the operator's business, and changing the dispatcher must not move the world.

RuleDispatcher is the rule this world always used, lifted out of World._assign_job. This file
keeps a verbatim copy of that old code beside it and plays the same vehicles through both, so
"unchanged for seed 7" is checked rather than asserted in a comment.

CuOptDispatcher speaks the REST API a cuOpt server actually speaks: POST /cuopt/request
answers {"reqId"} straight away, and GET /cuopt/solution/{id} answers {"reqId"} again until
the solve is done, then carries the solution. There is no GPU on this machine — cuOpt runs on
a Nebius AI Cloud instance — so the server here is a fake that answers in that shape. What is
tested is our half of the contract: what we send, what we make of the answer, and that every
way it can go wrong ends with the rule choosing that stop and the world carrying on.
"""

import http.server
import json
import math
import threading
import time
import unittest
from unittest import mock

from sim import dispatch as dispatch_module
from sim.dispatch import CuOptDispatcher, RuleDispatcher, build_dispatcher, dispatcher_for
from sim.world import LANDING_AREAS, PARCELS_PER_STOP, STOPS_PER_TRIP, Simulation, to_grid


def old_assign_job(world, vehicle):
    """The old body of World._assign_job, verbatim. Only the last two lines (setting the
    drop-off) were changed to return the stop."""
    taken = {other.job_label for other in world.vehicles.values()
             if other is not vehicle and other.job_label}
    pool = [area for area in LANDING_AREAS
            if area["name"] != vehicle.job_label and area["name"] not in taken]
    if not pool:
        pool = [area for area in LANDING_AREAS if area["name"] != vehicle.job_label]
    if not pool:
        return None
    if vehicle.stops_left < STOPS_PER_TRIP and vehicle.job_x is not None:
        from sim.world import to_latlon
        here_lat, here_lon = to_latlon(vehicle.job_x, vehicle.job_y)
        pool = sorted(pool, key=lambda a: math.hypot((a["lat"] - here_lat) * 110_570,
                                                     (a["lon"] - here_lon) * 84_400))[:6]
    return world._rng.choice(pool)


def _drive(world, pick, rounds: int = 80) -> list:
    """Picks a stop and applies it straight to the world, rounds times. The comparison only
    means something if both worlds pass through the same states."""
    names = []
    vehicles = list(world.vehicles.values())
    for step in range(rounds):
        vehicle = vehicles[step % len(vehicles)]
        vehicle.stops_left = (step % STOPS_PER_TRIP) + 1
        area = pick(world, vehicle)
        names.append(None if area is None else area["name"])
        if area is not None:
            vehicle.job_label = area["name"]
            vehicle.job_x, vehicle.job_y = to_grid(area["lat"], area["lon"])
    return names


class RuleDispatcherIsTheOldCodeTest(unittest.TestCase):
    def test_seed_7_hands_out_exactly_the_same_stops_as_before(self):
        before = Simulation(seed=7).worlds["guarded"]
        after = Simulation(seed=7).worlds["guarded"]
        dispatcher = RuleDispatcher()
        self.assertEqual(_drive(before, old_assign_job),
                         _drive(after, dispatcher.next_stop))

    def test_the_world_still_opens_on_the_scripted_first_stops(self):
        world = Simulation(seed=7).worlds["guarded"]
        self.assertEqual(world.vehicles["drone-01"].job_label, "Central Park North 110th")
        self.assertEqual(world.vehicles["drone-03"].job_label, "Morningside Park")
        for vehicle in world.vehicles.values():
            self.assertIsNotNone(vehicle.job_x)

    def test_no_open_landing_area_means_no_job(self):
        world = Simulation(seed=7).worlds["guarded"]
        with mock.patch.object(dispatch_module, "LANDING_AREAS", []):
            self.assertIsNone(RuleDispatcher().next_stop(world, world.vehicles["drone-01"]))


# ---------- Fake cuOpt server ----------


def _solution_for(body: dict, keys: str = "ids") -> dict:
    """A cuOpt-shaped answer. Deals the tasks sent out to the aircraft in turn."""
    ids = body["fleet_data"]["vehicle_ids"]
    tasks = body["task_data"]["task_ids"]
    routes = {}
    for index, vehicle_id in enumerate(ids):
        mine = tasks[index::len(ids)]
        routes[vehicle_id if keys == "ids" else str(index)] = {
            "task_id": ["Depot"] + mine + ["Depot"],
            "type": ["Depot"] + ["Delivery"] * len(mine) + ["Depot"],
            "arrival_stamp": list(range(len(mine) + 2)),
            "route": list(range(len(mine) + 2)),
        }
    return {"response": {"solver_response": {
        "status": 0, "num_vehicles": len(ids), "solution_cost": 1.0,
        "vehicle_data": routes, "dropped_tasks": {"task_id": [], "task_index": []}}},
        "reqId": "req-1"}


class FakeCuOpt(http.server.BaseHTTPRequestHandler):
    posts: list = []
    gets: list = []
    keys = "ids"
    post_status = 200
    polls_pending = 1
    mangle = None            # function that breaks the answer (per test)
    trickle_s = 0.0          # non-zero: the body goes out a byte at a time, this far apart

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        type(self).posts.append({"path": self.path, "body": body})
        if type(self).post_status != 200:
            self.send_error(type(self).post_status)
            return
        self._json({"reqId": "req-1"})

    def do_GET(self):
        type(self).gets.append(self.path)
        if len(type(self).gets) <= type(self).polls_pending:
            self._json({"reqId": "req-1"})      # still solving
            return
        answer = _solution_for(type(self).posts[-1]["body"], type(self).keys)
        if type(self).mangle is not None:
            answer = type(self).mangle(answer)
        self._json(answer)

    def _json(self, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not type(self).trickle_s:
            self.wfile.write(data)
            return
        # A timeout that restarts on every read cannot cut off an answer like this.
        for index in range(len(data)):
            time.sleep(type(self).trickle_s)
            try:
                self.wfile.write(data[index:index + 1])
                self.wfile.flush()
            except OSError:
                return

    def log_message(self, *args):
        pass


class CuOptDispatchTest(unittest.TestCase):
    def setUp(self):
        FakeCuOpt.posts, FakeCuOpt.gets = [], []
        FakeCuOpt.keys, FakeCuOpt.post_status = "ids", 200
        FakeCuOpt.polls_pending, FakeCuOpt.mangle, FakeCuOpt.trickle_s = 1, None, 0.0
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCuOpt)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.world = Simulation(seed=7).worlds["guarded"]
        for vehicle in self.world.vehicles.values():   # start from a fleet with no orders
            vehicle.job_label, vehicle.job_x, vehicle.job_y = "", None, None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _dispatcher(self, **kwargs):
        return CuOptDispatcher(self.url, timeout_s=5.0, time_limit_s=0.1, **kwargs)

    def test_it_posts_the_fleet_and_the_orders_in_cuopt_shape(self):
        dispatcher = self._dispatcher()
        area = dispatcher.next_stop(self.world, self.world.vehicles["drone-01"])
        self.assertIn(area["name"], {a["name"] for a in LANDING_AREAS})
        self.assertEqual(FakeCuOpt.posts[0]["path"], "/cuopt/request")
        body = FakeCuOpt.posts[0]["body"]
        self.assertEqual(sorted(body), ["cost_matrix_data", "fleet_data", "solver_config",
                                        "task_data", "travel_time_matrix_data"])
        fleet, tasks = body["fleet_data"], body["task_data"]
        self.assertEqual(fleet["vehicle_ids"], sorted(self.world.vehicles))
        # One aircraft's capacity = stops this trip × parcels dropped per stop. The load must
        # fit exactly for every order to be delivered; with room to spare the solver piles
        # them onto one aircraft.
        self.assertEqual(fleet["capacities"],
                         [[STOPS_PER_TRIP * PARCELS_PER_STOP] * len(self.world.vehicles)])
        self.assertTrue(all(pair[1] == 0 for pair in fleet["vehicle_locations"]),
                        "a loop ends at the depot (location 0)")
        self.assertEqual(len(tasks["task_ids"]), STOPS_PER_TRIP * len(self.world.vehicles))
        self.assertEqual(tasks["demand"], [[PARCELS_PER_STOP] * len(tasks["task_ids"])])
        matrix = body["cost_matrix_data"]["data"]["0"]
        self.assertTrue(all(len(row) == len(matrix) for row in matrix))
        self.assertEqual(matrix[0][0], 0)
        self.assertEqual(body["solver_config"]["time_limit"], 0.1)

    def test_it_polls_until_the_solution_appears_and_then_serves_the_queue(self):
        FakeCuOpt.polls_pending = 2
        dispatcher = self._dispatcher()
        drone = self.world.vehicles["drone-01"]
        first = dispatcher.next_stop(self.world, drone)
        drone.job_label = first["name"]
        self.assertGreaterEqual(len(FakeCuOpt.gets), 3)
        self.assertTrue(FakeCuOpt.gets[0].startswith("/cuopt/solution/req-1"))
        # The second stop comes from the plan already in hand — no second solve.
        second = dispatcher.next_stop(self.world, drone)
        self.assertEqual(len(FakeCuOpt.posts), 1)
        self.assertNotEqual(second["name"], first["name"])
        self.assertEqual(dispatcher.stats["solved"], 1)
        self.assertEqual(dispatcher.stats["served"], 2)
        self.assertEqual(dispatcher.stats["fell_back"], 0)

    def test_a_solution_keyed_by_vehicle_index_is_read_too(self):
        FakeCuOpt.keys = "index"
        dispatcher = self._dispatcher()
        area = dispatcher.next_stop(self.world, self.world.vehicles["drone-02"])
        self.assertIsNotNone(area)
        self.assertEqual(dispatcher.stats["fell_back"], 0)

    def test_the_plan_covers_the_whole_fleet_so_nobody_asks_twice(self):
        dispatcher = self._dispatcher()
        for vehicle in self.world.vehicles.values():
            area = dispatcher.next_stop(self.world, vehicle)
            vehicle.job_label = area["name"]
        self.assertEqual(len(FakeCuOpt.posts), 1, "plans the whole fleet in one go")
        labels = [vehicle.job_label for vehicle in self.world.vehicles.values()]
        self.assertEqual(len(set(labels)), len(labels),
                         "never sends two aircraft to one landing site")


class CuOptFallsBackTest(unittest.TestCase):
    """However cuOpt goes wrong, the rule picks that stop and the world keeps running."""

    def setUp(self):
        FakeCuOpt.posts, FakeCuOpt.gets = [], []
        FakeCuOpt.keys, FakeCuOpt.post_status = "ids", 200
        FakeCuOpt.polls_pending, FakeCuOpt.mangle, FakeCuOpt.trickle_s = 1, None, 0.0
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCuOpt)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.world = Simulation(seed=7).worlds["guarded"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _falls_back(self, dispatcher, why: str):
        drone = self.world.vehicles["drone-01"]
        area = dispatcher.next_stop(self.world, drone)
        self.assertIsNotNone(area, why)
        self.assertIn(area["name"], {a["name"] for a in LANDING_AREAS})
        self.assertEqual(dispatcher.stats["fell_back"], 1, why)
        self.assertEqual(dispatcher.stats["solved"], 0, why)

    def test_a_server_that_is_not_there(self):
        dispatcher = CuOptDispatcher("http://127.0.0.1:9", timeout_s=1.0)
        self._falls_back(dispatcher, "unreachable server")
        self.assertIn("cuopt/request", dispatcher.stats["last_error"])

    def test_a_server_that_answers_500(self):
        FakeCuOpt.post_status = 500
        self._falls_back(CuOptDispatcher(self.url, timeout_s=1.0), "500")

    def test_a_solve_that_never_finishes_inside_the_budget(self):
        FakeCuOpt.polls_pending = 10_000
        dispatcher = CuOptDispatcher(self.url, timeout_s=0.5)
        self._falls_back(dispatcher, "not solved in time")
        self.assertIn("did not answer", dispatcher.stats["last_error"])

    def test_an_answer_trickled_a_byte_at_a_time_cannot_hold_the_world(self):
        """urllib's timeout is per read, so an answer trickled a byte at a time held on for
        several times the budget (measured: 21~30 s on a 2 s budget). The world thread waits
        only as long as the budget, then goes to the rule."""
        FakeCuOpt.trickle_s = 0.2
        dispatcher = CuOptDispatcher(self.url, timeout_s=0.5)
        started = time.monotonic()
        self._falls_back(dispatcher, "answer trickled a byte at a time")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("did not answer", dispatcher.stats["last_error"])

    def test_a_solver_status_that_is_not_success(self):
        def broken(answer):
            answer["response"]["solver_response"]["status"] = 1
            return answer

        FakeCuOpt.mangle = staticmethod(broken)
        dispatcher = CuOptDispatcher(self.url, timeout_s=2.0)
        self._falls_back(dispatcher, "solver status 1")
        self.assertIn("status", dispatcher.stats["last_error"])

    def test_a_task_we_never_sent(self):
        def broken(answer):
            routes = answer["response"]["solver_response"]["vehicle_data"]
            first = next(iter(routes.values()))
            first["task_id"][1] = "la-mars"
            return answer

        FakeCuOpt.mangle = staticmethod(broken)
        dispatcher = CuOptDispatcher(self.url, timeout_s=2.0)
        self._falls_back(dispatcher, "unknown task")
        self.assertIn("la-mars", dispatcher.stats["last_error"])

    def test_garbage_instead_of_a_solution(self):
        FakeCuOpt.mangle = staticmethod(lambda answer: {"response": {"nonsense": True}})
        self._falls_back(CuOptDispatcher(self.url, timeout_s=2.0), "the answer is not an answer")

    def test_after_a_failure_it_backs_off_instead_of_stalling_every_stop(self):
        FakeCuOpt.post_status = 500
        dispatcher = CuOptDispatcher(self.url, timeout_s=1.0, backoff_s=60.0)
        drone = self.world.vehicles["drone-01"]
        self.assertIsNotNone(dispatcher.next_stop(self.world, drone))
        posted = len(FakeCuOpt.posts)
        for _ in range(3):
            self.assertIsNotNone(dispatcher.next_stop(self.world, drone))
        self.assertEqual(len(FakeCuOpt.posts), posted, "does not ask while backed off")
        self.assertEqual(dispatcher.stats["fell_back"], 4)


class WhichDispatcherTest(unittest.TestCase):
    def test_rules_unless_a_cuopt_server_is_named(self):
        with mock.patch.dict("os.environ", {"DISPATCH": "", "CUOPT_URL": ""}, clear=False):
            self.assertIsInstance(build_dispatcher(), RuleDispatcher)
        with mock.patch.dict("os.environ", {"DISPATCH": "cuopt", "CUOPT_URL": ""}):
            self.assertIsInstance(build_dispatcher(), RuleDispatcher)
        with mock.patch.dict("os.environ", {"DISPATCH": "cuopt",
                                            "CUOPT_URL": "http://gpu.test:8000"}):
            dispatcher = build_dispatcher()
        self.assertIsInstance(dispatcher, CuOptDispatcher)
        self.assertEqual(dispatcher.url, "http://gpu.test:8000")

    def test_each_world_keeps_its_own_dispatcher(self):
        simulation = Simulation(seed=7)
        guarded = dispatcher_for(simulation.worlds["guarded"])
        direct = dispatcher_for(simulation.worlds["direct"])
        self.assertIsNot(guarded, direct)
        self.assertIs(guarded, dispatcher_for(simulation.worlds["guarded"]))

    def test_the_runtime_knows_nothing_about_dispatch(self):
        """Dispatch is the operator's job. If a runtime file knows these names, that boundary
        has fallen."""
        import pathlib
        runtime = pathlib.Path(__file__).resolve().parent.parent / "backend"
        for path in runtime.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in ("sim.dispatch", "dispatcher_for", "Dispatcher", "cuopt", "cuOpt",
                         "CUOPT"):
                self.assertNotIn(word, text, f"{path.name} knows about dispatch")


if __name__ == "__main__":
    unittest.main()
