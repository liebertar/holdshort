"""METAR from aviationweather.gov, and the sqlite record of what the tower took in.

METAR is an official observation fetched without a key and read by code: its numbers are folded
into the spelled-out dialect the intake grammar already reads, so it takes the same path as the
tower's own bulletin — it applies the tick it lands. When the network is not there the source is
off with one ledger line. The store keeps every item and every rule that came of one, so a
restart does not read a searched page or a typed line twice, and the report lists them.
No real network anywhere in here.
"""

import contextlib
import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from backend.runtime.tower import Runtime
from backend.store.intake_store import IntakeStore
from shared.intake import parse_weather
from shared.metar import MetarClient, MetarFailed, MetarPoller, parse_observation
from shared.notam import Clock
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
CLOCK = Clock("0900", 0.8)
GUSTY = {"icaoId": "KNYC", "obsTime": 1757500000, "wdir": 240, "wspd": 18, "wgst": 30,
         "visib": "10+", "wxString": "-RA", "rawOb": "KNYC 101951Z 24018G30KT 10SM -RA"}
CALM = {"icaoId": "KLGA", "obsTime": 1757500060, "wdir": "VRB", "wspd": 3, "visib": 10,
        "rawOb": "KLGA 101952Z VRB03KT 10SM"}


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


def make_runtime(intake_db: str | None = None) -> Runtime:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0, intake_db=intake_db)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.notice_async = False
    runtime.intake_async = False
    runtime.advisory_async = False
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.tick = 2200
    runtime.telemetry = {"drone-01": {"lat": 40.71, "lon": -73.98, "alt_m": 0.0}}
    return runtime


def codes(runtime) -> list[str]:
    return [e["decision"]["code"] for e in runtime.ledger.read_all()
            if e["outcome"] != "pending" and e["proposal"]["action"] == "intake_source"]


def waiting(runtime) -> list[str]:
    return [p["action"] for p in runtime.snapshot()["awaiting_human"]]


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeMetar(http.server.BaseHTTPRequestHandler):
    """Mimics aviationweather.gov. Class attributes set the answer and the status code."""

    body: object = []
    status = 200
    paths: list[str] = []

    def do_GET(self):
        type(self).paths.append(self.path)
        payload = json.dumps(type(self).body).encode() if type(self).status == 200 else b""
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class FakeServerCase(unittest.TestCase):
    def setUp(self):
        FakeMetar.body, FakeMetar.status, FakeMetar.paths = [GUSTY, CALM], 200, []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMetar)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/data/metar"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()


class ParseObservationTest(unittest.TestCase):
    def test_an_observation_becomes_the_spelled_out_dialect_the_grammar_reads(self):
        observation = parse_observation(GUSTY)
        self.assertEqual(observation.text(), "KNYC WIND 240 AT 18 GUST 30 KT VIS 10SM -RA")
        report = parse_weather(observation.text(), CLOCK)
        self.assertAlmostEqual(report.gust_mps, 15.4, places=1)
        self.assertAlmostEqual(report.wind_mps, 9.3, places=1)
        self.assertAlmostEqual(report.visibility_m, 16093.4, places=1)
        item = observation.item()
        self.assertEqual((item["id"], item["source"], item["kind"]),
                         ("metar-KNYC-1757500000", "metar", "weather"))

    def test_variable_wind_fractions_and_what_is_not_an_observation(self):
        self.assertEqual(parse_observation(CALM).text(), "KLGA WIND VRB AT 3 KT VIS 10SM")
        half = parse_observation({"icaoId": "KJFK", "visib": "1/2", "obsTime": 1})
        self.assertEqual(half.text(), "KJFK VIS 1/2SM")
        self.assertAlmostEqual(parse_weather(half.text(), CLOCK).visibility_m, 804.7, places=1)
        self.assertIsNone(parse_observation({"icaoId": "KNYC"}),
                          "no wind and no visibility, nothing to read")
        self.assertIsNone(parse_observation({"wspd": 10}), "no station, no observation")
        self.assertIsNone(parse_observation("KNYC 24018KT"))


class MetarClientTest(FakeServerCase):
    def test_one_request_for_every_station_and_items_back(self):
        client = MetarClient(["knyc", "KLGA"], self.url, timeout_s=2.0)
        items = client.fetch()
        self.assertEqual([i["id"] for i in items],
                         ["metar-KNYC-1757500000", "metar-KLGA-1757500060"])
        self.assertEqual(FakeMetar.paths, ["/api/data/metar?ids=KNYC%2CKLGA&format=json"])

    def test_refusals_and_broken_answers_are_failures_not_quiet_days(self):
        client = MetarClient(["KNYC"], self.url, timeout_s=2.0)
        FakeMetar.status = 500
        with self.assertRaises(MetarFailed):
            client.fetch()
        FakeMetar.status, FakeMetar.body = 200, {"error": "not a list"}
        with self.assertRaises(MetarFailed):
            client.fetch()
        unreachable = MetarClient(["KNYC"], f"http://127.0.0.1:{closed_port()}/x", timeout_s=1.0)
        with self.assertRaises(MetarFailed):
            unreachable.fetch()
        self.assertEqual((client.failures, unreachable.failures), (2, 1))
        self.assertIn("unreachable", unreachable.last_error)

    def test_the_environment_turns_it_off_or_points_it_elsewhere(self):
        with mock.patch.dict(os.environ, {"METAR": "off"}):
            self.assertIsNone(MetarClient.from_env(["KNYC"]))
        with mock.patch.dict(os.environ, {"METAR": "on", "METAR_URL": self.url}):
            self.assertIsNone(MetarClient.from_env([]), "no stations means off")
            self.assertEqual(MetarClient.from_env(["KNYC"]).base_url, self.url)


class MetarRuntimeTest(FakeServerCase):
    def _runtime_with(self, url: str) -> tuple[Runtime, MetarPoller]:
        runtime = make_runtime()
        runtime.metar = MetarClient(["KNYC", "KLGA"], url, timeout_s=1.0)
        runtime.metar_status = "starting"
        return runtime, MetarPoller(runtime.metar, 60.0, runtime.take_metar)

    def test_a_gusty_observation_holds_takeoffs_the_tick_it_lands(self):
        runtime, poller = self._runtime_with(self.url)
        poller.fetch_once()
        runtime.absorb([])
        snap = runtime.snapshot()
        hold = snap["weather"]["hold"]
        self.assertIsNotNone(hold, "an official observation applies the tick it lands, "
                                   "like the runtime's own bulletin")
        self.assertEqual((hold["id"], hold["source"]), ("metar-KNYC-1757500000", "grammar"))
        self.assertEqual([p["action"] for p in snap["awaiting_human"]], ["lift_weather_hold"],
                         "no human-check card (publish_weather), only the 'lift early?' card")
        self.assertEqual(snap["intake"]["sources"]["metar"], "on")
        self.assertEqual(snap["intake"]["metar"]["last_fetch_tick"], 2200)
        metar_items = [i for i in snap["intake"]["items"] if i["source"] == "metar"]
        self.assertEqual(len(metar_items), 2)
        self.assertTrue(all(i["trusted"] for i in metar_items))
        self.assertEqual(codes(runtime), [], "starting up already on is nothing to log")
        poller.fetch_once()
        runtime.absorb([])
        self.assertEqual(len([i for i in runtime.snapshot()["intake"]["items"]
                              if i["source"] == "metar"]), 2, "the same observation only once")

    def test_an_unreachable_network_is_off_with_one_line_and_recovery_is_one_line(self):
        runtime, poller = self._runtime_with(f"http://127.0.0.1:{closed_port()}/x")
        for _ in range(3):
            poller.fetch_once()
            runtime.tick += 1
            runtime.absorb([])
        self.assertEqual(codes(runtime), ["intake_source_failed"])
        line = [e for e in runtime.ledger.read_all()
                if e["proposal"]["action"] == "intake_source"][-1]
        self.assertEqual(line["decision"]["detail"]["source"], "metar")
        snap = runtime.snapshot()["intake"]
        self.assertEqual((snap["sources"]["metar"], snap["metar"]["last_fetch_tick"]),
                         ("off", None))
        runtime.metar.base_url = self.url
        poller.fetch_once()
        runtime.absorb([])
        poller.fetch_once()
        runtime.absorb([])
        self.assertEqual(codes(runtime), ["intake_source_failed", "intake_source_recovered"])
        self.assertEqual(runtime.snapshot()["intake"]["sources"]["metar"], "on")

    def test_a_new_round_puts_the_last_observation_back_without_waiting_for_a_fetch(self):
        runtime, poller = self._runtime_with(self.url)
        poller.fetch_once()
        runtime.absorb([])
        first = runtime.snapshot()["weather"]["hold"]["id"]
        runtime._round = 0
        runtime.tick = 3
        runtime._follow_round(1)
        runtime.absorb([])
        hold = runtime.snapshot()["weather"]["hold"]
        self.assertIsNotNone(hold, "a new round does not stop the gusts")
        self.assertEqual(hold["id"], first)
        self.assertEqual(len(FakeMetar.paths), 1, "no wait for the next cycle (up to 1 min)")
        # Network down, the last observation stays — the window or a person lifts the hold.
        runtime.metar.base_url = f"http://127.0.0.1:{closed_port()}/x"
        poller.fetch_once()
        runtime.tick = 5
        runtime._follow_round(2)
        runtime.absorb([])
        self.assertEqual(runtime.snapshot()["weather"]["hold"]["id"], first)

    def test_a_runtime_made_in_code_is_off_and_the_service_turns_it_on(self):
        runtime = make_runtime()
        self.assertIsNone(runtime.metar)
        self.assertEqual(runtime.snapshot()["intake"]["sources"]["metar"], "off")
        with mock.patch.dict(os.environ, {"METAR": "on", "METAR_STATIONS": "KJFK, KEWR"}):
            with tempfile.NamedTemporaryFile(suffix=".jsonl") as handle:
                service = Runtime(CONFIG, "http://unused", handle.name, 0.0, metar=True)
        self.assertEqual(service.metar.stations, ["KJFK", "KEWR"])
        self.assertEqual(service.snapshot()["intake"]["sources"]["metar"], "starting")


class IntakeStoreTest(unittest.TestCase):
    def test_items_and_rules_round_trip(self):
        store = IntakeStore()
        store.put_item("t-1", "tavily", "Wind advisory", 10, "https://example.test/a", None)
        store.settle_item("t-1", "weather", "grammar", "held")
        rule = store.open_rule("t-1", "weather_hold", 10, 510, applied=False)
        store.apply_rule(rule, 20)
        store.extend_rule(rule, 600)
        store.close_rule(rule, "human", 300)
        self.assertEqual(store.items(), [{"id": "t-1", "source": "tavily", "kind": "weather",
                                          "text": "Wind advisory", "fetched_tick": 10,
                                          "url": "https://example.test/a", "read_by": "grammar",
                                          "outcome": "held"}])
        self.assertEqual(store.rules(), [{"id": rule, "item_id": "t-1", "kind": "weather_hold",
                                          "from_tick": 20, "until_tick": 300, "applied": True,
                                          "lifted_by": "human"}])
        self.assertEqual(store.report(), {"items": store.items(), "rules": store.rules(),
                                          "path": ":memory:", "error": None})
        self.assertEqual(store.counts()["items"], 1)

    def test_only_searched_pages_and_typed_lines_count_as_seen_across_restarts(self):
        store = IntakeStore()
        sources = ("tavily", "manual", "sim", "metar")
        for source in sources:
            store.put_item(f"{source}-1", source, "text", 1)
        self.assertFalse(any(store.seen(f"{source}-1", source) for source in sources),
                         "received but never settled is not seen — it gets read again")
        for source in sources:
            store.settle_item(f"{source}-1", "weather", "grammar", "read")
        self.assertTrue(store.seen("tavily-1", "tavily") and store.seen("manual-1", "manual"))
        self.assertFalse(store.seen("sim-1", "sim") or store.seen("metar-1", "metar"),
                         "per-round notices and still-valid observations must be read again")
        store.put_item("sim-1", "sim", "text", 99)
        again = next(i for i in store.items() if i["id"] == "sim-1")
        self.assertEqual((again["fetched_tick"], again["read_by"], again["outcome"]),
                         (99, None, None))


    def test_a_waiting_item_reopens_once_with_its_hints_and_an_answered_one_stays_answered(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "intake.sqlite")
            store = IntakeStore(path)
            store.put_item("manual-fire", "manual", "FIRE AT 4705 CENTER BOULEVARD", 5,
                           kind="incident", hints={"address": "4705 Center Boulevard",
                                                   "radius_m": 150.0})
            store.settle_item("manual-fire", "incident", "grammar", "held")
            store.open_rule("manual-fire", "incident", 5, 600, applied=False)
            store.put_item("manual-gust", "manual", "KNYC WIND 240 AT 18 GUST 28 KT", 6)
            store.settle_item("manual-gust", "weather", "grammar", "held")
            store.decide_item("manual-gust", "refused")
            store.decide_item("manual-gust", "approved")
            store.close()

            again = IntakeStore(path)
            self.assertEqual(again.reopen_waiting(), [{
                "id": "manual-fire", "source": "manual", "kind": "incident",
                "text": "FIRE AT 4705 CENTER BOULEVARD", "address": "4705 Center Boulevard",
                "radius_m": 150.0, "reopened": True}])
            self.assertFalse(again.seen("manual-fire", "manual"),
                             "read again so the card goes back up")
            answered = next(i for i in again.items() if i["id"] == "manual-gust")
            self.assertEqual(answered["outcome"], "refused", "once answered, the answer stands")
            self.assertTrue(again.seen("manual-gust", "manual"))
            self.assertEqual(again.rules()[0]["lifted_by"], "restart")
            self.assertEqual(again.reopen_waiting(), [], "reopens only once")

    def test_a_file_from_before_the_hints_column_opens_and_keeps_working(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.sqlite")
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE items (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                           "kind TEXT, text TEXT NOT NULL, fetched_tick INTEGER, url TEXT, "
                           "read_by TEXT, outcome TEXT)")
                db.execute("INSERT INTO items VALUES "
                           "('manual-old', 'manual', NULL, 'old line', 1, NULL, 'grammar', 'held')")
                db.commit()
            store = IntakeStore(path)
            self.assertIsNone(store.open_error)
            store.put_item("manual-new", "manual", "new line", 2, hints={"radius_m": 80.0})
            self.assertEqual([i["id"] for i in store.reopen_waiting()], ["manual-old"])
            self.assertEqual([i["id"] for i in store.items()], ["manual-old", "manual-new"])
            self.assertIsNone(store.error)


class StoreRuntimeTest(unittest.TestCase):
    def test_a_waiting_card_comes_back_once_after_a_restart_and_an_answered_one_never(self):
        line = {"text": "KNYC WIND 240 AT 18 GUST 28 KT", "id": "gust"}
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "intake.sqlite")
            first = make_runtime(path)
            first.submit_intake(line)
            first.absorb([])
            self.assertEqual(waiting(first), ["publish_weather"])
            # Restart. The card died with the process and nobody answered it — so it goes back
            # up, once.
            second = make_runtime(path)
            second.absorb([])
            self.assertEqual(waiting(second), ["publish_weather"])
            second.submit_intake(line)
            second.absorb([])
            self.assertEqual(waiting(second), ["publish_weather"],
                             "the same line submitted again still makes one card")
            received = [e for e in second.ledger.read_all() if e["outcome"] != "pending"
                        and e["decision"]["code"] == "intake_received"]
            self.assertEqual([e["decision"]["detail"].get("reopened") for e in received], [True])
            # A person answered. The next restart does not raise it again or reread the same line.
            second.approve(second.snapshot()["awaiting_human"][0]["id"], "controller", allow=False)
            third = make_runtime(path)
            third.submit_intake(line)
            third.absorb([])
            self.assertEqual(waiting(third), [])
            self.assertEqual([e for e in third.ledger.read_all()
                              if e["proposal"]["action"] == "intake"], [])
            # Simulator notices are read again every round — after a restart too.
            third.absorb([{**sim_world.WEATHER, "published_tick": 2175, "until_tick": 2700}])
            self.assertIsNotNone(third.snapshot()["weather"]["hold"])

    def test_rules_record_what_held_the_fleet_and_what_ended_it(self):
        runtime = make_runtime()
        weather = {**sim_world.WEATHER, "published_tick": 2175, "until_tick": 2700}
        runtime.absorb([weather])
        rule = runtime.store.rules()[-1]
        self.assertEqual((rule["item_id"], rule["kind"], rule["applied"], rule["lifted_by"]),
                         (weather["id"], "weather_hold", True, None))
        runtime.tick = 2701
        runtime.absorb([])
        rule = runtime.store.rules()[-1]
        self.assertEqual((rule["lifted_by"], rule["until_tick"]), ("window", 2701))
        item = next(i for i in runtime.store.items() if i["id"] == weather["id"])
        self.assertEqual((item["source"], item["kind"], item["read_by"], item["outcome"]),
                         ("sim", "weather", "grammar", "read"))

    def test_a_held_report_is_a_rule_waiting_for_a_person(self):
        runtime = make_runtime()
        runtime.submit_intake({"text": "KNYC WIND 240 AT 18 GUST 28 KT", "id": "a"})
        runtime.submit_intake({"text": "KLGA WIND 200 AT 20 GUST 29 KT", "id": "b"})
        runtime.absorb([])
        rules = {r["item_id"]: r for r in runtime.store.rules()}
        self.assertEqual({k: r["applied"] for k, r in rules.items()},
                         {"manual-a": False, "manual-b": False})
        held = {p["params"]["item"]: p["id"] for p in runtime.snapshot()["awaiting_human"]}
        runtime.approve(held["manual-a"], "controller", allow=True)
        runtime.approve(held["manual-b"], "controller", allow=False)
        rules = {r["item_id"]: r for r in runtime.store.rules()}
        self.assertEqual((rules["manual-a"]["applied"], rules["manual-a"]["lifted_by"]),
                         (True, None))
        self.assertEqual((rules["manual-b"]["applied"], rules["manual-b"]["lifted_by"]),
                         (False, "refused"))
        runtime._round = 0
        runtime._follow_round(1)
        self.assertEqual(runtime.store.rules()[0]["lifted_by"], "round")

    def test_an_incident_is_a_rule_until_its_window_closes(self):
        runtime = make_runtime()
        runtime.tick = sim_world.INCIDENT_TICK
        incident = {**sim_world.INCIDENT, "published_tick": sim_world.INCIDENT_TICK,
                    "until_tick": sim_world.INCIDENT_UNTIL}
        runtime.absorb([incident])
        rule = runtime.store.rules()[-1]
        self.assertEqual((rule["kind"], rule["applied"], rule["until_tick"]),
                         ("incident", True, sim_world.INCIDENT_UNTIL))
        runtime.tick = sim_world.INCIDENT_UNTIL + 1
        runtime.absorb([incident])
        runtime.absorb([])
        self.assertEqual(runtime.store.rules()[-1]["lifted_by"], "window")

    def test_the_report_carries_the_intake_rows(self):
        runtime = make_runtime()
        runtime.absorb([{**sim_world.WEATHER, "published_tick": 2175, "until_tick": 2700}])
        built = runtime.report()
        self.assertEqual([i["id"] for i in built["intake"]["items"]], [sim_world.WEATHER["id"]])
        self.assertEqual(built["intake"]["rules"][0]["kind"], "weather_hold")
        markdown = runtime.report(fmt="md")
        self.assertIn("## Intake", markdown)
        self.assertIn(f"| {sim_world.WEATHER['id']} | sim | weather |", markdown)


if __name__ == "__main__":
    unittest.main()
