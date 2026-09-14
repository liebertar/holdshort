"""Information intake: text comes in, code makes rules of it, a person loosens them.

The grammar reads METAR-like weather and incidents with a street address, deterministically.
A report over a limit opens a weather hold the tick it lands; the hold refuses takeoffs, leaves
airborne aircraft alone, and lifts only by expiry or by a person. An incident becomes a
keep-out circle through the notice path: corridors through it are pulled back, new filings
refused, landing areas inside unusable. What a model read is held for a person first, and
what code cannot validate is discarded. No network anywhere in here.
"""

import http.server
import json
import tempfile
import threading
import time
import unittest

from backend.intake.book import IntakeBook, WeatherHold
from backend.runtime.tower import Runtime
from shared.config import WeatherLimits
from shared.intake import (
    Gazetteer,
    normalise_address,
    parse_incident,
    parse_weather,
)
from shared.models import Proposal, Verdict
from shared.notam import Clock
from shared.tavily import IntakePoller, TavilyClient, reduce_results
from sim import world as sim_world
from tests.fixture_llm import FixtureLlm, load_fixtures

CONFIG = "configs/fleet.yaml"
CLOCK = Clock("0900", 0.8)
GANTRY = (40.74584, -73.95862)                 # Gantry Plaza landing site
HERE = (40.7100, -73.9855)


def addresses() -> list[dict]:
    with open("configs/airspace/nyc_addresses.json", encoding="utf-8") as handle:
        return json.load(handle)["addresses"]


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


def make_runtime(llm=None, **env):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.notice_async = False
    runtime.intake_async = False
    runtime.advisory_async = False
    if llm is not None:
        runtime.llm = llm
        runtime.notices.llm = llm
        runtime.intake.llm = llm
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.tick = 2200
    runtime.telemetry = {
        # One on the ground waiting for a cleared route (no route), one on the ground with a
        # clearance that has not taken off yet, and one airborne.
        "drone-01": {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0},
        "drone-02": {"lat": 40.7150, "lon": -73.9700, "alt_m": 0.0,
                     "route": [{"lat": 40.7300, "lon": -73.9700, "alt_m": 60.0}]},
        "drone-03": {"lat": 40.7100, "lon": -73.9700, "alt_m": 60.0},
    }
    return runtime, adapter


def ledger_lines(runtime):
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [entry for entry in (json.loads(line) for line in handle)
                if entry["outcome"] != "pending"]


def codes(runtime) -> list[str]:
    return [e["decision"]["code"] for e in ledger_lines(runtime)]


def weather_item(text=sim_world.WEATHER_TEXT, **extra) -> dict:
    return {**sim_world.WEATHER, "text": text, "published_tick": sim_world.WEATHER_TICK,
            "until_tick": sim_world.WEATHER_UNTIL, **extra}


def incident_item(**extra) -> dict:
    return {**sim_world.INCIDENT, "published_tick": sim_world.INCIDENT_TICK,
            "until_tick": sim_world.INCIDENT_UNTIL, **extra}


def route(asset, points, alt_m=60.0, action="fly_route"):
    return Proposal(asset_id=asset, action=action, cost_usd=12.0, blast_radius="schedule",
                    rationale="", params={"legs": [{"lat": lat, "lon": lon, "alt_m": alt_m}
                                                   for lat, lon in points]}).to_dict()


def pending(runtime, action):
    return [p for p in runtime.snapshot()["awaiting_human"] if p["action"] == action]


# ---------- Grammar ----------

class WeatherGrammarTest(unittest.TestCase):
    def test_spelled_out_metar_in_knots_and_statute_miles(self):
        report = parse_weather("KNYC 0929Z WIND 240 AT 18 GUST 28 KT VIS 2SM RA", CLOCK)
        self.assertAlmostEqual(report.wind_mps, 18 * 0.514444, places=3)
        self.assertAlmostEqual(report.gust_mps, 28 * 0.514444, places=3)
        self.assertAlmostEqual(report.visibility_m, 2 * 1609.34, places=1)
        self.assertEqual(report.precipitation, "RA")
        self.assertEqual((report.from_tick, report.until_tick), (None, None))

    def test_compact_metar_mps_and_metres_and_prose_mph(self):
        metar = parse_weather("KNYC 092951Z 24018G28KT 2SM RA BKN020", CLOCK)
        self.assertAlmostEqual(metar.gust_mps, 14.4, places=1)
        self.assertAlmostEqual(metar.visibility_m, 3218.7, places=1)
        mps = parse_weather("KJFK 1051Z 27012MPS 0800 FG", CLOCK)
        self.assertEqual((mps.wind_mps, mps.gust_mps, mps.visibility_m, mps.precipitation),
                         (12.0, None, 800.0, "FG"))
        prose = parse_weather("Winds 25 mph gusting to 45 mph this afternoon across Manhattan")
        self.assertAlmostEqual(prose.wind_mps, 11.2, places=1)
        self.assertAlmostEqual(prose.gust_mps, 20.1, places=1)
        self.assertIsNone(prose.visibility_m)
        miles = parse_weather("Visibility around 1 mile in fog, wind 5 kt")
        self.assertAlmostEqual(miles.visibility_m, 1609.34, places=1)
        self.assertEqual(miles.precipitation, "FOG")

    def test_a_window_in_ticks_or_zulu_rides_along(self):
        ticks = parse_weather("WIND 240 AT 12 KT TICK 100-200", CLOCK)
        self.assertEqual((ticks.from_tick, ticks.until_tick), (100, 200))
        zulu = parse_weather("WIND 240 AT 30 KT 0930-0940Z", CLOCK)
        self.assertEqual((zulu.from_tick, zulu.until_tick), (2250, 3000))
        self.assertIsNone(zulu.visibility_m, "the window's start time is not a visibility")

    def test_a_zulu_window_after_the_wind_group_is_not_a_metar_visibility(self):
        """Reading the 0930 in "KT 0930-0940Z" as a 930 m visibility adds a visibility breach
        that doesn't exist to a wind within limits, and takeoffs stop. A 4-digit visibility must
        stand alone."""
        for text in ("WIND 240 AT 12 KT 1200-1300Z", "WIND 240 AT 30 KT 0700-0800Z",
                     "KNYC 092951Z 24018G28KT 0935-0945Z"):
            self.assertIsNone(parse_weather(text, CLOCK).visibility_m, text)
        alone = parse_weather("KJFK 1051Z 27012MPS 0800", CLOCK)
        self.assertEqual(alone.visibility_m, 800.0)

    def test_prose_without_units_is_not_a_report(self):
        for text in ("Gusty afternoon across Manhattan, peaking near thirty",
                     "Nice day in the park", "gusts of about forty", "", "   ",
                     "The wind band plays at 8"):
            self.assertIsNone(parse_weather(text, CLOCK), text)


class IncidentGrammarTest(unittest.TestCase):
    def setUp(self):
        self.gazetteer = Gazetteer(addresses())
        self.gazetteer.add_building("bldg-t12345", [(40.760, -73.980), (40.761, -73.980),
                                                    (40.761, -73.981)])

    def test_the_simulators_sentence_resolves_to_the_address_and_radius(self):
        report = parse_incident(sim_world.INCIDENT_TEXT, self.gazetteer, CLOCK)
        self.assertEqual(report.kind, "fire")
        self.assertEqual(report.place, sim_world.INCIDENT_ADDRESS)
        self.assertEqual(report.name, "FIRE · 4705 Center Boulevard")
        self.assertEqual(report.radius_m, sim_world.INCIDENT_RADIUS_M)
        self.assertEqual(tuple(round(c, 6) for c in report.centre),
                         tuple(round(c, 6) for c in sim_world.INCIDENT_CENTRE))
        self.assertEqual(len(report.polygon()), 16)

    def test_abbreviated_addresses_normalise_to_the_gazetteer_label(self):
        self.assertEqual(normalise_address("10 W. 46th St, New York"),
                         "10 west 46th street new york")
        report = parse_incident("Structure fire at 10 W 46th St, Manhattan. Keep clear 200 m "
                                "radius TICK 3000-3600", self.gazetteer, CLOCK)
        self.assertEqual((report.place, report.radius_m, report.from_tick, report.until_tick),
                         ("10 West 46th Street", 200.0, 3000, 3600))

    def test_a_building_id_resolves_to_its_centroid(self):
        report = parse_incident("Gas leak reported at bldg-t12345 within 120 m", self.gazetteer)
        self.assertEqual((report.kind, report.building_id, report.radius_m),
                         ("gas_leak", "bldg-t12345", 120.0))
        self.assertAlmostEqual(report.centre[0], 40.76067, places=4)
        self.assertEqual(report.name, "GAS LEAK · bldg-t12345")

    def test_hints_fill_in_only_what_the_sentence_lacks(self):
        report = parse_incident("FDNY FIRE", self.gazetteer, CLOCK,
                                {"address": "1 Bowling Green", "radius_m": 120})
        self.assertEqual((report.place, report.radius_m), ("1 Bowling Green", 120.0))

    def test_no_place_no_incident_and_no_incident_word_no_incident(self):
        for text in ("Building collapse at 999 Nowhere Lane", "Fire somewhere in Brooklyn",
                     "Big sale at 10 West 46th Street", "", "Police"):
            self.assertIsNone(parse_incident(text, self.gazetteer, CLOCK), text)
        self.assertIsNone(parse_incident("Fire at bldg-t99999", self.gazetteer, CLOCK),
                          "an unknown building id is not a place")


class ThresholdTest(unittest.TestCase):
    def setUp(self):
        limits = WeatherLimits(max_wind_mps=10, max_gust_mps=12, min_visibility_m=1500,
                               hold_default_ticks=500)
        self.book = IntakeBook(CLOCK, None, limits, Gazetteer([]))

    def test_at_the_limit_is_not_a_breach_and_above_is(self):
        self.assertEqual(self.book.breaches(parse_weather("WIND 240 AT 10 M/S GUST 12 MPS")), [])
        self.assertEqual(self.book.breaches(parse_weather("VIS 1500 M WIND 240 AT 5 MPS")), [])
        self.assertEqual(self.book.breaches(parse_weather("WIND 240 AT 10 M/S GUST 13 MPS")),
                         ["gusts 13 m/s > 12"])
        self.assertEqual(self.book.breaches(parse_weather("WIND 240 AT 11 MPS")),
                         ["wind 11 m/s > 10"])
        self.assertEqual(self.book.breaches(parse_weather("VIS 1400 M WIND 240 AT 5 MPS")),
                         ["visibility 1400 m < 1500"])
        both = self.book.breaches(parse_weather("WIND 240 AT 11 MPS GUST 20 MPS VIS 800 M"))
        self.assertEqual(len(both), 3)

    def test_the_hold_length_comes_from_the_sentence_then_the_item_then_the_default(self):
        report = parse_weather("WIND 240 AT 30 KT TICK 100-200", CLOCK)
        self.assertEqual(self.book.hold_until(report, {}, 50), 200)
        plain = parse_weather("WIND 240 AT 30 KT", CLOCK)
        self.assertEqual(self.book.hold_until(plain, {"until_tick": 2700}, 2200), 2700)
        self.assertEqual(self.book.hold_until(plain, {}, 2200), 2700)

    def test_the_hold_is_three_ground_only_policies(self):
        hold = WeatherHold("wx", "WEATHER HOLD · gusts 14 m/s > 12", 2700, 2200, "grammar", {})
        policies = hold.policies()
        self.assertEqual([p.forbid_action for p in policies],
                         ["fly_route", "reserve_pad", "depart"])
        self.assertTrue(all(p.ground_only and p.active_until_tick == 2700 for p in policies))
        ground, airborne = {"alt_m": 0.0}, {"alt_m": 60.0}
        self.assertTrue(policies[0].matches("fly_route", None, ground, 2300))
        self.assertFalse(policies[0].matches("fly_route", None, airborne, 2300))
        self.assertFalse(policies[0].matches("fly_route", None, ground, 2701))


# ---------- Weather hold ----------

class WeatherHoldTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()

    def open_hold(self):
        self.runtime.absorb([weather_item()])
        return self.runtime.snapshot()

    def test_a_grammar_read_report_over_the_limit_holds_takeoffs_the_tick_it_lands(self):
        snap = self.open_hold()
        hold = snap["weather"]["hold"]
        self.assertEqual(hold["reason"], "WEATHER HOLD · gusts 14 m/s > 12")
        self.assertEqual((hold["until_tick"], hold["since_tick"], hold["source"]),
                         (sim_world.WEATHER_UNTIL, 2200, "grammar"))
        self.assertEqual(hold["breaches"], ["gusts 14 m/s > 12"])
        self.assertAlmostEqual(hold["report"]["gust_mps"], 14.4, places=1)
        self.assertEqual([p["id"] for p in snap["policies"]],
                         ["weather-hold:fly_route", "weather-hold:reserve_pad",
                          "weather-hold:depart"])
        self.assertTrue(all(p["ground_only"] for p in snap["policies"]))
        self.assertEqual(snap["weather"]["last_report"]["id"], sim_world.WEATHER["id"])
        # The aircraft cleared on the ground but not yet off is pulled back. The airborne one is
        # left alone.
        self.assertEqual([s[:2] for s in self.adapter.sent], [("drone-02", "divert_ground")])
        self.assertEqual(self.runtime.snapshot()["intake"]["items_read"], 1)
        self.assertEqual(codes(self.runtime),
                         ["intake_received", "intake_read", "weather_hold", "recalled"])
        recalled = ledger_lines(self.runtime)[-1]
        self.assertEqual((recalled["proposal"]["asset_id"], recalled["decision"]["policy_hit"]),
                         ("drone-02", "weather-hold"))
        # An item read once is neither read nor ledgered again.
        self.runtime.absorb([weather_item()])
        self.assertEqual(len(codes(self.runtime)), 4)

    def test_a_route_cleared_in_the_same_tick_the_hold_opens_is_pulled_back_too(self):
        """Telemetry is from the last poll, so the route just approved is not in it yet.

        The runtime's intent is what counts. Live, drone-02 was approved at tick 2175, when the
        hold opened, and took off 25 ticks later during the hold.
        """
        decision = self.runtime.file(route("drone-01", [HERE, (40.7200, -73.9855)]))
        self.assertEqual(decision.verdict, Verdict.AUTO, decision.reason)
        self.assertFalse(self.runtime.telemetry["drone-01"].get("route"), "telemetry is still old")
        self.open_hold()
        grounded = [sent[0] for sent in self.adapter.sent if sent[1] == "divert_ground"]
        self.assertEqual(sorted(grounded), ["drone-01", "drone-02"])
        self.assertIsNone(self.runtime.intents.get("drone-01") and
                          (self.runtime.intents.get("drone-01").live or None))

    def test_a_ground_route_and_a_departure_are_refused_with_code_policy(self):
        self.open_hold()
        decision = self.runtime.file(route("drone-01", [HERE, (40.7200, -73.9855)]))
        self.assertEqual((decision.verdict, decision.code), (Verdict.DENIED, "policy"))
        self.assertIn("WEATHER HOLD", decision.reason)
        self.assertEqual(decision.policy_hit, "weather-hold:fly_route")
        self.assertEqual(decision.detail, {"policy": "weather-hold:fly_route",
                                           "until_tick": sim_world.WEATHER_UNTIL})
        depart = self.runtime.file(Proposal(asset_id="drone-01", action="depart", cost_usd=0.0,
                                            blast_radius="none", rationale="").to_dict())
        self.assertEqual((depart.verdict, depart.code, depart.policy_hit),
                         (Verdict.DENIED, "policy", "weather-hold:depart"))
        self.assertIn("WEATHER HOLD", depart.reason)
        self.assertEqual([s[1] for s in self.adapter.sent], ["divert_ground"], "nothing executed")
        refused = [e for e in ledger_lines(self.runtime) if e["decision"]["code"] == "policy"]
        self.assertEqual(refused[0]["context"]["policies"][:3],
                         ["weather-hold:fly_route", "weather-hold:reserve_pad",
                          "weather-hold:depart"])

    def test_an_airborne_refile_is_not_refused_by_the_hold(self):
        self.open_hold()
        decision = self.runtime.file(route("drone-03", [(40.7100, -73.9700), (40.7200, -73.9700)]))
        self.assertEqual((decision.verdict, decision.code), (Verdict.AUTO, "within_limits"))
        self.assertIn(("drone-03", "fly_route"), [s[:2] for s in self.adapter.sent])

    def test_a_later_report_within_limits_does_not_lift_the_hold(self):
        self.open_hold()
        self.runtime.absorb([{"id": "wx-calm", "kind": "weather",
                              "text": "KNYC 0933Z WIND 240 AT 8 KT VIS 10SM"}])
        snap = self.runtime.snapshot()
        self.assertIsNotNone(snap["weather"]["hold"])
        self.assertEqual(snap["weather"]["hold"]["later_report"]["id"], "wx-calm")
        self.assertEqual(snap["weather"]["last_report"]["breaches"], [])
        self.assertEqual(len(snap["policies"]), 3)
        card = pending(self.runtime, "lift_weather_hold")[0]
        self.assertIn("later report within limits", card["rationale"])

    def test_the_hold_lifts_only_when_a_person_approves_the_lift_card(self):
        self.open_hold()
        cards = pending(self.runtime, "lift_weather_hold")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["asset_id"], "fleet")
        self.assertEqual(self.runtime._decisions[cards[0]["id"]].code, "human_lift")
        # Refused: the card comes down and the hold stays.
        denied = self.runtime.approve(cards[0]["id"], "controller", allow=False)
        self.assertEqual((denied.verdict, denied.code), (Verdict.DENIED, "lift_refused"))
        self.assertIsNotNone(self.runtime.snapshot()["weather"]["hold"])
        self.assertEqual(pending(self.runtime, "lift_weather_hold"), [])
        self.assertEqual(len(self.runtime.snapshot()["policies"]), 3)
        # A report within limits puts the card back up, and approving it lifts the hold on the spot.
        self.runtime.absorb([{"id": "wx-calm", "kind": "weather",
                              "text": "KNYC 0933Z WIND 240 AT 8 KT VIS 10SM"}])
        card = pending(self.runtime, "lift_weather_hold")[0]
        lifted = self.runtime.approve(card["id"], "controller", allow=True)
        self.assertEqual((lifted.verdict, lifted.code, lifted.approved_by),
                         (Verdict.AUTO, "weather_hold_lifted", "controller"))
        snap = self.runtime.snapshot()
        self.assertIsNone(snap["weather"]["hold"])
        self.assertEqual(snap["policies"], [])
        decision = self.runtime.file(route("drone-01", [HERE, (40.7200, -73.9855)]))
        self.assertEqual(decision.verdict, Verdict.AUTO)
        lines = [(e["decision"]["code"], e["outcome"]) for e in ledger_lines(self.runtime)
                 if e["proposal"]["action"] == "lift_weather_hold"]
        self.assertEqual(lines, [("lift_refused", "denied"), ("weather_hold_lifted", "done")])

    def test_the_hold_expires_with_its_window_and_takes_the_card_down(self):
        self.open_hold()
        self.runtime.tick = sim_world.WEATHER_UNTIL
        self.runtime.absorb([weather_item()])
        self.assertIsNotNone(self.runtime.snapshot()["weather"]["hold"],
                             "still in place inside the window")
        self.runtime.tick = sim_world.WEATHER_UNTIL + 1
        self.runtime.absorb([])
        snap = self.runtime.snapshot()
        self.assertIsNone(snap["weather"]["hold"])
        self.assertEqual(snap["policies"], [])
        self.assertEqual(snap["awaiting_human"], [])
        expired = [e for e in ledger_lines(self.runtime)
                   if e["decision"]["code"] == "weather_hold_expired"]
        self.assertEqual({e["proposal"]["action"] for e in expired},
                         {"weather_hold", "lift_weather_hold"})
        self.assertEqual({e["outcome"] for e in expired}, {"noted", "lapsed"})
        decision = self.runtime.file(route("drone-01", [HERE, (40.7200, -73.9855)]))
        self.assertEqual(decision.verdict, Verdict.AUTO)

    def test_an_approval_that_waited_through_the_hold_is_rejudged_against_it(self):
        """A route that sat on a person's card is judged again right before it runs — and hits
        the hold that arrived in the meantime."""
        public = Proposal(asset_id="drone-01", action="fly_route", cost_usd=12.0,
                          blast_radius="public", rationale="",
                          params={"legs": [{"lat": HERE[0], "lon": HERE[1], "alt_m": 60},
                                           {"lat": 40.7200, "lon": -73.9855, "alt_m": 60}]})
        self.assertIs(self.runtime.file(public.to_dict()).verdict, Verdict.HUMAN)
        self.open_hold()
        decision = self.runtime.approve(public.id, "controller", allow=True)
        self.assertEqual((decision.verdict, decision.code), (Verdict.DENIED, "policy"))
        self.assertIn("WEATHER HOLD", decision.reason)
        self.assertNotIn(("drone-01", "fly_route"), [s[:2] for s in self.adapter.sent])

    def test_a_round_change_clears_the_hold_and_reads_the_report_again_next_round(self):
        self.open_hold()
        self.runtime._follow_round(2)
        snap = self.runtime.snapshot()
        self.assertIsNone(snap["weather"]["hold"])
        self.assertEqual((snap["policies"], snap["awaiting_human"]), ([], []))
        self.runtime.absorb([weather_item()])
        self.assertIsNotNone(self.runtime.snapshot()["weather"]["hold"])

    def test_the_report_from_the_ledger_lists_the_hold(self):
        self.open_hold()
        self.runtime.tick = sim_world.WEATHER_UNTIL + 1
        self.runtime.absorb([])
        report = self.runtime.report()
        holds = report["fleet"]["weather_holds"]
        self.assertEqual(len(holds), 1)
        self.assertEqual((holds[0]["opened_tick"], holds[0]["until_tick"], holds[0]["source"],
                          holds[0]["expired_tick"], holds[0]["lifted"]),
                         (2200, sim_world.WEATHER_UNTIL, "grammar", sim_world.WEATHER_UNTIL + 1,
                          None))
        self.assertIn("weather hold · tick 2200", self.runtime.report(fmt="md"))


# ---------- Incidents ----------

class IncidentTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.tick = sim_world.INCIDENT_TICK
        centre = sim_world.INCIDENT_CENTRE
        # One aircraft flying a cleared route into Gantry. It is the one to recall.
        self.runtime.telemetry = {
            "drone-02": {"lat": centre[0] - 0.02, "lon": centre[1], "alt_m": 60.0,
                         "route": [{"lat": GANTRY[0], "lon": GANTRY[1], "alt_m": 60.0}]},
            "drone-01": {"lat": 40.7100, "lon": -73.9700, "alt_m": 0.0},
        }

    def test_a_grammar_read_incident_is_a_keep_out_circle_that_recalls_refuses_and_grounds(self):
        self.runtime.absorb([incident_item()])
        snap = self.runtime.snapshot()
        self.assertEqual([(i["id"], i["name"], i["kind"], i["radius_m"], i["until_tick"],
                           i["source"], i["applied"], i["held"]) for i in snap["incidents"]],
                         [(sim_world.INCIDENT["id"], "FIRE · 4705 Center Boulevard", "fire", 150.0,
                           sim_world.INCIDENT_UNTIL, "grammar", True, False)])
        self.assertEqual(snap["incidents"][0]["centre"],
                         [round(c, 6) for c in sim_world.INCIDENT_CENTRE])
        notice = next(n for n in snap["notices"] if n["id"] == sim_world.INCIDENT["id"])
        self.assertEqual((notice["kind"], notice["applied"], len(notice["polygon"])),
                         ("incident", True, 16))
        volume = self.runtime.airspace.get(sim_world.INCIDENT["id"])
        self.assertEqual((volume.floor_m, volume.ceiling_m, volume.rule),
                         (0.0, None, "forbidden"))
        self.assertIsNotNone(self.runtime.airspace.breach(*sim_world.INCIDENT_CENTRE, 120.0),
                             "blocked at every altitude")
        # The corridor in flight is recalled.
        self.assertEqual([s[:2] for s in self.adapter.sent], [("drone-02", "divert_ground")])
        # A new route is refused, and the refusal answers with the incident's name as a value.
        through = route("drone-01", [(40.7100, -73.9700), sim_world.INCIDENT_CENTRE,
                                     (40.7600, -73.9500)])
        decision = self.runtime.file(through)
        self.assertEqual((decision.verdict, decision.code, decision.forbids),
                         (Verdict.DENIED, "airspace", sim_world.INCIDENT["id"]))
        refused = ledger_lines(self.runtime)[-1]["proposal"]["params"]
        self.assertEqual(refused["blocked_name"], "FIRE · 4705 Center Boulevard")
        # A landing site inside the circle (or within 50 m of it) can't be used — Gantry Plaza is
        # 156 m from the centre, 6 m outside the edge, so the last leg hits the margin (40 m)
        # first. To see the landing check alone, come down from the north 45 m outside the edge
        # (past the margin but still within the 50 m landing ring).
        self.runtime.tick += 20
        landing = self.runtime.file(route("drone-01", [(40.7100, -73.9700), GANTRY]))
        self.assertEqual((landing.verdict, landing.code, landing.forbids),
                         (Verdict.DENIED, "airspace", sim_world.INCIDENT["id"]))
        centre = sim_world.INCIDENT_CENTRE
        edge = (centre[0] + (sim_world.INCIDENT_RADIUS_M + 45.0) / 110_570.0, centre[1])
        self.runtime.telemetry["drone-01"] = {"lat": 40.7600, "lon": centre[1], "alt_m": 0.0}
        self.runtime.tick += 20
        landing = self.runtime.file(route("drone-01", [(40.7600, centre[1]), edge]))
        self.assertEqual((landing.verdict, landing.code), (Verdict.DENIED, "airspace"))
        landed = ledger_lines(self.runtime)[-1]["proposal"]["params"]
        self.assertEqual((landed["blocked_kind"], landed["blocked_name"]),
                         ("landing", "FIRE · 4705 Center Boulevard"))
        self.assertIn("cannot touch down", landing.reason)
        keepout = [e for e in ledger_lines(self.runtime)
                   if e["decision"]["code"] == "incident_keepout"]
        self.assertEqual(len(keepout), 1)
        self.assertEqual(keepout[0]["decision"]["detail"]["name"], "FIRE · 4705 Center Boulevard")
        self.assertEqual(self.runtime.report()["fleet"]["incidents"][0]["radius_m"], 150.0)

    def test_it_expires_with_its_window_and_the_landing_area_opens_again(self):
        self.runtime.absorb([incident_item()])
        self.runtime.tick = sim_world.INCIDENT_UNTIL + 1
        self.runtime.absorb([incident_item()])
        snap = self.runtime.snapshot()
        self.assertEqual(snap["incidents"], [])
        self.assertIsNone(self.runtime.airspace.get(sim_world.INCIDENT["id"]))
        self.assertNotIn(sim_world.INCIDENT["id"],
                         self.runtime._context(None)["policies"],
                         "the policy ends with the window too")
        decision = self.runtime.file(route("drone-01", [(40.7100, -73.9700), GANTRY]))
        self.assertEqual(decision.verdict, Verdict.AUTO)

    def test_a_manual_line_with_a_building_id_becomes_a_keep_out_around_that_building(self):
        from shared.geo import Volume

        self.runtime.airspace.add(Volume.from_dict({
            "id": "bldg-t777", "name": "BUILDING 60 m", "ceiling_m": 60.0,
            "polygon": [[40.7500, -73.9900], [40.7500, -73.9890], [40.7508, -73.9890],
                        [40.7508, -73.9900]]}))
        status, body = self.runtime.submit_intake(
            {"text": "Gas leak reported at bldg-t777, keep clear 100 m radius",
             "kind": "incident"})
        self.assertEqual((status, body["queued"]), (200, True))
        self.assertTrue(body["id"].startswith("manual-"))
        self.assertEqual(self.runtime.snapshot()["incidents"], [],
                         "the world thread does the reading")
        self.runtime.absorb([])
        incident = self.runtime.snapshot()["incidents"][0]
        self.assertEqual((incident["id"], incident["name"], incident["radius_m"],
                          incident["until_tick"], incident["held"], incident["applied"]),
                         (body["id"], "GAS LEAK · bldg-t777", 100.0,
                          sim_world.INCIDENT_TICK + 500, True, False))
        self.assertAlmostEqual(incident["centre"][0], 40.7504, places=4)
        # Even read by the grammar, a manual entry is not a notice from the runtime's own feed —
        # it applies only once a person confirms.
        self.assertIsNone(self.runtime.airspace.get(body["id"]))
        card = pending(self.runtime, "publish_notice")[0]
        self.assertEqual(card["author"], "grammar")
        self.assertIn("manual", self.runtime._decisions[card["id"]].reason)
        self.runtime.approve(card["id"], "controller", allow=True)
        self.assertIsNotNone(self.runtime.airspace.get(body["id"]))
        self.assertEqual(self.runtime.snapshot()["incidents"][0]["confirmed_by"], "controller")
        self.assertEqual(self.runtime.submit_intake({"text": "   "})[0], 400)


# ---------- What a model reads ----------

def fixture_super(*labels) -> FixtureLlm:
    records = [r for r in load_fixtures() if r.get("label") in labels]
    assert records, labels
    return FixtureLlm(records, model=records[0]["model"], tiers=("super",))


GUSTY = "Gusty afternoon across Manhattan, peaking near thirty this evening"
CALM = "Calm and clear over the harbor tonight"
# The grammar reads "AT <number> <street>". This sentence's address is not in that form, so it
# falls to the model.
MIDTOWN = ("Three-alarm blaze tearing through a Midtown office tower; 10 W. 46th St. was "
           "evacuated, FDNY says")
INVENTED = "Fire crews responding to a building near the river"
YANKEES = "Yankees clinch the division with a walk-off"


class ModelPathTest(unittest.TestCase):
    def setUp(self):
        self.llm = fixture_super("intake-weather-gusty", "intake-weather-calm",
                                 "intake-incident-midtown", "intake-incident-invented",
                                 "intake-none")
        self.runtime, self.adapter = make_runtime(self.llm)

    def test_a_model_read_report_over_the_limit_is_held_then_holds_after_a_person_confirms(self):
        self.runtime.absorb([{"id": "news-1", "kind": "weather", "text": GUSTY,
                              "source": "tavily", "url": "https://example.test/wx"}])
        self.assertEqual([tier for tier, _ in self.llm.asked], ["super"])
        snap = self.runtime.snapshot()
        self.assertIsNone(snap["weather"]["hold"], "nothing takes effect until a person confirms")
        self.assertEqual(snap["policies"], [])
        self.assertEqual([(h["id"], h["breaches"], h["source"]) for h in snap["weather"]["held"]],
                         [("news-1", ["gusts 16 m/s > 12"], "model:nemotron-3-super-reference")])
        card = pending(self.runtime, "publish_weather")[0]
        self.assertEqual(card["author"], "model:nemotron-3-super-reference")
        self.assertEqual(self.runtime._decisions[card["id"]].code, "human_weather")
        self.assertIs(self.runtime.file(route("drone-01", [HERE, (40.7200, -73.9855)])).verdict,
                      Verdict.AUTO)
        decision = self.runtime.approve(card["id"], "controller", allow=True)
        self.assertEqual((decision.verdict, decision.code), (Verdict.AUTO, "weather_confirmed"))
        snap = self.runtime.snapshot()
        self.assertEqual((snap["weather"]["hold"]["source"], snap["weather"]["hold"]["reason"]),
                         ("human", "WEATHER HOLD · gusts 16 m/s > 12"))
        self.assertEqual(snap["weather"]["hold"]["until_tick"], 2200 + 500)
        self.assertEqual(snap["weather"]["held"], [])
        self.runtime.tick += 20
        refused = self.runtime.file(route("drone-01", [HERE, (40.7200, -73.9855)]))
        self.assertEqual((refused.verdict, refused.code), (Verdict.DENIED, "policy"))
        received = next(e for e in ledger_lines(self.runtime)
                        if e["decision"]["code"] == "intake_received")
        self.assertEqual((received["decision"]["detail"]["source"],
                          received["decision"]["detail"]["url"]),
                         ("tavily", "https://example.test/wx"))
        self.assertEqual(received["context"]["checks_run"], ["intake:grammar", "intake:model"])
        self.assertEqual(self.runtime.snapshot()["intake"]["items"][0]["read_by"],
                         "model:nemotron-3-super-reference")

    def test_a_person_can_refuse_it_and_it_lapses_unseen(self):
        self.runtime.absorb([{"id": "news-1", "kind": "weather", "text": GUSTY}])
        card = pending(self.runtime, "publish_weather")[0]
        decision = self.runtime.approve(card["id"], "controller", allow=False)
        self.assertEqual((decision.verdict, decision.code), (Verdict.DENIED, "weather_refused"))
        self.assertIsNone(self.runtime.snapshot()["weather"]["hold"])
        self.assertEqual(self.runtime.snapshot()["weather"]["held"], [])
        self.runtime.absorb([{"id": "news-2", "kind": "weather", "text": GUSTY}])
        self.runtime.tick = 2200 + 501
        self.runtime.absorb([])
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        lapsed = [e for e in ledger_lines(self.runtime) if e["outcome"] == "lapsed"]
        self.assertEqual([e["decision"]["code"] for e in lapsed], ["weather_lapsed"])

    def test_a_report_within_limits_is_read_and_holds_nothing(self):
        self.runtime.absorb([{"id": "news-3", "text": CALM}])
        snap = self.runtime.snapshot()
        self.assertEqual((snap["weather"]["hold"], snap["weather"]["held"],
                          snap["awaiting_human"]), (None, [], []))
        self.assertEqual(snap["weather"]["last_report"]["breaches"], [])
        self.assertEqual(codes(self.runtime), ["intake_received", "intake_read"])

    def test_a_model_read_incident_is_held_and_applies_only_after_a_person_confirms(self):
        self.runtime.tick = 3000
        self.runtime.absorb([{"id": "news-4", "text": MIDTOWN, "source": "tavily"}])
        snap = self.runtime.snapshot()
        self.assertEqual([(i["id"], i["name"], i["held"], i["applied"]) for i in snap["incidents"]],
                         [("news-4", "FIRE · 10 West 46th Street", True, False)])
        self.assertIsNone(self.runtime.airspace.get("news-4"))
        card = pending(self.runtime, "publish_notice")[0]
        self.assertEqual(card["params"]["notice"]["kind"], "incident")
        decision = self.runtime.approve(card["id"], "controller", allow=True)
        self.assertEqual((decision.verdict, decision.code), (Verdict.AUTO, "notice_published"))
        incident = self.runtime.snapshot()["incidents"][0]
        self.assertEqual((incident["applied"], incident["held"], incident["source"],
                          incident["confirmed_by"]), (True, False, "human", "controller"))
        self.assertIsNotNone(self.runtime.airspace.get("news-4"))
        self.assertEqual([e["decision"]["code"] for e in ledger_lines(self.runtime)][-2:],
                         ["notice_published", "incident_keepout"])

    def test_an_invented_address_is_discarded_not_held(self):
        self.runtime.absorb([{"id": "news-5", "text": INVENTED}])
        snap = self.runtime.snapshot()
        self.assertEqual((snap["incidents"], snap["awaiting_human"]), ([], []))
        self.assertEqual(snap["intake"]["items_unreadable"], 1)
        unread = [e for e in ledger_lines(self.runtime)
                  if e["decision"]["code"] == "intake_unreadable"]
        self.assertEqual(len(unread), 1)
        self.assertIn("999 Nowhere Lane", unread[0]["decision"]["detail"]["why"])
        self.assertEqual(self.llm.stats["super"].fallback, 1,
                         "a discarded answer counts as a rules fallback")
        self.assertIn("999 Nowhere Lane", snap["intake"]["items"][0]["why"])

    def test_kind_none_is_read_and_changes_nothing(self):
        self.runtime.absorb([{"id": "news-6", "text": YANKEES}])
        snap = self.runtime.snapshot()
        self.assertEqual((snap["weather"]["hold"], snap["incidents"], snap["awaiting_human"],
                          snap["policies"]), (None, [], [], []))
        self.assertEqual(snap["intake"]["items"][0]["kind"], "none")
        line = ledger_lines(self.runtime)[-1]
        self.assertEqual((line["decision"]["code"], line["decision"]["detail"]["kind"]),
                         ("intake_read", "none"))

    def test_without_a_super_model_prose_is_unreadable_and_asked_once(self):
        runtime, _ = make_runtime()
        runtime.absorb([{"id": "news-7", "text": GUSTY}])
        runtime.absorb([{"id": "news-7", "text": GUSTY}])
        self.assertEqual(codes(runtime), ["intake_received", "intake_unreadable"])
        self.assertIn("no model to structure it", ledger_lines(runtime)[-1]["decision"]["reason"])
        self.assertEqual(runtime.snapshot()["intake"]["sources"],
                         {"tavily": "off", "sim": True, "metar": "off"})

    def test_the_model_reads_off_the_world_thread_and_the_next_poll_collects_it(self):
        self.runtime.intake_async = True
        self.runtime.absorb([{"id": "news-8", "kind": "weather", "text": GUSTY}])
        self.assertIn("news-8", self.runtime._reading_intake)
        for thread in threading.enumerate():
            if thread.name.startswith("intake-"):
                self.assertTrue(thread.daemon)
                thread.join(5.0)
        self.assertEqual(self.runtime.snapshot()["weather"]["held"], [], "not recorded yet")
        self.runtime.absorb([])
        self.assertEqual(self.runtime._reading_intake, set())
        self.assertEqual([h["id"] for h in self.runtime.snapshot()["weather"]["held"]], ["news-8"])
        self.assertEqual(len(self.llm.asked), 1)

    def test_an_answer_from_an_older_round_is_dropped(self):
        self.runtime.intake_async = True
        self.runtime._round = 1
        self.runtime.absorb([{"id": "news-9", "kind": "weather", "text": GUSTY}])
        for thread in threading.enumerate():
            if thread.name.startswith("intake-"):
                thread.join(5.0)
        self.runtime._follow_round(2)
        self.runtime.absorb([])
        self.assertEqual(self.runtime.snapshot()["weather"]["held"], [],
                         "an answer from the last round is dropped")
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])


# ---------- Tavily ----------

class FakeTavily(http.server.BaseHTTPRequestHandler):
    """Mimics the search server. Class attributes set the answer and the delay."""

    results: list[dict] = []
    delay_s = 0.0
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append({**body, "auth": self.headers.get("Authorization")})
        time.sleep(type(self).delay_s)
        payload = json.dumps({"query": body.get("query"), "results": type(self).results}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def fake_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTavily)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/search"


class TavilyTest(unittest.TestCase):
    def setUp(self):
        FakeTavily.results = [
            {"title": "Wind advisory for New York City", "url": "https://example.test/a",
             "content": "KNYC 0929Z WIND 240 AT 18 GUST 28 KT VIS 2SM RA", "score": 0.9},
            {"title": "Yankees clinch the division", "url": "https://example.test/b",
             "content": "with a walk-off"},
            {"title": "", "url": "", "content": ""},
        ]
        FakeTavily.delay_s = 0.0
        FakeTavily.requests = []
        self.server, self.url = fake_server()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_results_reduce_to_items_with_stable_ids(self):
        client = TavilyClient("tvly-test", self.url, timeout_s=2.0)
        items = client.search("New York City wind gust forecast today")
        self.assertEqual([i["source"] for i in items], ["tavily", "tavily"])
        self.assertEqual(items[0]["url"], "https://example.test/a")
        self.assertTrue(items[0]["text"].startswith("Wind advisory for New York City. KNYC"))
        self.assertEqual(items[0]["query"], "New York City wind gust forecast today")
        again = reduce_results("q", {"results": FakeTavily.results})
        self.assertEqual([i["id"] for i in again], [i["id"] for i in items])
        self.assertEqual(FakeTavily.requests[0]["auth"], "Bearer tvly-test")
        self.assertEqual(FakeTavily.requests[0]["max_results"], 5)
        self.assertEqual(reduce_results("q", {"results": "nope"}), [])

    def test_the_poller_feeds_the_runtime_which_ledgers_each_page_once(self):
        runtime, _ = make_runtime()
        client = TavilyClient("tvly-test", self.url, timeout_s=2.0)
        runtime.tavily = client
        poller = IntakePoller(client, ["q1"], period_s=60.0, deliver=runtime.take_in)
        poller.fetch_once()
        self.assertEqual(runtime.snapshot()["intake"]["last_fetch_tick"], 2200)
        runtime.absorb([])
        snap = runtime.snapshot()
        self.assertEqual(snap["intake"]["items_read"], 1)
        self.assertEqual(snap["intake"]["items_unreadable"], 1,
                         "the baseball story can't be read without a model")
        self.assertEqual(snap["intake"]["sources"]["tavily"], "enabled")
        self.assertEqual(snap["intake"]["fetch"]["ok"], True)
        # A search result is not a notice from the runtime's own feed even if the grammar reads
        # it — nothing takes effect before a person confirms.
        self.assertIsNone(snap["weather"]["hold"])
        self.assertEqual(snap["policies"], [])
        self.assertEqual([(h["breaches"], h["source"]) for h in snap["weather"]["held"]],
                         [(["gusts 14 m/s > 12"], "grammar")])
        card = pending(runtime, "publish_weather")[0]
        self.assertEqual((card["author"], card["params"]["origin"]), ("grammar", "tavily"))
        self.assertIn("until tick 2700", card["rationale"])
        first = codes(runtime)
        self.assertEqual(first.count("intake_received"), 2)
        self.assertEqual(first.count("intake_read"), 1)
        poller.fetch_once()
        runtime.absorb([])
        self.assertEqual(codes(runtime), first, "the same page is not ledgered again")
        self.assertEqual(poller.fetches, 2)
        runtime.approve(card["id"], "controller", allow=True)
        hold = runtime.snapshot()["weather"]["hold"]
        self.assertEqual((hold["source"], hold["until_tick"]), ("human", 2200 + 500))

    def test_a_slow_server_never_stalls_the_world_thread(self):
        FakeTavily.delay_s = 1.5
        runtime, _ = make_runtime()
        client = TavilyClient("tvly-test", self.url, timeout_s=0.4)
        runtime.tavily = client
        poller = IntakePoller(client, ["q1"], period_s=60.0, deliver=runtime.take_in)
        thread = poller.start()
        # While the search thread waits, the world thread (absorb) keeps advancing ticks.
        slowest = 0.0
        for _ in range(20):
            runtime.tick += 1
            started = time.monotonic()
            runtime.absorb([])
            slowest = max(slowest, time.monotonic() - started)
            time.sleep(0.02)
        self.assertLess(slowest, 0.2, f"absorb waited on the network ({slowest:.2f}s)")
        self.assertEqual(runtime.tick, 2220)
        poller.stop()
        thread.join(3.0)
        self.assertEqual(client.failures, 1,
                         "a timeout counts as a failure and the next period asks again")
        runtime.absorb([])
        self.assertEqual(codes(runtime), ["intake_source_failed"],
                         "a late answer ledgers no items — only one line that the source is late")
        self.assertEqual(runtime.snapshot()["intake"]["sources"]["tavily"], "failed")

    def test_without_a_key_the_source_is_off_and_nothing_is_ledgered(self):
        runtime, _ = make_runtime()
        self.assertIsNone(runtime.tavily)
        self.assertIsNone(runtime.intake_poller)
        for _ in range(5):
            runtime.tick += 1
            runtime.absorb([])
        self.assertEqual(runtime.snapshot()["intake"]["sources"]["tavily"], "off")
        self.assertIsNone(runtime.snapshot()["intake"]["last_fetch_tick"])
        self.assertEqual(codes(runtime), [])
        self.assertEqual(runtime.snapshot()["intake"]["items"], [])


class FailingTavily(http.server.BaseHTTPRequestHandler):
    """A server that refuses the key. Tests change only its status code."""

    status = 401
    hits = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).hits += 1
        payload = b'{"results": []}' if type(self).status == 200 else b""
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class TavilyFailureTest(unittest.TestCase):
    """A source dying quietly looks exactly like a quiet day. The ledger must tell them apart."""

    def setUp(self):
        FailingTavily.status, FailingTavily.hits = 401, 0
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FailingTavily)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/search"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_a_refused_key_is_ledgered_once_and_shows_in_state_until_it_recovers(self):
        runtime, _ = make_runtime()
        client = TavilyClient("junk-key", self.url, timeout_s=2.0)
        runtime.tavily = client
        poller = IntakePoller(client, ["q1", "q2"], period_s=60.0, deliver=runtime.take_in)
        for _ in range(3):
            poller.fetch_once()
            runtime.tick += 1
            runtime.absorb([])
        self.assertEqual(FailingTavily.hits, 6)
        self.assertEqual(codes(runtime), ["intake_source_failed"],
                         "a failure is one line, written when it changes")
        line = ledger_lines(runtime)[-1]
        self.assertEqual((line["outcome"], line["decision"]["detail"]["error"]),
                         ("failed", "HTTP 401"))
        snap = runtime.snapshot()["intake"]
        self.assertEqual(snap["sources"]["tavily"], "failed")
        self.assertIsNone(snap["last_fetch_tick"], "a failed, empty period is not 'just asked'")
        self.assertEqual((snap["fetch"]["ok"], snap["fetch"]["failures"]), (False, 6))
        # Once the key works again: one recovery line, then enabled again.
        FailingTavily.status = 200
        poller.fetch_once()
        runtime.absorb([])
        self.assertEqual(codes(runtime), ["intake_source_failed", "intake_source_recovered"])
        snap = runtime.snapshot()["intake"]
        self.assertEqual((snap["sources"]["tavily"], snap["last_fetch_tick"]),
                         ("enabled", 2203))
        poller.fetch_once()
        runtime.absorb([])
        self.assertEqual(len(codes(runtime)), 2, "recovery is one line too")

    def test_a_malformed_url_does_not_kill_the_poller_thread(self):
        runtime, _ = make_runtime()
        client = TavilyClient("junk", "junk-url", timeout_s=1.0)
        runtime.tavily = client
        poller = IntakePoller(client, ["q1"], period_s=0.05, deliver=runtime.take_in)
        thread = poller.start()
        time.sleep(0.2)
        self.assertTrue(thread.is_alive(), "a bad URL is a failure, not the thread's death")
        poller.stop()
        thread.join(2.0)
        self.assertGreaterEqual(client.failures, 2, "asks again every period")
        self.assertIn("ValueError", client.last_error)
        runtime.absorb([])
        self.assertEqual(codes(runtime), ["intake_source_failed"])


class TrustBySourceTest(unittest.TestCase):
    """The grammar reading it is not the same as the runtime's own feed saying it."""

    def setUp(self):
        self.runtime, self.adapter = make_runtime()

    def test_a_search_snippet_the_grammar_reads_is_held_for_a_person_not_applied(self):
        sandy = ("Remembering Superstorm Sandy: winds 50 mph gusting to 70 mph battered "
                 "Manhattan on Oct 29, 2012")
        self.runtime.absorb([{"id": "tavily-sandy", "source": "tavily", "text": sandy,
                              "url": "https://example.test/sandy"}])
        snap = self.runtime.snapshot()
        self.assertIsNone(snap["weather"]["hold"])
        self.assertEqual((snap["policies"], self.adapter.sent), ([], []))
        self.assertEqual(snap["intake"]["items"][0]["held"], True)
        self.assertEqual(snap["intake"]["items"][0]["trusted"], False)
        card = pending(self.runtime, "publish_weather")[0]
        self.assertEqual(self.runtime._decisions[card["id"]].reason,
                         "text from tavily is not a runtime notice — it applies only "
                         "once a human confirms it")
        self.assertEqual(codes(self.runtime), ["intake_received", "intake_read"])
        self.assertTrue(ledger_lines(self.runtime)[-1]["decision"]["detail"]["held"])

    def test_a_manual_incident_is_held_and_a_sim_bulletin_applies_at_once(self):
        self.runtime.tick = sim_world.INCIDENT_TICK
        self.runtime.absorb([{"id": "manual-ramen", "source": "manual",
                              "text": "Explosion of flavor at 130 Fulton Street: new ramen bar"}])
        held = self.runtime.snapshot()["incidents"]
        self.assertEqual([(i["held"], i["applied"], i["source"]) for i in held],
                         [(True, False, "grammar")])
        self.assertEqual(len(pending(self.runtime, "publish_notice")), 1)
        self.assertIsNone(self.runtime.airspace.get("manual-ramen"))
        self.runtime.absorb([incident_item()])
        applied = next(i for i in self.runtime.snapshot()["incidents"]
                       if i["id"] == sim_world.INCIDENT["id"])
        self.assertEqual((applied["held"], applied["applied"]), (False, True))

    def test_a_search_notice_in_faa_dialect_is_held_too(self):
        text = ("!NYC 09/001 ZNY AIRSPACE 0.5NM RADIUS OF 404400N0735900W SFC-400FT AGL "
                "TICK 2250-2600")
        self.runtime.absorb([{"id": "tavily-tfr", "source": "tavily", "text": text}])
        notice = next(n for n in self.runtime.snapshot()["notices"] if n["id"] == "tavily-tfr")
        self.assertEqual((notice["held"], notice["applied"]), (True, False))
        self.assertEqual(len(pending(self.runtime, "publish_notice")), 1)


class GrammarRangeTest(unittest.TestCase):
    """Range checks apply whoever did the reading. A 9999 m radius read by the grammar is a
    misreading, not an observation."""

    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.tick = sim_world.INCIDENT_TICK

    def unreadable_why(self):
        line = ledger_lines(self.runtime)[-1]
        self.assertEqual(line["decision"]["code"], "intake_unreadable")
        return line["decision"]["detail"]["why"]

    def test_an_absurd_radius_in_the_sentence_is_refused_not_applied(self):
        text = "FDNY FIRE AT 10 WEST 46TH STREET. KEEP CLEAR 9999 M RADIUS"
        self.runtime.absorb([{"id": "fire-9999", "kind": "incident", "text": text}])
        self.assertEqual(self.runtime.snapshot()["incidents"], [])
        self.assertEqual(self.adapter.sent, [])
        self.assertIn("radius 9999 m", self.unreadable_why())
        self.assertEqual(self.runtime.snapshot()["intake"]["items"][0]["read_by"], "grammar")

    def test_an_absurd_radius_hint_is_refused_too(self):
        self.runtime.absorb([{"id": "fire-hint", "kind": "incident", "radius_m": 50_000,
                              "text": "FDNY FIRE AT 10 WEST 46TH STREET"}])
        self.assertEqual(self.runtime.snapshot()["incidents"], [])
        self.assertIn("radius 50000 m", self.unreadable_why())

    def test_an_impossible_wind_is_refused_not_a_hold(self):
        self.runtime.absorb([{"id": "wx-900", "kind": "weather",
                              "text": "KNYC 0929Z WIND 240 AT 900 KT"}])
        self.assertIsNone(self.runtime.snapshot()["weather"]["hold"])
        self.assertIn("wind", self.unreadable_why())

    def test_the_simulators_two_sentences_still_pass_the_checks(self):
        self.runtime.tick = 2200
        self.runtime.absorb([weather_item()])
        self.assertIsNotNone(self.runtime.snapshot()["weather"]["hold"])
        self.runtime.tick = sim_world.INCIDENT_TICK
        self.runtime.absorb([incident_item()])
        self.assertEqual(len(self.runtime.snapshot()["incidents"]), 1)


class WindowAndHintTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()

    def test_a_breaching_report_whose_window_already_closed_grounds_nothing(self):
        self.runtime.absorb([{"id": "stale", "kind": "weather",
                              "text": "KNYC WIND 240 AT 30 KT TICK 100-200"}])
        self.assertEqual(self.adapter.sent, [],
                         "a cleared route not yet flown is not pulled back needlessly")
        snap = self.runtime.snapshot()
        self.assertEqual((snap["weather"]["hold"], snap["policies"]), (None, []))
        self.assertEqual(codes(self.runtime), ["intake_received", "intake_read"])
        line = ledger_lines(self.runtime)[-1]
        self.assertTrue(line["decision"]["detail"]["window_closed"])
        self.assertIn("window already closed", line["decision"]["reason"])

    def test_a_stale_incident_is_read_and_closes_nothing(self):
        self.runtime.tick = sim_world.INCIDENT_UNTIL + 50
        self.runtime.absorb([incident_item()])
        self.assertEqual(self.runtime.snapshot()["incidents"], [])
        self.assertIsNone(self.runtime.airspace.get(sim_world.INCIDENT["id"]))
        self.assertEqual(codes(self.runtime), ["intake_received", "intake_read"])

    def test_non_numeric_hints_are_refused_at_the_door(self):
        for body in ({"text": "KNYC 0929Z WIND 240 AT 18 GUST 28 KT", "until_tick": "abc"},
                     {"text": "FDNY FIRE AT 10 WEST 46TH STREET", "radius_m": "big"}):
            status, reply = self.runtime.submit_intake(body)
            self.assertEqual(status, 400, body)
            self.assertIn("must be numbers", reply["error"])
        self.runtime.absorb([])
        self.assertEqual(codes(self.runtime), [])
        status, reply = self.runtime.submit_intake(
            {"text": "KNYC 0929Z WIND 240 AT 18 GUST 28 KT", "until_tick": "2900"})
        self.assertEqual(status, 200)
        self.runtime.absorb([])
        self.assertEqual(pending(self.runtime, "publish_weather")[0]["params"]["until_tick"],
                         2900)

    def test_a_broken_hint_that_slips_past_the_door_does_not_stop_the_poll(self):
        self.runtime.absorb([{"id": "odd", "kind": "weather", "until_tick": "abc",
                              "text": "KNYC 0929Z WIND 240 AT 18 GUST 28 KT"},
                             weather_item()])
        self.assertIsNotNone(self.runtime.snapshot()["weather"]["hold"], "the next item is read")
        self.assertEqual(self.runtime.snapshot()["weather"]["hold"]["id"], "odd",
                         "a non-numeric hint is no hint — the hold gets the default length")

    def test_a_manual_id_cannot_shadow_a_simulator_bulletin(self):
        self.runtime.submit_intake({"id": sim_world.WEATHER["id"], "kind": "weather",
                                    "text": "KNYC 0920Z WIND 240 AT 5 KT VIS 10SM"})
        self.runtime.absorb([])
        self.runtime.absorb([weather_item()])
        snap = self.runtime.snapshot()
        self.assertEqual(snap["weather"]["hold"]["id"], sim_world.WEATHER["id"])
        self.assertEqual(sorted(i["id"] for i in snap["intake"]["items"]),
                         sorted([sim_world.WEATHER["id"], f"manual-{sim_world.WEATHER['id']}"]))

    def test_a_far_window_is_clamped_to_the_horizon(self):
        book = self.runtime.intake
        self.assertEqual(book.window_until(None, 99_999_999, {}, 2200), 2200 + 4 * 500)
        self.assertEqual(book.window_until(None, None, {"until_tick": 99_999}, 2200), 4200)
        self.assertEqual(book.window_until(None, 2900, {}, 2200), 2900)


class HoldBookkeepingTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.absorb([weather_item()])

    def test_a_round_change_closes_an_open_hold_in_the_ledger(self):
        self.runtime.tick = 2400
        self.runtime._follow_round(2)
        closed = [e for e in ledger_lines(self.runtime)
                  if e["decision"]["code"] == "weather_hold_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["context"]["tick"], 2400)
        hold = self.runtime.report()["fleet"]["weather_holds"][0]
        self.assertEqual((hold["expired_tick"], hold["closed_by"], hold["lifted"]),
                         (2400, "round", None))
        self.assertIn("closed tick 2400 (round changed)", self.runtime.report(fmt="md"))

    def test_a_later_bulletin_that_extends_the_hold_refreshes_the_lift_card(self):
        self.runtime.absorb([{"id": "wx-later", "kind": "weather",
                              "text": "KNYC 0933Z WIND 240 AT 20 GUST 30 KT TICK 2200-2900"}])
        hold = self.runtime.snapshot()["weather"]["hold"]
        self.assertEqual(hold["until_tick"], 2900)
        card = pending(self.runtime, "lift_weather_hold")[0]
        self.assertEqual(card["params"]["until_tick"], 2900)
        self.assertIn("until tick 2900", card["rationale"])
        self.assertTrue(all(p["active_until_tick"] == 2900
                            for p in self.runtime.snapshot()["policies"]))

    def test_a_search_snippet_never_extends_a_hold(self):
        self.runtime.absorb([{"id": "tavily-later", "source": "tavily", "kind": "weather",
                              "text": "KNYC 0933Z WIND 240 AT 20 GUST 30 KT TICK 2200-4000"}])
        self.assertEqual(self.runtime.snapshot()["weather"]["hold"]["until_tick"],
                         sim_world.WEATHER_UNTIL)
        self.assertEqual(pending(self.runtime, "publish_weather"), [],
                         "with a hold already in place, no second card goes up")


class ModelWindowTest(unittest.TestCase):
    def test_a_model_window_past_the_horizon_is_clamped_before_the_card(self):
        llm = fixture_super("intake-weather-forever")
        runtime, _ = make_runtime(llm)
        runtime.absorb([{"id": "news-far", "kind": "weather",
                         "text": "Gale warning in effect until further notice"}])
        card = pending(runtime, "publish_weather")[0]
        self.assertEqual(card["params"]["until_tick"], 2200 + 4 * 500)
        self.assertIn("until tick 4200", card["rationale"])
        runtime.approve(card["id"], "controller", allow=True)
        self.assertEqual(runtime.snapshot()["weather"]["hold"]["until_tick"], 4200)


# ---------- Simulator scene ----------

class SimSceneTest(unittest.TestCase):
    def test_the_weather_and_incident_bulletins_appear_in_their_windows_as_text(self):
        simulation = sim_world.Simulation()
        kinds = {}
        for tick in (sim_world.WEATHER_TICK - 1, sim_world.WEATHER_TICK, sim_world.WEATHER_UNTIL,
                     sim_world.WEATHER_UNTIL + 1, sim_world.INCIDENT_TICK - 1,
                     sim_world.INCIDENT_TICK, sim_world.INCIDENT_UNTIL,
                     sim_world.INCIDENT_UNTIL + 1):
            simulation.tick_count = tick
            kinds[tick] = [b["kind"] for b in simulation.bulletins()
                           if b["kind"] in ("weather", "incident")]
        self.assertEqual(kinds, {2174: [], 2175: ["weather"], 2700: ["weather"], 2701: [],
                                 2999: [], 3000: ["incident"], 3600: ["incident"], 3601: []})
        self.assertEqual((sim_world.WEATHER_TICK, sim_world.WEATHER_UNTIL), (2175, 2700))
        self.assertEqual((sim_world.INCIDENT_TICK, sim_world.INCIDENT_UNTIL), (3000, 3600))
        simulation.tick_count = sim_world.WEATHER_TICK
        weather = next(b for b in simulation.bulletins() if b["kind"] == "weather")
        self.assertEqual(weather["text"], sim_world.WEATHER_TEXT)
        self.assertNotIn("polygon", weather)
        self.assertEqual((weather["published_tick"], weather["until_tick"]), (2175, 2700))
        simulation.tick_count = sim_world.INCIDENT_TICK
        incident = next(b for b in simulation.bulletins() if b["kind"] == "incident")
        self.assertEqual((incident["text"], incident["address"], incident["radius_m"]),
                         (sim_world.INCIDENT_TEXT, sim_world.INCIDENT_ADDRESS, 150.0))

    def test_both_sentences_are_grammar_readable_and_the_weather_breaches_the_gust_limit(self):
        report = parse_weather(sim_world.WEATHER_TEXT, sim_world.CLOCK)
        book = IntakeBook(sim_world.CLOCK, None, WeatherLimits(), Gazetteer(addresses()))
        self.assertEqual(book.breaches(report), ["gusts 14 m/s > 12"])
        incident = parse_incident(sim_world.INCIDENT_TEXT, book.gazetteer, sim_world.CLOCK)
        self.assertEqual(incident.name, "FIRE · 4705 Center Boulevard")
        # The scene only works with the landing site inside the circle's landing ring.
        from shared.intake import distance_m
        self.assertLess(distance_m(incident.centre, GANTRY), incident.radius_m + 50.0)

    def test_the_scoreboard_counts_a_takeoff_during_the_hold_and_an_entry_into_the_circle(self):
        world = sim_world.Simulation(seed=7).worlds["direct"]
        vehicle = world.vehicles["drone-01"]
        vehicle.alt = 0.0
        world._detect_weather_takeoffs(sim_world.WEATHER_TICK)
        vehicle.alt = 5.0
        world._detect_weather_takeoffs(sim_world.WEATHER_TICK)
        self.assertEqual(world.score.weather_hold_takeoffs, 0,
                         "the window's first tick is not counted")
        vehicle.alt = 0.0
        world._detect_weather_takeoffs(sim_world.WEATHER_TICK + 1)
        vehicle.alt = 5.0
        world._detect_weather_takeoffs(sim_world.WEATHER_TICK + 2)
        world._detect_weather_takeoffs(sim_world.WEATHER_TICK + 3)
        self.assertEqual(world.score.weather_hold_takeoffs, 1, "one takeoff counts once")
        vehicle.alt = 0.0
        world._detect_weather_takeoffs(sim_world.WEATHER_UNTIL + 1)
        vehicle.alt = 5.0
        world._detect_weather_takeoffs(sim_world.WEATHER_UNTIL + 2)
        self.assertEqual(world.score.weather_hold_takeoffs, 1,
                         "takeoffs outside the window don't count")
        vehicle.x, vehicle.y = sim_world.to_grid(*sim_world.INCIDENT_CENTRE)
        world._detect_incident_incursions(sim_world.INCIDENT_TICK - 1)
        world._detect_incident_incursions(sim_world.INCIDENT_TICK)
        self.assertEqual(world.score.incident_incursions, 0,
                         "already inside when the incident arrives: not counted")
        vehicle.x, vehicle.y = sim_world.to_grid(sim_world.INCIDENT_CENTRE[0] + 0.01,
                                                 sim_world.INCIDENT_CENTRE[1])
        world._detect_incident_incursions(sim_world.INCIDENT_TICK + 1)
        vehicle.x, vehicle.y = sim_world.to_grid(*sim_world.INCIDENT_CENTRE)
        world._detect_incident_incursions(sim_world.INCIDENT_TICK + 2)
        world._detect_incident_incursions(sim_world.INCIDENT_TICK + 3)
        self.assertEqual(world.score.incident_incursions, 1)
        self.assertIn("weather_hold_takeoffs", world.score.public())
        self.assertIn("incident_incursions", world.score.public())


if __name__ == "__main__":
    unittest.main()
