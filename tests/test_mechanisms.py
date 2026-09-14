"""Each mechanism on its own, without depending on a 420-tick scenario going a certain way.

A full run is good for the structural guarantees. It is a bad place to assert that a
particular event happened, because the moment the scenario shifts the test starts failing
for reasons that have nothing to do with the code being wrong.
"""

import tempfile
import unittest

from backend.runtime.authority import AuthorityCheck
from backend.runtime.policy import PolicyBook
from backend.runtime.tower import Runtime
from shared.config import Authority, Policy
from shared.models import Proposal, Verdict
from sim.world import RECALL, RECALL_TICK, Simulation


def proposal(**kwargs) -> Proposal:
    base = dict(asset_id="drone-01", action="fast_charge", cost_usd=60.0,
                blast_radius="none", rationale="battery 12%")
    return Proposal(**{**base, **kwargs})


class RecallTest(unittest.TestCase):
    """Block from the moment the notice arrives, or wait until each aircraft checks?"""

    def setUp(self):
        self.policies = PolicyBook()
        self.check = AuthorityCheck(Authority(320, 720), self.policies)
        self.recall = Policy(RECALL["id"], RECALL["reason"],
                             forbid_action=RECALL["forbid_action"],
                             applies_to=RECALL["applies_to"])
        self.asset = {"model": "dv-x500"}

    def test_before_the_recall_it_clears(self):
        self.assertIs(self.check.evaluate(proposal(), self.asset, 0).verdict, Verdict.AUTO)

    def test_the_tick_the_recall_lands_it_stops(self):
        self.policies.add(self.recall)
        banned = proposal(action=RECALL["forbid_action"])
        decision = self.check.evaluate(banned, self.asset, 0)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, RECALL["id"])
        self.assertEqual(decision.forbids, RECALL["forbid_action"])

    def test_an_agent_polling_on_its_own_has_a_window(self):
        """Without an enforcement point there is a gap between notice and compliance. That
        window is the size of the risk."""
        # The sim decides when the notice goes out. Hard-code a number here and, once the
        # scenario is tuned, the test quietly ends up looking at a different moment.
        published_at, poll_every = RECALL_TICK, 25
        simulation = Simulation()
        world = simulation.worlds["direct"]
        for _ in range(published_at + 5):
            simulation.step()
        # The notice is out and the aircraft has not polled again yet
        self.assertTrue(any(b["id"] == RECALL["id"] for b in simulation.bulletins()))
        vehicle = world.vehicles["drone-01"]
        vehicle.state = "cruising"
        result = world.act("drone-01", RECALL["forbid_action"], {}, None, "schedule", None,
                           simulation.tick_count)
        self.assertGreater(world.score.post_recall_violations, 0,
                           "with no enforcement point the banned action goes out in this window")
        del result, poll_every


class PassengerGateTest(unittest.TestCase):
    def setUp(self):
        self.check = AuthorityCheck(
            Authority(320, 720, human_required_blast=["passenger", "public"],
                      human_required_actions=["disengage_autonomy"]),
            PolicyBook(),
        )

    def test_a_free_action_still_needs_a_human_when_passengers_are_aboard(self):
        decision = self.check.evaluate(
            proposal(action="disengage_autonomy", cost_usd=0.0, blast_radius="passenger"),
            {}, 0,
        )
        self.assertIs(decision.verdict, Verdict.HUMAN)

    def test_the_actuator_records_it_as_unapproved_when_nobody_looked(self):
        world = Simulation().worlds["direct"]
        world.act("drone-01", "disengage_autonomy", {}, None, "passenger", None, 1)
        self.assertEqual(world.score.unapproved_passenger_actions, 1)

    def test_and_not_when_someone_did(self):
        world = Simulation().worlds["guarded"]
        world.act("drone-01", "disengage_autonomy", {}, "l_1", "passenger", "controller", 1)
        self.assertEqual(world.score.unapproved_passenger_actions, 0)
        self.assertEqual(world.score.human_approvals, 1)


class RevocationTest(unittest.TestCase):
    """Refusing is not the same as undoing what has already happened."""

    def test_a_ban_takes_back_a_resource_already_held(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

        class LocalAdapter:
            def __init__(self):
                self.sent = []

            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                self.sent.append((asset_id, action))
                return {"ok": True}

            def telemetry(self):
                return {}

        adapter = LocalAdapter()
        runtime.adapter = adapter
        runtime.committer.adapter = adapter
        runtime.locks.acquire("pad:P1", "drone-01", "p_1")

        decision = runtime.revoke_under(
            Policy("nofly-x", "medevac", forbid_resource="pad:P1")
        )
        self.assertIsNotNone(decision)
        self.assertIn(("drone-01", "divert_ground"), adapter.sent)
        self.assertIsNone(runtime.locks.holder("pad:P1"))
        self.assertEqual(decision.policy_hit, "nofly-x")

    def test_a_closing_zone_pulls_a_crossing_flight_back_through_a_json_adapter(self):
        """The recall command goes through the HTTP adapter. It must carry the id, not the
        ledger entry object.

        Tests with only a local adapter missed this; in the real stack this one line killed the
        background thread and the runtime kept publishing stale positions.
        """
        import json

        from shared.geo import Volume, box

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

        class JsonAdapter:
            def __init__(self):
                self.sent = []

            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                # Just as FleetSimAdapter does — anything that can't become JSON blows up here.
                json.dumps({"asset": asset_id, "action": action, "params": params,
                            "ledger_id": ledger_id, "blast": blast, "approved_by": approved_by})
                self.sent.append((asset_id, action, ledger_id))
                return {"ok": True}

            def telemetry(self):
                return {}

        adapter = JsonAdapter()
        runtime.adapter = adapter
        runtime.committer.adapter = adapter
        runtime.telemetry = {"drone-01": {"lat": 40.7200, "lon": -73.9850, "alt_m": 55.0,
                                          "route": [{"lat": 40.7300, "lon": -73.9850,
                                                     "alt_m": 55.0}]}}
        zone = Volume("nofly-t", "test zone", box(40.7230, -73.9900, 40.7260, -73.9800))
        pulled = runtime.recall_flights(zone)
        self.assertEqual(len(pulled), 1)
        self.assertEqual(adapter.sent[0][:2], ("drone-01", "divert_ground"))
        self.assertIsInstance(adapter.sent[0][2], str)
        self.assertEqual(pulled[0].ledger_id, adapter.sent[0][2])

    def test_it_does_nothing_when_nobody_holds_it(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
        self.assertIsNone(
            runtime.revoke_under(Policy("nofly-y", "x", forbid_resource="pad:P9"))
        )


if __name__ == "__main__":
    unittest.main()


class PadContentionTest(unittest.TestCase):
    """A landing pad takes one aircraft at a time. What enforces that?

    The direct wiring has nothing to enforce it. If two aircraft pick the same pad at the
    same moment, both go — neither can see the other's intent to reserve. Rather than run
    both worlds for a long time and wait for that moment to come by chance, this test makes
    it directly.
    """

    def test_without_a_lock_table_two_vehicles_take_the_same_pad(self):
        from sim.world import Simulation

        world = Simulation(lock_actuator=False).worlds["direct"]
        for asset in ("drone-01", "drone-02"):
            vehicle = world.vehicles[asset]
            vehicle.battery = 40.0
            result = world.act(asset, "reserve_pad", {"pad": "pad:launch"}, None, "schedule",
                               None, 1)
            self.assertTrue(result["ok"], "the actuator takes anyone's command")
            vehicle.state = "landed"
            vehicle.assigned_pad = "pad:launch"

        world._detect_pad_conflicts(2)
        self.assertGreater(world.score.pad_conflicts, 0)

    def test_a_lock_table_hands_the_pad_to_one_of_them(self):
        from backend.runtime.locks import LockTable

        locks = LockTable(["pad:launch", "pad:launch"])
        self.assertTrue(locks.acquire("pad:launch", "drone-01", "p1"))
        self.assertFalse(locks.acquire("pad:launch", "drone-02", "p2"))
        self.assertEqual(locks.holder("pad:launch").asset_id, "drone-01")


class RoundResetTest(unittest.TestCase):
    """When the round changes, per-round state has to start over too.

    With the screen open the simulator starts the next round by itself. If the runtime's
    budget were still accumulated then, from the second round on the limit would be full and
    nothing would be approved. Only the guarded side would stop while the direct side kept
    flying, and the demo would say the exact opposite.
    """

    def _runtime(self):
        import tempfile

        from backend.runtime.tower import Runtime

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            return Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

    def test_a_new_round_hands_the_budget_back(self):
        from shared.models import Proposal

        runtime = self._runtime()
        runtime._follow_round(0)
        # The demo config has no limit (null = money is not judged). This test covers the round
        # reset when there is one.
        runtime.authority.authority.fleet_usd = 100.0
        limit = runtime.authority.authority.fleet_usd
        while runtime.authority.fleet_spend < limit:
            runtime.authority.record_spend(
                Proposal(asset_id="drone-01", action="charge", cost_usd=22.0,
                         blast_radius="none", rationale="")
            )
        self.assertGreaterEqual(runtime.authority.fleet_spend, limit)

        runtime._follow_round(1)
        self.assertEqual(runtime.authority.fleet_spend, 0.0)
        self.assertEqual(runtime.authority.asset_spend("drone-01"), 0.0)

    def test_a_new_round_lets_go_of_pads_and_stale_bans(self):
        from shared.config import Policy

        runtime = self._runtime()
        runtime.telemetry = {"drone-01": {}}
        runtime._follow_round(0)
        runtime.locks.acquire("pad:launch", "drone-01", "p_1")
        runtime.policies.add(Policy("nofly-x", "notice from the last round",
                                    forbid_resource="pad:launch"))

        runtime._follow_round(1)
        self.assertIsNone(runtime.locks.holder("pad:launch"),
                          "a reservation left from the last round blocks everyone after the reset")
        self.assertEqual(runtime.policies.all(), [])

    def test_the_same_round_is_not_a_reset(self):
        from shared.models import Proposal

        runtime = self._runtime()
        runtime._follow_round(3)
        runtime.authority.record_spend(
            Proposal(asset_id="drone-01", action="charge", cost_usd=22.0,
                     blast_radius="none", rationale="")
        )
        runtime._follow_round(3)
        self.assertEqual(runtime.authority.fleet_spend, 22.0)


class CountingAdapter:
    """Counts executed routes. The runtime's guarantee is measured by what reaches here."""

    def __init__(self):
        self.routes = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        if params.get("legs"):
            self.routes.append((asset_id, action, params["legs"]))
        return {"ok": True}

    def telemetry(self):
        return {}


def _runtime_with(volumes):
    from shared.geo import Volume

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
    adapter = CountingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    for raw in volumes:
        runtime.airspace.add(raw if isinstance(raw, Volume) else Volume.from_dict(raw))
    return runtime, adapter


class RouteFormTest(unittest.TestCase):
    """The form check before judgement. Not every finite number is a route.

    A negative altitude slipped 'under' every zone, so a route through the middle of a
    building passed; a 1e300 coordinate walked 1e600 cells of the index grid and judgement
    never finished. The shipped drafter catches both in its own checks, but the runtime's
    guarantee has to hold without a drafter too.
    """

    @classmethod
    def setUpClass(cls):
        from sim.world import seat_of, to_latlon

        cls.volumes = Simulation(seed=7).worlds["guarded"].snapshot(0, volumes=True)["volumes"]
        cls.start = tuple(round(v, 6) for v in to_latlon(*seat_of(0)))
        # The building with a roof over 100m nearest the start. The route goes through its middle.
        tall = [v for v in cls.volumes if v["id"].startswith("bldg-")
                and (v.get("ceiling_m") or 0) >= 100]
        nearest = min(tall, key=lambda v: (v["polygon"][0][0] - cls.start[0]) ** 2
                      + (v["polygon"][0][1] - cls.start[1]) ** 2)
        cls.centre = (sum(p[0] for p in nearest["polygon"]) / len(nearest["polygon"]),
                      sum(p[1] for p in nearest["polygon"]) / len(nearest["polygon"]))

    def setUp(self):
        self.runtime, self.adapter = _runtime_with(self.volumes)

    def through_building(self, alt_m):
        return [{"lat": self.start[0], "lon": self.start[1], "alt_m": alt_m},
                {"lat": self.centre[0], "lon": self.centre[1], "alt_m": alt_m},
                {"lat": self.start[0] + 0.001, "lon": self.start[1], "alt_m": alt_m}]

    def file_route(self, legs):
        return self.runtime.file(proposal(action="fly_route", cost_usd=40.0, blast_radius="cargo",
                                          params={"legs": legs}).to_dict())

    def test_a_route_below_ground_through_a_building_is_refused(self):
        for alt_m in (-1.0, -0.001, -1e9):
            with self.subTest(alt_m=alt_m):
                decision = self.file_route(self.through_building(alt_m))
                self.assertIs(decision.verdict, Verdict.DENIED)
                self.assertEqual(decision.code, "airspace")
                self.assertIn("not in valid form", decision.reason)
        self.assertEqual(self.adapter.routes, [])
        # The same path filed at 60m is stopped by judgement, not the form — the building
        # really is there.
        decision = self.file_route(self.through_building(60.0))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertNotIn("not in valid form", decision.reason)

    def test_the_judge_itself_treats_below_ground_as_ground(self):
        """Even when the form check is skipped (another entry point), judgement puts -1m
        inside the building."""
        from shared.geo import first_breach

        found = first_breach(self.runtime.airspace, self.through_building(-1.0))
        self.assertIsNotNone(found)
        self.assertTrue(found[1].id.startswith("bldg-"))

    def test_coordinates_off_the_planet_are_refused_at_once(self):
        import time

        legs = [{"lat": 1e300, "lon": 1e300, "alt_m": 60},
                {"lat": -1e300, "lon": 1e300, "alt_m": 60}]
        started = time.monotonic()
        decision = self.file_route(legs)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertIn("not in valid form", decision.reason)

    def test_a_leg_longer_than_the_runtime_maximum_is_refused(self):
        from backend.runtime.form import MAX_LEG_M

        legs = [{"lat": 40.70, "lon": -73.97, "alt_m": 60},
                {"lat": 40.70 + (MAX_LEG_M + 1000) / 110_570.0, "lon": -73.97, "alt_m": 60}]
        decision = self.file_route(legs)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertIn("is too long", decision.reason)

    def test_the_index_refuses_to_walk_a_planet_sized_leg(self):
        """The last line of defence. Fed straight to the judge with no form check, it refuses
        instead of looping."""
        from shared.geo import Airspace, Volume, box, first_breach

        airspace = Airspace([Volume("nofly", "x", box(40.72, -73.99, 40.73, -73.98))])
        with self.assertRaises(ValueError):
            first_breach(airspace, [{"lat": -90, "lon": -180, "alt_m": 60},
                                    {"lat": 90, "lon": 180, "alt_m": 60}])


class RejudgeBeforeCommitTest(unittest.TestCase):
    """Judgement runs again right before execution, not just once at intake.

    If a zone closes while a filing waits for human approval or stands in a resource queue,
    that route was judged against the old airspace. Without a second look at execution time,
    an approval or an assignment sends the aircraft into the closed zone.
    """

    def setUp(self):
        from sim.world import ZONE

        self.zone = {**ZONE, "published_tick": 560, "until_tick": 900}
        # Airspace with nothing but the zone. A path through its middle passes until it closes.
        self.runtime, self.adapter = _runtime_with([])
        centre = (sum(p[0] for p in ZONE["polygon"]) / 4, sum(p[1] for p in ZONE["polygon"]) / 4)
        self.legs = [{"lat": 40.7100, "lon": -73.9855, "alt_m": 60},
                     {"lat": centre[0], "lon": centre[1], "alt_m": 60},
                     {"lat": 40.7350, "lon": -73.9855, "alt_m": 60}]

    def test_a_human_approval_does_not_revive_a_route_the_zone_has_since_closed(self):
        filed = self.runtime.file(proposal(action="fly_route", cost_usd=40.0,
                                           blast_radius="passenger",
                                           params={"legs": self.legs}).to_dict())
        self.assertIs(filed.verdict, Verdict.HUMAN)
        self.runtime.absorb([self.zone])
        decision = self.runtime.approve(filed.proposal_id, "controller", allow=True)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.code, "airspace")
        self.assertEqual(decision.forbids, self.zone["id"])
        self.assertFalse(decision.committed)
        self.assertEqual(self.adapter.routes, [])

    def test_the_arbiter_does_not_hand_a_pad_to_a_route_the_zone_has_since_closed(self):
        filed = self.runtime.file(proposal(action="reserve_pad", cost_usd=18.0,
                                           blast_radius="schedule", resource="pad:launch",
                                           params={"legs": self.legs}).to_dict())
        self.assertIs(filed.verdict, Verdict.QUEUED)
        self.runtime.absorb([self.zone])
        self.runtime._settle_contended()
        self.assertIs(filed.verdict, Verdict.DENIED)
        self.assertEqual(filed.code, "airspace")
        self.assertEqual(self.adapter.routes, [])
        self.assertIsNone(self.runtime.locks.holder("pad:launch"))

    def test_without_a_zone_change_the_same_paths_still_commit(self):
        filed = self.runtime.file(proposal(action="fly_route", cost_usd=40.0,
                                           blast_radius="passenger",
                                           params={"legs": self.legs}).to_dict())
        decision = self.runtime.approve(filed.proposal_id, "controller", allow=True)
        self.assertTrue(decision.committed)
        # The second aircraft files a path 300m to the side at the same time. The same path at
        # the same time would be a traffic refusal, not an airspace one, and test_intents
        # covers that.
        beside = [{**leg, "lon": leg["lon"] + 300 / 84_400.0} for leg in self.legs]
        queued = self.runtime.file(proposal(asset_id="drone-02", action="reserve_pad",
                                            cost_usd=18.0, blast_radius="schedule",
                                            resource="pad:launch",
                                            params={"legs": beside}).to_dict())
        self.runtime._settle_contended()
        self.assertTrue(queued.committed, queued.reason)
        self.assertEqual(len(self.adapter.routes), 2)
