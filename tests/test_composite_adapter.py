"""The simulator decides, PX4 mirrors.

Runs CompositeAdapter against a fake world of record and the MAVLink autopilot stub from
tests/test_mavlink_adapter.py. What is being held here: the cleared route reaches a real
autopilot as a mission, a recall reaches it in flight, a refused filing never arms it, and
nothing PX4 says or fails to say changes a verdict or holds up the world thread.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_mavlink_adapter import HAS_PYMAVLINK, AutopilotStub, next_port, wait_until

if HAS_PYMAVLINK:
    from pymavlink import mavutil

MIRROR = "drone-01"
OTHER = "drone-02"
# Threshold (s) for "the world thread did not wait on PX4". The runtime polls the world
# every 0.25 s, and waiting on PX4 would take the per-step reply timeout (1 s here). At 50 ms
# this flaked on a machine running two test runs and three stacks at once — waiting and
# merely being busy still separate cleanly at this threshold.
NEVER_WAITED_S = 0.2
LEGS = [{"lat": 40.701803, "lon": -73.970437, "alt_m": 70.0},
        {"lat": 40.710000, "lon": -73.980000, "alt_m": 70.0},
        {"lat": 40.720000, "lon": -73.990000, "alt_m": 60.0}]
EXIT = {"lat": 40.705000, "lon": -73.975000}


class FakeWorld:
    """Stands in for the simulator. Records the commands it gets and returns preset answers.
    Tests change the altitude by hand."""

    def __init__(self, refuse=()):
        self.sent: list[tuple] = []
        self.refuse = set(refuse)
        self.alt_m = {MIRROR: 0.0, OTHER: 0.0}

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        self.sent.append((asset_id, action, ledger_id))
        if action in self.refuse:
            return {"ok": False, "error": "no delivery assigned"}
        return {"ok": True, "cost_usd": 12.0, "state": "ready"}

    def telemetry(self):
        return {"tick": 7, "assets": {
            asset: {"id": asset, "lat": 40.7018, "lon": -73.9704, "alt_m": alt}
            for asset, alt in self.alt_m.items()}}


@unittest.skipUnless(HAS_PYMAVLINK, "pymavlink not installed (pip install pymavlink)")
class CompositeAdapterTest(unittest.TestCase):
    def _composite(self, world=None, wait_link=True, **stub_options):
        from backend.adapters.composite import (
            AutopilotJournal,
            AutopilotMirror,
            CompositeAdapter,
        )
        from backend.adapters.mavlink_fleet import MavlinkFleetAdapter

        port = next_port()
        stub = AutopilotStub(port, **stub_options).start()
        self.addCleanup(stub.stop)
        autopilot = MavlinkFleetAdapter({MIRROR: f"udpin:127.0.0.1:{port}"},
                                        ack_timeout_s=1.0, link_timeout_s=1.0)
        self.addCleanup(autopilot.close)
        journal_path = Path(tempfile.mkdtemp()) / "autopilot.jsonl"
        world = world if world is not None else FakeWorld()
        adapter = CompositeAdapter(
            world, AutopilotMirror(MIRROR, autopilot, AutopilotJournal(journal_path)))
        if wait_link:
            self.assertTrue(wait_until(lambda: autopilot.link_up(MIRROR), 8),
                            "no heartbeat from the stub")
        return adapter, world, stub, journal_path

    def _lift_off(self, adapter, world, alt_m=5.0):
        world.alt_m[MIRROR] = alt_m
        adapter.telemetry()

    @staticmethod
    def _records(journal_path) -> list[dict]:
        if not journal_path.exists():
            return []
        return [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]

    def test_the_world_answers_and_the_autopilot_is_written_beside_it(self):
        adapter, world, _, _ = self._composite()
        started = time.monotonic()
        result = adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        spent = time.monotonic() - started
        self.assertTrue(result["ok"])            # the runtime gets the simulator's answer
        self.assertEqual(result["cost_usd"], 12.0)
        self.assertEqual(result["autopilot"]["result"], "queued")
        self.assertIsNone(result["autopilot"]["ok"])
        self.assertLess(spent, NEVER_WAITED_S, "the world thread waited on PX4")
        self.assertEqual(world.sent, [(MIRROR, "fly_route", "l_route")])

    def test_the_cleared_route_becomes_the_same_mission(self):
        adapter, _, stub, _ = self._composite()
        adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self.assertTrue(wait_until(lambda: stub.missions, 8), "no mission was uploaded")
        items = stub.missions[-1]
        self.assertEqual([item.command for item in items],
                         [mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                          mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                          mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                          mavutil.mavlink.MAV_CMD_NAV_LAND])
        for item, leg in zip(items, LEGS, strict=False):
            self.assertEqual(item.frame, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT)
            self.assertEqual((item.x, item.y), (round(leg["lat"] * 1e7), round(leg["lon"] * 1e7)))
            self.assertAlmostEqual(item.z, leg["alt_m"], places=3)

    def test_the_autopilot_arms_only_when_the_world_of_record_lifts_off(self):
        adapter, world, stub, journal = self._composite()
        adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self.assertTrue(wait_until(lambda: stub.missions, 8))
        time.sleep(0.4)
        self.assertEqual(stub.received, [], "armed with the map's aircraft still on the ground")
        self.assertEqual(adapter.autopilots()[MIRROR]["pending_start"], "l_route")

        self._lift_off(adapter, world)
        self.assertTrue(wait_until(
            lambda: mavutil.mavlink.MAV_CMD_MISSION_START in stub.received, 8))
        self.assertEqual(stub.received, [mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                                         mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                                         mavutil.mavlink.MAV_CMD_MISSION_START])
        self.assertEqual((int(stub.commands[0].param2), int(stub.commands[0].param3)), (4, 4))
        results = [record["result"] for record in self._records(journal)]
        self.assertEqual(results, ["uploaded", "started"])

    def test_a_refiling_in_the_air_starts_at_once(self):
        adapter, world, stub, _ = self._composite(airborne=True)
        self._lift_off(adapter, world, alt_m=60.0)
        adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self.assertTrue(wait_until(
            lambda: mavutil.mavlink.MAV_CMD_MISSION_START in stub.received, 8))
        self.assertEqual(stub.missions[-1][0].command, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT)
        # An airborne aircraft is not armed again.
        self.assertNotIn(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, stub.received)

    def test_a_recall_in_flight_replaces_the_mission_with_the_way_out(self):
        adapter, world, stub, _ = self._composite(airborne=True)
        self._lift_off(adapter, world, alt_m=60.0)
        adapter.execute(MIRROR, "divert_ground", {"exit": EXIT, "volume": "nofly-t"}, "l_recall")
        self.assertTrue(wait_until(lambda: stub.missions, 8), "no recall mission was uploaded")
        items = stub.missions[-1]
        self.assertEqual([item.command for item in items],
                         [mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, mavutil.mavlink.MAV_CMD_NAV_LAND])
        self.assertEqual(items[0].x, round(EXIT["lat"] * 1e7))
        # The mission shows on the screen once the mirror has finished the recall.
        self.assertTrue(wait_until(lambda: adapter.autopilots()[MIRROR]["mission"], 8))
        mission = adapter.autopilots()[MIRROR]["mission"]
        self.assertEqual(mission["ledger_id"], "l_recall")
        self.assertEqual([item["command"] for item in mission["items"]], ["waypoint", "land"])

    def test_a_recall_before_lift_off_means_the_autopilot_never_arms(self):
        adapter, world, stub, _ = self._composite()
        adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self.assertTrue(wait_until(lambda: stub.missions, 8))
        adapter.execute(MIRROR, "divert_ground", {"hold": "weather"}, "l_recall")
        self.assertTrue(wait_until(lambda: stub.cleared == 1, 8), "the mission was not cleared")
        self._lift_off(adapter, world)
        time.sleep(0.5)
        self.assertEqual(stub.received, [], "armed on a recalled route")
        view = adapter.autopilots()[MIRROR]
        self.assertIsNone(view["pending_start"])
        self.assertIsNone(view["mission"])

    def test_what_the_world_refuses_never_reaches_the_autopilot(self):
        adapter, _, stub, journal = self._composite(world=FakeWorld(refuse={"fly_route"}))
        result = adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self.assertFalse(result["ok"])
        self.assertEqual(result["autopilot"]["result"], "not_sent")
        time.sleep(0.4)
        self.assertEqual(stub.traffic, [])
        self.assertEqual([record["result"] for record in self._records(journal)], ["not_sent"])

    def test_an_autopilot_refusal_never_flips_the_verdict(self):
        adapter, world, stub, journal = self._composite(
            refuse_commands=(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,))
        result = adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self.assertTrue(result["ok"],
                        "the world of record's answer stands even when the autopilot refuses")
        self._lift_off(adapter, world)
        self.assertTrue(wait_until(
            lambda: any(r["result"] == "start_refused" for r in self._records(journal)), 8))
        refused = [r for r in self._records(journal) if r["result"] == "start_refused"][0]
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["ledger_id"], "l_route")
        self.assertEqual(adapter.autopilots()[MIRROR]["commands"][0]["result"], "start_refused")

    def test_a_silent_autopilot_never_holds_up_the_world_thread(self):
        adapter, world, stub, journal = self._composite(silent=True)
        worst = 0.0
        for index in range(6):
            started = time.monotonic()
            adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, f"l_route_{index}")
            adapter.execute(MIRROR, "divert_ground", {}, f"l_recall_{index}")
            adapter.telemetry()
            worst = max(worst, time.monotonic() - started)
        self.assertLess(worst, NEVER_WAITED_S, f"the world thread stalled for {worst:.3f}s")
        self.assertTrue(wait_until(
            lambda: any(r["result"] == "upload_failed" for r in self._records(journal)), 15))
        self.assertEqual(stub.received, [])

    def test_a_missing_autopilot_is_recorded_and_nothing_else_changes(self):
        # A port nobody listens on: the wiring when there is no PX4 container.
        from backend.adapters.composite import (
            AutopilotJournal,
            AutopilotMirror,
            CompositeAdapter,
        )
        from backend.adapters.mavlink_fleet import MavlinkFleetAdapter

        autopilot = MavlinkFleetAdapter({MIRROR: f"udpin:127.0.0.1:{next_port()}"},
                                        ack_timeout_s=1.0, link_timeout_s=1.0)
        self.addCleanup(autopilot.close)
        journal_path = Path(tempfile.mkdtemp()) / "autopilot.jsonl"
        world = FakeWorld()
        adapter = CompositeAdapter(
            world, AutopilotMirror(MIRROR, autopilot, AutopilotJournal(journal_path)))
        result = adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self.assertTrue(result["ok"])
        self.assertTrue(wait_until(
            lambda: any(r["result"] == "link_down" for r in self._records(journal_path)), 8))
        self.assertEqual(adapter.autopilots()[MIRROR]["link"], "waiting")

    def test_the_other_aircraft_fly_in_the_simulator_only(self):
        adapter, world, stub, _ = self._composite()
        result = adapter.execute(OTHER, "fly_route", {"legs": LEGS}, "l_other")
        self.assertNotIn("autopilot", result)
        time.sleep(0.3)
        self.assertEqual(stub.traffic, [])
        self.assertEqual(world.sent, [(OTHER, "fly_route", "l_other")])

    def test_loading_and_ground_work_send_the_autopilot_nothing(self):
        adapter, _, stub, _ = self._composite()
        for action in ("depart", "decline_job", "fast_charge"):
            outcome = adapter.execute(MIRROR, action, {}, f"l_{action}")["autopilot"]
            self.assertEqual(outcome["result"], "no_command", action)
            self.assertTrue(outcome["ok"])
        time.sleep(0.3)
        self.assertEqual(stub.traffic, [])

    def test_the_state_field_shows_the_autopilot_without_deciding_anything(self):
        adapter, world, stub, _ = self._composite()
        adapter.execute(MIRROR, "fly_route", {"legs": LEGS}, "l_route")
        self._lift_off(adapter, world)
        self.assertTrue(wait_until(
            lambda: mavutil.mavlink.MAV_CMD_MISSION_START in stub.received, 8))
        self.assertTrue(wait_until(
            lambda: adapter.autopilots()[MIRROR]["mode"] == "AUTO.MISSION", 8))
        view = adapter.autopilots()[MIRROR]
        self.assertEqual(view["role"], "mirror")
        self.assertEqual(view["link"], "up")
        self.assertLess(view["last_heartbeat_s"], 3.0)
        self.assertTrue(view["armed"])
        self.assertAlmostEqual(view["lat"], 47.397971, places=5)
        self.assertEqual(view["mission"]["ledger_id"], "l_route")
        self.assertTrue(view["mission"]["started"])
        self.assertEqual(len(view["mission"]["items"]), 4)
        self.assertEqual(view["commands"][0]["result"], "started")
        # The telemetry used for judgement is still the world of record's.
        self.assertEqual(adapter.telemetry()["assets"][MIRROR]["lat"], 40.7018)


@unittest.skipUnless(HAS_PYMAVLINK, "pymavlink not installed (pip install pymavlink)")
class RuntimeWithAMirrorTest(unittest.TestCase):
    """When the runtime refuses, nothing goes to the actuator. The mirror is covered too."""

    def _runtime(self, adapter):
        from backend.runtime.tower import Runtime

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
        runtime.adapter = adapter
        runtime.committer.adapter = adapter
        return runtime

    def test_a_refused_filing_never_reaches_the_autopilot(self):
        from backend.adapters.composite import AutopilotMirror, CompositeAdapter
        from backend.adapters.mavlink_fleet import MavlinkFleetAdapter
        from shared.models import Proposal, Verdict

        port = next_port()
        stub = AutopilotStub(port).start()
        self.addCleanup(stub.stop)
        autopilot = MavlinkFleetAdapter({MIRROR: f"udpin:127.0.0.1:{port}"},
                                        ack_timeout_s=1.0, link_timeout_s=1.0)
        self.addCleanup(autopilot.close)
        world = FakeWorld()
        adapter = CompositeAdapter(world, AutopilotMirror(MIRROR, autopilot))
        runtime = self._runtime(adapter)
        self.assertTrue(wait_until(lambda: autopilot.link_up(MIRROR), 8))

        # A route below ground. The form check catches it — a refusal before judgement.
        underground = [{**leg, "alt_m": -1.0} for leg in LEGS]
        refused = runtime.file(Proposal(
            asset_id=MIRROR, action="fly_route", cost_usd=40.0, blast_radius="cargo",
            rationale="test", params={"legs": underground}).to_dict())
        self.assertIs(refused.verdict, Verdict.DENIED)
        time.sleep(0.4)
        self.assertEqual(world.sent, [])
        self.assertEqual(stub.traffic, [], "a refused filing reached the autopilot")

        # The same route filed within the rules executes, and only then is the mission uploaded.
        allowed = runtime.file(Proposal(
            asset_id=MIRROR, action="fly_route", cost_usd=40.0, blast_radius="cargo",
            rationale="test", params={"legs": LEGS}).to_dict())
        self.assertIsNot(allowed.verdict, Verdict.DENIED, allowed.reason)
        self.assertTrue(wait_until(lambda: stub.missions, 8))
        self.assertEqual(runtime.snapshot()["autopilots"][MIRROR]["role"], "mirror")

    def test_a_runtime_without_a_mirror_reports_an_empty_table(self):
        class LocalAdapter:
            def execute(self, asset_id, action, params, ledger_id, blast="none",
                        approved_by=None):
                return {"ok": True}

            def telemetry(self):
                return {}

        runtime = self._runtime(LocalAdapter())
        self.assertEqual(runtime.snapshot()["autopilots"], {})


if __name__ == "__main__":
    unittest.main()
