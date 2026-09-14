"""A notice arrives as a sentence. What the runtime does with it depends on who read it.

The grammar reads the FAA dialect and the rule applies the tick it lands. Prose the grammar
cannot read goes to a model, is validated hard, and waits for a person. Neither path lets
the simulator hand the runtime a polygon: the bulletin feed is text.
"""

import json
import tempfile
import unittest

from backend.runtime.tower import Runtime
from shared.geo import box
from shared.llm.client import LlmReply, TieredLlm
from shared.models import Proposal, Verdict
from shared.notam import (
    MAX_AREA_M2,
    Clock,
    Notice,
    area_m2,
    format_dms,
    parse_dms,
    parse_notice,
    validate,
)
from sim import world as sim_world
from tests.fixture_llm import FixtureLlm, load_fixtures

CONFIG = "configs/fleet.yaml"


class GrammarTest(unittest.TestCase):
    def test_dms_round_trips_to_the_second(self):
        for lat, lon in ((40.71950, -73.98900), (40.72550, -73.98200), (-33.8688, 151.2093)):
            token = format_dms(lat, lon)
            back = parse_dms(token)
            self.assertAlmostEqual(back[0], lat, delta=0.5 / 3600)
            self.assertAlmostEqual(back[1], lon, delta=0.5 / 3600)
        self.assertEqual(format_dms(40.71950, -73.98900), "404310N0735920W")
        self.assertEqual(parse_dms("404310N0735920W"), (40 + 43 / 60 + 10 / 3600,
                                                        -(73 + 59 / 60 + 20 / 3600)))
        with self.assertRaises(ValueError):
            parse_dms("404370N0735920W")     # no such thing as 70 seconds

    def test_the_simulators_notice_parses_to_its_own_polygon_and_window(self):
        notice = parse_notice(sim_world.ZONE_TEXT, sim_world.CLOCK)
        self.assertIsNotNone(notice)
        self.assertEqual([[lat, lon] for lat, lon in notice.polygon], sim_world.ZONE["polygon"])
        self.assertEqual((notice.from_tick, notice.until_tick),
                         (sim_world.ZONE_TICK, sim_world.ZONE_UNTIL))
        self.assertEqual((notice.from_tick, notice.until_tick), (525, 900))
        self.assertAlmostEqual(notice.ceiling_m, 121.92)
        self.assertEqual(notice.floor_m, 0.0)
        self.assertEqual(notice.reference, "AGL")
        self.assertTrue(sim_world.ZONE_VOLUME.covers(40.7225, -73.9855))

    def test_radius_tick_window_and_a_name(self):
        notice = parse_notice("HOSPITAL PAD: 0.5NM RADIUS OF 404310N0735920W SFC-UNL TICK 10-20")
        self.assertEqual(notice.name, "HOSPITAL PAD")
        self.assertEqual(len(notice.polygon), 16)
        self.assertIsNone(notice.ceiling_m)
        self.assertEqual((notice.from_tick, notice.until_tick), (10, 20))
        self.assertAlmostEqual(area_m2(notice.polygon) / 1e6, 3.14 * (0.5 * 1.852) ** 2, delta=0.1)

    def test_floor_and_ceiling_in_feet(self):
        notice = parse_notice("AREA BOUNDED BY 404310N0735920W 404310N0735855W 404332N0735855W "
                              "200FT-1000FT AMSL 0930-1030Z")
        self.assertAlmostEqual(notice.floor_m, 60.96)
        self.assertAlmostEqual(notice.ceiling_m, 304.8)
        self.assertEqual(notice.reference, "AMSL")
        self.assertEqual((notice.from_tick, notice.until_tick), (2250, 6750))

    def test_prose_is_not_read(self):
        for text in ("Emergency helicopter operations over the East Village hospital until noon",
                     "AREA BOUNDED BY 404310N0735920W 404310N0735855W",   # two points make no area
                     "", "   "):
            self.assertIsNone(parse_notice(text), text)

    def test_the_clock_maps_zulu_to_ticks_both_ways(self):
        clock = Clock("0900", 0.8)
        self.assertEqual(clock.tick_of("0907"), 525)
        self.assertEqual(clock.tick_of("0912"), 900)
        self.assertEqual(clock.zulu_of(525), "0907")
        self.assertEqual(Clock("2350", 0.8).tick_of("0010"), 20 * 60 / 0.8)
        with self.assertRaises(ValueError):
            clock.tick_of("2560")


class ValidationTest(unittest.TestCase):
    """Checks on what a model made up. Failing any one means it is not even held."""

    def setUp(self):
        self.bbox = (40.68, -74.03, 40.83, -73.93)
        self.good = Notice(polygon=box(40.7195, -73.989, 40.7255, -73.982), ceiling_m=121.9)

    def test_a_reasonable_notice_passes(self):
        self.assertEqual(validate(self.good, self.bbox), [])

    def test_each_gate(self):
        outside = Notice(polygon=box(41.0, -73.0, 41.01, -72.99))
        self.assertTrue(any("outside" in p for p in validate(outside, self.bbox)))
        huge = Notice(polygon=box(40.70, -74.02, 40.75, -73.95))
        self.assertGreater(area_m2(huge.polygon), MAX_AREA_M2)
        self.assertTrue(any("km²" in p for p in validate(huge, self.bbox)))
        self.assertTrue(validate(Notice(polygon=self.good.polygon, floor_m=200.0), self.bbox))
        self.assertTrue(validate(Notice(polygon=self.good.polygon, ceiling_m=2000.0), self.bbox))
        self.assertTrue(validate(Notice(polygon=self.good.polygon, from_tick=9, until_tick=8),
                                 self.bbox))
        self.assertTrue(validate(Notice(polygon=self.good.polygon[:2]), self.bbox))
        many = Notice(polygon=[(40.72 + i * 1e-5, -73.985) for i in range(33)])
        self.assertTrue(validate(many, self.bbox))
        # With no box known (before the landing-site list) only the box check is skipped
        self.assertEqual(validate(outside, None), [])


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


def make_runtime(llm=None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    # Tests look at the state after absorb returns (the thread is tested separately below)
    runtime.notice_async = False
    if llm is not None:
        runtime.llm = llm
        runtime.notices.llm = llm
    runtime.landing_areas = sim_world.LANDING_AREAS
    return runtime, adapter


def ledger_lines(runtime):
    """Closed entries only. The ledger writes a line on open and one on close, so the same id
    appears twice."""
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [entry for entry in (json.loads(line) for line in handle)
                if entry["outcome"] != "pending"]


class GrammarNoticeEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.item = {"id": "nofly-t", "kind": "notam", "name": "test corridor",
                     "text": sim_world.ZONE_TEXT, "published_tick": 525, "until_tick": 900}

    def test_it_applies_the_tick_it_lands_and_pulls_a_crossing_flight(self):
        inside = (40.7225, -73.9855)
        self.runtime.telemetry = {"drone-01": {"lat": 40.7150, "lon": -73.9855, "alt_m": 60.0,
                                               "route": [{"lat": 40.7300, "lon": -73.9855,
                                                          "alt_m": 60.0}]}}
        self.runtime.tick = 524
        self.runtime.absorb([self.item])
        self.assertEqual(self.runtime.snapshot()["notices"][0]["applied"], False,
                         "before the window opens it is only scheduled")
        self.assertIsNone(self.runtime.airspace.breach(*inside, 60.0))
        self.runtime.tick = 525
        self.runtime.absorb([self.item])
        volume = self.runtime.airspace.breach(*inside, 60.0)
        self.assertIsNotNone(volume)
        self.assertEqual(volume.id, "nofly-t")
        self.assertEqual((volume.from_tick, volume.until_tick), (525, 900))
        self.assertEqual(volume.source, "grammar")
        self.assertEqual([s[:2] for s in self.adapter.sent], [("drone-01", "divert_ground")])
        notices = self.runtime.snapshot()["notices"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(set(notices[0]) >= {"id", "name", "kind", "from_tick", "until_tick",
                                             "source", "polygon"}, True)
        self.assertEqual(notices[0]["source"], "grammar")
        self.assertEqual(notices[0]["polygon"], sim_world.ZONE["polygon"])
        self.assertIn("nofly-t", [p["id"] for p in self.runtime.snapshot()["policies"]])

    def test_it_lapses_when_the_window_closes_or_the_feed_drops_it(self):
        self.runtime.tick = 600
        self.runtime.absorb([self.item])
        self.assertIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.runtime.tick = 901
        self.runtime.absorb([self.item])
        self.assertNotIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.runtime.tick = 700
        self.runtime.absorb([self.item])
        self.assertIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.runtime.absorb([])
        self.assertNotIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.assertEqual(self.runtime.snapshot()["notices"], [])

    def test_the_bulletin_feed_is_text_only(self):
        simulation = sim_world.Simulation()
        for _ in range(sim_world.ZONE_TICK):
            simulation.step()
        zone = next(b for b in simulation.bulletins() if b["kind"] == "notam")
        self.assertEqual(zone["text"], sim_world.ZONE_TEXT)
        self.assertNotIn("polygon", zone)
        self.assertEqual((zone["published_tick"], zone["until_tick"]), (525, 900))

    def test_a_route_through_the_notice_is_refused_while_it_holds(self):
        self.runtime.tick = 600
        self.runtime.telemetry = {"drone-01": {"lat": 40.7100, "lon": -73.9855, "alt_m": 0.0}}
        self.runtime.absorb([self.item])
        legs = [{"lat": 40.7100, "lon": -73.9855, "alt_m": 60}, {"lat": 40.7225, "lon": -73.9855,
                                                                  "alt_m": 60},
                {"lat": 40.7350, "lon": -73.9855, "alt_m": 60}]
        decision = self.runtime.file(Proposal(asset_id="drone-01", action="fly_route",
                                              cost_usd=12.0, blast_radius="schedule",
                                              rationale="", params={"legs": legs}).to_dict())
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.forbids, "nofly-t")
        self.assertEqual(ledger_lines(self.runtime)[-1]["context"]["policies"], ["nofly-t"])


class ProseNoticeTest(unittest.TestCase):
    PROSE = ("Emergency helicopter operations at the East Village hospital helipad. "
             "Uncrewed aircraft keep clear of the block bounded by E 14th, Ave A, E 10th "
             "and 1st Ave, surface to 400 ft, from 0907Z to 0912Z.")

    def item(self):
        return {"id": "nofly-prose", "kind": "notam", "name": "medevac", "text": self.PROSE,
                "published_tick": 525, "until_tick": 900}

    def test_without_a_model_it_is_recorded_as_unreadable_and_never_applies(self):
        runtime, adapter = make_runtime()
        runtime.tick = 600
        runtime.absorb([self.item()])
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        self.assertNotIn("nofly-prose", {v.id for v in runtime.airspace.all()})
        unread = [e for e in ledger_lines(runtime)
                  if e["decision"].get("code") == "notice_unreadable"]
        self.assertEqual(len(unread), 1, "ledgered once, not asked again on every poll")
        self.assertEqual(unread[0]["proposal"]["action"], "publish_notice")
        self.assertEqual(unread[0]["context"]["checks_run"], ["notice:grammar", "notice:model"])
        self.assertEqual(adapter.sent, [])

    def _model(self, polygon, **extra):
        form = {"name": "East Village helipad", "polygon": polygon, "floor_m": 0,
                "ceiling_m": 121.9, "from": "0907", "until": "0912", **extra}

        class StubSuper(TieredLlm):
            def __init__(self):
                super().__init__(base_url="http://stub", models={"super": "stub-super"},
                                 timeout_s=0.0, request_extra={}, record_dir="")
                self.asked = []

            def ask(self, tier, system, user, max_tokens=400, json_object=False,
                    timeout_s=None):
                self.asked.append((tier.value, user))
                return LlmReply(text=json.dumps(form), model="stub-super")

        return StubSuper()

    def test_a_model_compiled_notice_is_held_until_a_person_confirms_it(self):
        llm = self._model([[40.7195, -73.989], [40.7195, -73.982], [40.7255, -73.982],
                           [40.7255, -73.989]])
        runtime, adapter = make_runtime(llm)
        runtime.tick = 600
        inside = (40.7225, -73.9855)
        runtime.absorb([self.item()])
        self.assertEqual([tier for tier, _ in llm.asked], ["super"])
        # Held: not in the airspace, on the banner as 'awaiting a person' (held), and in the
        # approval list
        self.assertIsNone(runtime.airspace.breach(*inside, 60.0))
        shown = runtime.snapshot()["notices"]
        self.assertEqual([(n["id"], n["held"], n["applied"]) for n in shown],
                         [("nofly-prose", True, False)])
        self.assertEqual([n["id"] for n in runtime.notices.pending()], ["nofly-prose"])
        pending = runtime.snapshot()["awaiting_human"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "publish_notice")
        self.assertEqual(pending[0]["author"], "model:stub-super")
        self.assertEqual(pending[0]["params"]["notice"]["source"], "model:stub-super")
        self.assertEqual(pending[0]["params"]["notice"]["from_tick"], 525)
        self.assertEqual(pending[0]["cost_usd"], 0.0)
        runtime.absorb([self.item()])
        self.assertEqual(len(llm.asked), 1, "the same notice is not asked again")
        # Once a person confirms, it applies from then on — on the person's word
        decision = runtime.approve(pending[0]["id"], "controller", allow=True)
        self.assertIs(decision.verdict, Verdict.AUTO)
        self.assertEqual(decision.code, "notice_published")
        volume = runtime.airspace.breach(*inside, 60.0)
        self.assertIsNotNone(volume)
        self.assertEqual(volume.source, "human")
        notices = runtime.snapshot()["notices"]
        self.assertEqual((notices[0]["source"], notices[0]["confirmed_by"]),
                         ("human", "controller"))
        self.assertEqual((notices[0]["held"], notices[0]["applied"]), (False, True))
        self.assertEqual(runtime.notices.pending(), [])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])

    def test_a_person_can_refuse_it_and_nothing_applies(self):
        llm = self._model([[40.7195, -73.989], [40.7195, -73.982], [40.7255, -73.982]])
        runtime, _ = make_runtime(llm)
        runtime.tick = 600
        runtime.absorb([self.item()])
        pending = runtime.snapshot()["awaiting_human"][0]
        decision = runtime.approve(pending["id"], "controller", allow=False)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.code, "notice_refused")
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertNotIn("nofly-prose", {v.id for v in runtime.airspace.all()})
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [],
                         "a refused notice is not put up again")

    def test_a_model_polygon_outside_the_service_box_is_discarded_not_held(self):
        llm = self._model([[41.5, -72.0], [41.5, -71.99], [41.51, -71.99]])
        runtime, _ = make_runtime(llm)
        runtime.tick = 600
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(runtime.snapshot()["notices"], [])
        unread = [e for e in ledger_lines(runtime)
                  if e["decision"].get("code") == "notice_unreadable"]
        self.assertEqual(len(unread), 1)
        self.assertIn("outside", unread[0]["decision"]["reason"])

    def test_a_model_answer_that_is_not_the_schema_is_discarded(self):
        llm = self._model("north of the hospital")
        runtime, _ = make_runtime(llm)
        runtime.tick = 600
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(llm.stats["super"].fallback, 1)


def medevac_item() -> dict:
    """The simulator's second notice, exactly as it appears in the notice feed (text only)."""
    return {"id": sim_world.MEDEVAC["id"], "kind": "notam", "name": sim_world.MEDEVAC["name"],
            "text": sim_world.MEDEVAC_TEXT, "published_tick": sim_world.MEDEVAC_TICK,
            "until_tick": sim_world.MEDEVAC_UNTIL}


def fixture_super(label: str) -> FixtureLlm:
    """A Super tier that answers with one record from tests/fixtures/llm/notices_super.json."""
    records = [r for r in load_fixtures() if r.get("label") == label]
    assert records, label
    return FixtureLlm(records, model=records[0]["model"], tiers=("super",))


class SecondNoticeTest(unittest.TestCase):
    """The second notice is free prose outside the grammar. It comes only as text, after the
    first zone lifts."""

    def test_the_simulator_publishes_prose_the_grammar_cannot_read_once_the_first_zone_lapses(self):
        self.assertIsNone(parse_notice(sim_world.MEDEVAC_TEXT, sim_world.CLOCK))
        self.assertEqual((sim_world.MEDEVAC_TICK, sim_world.MEDEVAC_UNTIL), (1350, 2100))
        self.assertEqual(sim_world.CLOCK.zulu_of(sim_world.MEDEVAC_TICK), "0918")
        self.assertGreater(sim_world.MEDEVAC_TICK, sim_world.ZONE_UNTIL)
        simulation = sim_world.Simulation()
        simulation.tick_count = sim_world.ZONE_UNTIL + 1
        self.assertEqual([b["kind"] for b in simulation.bulletins()], [])
        simulation.tick_count = sim_world.MEDEVAC_TICK
        notams = [b for b in simulation.bulletins() if b["kind"] == "notam"]
        self.assertEqual([b["id"] for b in notams], ["nofly-2026-09-medevac"])
        self.assertEqual(notams[0]["text"], sim_world.MEDEVAC_TEXT)
        self.assertNotIn("polygon", notams[0])
        self.assertEqual((notams[0]["published_tick"], notams[0]["until_tick"]), (1350, 2100))
        simulation.tick_count = sim_world.MEDEVAC_UNTIL + 1
        self.assertNotIn("nofly-2026-09-medevac", [b["id"] for b in simulation.bulletins()])

    def test_without_a_model_it_is_ledgered_unreadable_and_stays_out_of_the_notices(self):
        """The banner writes a notice absent from /state.notices verbatim, as 'not yet read by the
        runtime'."""
        runtime, adapter = make_runtime()
        runtime.tick = 1400
        runtime.absorb([medevac_item()])
        runtime.absorb([medevac_item()])
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        unread = [e for e in ledger_lines(runtime)
                  if e["decision"].get("code") == "notice_unreadable"]
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["decision"]["detail"]["notice"], "nofly-2026-09-medevac")
        self.assertIn("no model to structure it", unread[0]["decision"]["reason"])
        self.assertEqual(adapter.sent, [])


class HeldNoticeTest(unittest.TestCase):
    """Text → model form → held → a person confirms → applied. Before confirmation it never
    enters judgement."""

    CENTRE = (40.81444, -73.93972)        # 404852N0735623W, the Harlem hospital

    def setUp(self):
        self.llm = fixture_super("reference")
        self.runtime, self.adapter = make_runtime(self.llm)
        self.runtime.tick = 1400
        # One aircraft flying a cleared route over the hospital (to be recalled) and one on the
        # ground.
        self.runtime.telemetry = {
            "drone-02": {"lat": 40.8050, "lon": -73.93972, "alt_m": 60.0,
                         "route": [{"lat": 40.8250, "lon": -73.93972, "alt_m": 60.0}]},
            "drone-01": {"lat": 40.8050, "lon": -73.9450, "alt_m": 0.0},
        }

    def through(self) -> list[dict]:
        return [{"lat": 40.8050, "lon": -73.9450, "alt_m": 60},
                {"lat": 40.8144, "lon": -73.9397, "alt_m": 60},
                {"lat": 40.8250, "lon": -73.9350, "alt_m": 60}]

    def file_through(self) -> Verdict:
        decision = self.runtime.file(Proposal(
            asset_id="drone-01", action="fly_route", cost_usd=12.0, blast_radius="schedule",
            rationale="", params={"legs": self.through()}).to_dict())
        return decision

    def test_it_is_held_shown_kept_out_of_judging_and_applied_only_after_a_person_confirms(self):
        self.runtime.absorb([medevac_item()])
        self.assertEqual(self.llm.served, ["notices_super.json"])
        shown = self.runtime.snapshot()["notices"]
        self.assertEqual([(n["id"], n["held"], n["applied"], n["source"]) for n in shown],
                         [("nofly-2026-09-medevac", True, False,
                           "model:nemotron-3-super-reference")])
        self.assertEqual((shown[0]["from_tick"], shown[0]["until_tick"]), (1350, 2100))
        self.assertEqual(len(shown[0]["polygon"]), 16)
        pending = self.runtime.snapshot()["awaiting_human"]
        self.assertEqual([p["action"] for p in pending], ["publish_notice"])
        # While held it plays no part in judgement: a new route over the hospital is approved and
        # the aircraft in flight is not recalled.
        self.assertIsNone(self.runtime.airspace.breach(*self.CENTRE, 60.0))
        self.assertIs(self.file_through().verdict, Verdict.AUTO)
        self.assertEqual([s[1] for s in self.adapter.sent], ["fly_route"])
        self.assertNotIn("nofly-2026-09-medevac",
                         [p["id"] for p in self.runtime.snapshot()["policies"]])
        # From a person's confirmation on: it enters the airspace, the route passing through is
        # recalled, and a new route is refused.
        decision = self.runtime.approve(pending[0]["id"], "controller", allow=True)
        self.assertEqual((decision.verdict, decision.code), (Verdict.AUTO, "notice_published"))
        volume = self.runtime.airspace.breach(*self.CENTRE, 60.0)
        self.assertEqual((volume.id, volume.source), ("nofly-2026-09-medevac", "human"))
        recalled = [s for s in self.adapter.sent if s[1] == "divert_ground"]
        self.assertEqual([s[0] for s in recalled], ["drone-02"])
        self.assertEqual(recalled[0][2]["volume"], "nofly-2026-09-medevac")
        self.runtime.tick = 1420       # refile outside the dedupe window (15 ticks)
        decision = self.file_through()
        self.assertEqual((decision.verdict, decision.forbids),
                         (Verdict.DENIED, "nofly-2026-09-medevac"))
        shown = self.runtime.snapshot()["notices"][0]
        self.assertEqual((shown["held"], shown["applied"], shown["confirmed_by"]),
                         (False, True, "controller"))
        self.assertIn("nofly-2026-09-medevac",
                      [p["id"] for p in self.runtime.snapshot()["policies"]])
        published = [e for e in ledger_lines(self.runtime)
                     if e["decision"].get("code") == "notice_published"]
        self.assertEqual(published[0]["context"]["checks_run"],
                         ["notice:grammar", "notice:model", "notice:human"])
        self.assertEqual(published[0]["outcome"], "done")
        # The entry opened when it was held is closed by the person's answer — same ledger id,
        # no line left open
        self.assertEqual(published[0]["id"], self._card_id(pending[0]["id"]))
        self.assertEqual(self._open_ids(), set())
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])

    def _lines(self):
        with open(self.runtime.ledger.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def _card_id(self, proposal_id):
        return next(e["id"] for e in self._lines() if e["proposal"]["id"] == proposal_id)

    def _open_ids(self):
        """Ledger ids that were opened but never closed."""
        opened = {e["id"] for e in self._lines() if e["outcome"] == "pending"}
        closed = {e["id"] for e in self._lines() if e["outcome"] != "pending"}
        return opened - closed

    def test_a_refusal_closes_the_held_entry_instead_of_opening_another(self):
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime.approve(pending["id"], "controller", allow=False)
        rows = [(e["id"], e["outcome"], e["decision"]["code"]) for e in self._lines()
                if e["proposal"]["id"] == pending["id"]]
        self.assertEqual(rows, [(rows[0][0], "pending", "human_notice"),
                                (rows[0][0], "denied", "notice_refused")])
        self.assertEqual(self._open_ids(), set())

    def test_a_round_change_closes_a_held_card_as_lapsed(self):
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime._follow_round(2)
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        rows = [(e["outcome"], e["decision"]["code"]) for e in self._lines()
                if e["proposal"]["id"] == pending["id"]]
        self.assertEqual(rows, [("pending", "human_notice"), ("lapsed", "notice_lapsed")])
        self.assertEqual(self._open_ids(), set())

    def test_confirming_after_the_window_closed_applies_nothing_and_says_so(self):
        """A race inside one poll. Approval after the window closed is 'lapsed', not 'applied'."""
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime.tick = 2101
        decision = self.runtime.approve(pending["id"], "controller", allow=True)
        self.assertEqual((decision.verdict, decision.code), (Verdict.DENIED, "notice_lapsed"))
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        self.assertEqual(self.runtime.airspace.all(), [])
        self.assertNotIn("nofly-2026-09-medevac",
                         [p["id"] for p in self.runtime.snapshot()["policies"]])
        self.assertEqual([e["outcome"] for e in self._lines()
                          if e["proposal"]["id"] == pending["id"]], ["pending", "lapsed"])
        self.runtime.absorb([medevac_item()])
        self.assertEqual(len(self.llm.asked), 1, "not asked again once the window closed")

    def test_confirmed_before_the_window_opens_it_is_shown_as_confirmed_then_applies(self):
        self.runtime.tick = 1300
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime.approve(pending["id"], "controller", allow=True)
        shown = self.runtime.snapshot()["notices"][0]
        self.assertEqual((shown["held"], shown["applied"], shown["source"]),
                         (False, False, "human"))
        self.runtime.tick = 1350
        self.runtime.absorb([medevac_item()])
        self.assertTrue(self.runtime.snapshot()["notices"][0]["applied"])
        # Confirmed but dropped from the feed before it applies: the record goes too
        other, _ = make_runtime(fixture_super("reference"))
        other.tick = 1300
        other.absorb([medevac_item()])
        other.approve(other.snapshot()["awaiting_human"][0]["id"], "controller", allow=True)
        other.absorb([])
        self.assertEqual(other.snapshot()["notices"], [])

    def test_the_model_reads_off_the_world_thread_and_the_next_poll_collects_it(self):
        """The production wiring. absorb returns at once while the reader thread runs, and the
        next poll records the answer."""
        import threading

        self.runtime.notice_async = True
        self.runtime.absorb([medevac_item()])
        self.assertIn("nofly-2026-09-medevac", self.runtime._reading)
        for thread in threading.enumerate():
            if thread.name.startswith("notice-"):
                self.assertTrue(thread.daemon)
                thread.join(5.0)
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [], "not recorded yet")
        self.runtime.absorb([medevac_item()])
        self.assertEqual(self.runtime._reading, set())
        shown = self.runtime.snapshot()["notices"]
        self.assertEqual([(n["id"], n["held"]) for n in shown], [("nofly-2026-09-medevac", True)])
        self.assertEqual([p["action"] for p in self.runtime.snapshot()["awaiting_human"]],
                         ["publish_notice"])
        self.assertEqual(len(self.llm.asked), 1)

    def test_an_answer_that_arrives_after_the_round_changed_is_dropped(self):
        import threading

        self.runtime.notice_async = True
        self.runtime._round = 1
        self.runtime.absorb([medevac_item()])
        for thread in threading.enumerate():
            if thread.name.startswith("notice-"):
                thread.join(5.0)
        self.runtime._follow_round(2)
        self.runtime.absorb([medevac_item()])
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [],
                         "an answer from the last round is dropped")
        # The same poll asks again under the new round, and the next poll records that answer
        self.assertEqual(self.runtime._reading, {"nofly-2026-09-medevac"})
        for thread in threading.enumerate():
            if thread.name.startswith("notice-"):
                thread.join(5.0)
        self.runtime.absorb([medevac_item()])
        self.assertEqual(len(self.llm.asked), 2)

    def test_a_held_notice_lapses_with_its_window_and_leaves_no_card(self):
        """The window closed before a person looked. The card and the banner come down, and since
        it never applied there is nothing to remove."""
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"]
        self.assertEqual(len(pending), 1)
        self.runtime.tick = 2101
        self.runtime.absorb([medevac_item()])          # still listed, but the window is closed
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(self.runtime.airspace.all(), [])
        lapsed = [e for e in ledger_lines(self.runtime) if e["outcome"] == "lapsed"]
        self.assertEqual([(e["proposal"]["id"], e["decision"]["code"], e["decision"]["verdict"])
                          for e in lapsed], [(pending[0]["id"], "notice_lapsed", "denied")])
        self.runtime.absorb([medevac_item()])
        self.assertEqual(len(self.llm.asked), 1, "not asked again once the window closed")
        self.assertIsNone(self.runtime.approve(pending[0]["id"], "controller", allow=True),
                          "a card that came down can't be approved")

    def test_a_held_notice_dropped_from_the_feed_leaves_no_card_either(self):
        self.runtime.absorb([medevac_item()])
        self.runtime.tick = 1500
        self.runtime.absorb([])
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        self.assertEqual([e["decision"]["reason"] for e in ledger_lines(self.runtime)
                          if e["outcome"] == "lapsed"],
                         ["the notice was taken down before a human confirmed — never applied"])

    def test_the_local_stand_in_answer_is_held_and_a_person_can_refuse_it(self):
        """A real answer from the 30B stand-in on Ollama. It passes the form and the checks, but
        the polygon is not the notice.

        An 80m sliver 3km south of the hospital. This is why a person has to look."""
        llm = fixture_super("ollama-recorded")
        runtime, adapter = make_runtime(llm)
        runtime.tick = 1400
        runtime.absorb([medevac_item()])
        held = runtime.notices.pending()
        self.assertEqual([h["id"] for h in held], ["nofly-2026-09-medevac"])
        centre_lat = sum(p[0] for p in held[0]["polygon"]) / len(held[0]["polygon"])
        self.assertGreater(abs(centre_lat - self.CENTRE[0]) * 110_570, 1000)
        pending = runtime.snapshot()["awaiting_human"][0]
        self.assertEqual(pending["author"], "model:nemotron-3-nano:latest")
        decision = runtime.approve(pending["id"], "controller", allow=False)
        self.assertEqual((decision.verdict, decision.code), (Verdict.DENIED, "notice_refused"))
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertEqual(runtime.airspace.all(), [])
        self.assertEqual(adapter.sent, [])
        self.assertNotIn("nofly-2026-09-medevac", [p["id"] for p in runtime.snapshot()["policies"]])
        runtime.absorb([medevac_item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [],
                         "a refused notice is not put up again")
        self.assertEqual(len(llm.asked), 1, "the same notice is not asked again")


if __name__ == "__main__":
    unittest.main()
