"""A tower advisory is information after repeated refusals. The runtime writes it; nothing changes.

Three refusals in a row (or a decline after refusals) make the runtime list the options the
code can see and check each one with the same deterministic judge. A model may pick one id
out of that list and phrase the summary; anything else it says is dropped and a rule picks.
"""

import json
import tempfile
import unittest

from backend.runtime.advisory import (
    ADVISORY_AFTER,
    CLIMB_M,
    AdvisoryDesk,
    Option,
    Refusal,
    build_options,
    parse_advice,
    rule_pick,
)
from backend.runtime.tower import Runtime
from shared.geo import Volume, box
from shared.llm.client import LlmReply, TieredLlm
from shared.models import Proposal, Verdict
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
ZONE_ITEM = {"id": "nofly-t", "kind": "notam", "name": "test corridor", "text": sim_world.ZONE_TEXT,
             "published_tick": 525, "until_tick": 900}
HERE = (40.7100, -73.9855)


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


class StubSuper(TieredLlm):
    """A Super tier that answers with a fixed text."""

    def __init__(self, text: str):
        super().__init__(base_url="http://stub", models={"super": "stub-super"},
                         timeout_s=0.0, request_extra={}, record_dir="")
        self.text = text
        self.asked = []

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        self.asked.append((tier.value, user))
        reply = LlmReply(text=self.text, model="stub-super")
        self.account(tier, reply, 0)
        return reply


def make_runtime(llm=None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.advisory_async = False       # tests inspect the ledger after the advisory is written
    runtime.notice_async = False
    if llm is not None:
        runtime.llm = llm
        runtime.notices.llm = llm
        runtime.advisor.llm = llm
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.telemetry = {"drone-01": {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0}}
    runtime.tick = 600
    return runtime, adapter


def ledger_lines(runtime):
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [entry for entry in (json.loads(line) for line in handle)
                if entry["outcome"] != "pending"]


def advisories(runtime):
    return [e for e in ledger_lines(runtime) if e["proposal"]["action"] == "advisory"]


def legs(points, alt_m):
    return [{"lat": lat, "lon": lon, "alt_m": alt_m} for lat, lon in points]


THROUGH_ZONE = legs([HERE, (40.7225, -73.9855), (40.7350, -73.9855)], 60)
AWAY = legs([HERE, (40.7000, -73.9855)], 60)


def file_route(runtime, route, asset="drone-01", **params):
    return runtime.file(Proposal(asset_id=asset, action="fly_route", cost_usd=12.0,
                                 blast_radius="schedule", rationale="",
                                 params={"legs": route, **params}).to_dict())


class TriggerTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.absorb([ZONE_ITEM])

    def test_exactly_three_consecutive_refusals_and_a_success_resets(self):
        for _ in range(ADVISORY_AFTER - 1):
            self.assertIs(file_route(self.runtime, THROUGH_ZONE).verdict, Verdict.DENIED)
        self.assertEqual(advisories(self.runtime), [], "two are still within the rewrite ladder")
        self.assertEqual(self.runtime.snapshot()["advisories"], [])
        # One approval breaks the streak
        self.assertTrue(file_route(self.runtime, AWAY).committed)
        self.runtime.tick += 20         # outside the dedupe window
        for _ in range(ADVISORY_AFTER - 1):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(advisories(self.runtime), [])
        file_route(self.runtime, THROUGH_ZONE)
        noted = advisories(self.runtime)
        self.assertEqual(len(noted), 1)
        entry = noted[0]
        self.assertEqual((entry["proposal"]["author"], entry["decision"]["verdict"],
                          entry["decision"]["code"], entry["outcome"]),
                         ("runtime", "auto", "advisory", "noted"))
        self.assertEqual(entry["context"]["checks_run"], ["advisory"])
        params = entry["proposal"]["params"]
        self.assertEqual(params["trigger"], "refusals")
        self.assertEqual(len(params["refusals"]), ADVISORY_AFTER,
                         "only refusals after the approval count")
        self.assertEqual({r["code"] for r in params["refusals"]}, {"airspace"})
        self.assertEqual({r["blocked_volume"] for r in params["refusals"]}, {"nofly-t"})
        shown = self.runtime.snapshot()["advisories"]
        self.assertEqual([(a["asset"], a["ledger_id"], a["chosen"]) for a in shown],
                         [("drone-01", entry["id"], params["chosen"])])
        # A fourth refusal gets no new advisory and nothing executes — an advisory changes nothing
        file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(advisories(self.runtime)), 1)
        self.assertEqual([s[1] for s in self.adapter.sent], ["fly_route"])

    def test_the_zone_offers_the_notice_window_and_the_lifted_route_is_still_illegal(self):
        for _ in range(ADVISORY_AFTER):
            file_route(self.runtime, THROUGH_ZONE)
        params = advisories(self.runtime)[0]["proposal"]["params"]
        by_id = {o["id"]: o for o in params["options"]}
        self.assertEqual(list(by_id), ["climb", "notice_window", "decline", "escalate"])
        self.assertFalse(by_id["climb"]["legal"],
                         "the zone reaches 400ft, so climbing 30m is still inside it")
        self.assertIn("test corridor", by_id["climb"]["why"])
        self.assertEqual((by_id["notice_window"]["legal"], by_id["notice_window"]["until_tick"]),
                         (True, 900))
        self.assertTrue(by_id["decline"]["legal"] and by_id["escalate"]["legal"])
        self.assertEqual((params["chosen"], params["source"], params["model"]),
                         ("notice_window", "rules", ""))
        self.assertIn("3 times in a row", params["summary"])
        self.assertIn("notice window", params["summary"])

    def test_a_decline_after_refusals_is_an_advisory_and_a_plain_decline_is_not(self):
        decline = Proposal(asset_id="drone-01", action="decline_job", cost_usd=0.0,
                           blast_radius="none", rationale="no legal route")
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        self.assertEqual(advisories(self.runtime), [], "no refusals, no advisory")
        file_route(self.runtime, THROUGH_ZONE)
        self.runtime.tick += 20
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        noted = advisories(self.runtime)
        self.assertEqual(len(noted), 1)
        params = noted[0]["proposal"]["params"]
        self.assertEqual((params["trigger"], len(params["refusals"])),
                         ("decline_after_refusals", 1))
        self.assertIn("declined the order after 1 refusals", params["summary"])
        self.assertEqual(self.runtime.snapshot()["advisories"][0]["trigger"],
                         "decline_after_refusals")

    def test_the_same_block_is_not_advised_again_and_a_new_block_is(self):
        """Live run: refiling every few ticks while someone else held the landing pad → the same
        'hold until tick X' every third time. One advisory while the same block lasts; when the
        block changes and three refusals pile up again, it writes another."""
        for _ in range(ADVISORY_AFTER * 3):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(advisories(self.runtime)), 1)
        # Blocked by something else (a building, not the zone) → three more make a new advisory
        self.runtime.airspace.add(Volume(id="bldg-new", name="new building",
                                         polygon=box(40.7040, -73.9860, 40.7050, -73.9850),
                                         floor_m=0.0, ceiling_m=130.0, clearance_m=50.0,
                                         rule="forbidden"))
        south = legs([HERE, (40.7045, -73.9855), (40.7000, -73.9855)], 60)
        for index in range(ADVISORY_AFTER):
            self.assertEqual(file_route(self.runtime, south).forbids, "bldg-new")
            self.assertEqual(len(advisories(self.runtime)), 1 if index < ADVISORY_AFTER - 1 else 2)
        self.assertEqual({r["blocked_volume"] for r in
                          advisories(self.runtime)[1]["proposal"]["params"]["refusals"]},
                         {"nofly-t", "bldg-new"})

    def test_a_committed_decline_resets_the_streak(self):
        decline = Proposal(asset_id="drone-01", action="decline_job", cost_usd=0.0,
                           blast_radius="none", rationale="no legal route")
        file_route(self.runtime, THROUGH_ZONE)
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        self.assertEqual(len(advisories(self.runtime)), 1)
        self.assertEqual(self.runtime.advisor.streaks, {}, "the order is gone, so is the streak")
        self.runtime.tick += 20
        for _ in range(ADVISORY_AFTER - 1):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(advisories(self.runtime)), 1, "a new order's refusals count afresh")
        file_route(self.runtime, THROUGH_ZONE)
        noted = advisories(self.runtime)
        self.assertEqual(len(noted), 2)
        self.assertEqual(len(noted[1]["proposal"]["params"]["refusals"]), ADVISORY_AFTER)

    def test_a_duplicate_decline_writes_no_second_advisory(self):
        decline = Proposal(asset_id="drone-01", action="decline_job", cost_usd=0.0,
                           blast_radius="none", rationale="no legal route")
        file_route(self.runtime, THROUGH_ZONE)
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        again = self.runtime.file(Proposal(asset_id="drone-01", action="decline_job",
                                           cost_usd=0.0, blast_radius="none",
                                           rationale="no legal route").to_dict())
        self.assertEqual((again.verdict, again.code), (Verdict.DENIED, "duplicate"))
        self.assertEqual(len(advisories(self.runtime)), 1)
        self.assertEqual([s[1] for s in self.adapter.sent], ["decline_job"])

    def test_a_model_summary_that_arrives_after_the_round_changed_is_dropped(self):
        import threading
        import time

        class SlowSuper(StubSuper):
            def ask(self, *args, **kwargs):
                time.sleep(0.2)
                return super().ask(*args, **kwargs)

        runtime, _ = make_runtime(SlowSuper(json.dumps({"choice": "decline", "summary": "x"})))
        runtime.advisory_async = True
        runtime._round = 1
        runtime.absorb([ZONE_ITEM])
        for _ in range(ADVISORY_AFTER):
            file_route(runtime, THROUGH_ZONE)
        runtime._follow_round(2)
        for thread in threading.enumerate():
            if thread.name.startswith("advisory-"):
                thread.join(2.0)
        self.assertEqual(runtime.snapshot()["advisories"], [])
        self.assertEqual(advisories(runtime), [])

    def test_a_new_round_clears_the_streak_and_the_card(self):
        for _ in range(ADVISORY_AFTER):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(self.runtime.snapshot()["advisories"]), 1)
        self.runtime._follow_round(7)
        self.assertEqual(self.runtime.snapshot()["advisories"], [])
        self.assertEqual(self.runtime.advisor.streaks, {})


class JudgeTest(unittest.TestCase):
    """The same judging function decides whether an option is legal."""

    def roof(self, ceiling_m: float):
        runtime, adapter = make_runtime()
        runtime.airspace.add(Volume(id="bldg-roof", name=f"roof {ceiling_m:.0f}",
                                    polygon=box(40.7190, -73.9860, 40.7200, -73.9850),
                                    floor_m=0.0, ceiling_m=ceiling_m, clearance_m=50.0,
                                    rule="forbidden"))
        return runtime, adapter

    def test_a_lifted_route_over_a_130_m_roof_is_illegal(self):
        runtime, _ = self.roof(130.0)
        route = legs([HERE, (40.7195, -73.9855), (40.7350, -73.9855)], 60)
        for _ in range(ADVISORY_AFTER):
            decision = file_route(runtime, route)
            self.assertEqual(decision.forbids, "bldg-roof")
        params = advisories(runtime)[0]["proposal"]["params"]
        climb = next(o for o in params["options"] if o["id"] == "climb")
        self.assertEqual((climb["legal"], climb["shift_m"]), (False, CLIMB_M))
        self.assertIn("roof 130", climb["why"])
        self.assertEqual([o["id"] for o in params["options"]], ["climb", "decline", "escalate"])
        self.assertEqual(params["chosen"], "decline")

    def test_a_lifted_route_over_a_30_m_roof_is_legal_and_chosen(self):
        # Roof 30m + margin 50m = blocked up to 80m. 60m is refused, 90m passes.
        runtime, adapter = self.roof(30.0)
        route = legs([HERE, (40.7195, -73.9855), (40.7350, -73.9855)], 60)
        for _ in range(ADVISORY_AFTER):
            decision = file_route(runtime, route)
            self.assertEqual(decision.forbids, "bldg-roof")
        params = advisories(runtime)[0]["proposal"]["params"]
        climb = next(o for o in params["options"] if o["id"] == "climb")
        self.assertTrue(climb["legal"], climb["why"])
        self.assertEqual(params["chosen"], "climb")
        self.assertEqual(adapter.sent, [], "an advisory only evaluates; it executes nothing")

    def test_a_pad_filed_by_resource_alone_is_judged_against_that_pad(self):
        """reserve_pad may carry only resource, with no params.pad. The endpoint check's
        destination is the resource — without it the advisory misses the endpoint gap and says
        'just climb'."""
        runtime, _ = make_runtime()
        runtime.pad_coords = {"pad:launch": (40.7200, -73.9800)}
        runtime.airspace.add(Volume(id="bldg-roof", name="roof 30",
                                    polygon=box(40.7140, -73.9860, 40.7150, -73.9850),
                                    floor_m=0.0, ceiling_m=30.0, clearance_m=50.0,
                                    rule="forbidden"))
        astray = legs([HERE, (40.7145, -73.9855), (40.7200, -73.9855)], 60)   # ends 460m from pad
        for _ in range(ADVISORY_AFTER):
            decision = runtime.file(Proposal(asset_id="drone-01", action="reserve_pad",
                                             cost_usd=28.0, blast_radius="schedule",
                                             rationale="", resource="pad:launch",
                                             params={"legs": astray}).to_dict())
            self.assertIs(decision.verdict, Verdict.DENIED)
            self.assertIn("last point", decision.reason)
        params = advisories(runtime)[0]["proposal"]["params"]
        climb = next(o for o in params["options"] if o["id"] == "climb")
        self.assertFalse(climb["legal"], climb["why"])
        self.assertIn("last point", climb["why"])

    def test_a_crossing_offers_holding_until_the_other_corridor_clears(self):
        runtime, adapter = make_runtime()
        runtime.telemetry["drone-02"] = {"lat": 40.7100, "lon": -73.9800, "alt_m": 0.0}
        self.assertTrue(file_route(runtime, legs([(40.7100, -73.9800), (40.7400, -73.9800)], 60),
                                   asset="drone-02").committed)
        runtime.telemetry["drone-01"] = {"lat": 40.7200, "lon": -73.9900, "alt_m": 0.0}
        crossing = legs([(40.7200, -73.9900), (40.7200, -73.9700)], 60)
        for _ in range(ADVISORY_AFTER):
            decision = file_route(runtime, crossing)
            self.assertEqual(decision.policy_hit, "traffic")
        params = advisories(runtime)[0]["proposal"]["params"]
        by_id = {o["id"]: o for o in params["options"]}
        self.assertEqual(list(by_id), ["hold", "climb", "decline", "escalate"])
        until = decision.detail["blocked_until_tick"]
        self.assertEqual((by_id["hold"]["legal"], by_id["hold"]["until_tick"]), (True, until))
        self.assertIn("drone-02", by_id["hold"]["why"])
        self.assertTrue(by_id["climb"]["legal"],
                        "the other corridor spans ±27m vertically, so 30m up clears it")
        self.assertEqual(params["chosen"], "hold")
        self.assertEqual({r["blocked_asset"] for r in params["refusals"]}, {"drone-02"})


class ModelTest(unittest.TestCase):
    """The model picks one option from the list and writes the summary. Outside the list, a rule
    picks and the text comes from a template."""

    def setUp(self):
        self.refusals = [Refusal("drone-01", 600 + i, "airspace", "traffic", "traffic", None,
                                 "drone-02", 700, f"p{i}", "fly_route", legs=THROUGH_ZONE)
                         for i in range(3)]
        self.options = build_options(self.refusals, lambda refusal, route: None,
                                     lambda volume_id: None, airborne=False)

    def test_options_are_ordered_and_the_rule_takes_the_first_legal_one(self):
        self.assertEqual([o.id for o in self.options], ["hold", "climb", "decline", "escalate"])
        self.assertEqual(rule_pick(self.options), "hold")
        self.assertEqual(rule_pick([Option("hold", "", False, ""), Option("climb", "", True, "")]),
                         "climb")
        airborne = build_options(self.refusals, lambda r, route: "roof", lambda v: None, True)
        self.assertFalse(airborne[0].legal, "an airborne aircraft cannot hold on the ground")
        self.assertFalse(airborne[1].legal)
        self.assertEqual(rule_pick(airborne), "decline")

    def test_a_choice_in_the_list_is_kept_with_the_models_summary(self):
        llm = StubSuper(json.dumps({"choice": "climb", "summary": "Two crossings with drone-02. "
                                    "Climb thirty metres and refile."}))
        desk = AdvisoryDesk(llm)
        params = desk.compose("drone-01", "refusals", self.refusals, self.options, False)
        self.assertEqual((params["chosen"], params["source"], params["model"]),
                         ("climb", "super", "stub-super"))
        self.assertTrue(params["summary"].startswith("Two crossings"))
        self.assertIn("[hold] hold on the ground until tick 700 — legal", llm.asked[0][1])

    def test_a_choice_outside_the_list_is_ignored_and_the_rule_picks(self):
        for text in (json.dumps({"choice": "teleport", "summary": "Teleport past drone-02."}),
                     "just climb", ""):
            llm = StubSuper(text)
            desk = AdvisoryDesk(llm)
            params = desk.compose("drone-01", "refusals", self.refusals, self.options, False)
            self.assertEqual((params["chosen"], params["source"]), ("hold", "rules"), text)
            self.assertIn("The rules suggest: hold on the ground until tick 700",
                          params["summary"])
            self.assertNotIn("Teleport", params["summary"])
            self.assertEqual(llm.stats["super"].fallback, 1)

    def test_a_choice_echoed_with_the_brackets_of_the_list_is_read(self):
        """Live run: 155 of 160 advisories answered "[hold]" and fell back to the rule's pick."""
        for text in ('{"choice": "[hold]", "summary": "Wait it out."}',
                     '{"choice": " [HOLD] ", "summary": "Wait it out."}',
                     '{"choice": "\'hold\'", "summary": "Wait it out."}'):
            self.assertEqual(parse_advice(text, self.options), ("hold", "Wait it out."), text)
        self.assertEqual(parse_advice('{"choice": "[teleport]"}', self.options), (None, ""))

    def test_an_illegal_pick_counts_as_outside_the_list(self):
        options = [Option("hold", "hold", False, "airborne"),
                   Option("decline", "decline", True, "")]
        self.assertEqual(parse_advice(json.dumps({"choice": "hold", "summary": "x"}), options),
                         (None, "x"))
        self.assertEqual(parse_advice(json.dumps({"choice": "decline"}), options),
                         ("decline", ""))

    def test_no_model_means_a_template_and_no_call(self):
        desk = AdvisoryDesk(TieredLlm(base_url="", models={}))
        self.assertFalse(desk.has_model)
        params = desk.compose("drone-01", "refusals", self.refusals, self.options, False)
        self.assertEqual((params["chosen"], params["source"], params["model"]),
                         ("hold", "rules", ""))
        self.assertEqual(params["summary"],
                         "drone-01 was refused 3 times in a row (traffic). "
                         "The rules suggest: hold on the ground until tick 700.")

    def test_the_runtime_records_the_model_word_on_the_ledger(self):
        llm = StubSuper(json.dumps({"choice": "decline", "summary": "The zone is closed until "
                                    "tick 900. Decline this order."}))
        runtime, _ = make_runtime(llm)
        runtime.absorb([ZONE_ITEM])
        for _ in range(ADVISORY_AFTER):
            file_route(runtime, THROUGH_ZONE)
        entry = advisories(runtime)[0]
        self.assertEqual(entry["decision"]["detail"],
                         {"resource": "drone-01", "chosen": "decline", "trigger": "refusals",
                          "source": "super"})
        self.assertEqual(entry["proposal"]["params"]["model"], "stub-super")
        self.assertEqual(runtime.snapshot()["advisories"][0]["summary"],
                         "The zone is closed until tick 900. Decline this order.")
        self.assertEqual([tier for tier, _ in llm.asked], ["super"])


if __name__ == "__main__":
    unittest.main()
