"""What makes the runtime binding rather than advisory.

Network isolation and separate images stop an agent that plays by the rules. The thing
that stops one that does not is the actuator refusing to move without an authorization
receipt. The ledger id is that receipt: the runtime writes the entry before it acts, and
the entry id travels with the command.
"""

import unittest

from sim.world import Simulation


class ReceiptTest(unittest.TestCase):
    def test_an_open_actuator_obeys_anyone(self):
        world = Simulation(lock_actuator=False).worlds["direct"]
        result = world.act("drone-01", "reserve_pad", {"pad": "pad:launch"}, None, "schedule",
                           None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.unrecorded_actions, 1)

    def test_a_locked_actuator_refuses_a_command_with_no_receipt(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("drone-01", "reserve_pad", {"pad": "pad:launch"}, None, "schedule",
                           None, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 1)
        self.assertEqual(world.vehicles["drone-01"].assigned_pad, None)

    def test_a_locked_actuator_still_obeys_the_runtime(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("drone-01", "reserve_pad", {"pad": "pad:launch"}, "l_abc123",
                           "schedule", None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 0)
        self.assertEqual(world.score.unrecorded_actions, 0)

    def test_locking_costs_nothing_when_everyone_already_goes_through(self):
        """The guarded wiring gets the same result whether the actuator is locked or not."""
        for locked in (False, True):
            world = Simulation(lock_actuator=locked).worlds["guarded"]
            result = world.act("drone-01", "reserve_pad", {"pad": "pad:launch"}, "l_1",
                               "schedule", None, 1)
            self.assertTrue(result["ok"], f"locked={locked}")


if __name__ == "__main__":
    unittest.main()


class LedgerTruthTest(unittest.TestCase):
    """A ledger that records something false is not a ledger."""

    def test_the_closing_entry_reports_what_actually_happened(self):
        import tempfile

        from backend.runtime.authority import AuthorityCheck
        from backend.runtime.commit import Committer
        from backend.runtime.locks import LockTable
        from backend.runtime.policy import PolicyBook
        from backend.store.ledger import Ledger
        from shared.config import Authority
        from shared.models import Decision, Proposal, Verdict

        simulation = Simulation()
        world = simulation.worlds["guarded"]

        class LocalAdapter:
            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                return world.act(asset_id, action, params, ledger_id, blast, approved_by, 1)

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            ledger = Ledger(handle.name)
        authority = AuthorityCheck(Authority(200, 500), PolicyBook())
        committer = Committer(LocalAdapter(), LockTable(["pad:launch"]), ledger, authority)

        proposal = Proposal(asset_id="drone-01", action="reserve_pad", cost_usd=28.0,
                            blast_radius="schedule", rationale="", resource="pad:launch",
                            params={"pad": "pad:launch"})
        committer.commit(proposal, Decision(proposal.id, Verdict.AUTO, "within limits"))

        closed = [e for e in ledger.tail(10) if e["outcome"] != "pending"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["outcome"], "done")
        self.assertTrue(closed[0]["decision"]["committed"],
                        "the outcome is done but the entry says it was not executed")
        self.assertIsNotNone(closed[0]["decision"]["ledger_id"])


class AirspaceTest(unittest.TestCase):
    """Whether the real FAA grid is loaded and used."""

    def test_real_faa_cells_are_loaded(self):
        from sim.world import AIRSPACE

        volumes = AIRSPACE.all()
        self.assertGreater(len(volumes), 40, "could not read the FAA grid")
        self.assertTrue(any(v.rule == "forbidden" for v in volumes))
        self.assertTrue(any(v.rule == "ceiling" for v in volumes))
        self.assertTrue(any(v.source.startswith("FAA") for v in volumes))
        # Airspace now holds rules and physical objects alike. Either kind must have a source.
        self.assertTrue(all(v.source for v in volumes), "a zone has no source")

    def test_a_building_is_a_volume_you_may_not_be_inside(self):
        """A building is not a new concept: forbidden from the ground to the roof, open above.

        So the judgement code does not grow by a single line. The same first_breach answers.
        """
        from shared.geo import first_breach
        from sim.world import AIRSPACE, BUILDINGS

        if not BUILDINGS:
            self.skipTest("no building data (scripts/fetch_tile_buildings.mjs)")
        tall = max((v for v in AIRSPACE.all() if v.id.startswith("bldg-")),
                   key=lambda v: v.ceiling_m)
        inside = tall.polygon[0]
        below = [{"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m - 10},
                 {"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m - 10}]
        above = [{"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m + 10},
                 {"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m + 10}]
        self.assertIsNotNone(first_breach(AIRSPACE, below),
                             "flies through a building and still passes")
        self.assertEqual(tall.rule, "forbidden")
        self.assertIsNotNone(tall.ceiling_m, "without a roof there is no flying over it either")
        del above   # above the roof the FAA ceiling applies; here we check the building only

    def test_a_sub_sample_building_crossing_is_rejected_in_both_directions(self):
        from shared.geo import Airspace, Volume, box, first_breach

        # A 1m-wide obstacle sits between the 8m samples of a 100m route. It is a building, so
        # the margin is 10m.
        obstacle = Volume("bldg-thin", "thin building", box(-.0001, .000031, .0001, .000041),
                          ceiling_m=70)
        airspace = Airspace([obstacle], default_ceiling_m=None)
        legs = [{"lat": 0, "lon": 0, "alt_m": 55},
                {"lat": 0, "lon": .0012, "alt_m": 55}]
        for path in (legs, list(reversed(legs))):
            found = first_breach(airspace, path, samples=1)
            self.assertIsNotNone(found)
            self.assertEqual(found[1].id, "bldg-thin")
            self.assertTrue(obstacle.covers(*found[3]))
        self.assertIsNone(first_breach(airspace, [{**p, "alt_m": 71} for p in legs]))
        self.assertIsNone(first_breach(airspace, [{**p, "lat": .0002} for p in legs]))

    def test_a_low_line_across_lower_manhattan_hits_a_building(self):
        """A measured leg where a drone on a cleared route grazed a building corner for 139
        ticks. 8m samples missed it."""
        from shared.geo import first_breach
        from sim.world import AIRSPACE

        legs = [{"lat": 40.7106, "lon": -73.985, "alt_m": 55},
                {"lat": 40.7106, "lon": -73.992, "alt_m": 55}]
        found = first_breach(AIRSPACE, legs)
        self.assertIsNotNone(found)
        self.assertTrue(found[1].id.startswith("bldg-"))

    def test_a_zero_foot_cell_becomes_a_ban_not_a_ceiling(self):
        """A 0ft ceiling means 'no flying without clearance', not 'fly low'."""
        from sim.world import AIRSPACE

        zeros = [v for v in AIRSPACE.all() if v.tags.get("ceiling_ft") == 0]
        self.assertTrue(zeros)
        for volume in zeros:
            self.assertEqual(volume.rule, "forbidden")
            self.assertIsNone(volume.ceiling_m)

    def test_the_runtime_refuses_a_route_through_restricted_airspace(self):
        import tempfile

        from backend.runtime.tower import Runtime
        from shared.geo import Volume
        from shared.models import Verdict
        from sim.world import Simulation as Sim

        simulation = Sim()
        snapshot = simulation.worlds["guarded"].snapshot(0, volumes=True)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
        for raw in snapshot["volumes"]:
            runtime.airspace.add(Volume.from_dict(raw))

        forbidden = next(v for v in runtime.airspace.all() if v.rule == "forbidden")
        centre = (
            sum(p[0] for p in forbidden.polygon) / len(forbidden.polygon),
            sum(p[1] for p in forbidden.polygon) / len(forbidden.polygon),
        )
        runtime.pad_coords = {"pad:X": centre}
        runtime.telemetry = {"drone-01": {"lat": centre[0] + 0.02, "lon": centre[1] + 0.02}}

        # The route the operator drew crosses a forbidden zone
        decision = runtime.file({
            "asset_id": "drone-01", "action": "reserve_pad", "resource": "pad:X",
            "params": {"pad": "pad:X", "legs": [
                {"lat": centre[0] + 0.02, "lon": centre[1] + 0.02, "alt_m": 60.0},
                {"lat": centre[0], "lon": centre[1], "alt_m": 60.0},
            ]},
            "cost_usd": 28.0, "blast_radius": "schedule", "rationale": "low battery",
        })
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "airspace")
        self.assertIn("breaks the rules", decision.reason)


class RouterAgreesWithTheJudgeTest(unittest.TestCase):
    """The planner and the runtime must reach the same judgement.

    Looking only at grid points, a diagonal step that cuts a polygon corner looks clear
    because both ends are outside. The judge looks at the segment, so it calls it a breach.
    Then every route A* hands over fails the final check, nothing gets cleared, and it all
    ends in decline_job.
    """

    def test_a_diagonal_step_that_clips_a_corner_is_not_a_free_step(self):
        from shared.geo import Airspace, Volume, first_breach
        from shared.route import Router

        airspace = Airspace()
        airspace.add(Volume(
            id="corner", name="0ft", rule="forbidden",
            # The real boundary of a KTEB 0ft cell in the FAA UASFM.
            polygon=[(40.791673474, -74.000005947), (40.800006809, -74.000005947),
                     (40.800006809, -73.991672612), (40.791673474, -73.991672612)],
        ))
        router = Router(airspace)
        # One point outside the zone to the east, one outside to the south (both farther out
        # than the 10m margin). The segment joining them crosses the south-east corner.
        outside_east = router._node(40.7934, -73.9908)
        outside_south = router._node(40.79115, -73.9926)
        self.assertFalse(router._blocked(outside_east))
        self.assertFalse(router._blocked(outside_south))
        self.assertTrue(router._crosses(outside_east, outside_south))

        legs = router._to_legs([outside_east, outside_south])
        breach = first_breach(airspace, [leg.to_dict() for leg in legs])
        self.assertIsNotNone(breach, "the judge calls it a breach; the planner must not pass it")

    def test_every_route_the_planner_hands_over_survives_the_judge(self):
        from drone.agent.planner import OperatorPlanner
        from shared.geo import first_breach
        from sim.world import Simulation as Sim

        planner = OperatorPlanner()
        planner.load(Sim().worlds["guarded"].snapshot(0, volumes=True)["volumes"])

        from sim.world import PADS, to_latlon

        bay = to_latlon(*PADS["pad:launch"])     # must be a landable spot (50m clear around)
        # Start points are designated landing sites. A spot in the middle of Midtown has no
        # open grid point within two cells, so "no route" is the right answer there, and that
        # is not what this test is about.
        starts = [(40.70335, -74.01565), (40.7308, -73.9973), (40.7359, -73.99063),
                  (40.7425, -73.9605), (40.7206, -73.952)]
        drawn = 0
        for start in starts:
            legs = planner.draw(start, bay)
            if legs is None:
                continue   # some spots have no legal route. Saying so is an answer too
            drawn += 1
            self.assertIsNone(first_breach(planner.airspace, legs),
                              f"the runtime refuses the route drawn from {start}")
        self.assertTrue(drawn, "a planner that draws nothing in the real airspace is broken")


class WeavingBetweenBuildingsTest(unittest.TestCase):
    """Once buildings come in, pathfinding becomes a different problem.

    With only the FAA grid a cell was 900m, so a 200m grid was enough. Buildings are 30~60m,
    so with 200m edges almost every edge in Manhattan grazes something, and what comes out is
    not a route but only 'no route'.
    """

    # From the pad to Washington Square (a designated landing site). The goal must be landable.
    START = (40.7019, -73.97049)
    GOAL = (40.7308, -73.9973)

    def _router(self):
        from shared.route import Router
        from sim.world import AIRSPACE, BUILDINGS

        if not BUILDINGS:
            self.skipTest("no building data (scripts/fetch_tile_buildings.mjs)")
        return Router(AIRSPACE)

    def test_a_route_through_the_city_exists_and_survives_the_judge(self):
        from shared.geo import first_breach

        router = self._router()
        route = router.plan(self.START, self.GOAL)
        self.assertIsNotNone(route, "not a single route across the city comes out")
        legs = [leg.to_dict() for leg in route.legs]
        self.assertIsNone(first_breach(router.airspace, legs),
                          "the planner produced a route that breaks its own rules")
        self.assertTrue(all(leg["alt_m"] <= router.cruise_alt_m + 0.1 for leg in legs),
                        "above cruise altitude it would never have to deal with buildings")

    def test_a_leg_flies_fifty_metres_over_a_roof_or_goes_around(self):
        """Leg altitude = tallest roof below + 50m. Above max cruise, that leg cannot be flown.

        Dodging footprints without looking at altitude makes even a 20m building a wall
        forever, and one fixed altitude hides on screen the fact that roof margins are judged.
        """
        from shared.geo import required_top_along
        from shared.route import CRUISE_ALT_M, FLOOR_ALT_M

        router = self._router()
        # Over the river: nothing there, so the floor altitude — except in a low-ceiling cell,
        # where it is ceiling - 1 (the 70 m minimum only when the ceiling allows it)
        water = ((40.7150, -73.9720), (40.7180, -73.9700))
        over_water = router.leg_altitude(*water)
        self.assertEqual(over_water, min(FLOOR_ALT_M, router._ceiling_allowance(*water)))
        # Downtown: a leg over buildings flies at roof + that building's margin (50 by default,
        # 20 for a low building in a low cell); a leg over nothing flies at the floor.
        route = router.plan(self.START, self.GOAL)
        self.assertIsNotNone(route)
        for a, b in zip(route.legs, route.legs[1:], strict=False):
            here, nxt = {"lat": a.lat, "lon": a.lon}, {"lat": b.lat, "lon": b.lon}
            top = required_top_along(router.airspace, here, nxt)
            allowed = router._ceiling_allowance((a.lat, a.lon), (b.lat, b.lon))
            floor = max(min(FLOOR_ALT_M, allowed), top if top else 0)
            self.assertGreaterEqual(b.alt_m + 0.01, floor,
                                    "a leg does not keep its margin above the roof")
            self.assertLessEqual(b.alt_m, CRUISE_ALT_M + 0.01)
        # No leg directly over a building that would need more than max cruise (roof > 70m)
        tall = max((v for v in router.airspace.all() if v.id.startswith("bldg-")),
                   key=lambda v: v.ceiling_m)
        inside = tall.polygon[0]
        self.assertIsNone(router.leg_altitude((inside[0] - 0.0005, inside[1]),
                                              (inside[0] + 0.0005, inside[1])))

    def test_the_route_starts_where_you_are_and_ends_where_you_are_going(self):
        """End on a grid point and the last 100m gets flown without anyone ever judging it."""
        router = self._router()
        start, goal = self.START, self.GOAL
        route = router.plan(start, goal)
        self.assertIsNotNone(route)
        self.assertAlmostEqual(route.legs[0].lat, start[0], places=5)
        self.assertAlmostEqual(route.legs[0].lon, start[1], places=5)
        self.assertAlmostEqual(route.legs[-1].lat, goal[0], places=5)
        self.assertAlmostEqual(route.legs[-1].lon, goal[1], places=5)

    def test_a_building_taller_than_the_cruise_altitude_is_not_a_shortcut(self):
        from shared.geo import first_breach
        from sim.world import AIRSPACE

        router = self._router()
        tall = max((v for v in AIRSPACE.all()
                    if v.id.startswith("bldg-") and v.ceiling_m > router.cruise_alt_m),
                   key=lambda v: v.ceiling_m)
        centre = (sum(p[0] for p in tall.polygon) / len(tall.polygon),
                  sum(p[1] for p in tall.polygon) / len(tall.polygon))
        through = [{"lat": centre[0] - 0.004, "lon": centre[1], "alt_m": router.cruise_alt_m},
                   {"lat": centre[0] + 0.004, "lon": centre[1], "alt_m": router.cruise_alt_m}]
        self.assertIsNotNone(first_breach(AIRSPACE, through),
                             "a straight line through the middle of a building comes out clear")
