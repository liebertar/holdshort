"""A route the model sketches is a proposal, never a route.

Every check here is code: shape, count, box, altitude band, snapped ends, length, and
then the same judge the runtime uses. The model can be garbage, adversarial, or right —
what leaves `draft()` is either something the judge passed or nothing.
"""

import json
import pathlib
import unittest

from drone.agent.drafter import (
    ALT_MAX_M,
    DRAFT_TIMEOUT_S,
    MAX_LEGS,
    ModelDrafter,
    describe,
    service_bbox,
)
from drone.agent.planner import OperatorPlanner
from shared.geo import Volume, first_breach
from shared.llm.client import LlmReply, TieredLlm
from sim.world import LANDING_AREAS, Simulation, seat_of, to_latlon
from tests.fixture_llm import FIXTURE_DIR, FixtureLlm, load_fixtures

RUNTIME_DIR = pathlib.Path(__file__).resolve().parent.parent / "backend"


class ScriptedLlm(TieredLlm):
    """Replies in order and counts how many times it was asked."""

    def __init__(self, *texts):
        super().__init__(base_url="http://scripted", models={"nano": "scripted-nano"},
                         timeout_s=1.0, request_extra={}, record_dir="")
        self.texts = list(texts)
        self.prompts = []
        self.budgets = []

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        self.prompts.append(user)
        self.budgets.append(timeout_s)
        if not self.texts:
            return None
        text = self.texts.pop(0)
        return None if text is None else LlmReply(text=text, model="scripted-nano")


def _fleet_planner() -> OperatorPlanner:
    planner = OperatorPlanner()
    planner.load(Simulation(seed=7).worlds["guarded"].snapshot(0, volumes=True)["volumes"])
    return planner


def _bbox():
    return service_bbox([(a["lat"], a["lon"]) for a in LANDING_AREAS])


# First seat in the yard next to the pad → Brooklyn Bridge Park. A short route over the river,
# so it can be drawn by hand.
START = tuple(round(v, 6) for v in to_latlon(*seat_of(0)))
GOAL = (40.70200, -73.99650)


def _hand_drawn():
    """Three legs that loop over the river. The test judges it itself and uses it once it passes.

    Leaves the yard westward, crosses the low-rise Vinegar Hill and DUMBO blocks (63m at most) at
    114m and lands in the park. Looping over the river alone, the last leg hits the 83m building
    on the DUMBO waterfront."""
    return {"legs": [
        {"lat": START[0], "lon": START[1], "alt_m": 70},
        {"lat": 40.70115, "lon": -73.97055, "alt_m": 90},
        {"lat": 40.70160, "lon": -73.99575, "alt_m": 114},
        {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 90},
    ]}


class ValidationTest(unittest.TestCase):
    """Whatever the model sends, code filters it. Before the judgement, and independent of it."""

    def setUp(self):
        self.bbox = _bbox()

    def check(self, form):
        return ModelDrafter.validate(form, START, GOAL, self.bbox)

    def test_garbage_shapes_are_rejected(self):
        for form in (None, {}, {"legs": "north"}, {"legs": [1, 2]}, {"legs": [{"lat": 1}]},
                     {"legs": [{"lat": "a", "lon": "b", "alt_m": "c"},
                               {"lat": 1, "lon": 2, "alt_m": 3}]},
                     {"legs": [{"lat": 40.7, "lon": -73.9, "alt_m": 60}]}):
            with self.subTest(form=form):
                legs, why = self.check(form)
                self.assertIsNone(legs)
                self.assertTrue(why)

    def test_misspelt_altitude_keys_are_read_as_alt_m(self):
        """In a live run the 4B wrote alt_ma in 4 of 10 answers. A key name is form, not a rule."""
        for key in ("alt_ma", "altitude_m", "altitude", "alt"):
            with self.subTest(key=key):
                legs, why = self.check({"legs": [{"lat": START[0], "lon": START[1], key: 60},
                                                 {"lat": GOAL[0], "lon": GOAL[1], key: 200}]})
                self.assertIsNone(legs)
                self.assertIn("outside", why, "the value gets the same range check")
                legs, why = self.check({"legs": [{"lat": START[0], "lon": START[1], key: 60},
                                                 {"lat": GOAL[0], "lon": GOAL[1], key: 90}]})
                self.assertIsNone(why)
                self.assertEqual([leg["alt_m"] for leg in legs], [60.0, 90.0])
                self.assertNotIn(key if key != "alt_m" else "x", legs[0])

    def test_too_many_legs(self):
        legs = [{"lat": 40.70 + i * 0.0005, "lon": -73.98, "alt_m": 60}
                for i in range(MAX_LEGS + 1)]
        self.assertIsNone(self.check({"legs": legs})[0])
        self.assertIsNotNone(self.check({"legs": legs[:MAX_LEGS]})[0])

    def test_out_of_bbox_and_absurd_altitude(self):
        good = _hand_drawn()
        far = json.loads(json.dumps(good))
        far["legs"][1]["lat"] = 41.5            # Connecticut
        self.assertIn("service box", self.check(far)[1])
        for altitude in (-5, 0, 39.9, 120.1, 5000, float("nan"), float("inf")):
            bad = json.loads(json.dumps(good, allow_nan=True), parse_constant=float)
            bad["legs"][2]["alt_m"] = altitude
            with self.subTest(altitude=altitude):
                self.assertIsNone(self.check(bad)[0])

    def test_ends_are_snapped_and_a_wander_is_too_long(self):
        form = _hand_drawn()
        form["legs"][0]["lat"] += 0.001          # the model writes the start 110m off
        form["legs"][-1]["lon"] -= 0.001
        legs, why = self.check(form)
        self.assertIsNone(why)
        self.assertEqual((legs[0]["lat"], legs[0]["lon"]), START)
        self.assertEqual((legs[-1]["lat"], legs[-1]["lon"]), GOAL)
        wander = _hand_drawn()
        wander["legs"].insert(1, {"lat": 40.80, "lon": -73.95, "alt_m": 60})   # Harlem and back
        self.assertIn("longer than", self.check(wander)[1])

    def test_repeated_points_collapse(self):
        form = {"legs": [{"lat": START[0], "lon": START[1], "alt_m": 60},
                         {"lat": START[0], "lon": START[1], "alt_m": 60},
                         {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 60}]}
        legs, _ = self.check(form)
        self.assertEqual(len(legs), 2)


class DraftFlowTest(unittest.TestCase):
    """Ask, filter, judge, ask once more, and if it still fails, let go."""

    @classmethod
    def setUpClass(cls):
        cls.planner = _fleet_planner()
        cls.bbox = _bbox()
        # The test first checks that the hand-drawn route passes in the real airspace
        legs, why = ModelDrafter.validate(_hand_drawn(), START, GOAL, cls.bbox)
        assert why is None, why
        assert first_breach(cls.planner.airspace, legs) is None, "hand-drawn route fails the judge"

    def drafter(self, *texts):
        return ModelDrafter(ScriptedLlm(*texts), self.planner, bbox=self.bbox)

    def test_a_passing_draft_is_returned_with_its_ends_snapped(self):
        drafter = self.drafter(json.dumps(_hand_drawn()))
        legs = drafter.draft(START, GOAL, {"reason": "x", "forbids": None})
        self.assertIsNotNone(legs)
        self.assertEqual(drafter.last_attempts, 1)
        self.assertEqual((legs[0]["lat"], legs[0]["lon"]), START)
        self.assertIsNone(first_breach(self.planner.airspace, legs))
        self.assertEqual(drafter.name, "nano:scripted-nano")

    def test_json_only_inside_think_is_garbage_then_a_second_ask(self):
        drafter = self.drafter("<think>" + json.dumps(_hand_drawn()) + "</think>",
                               json.dumps(_hand_drawn()))
        legs = drafter.draft(START, GOAL)
        self.assertIsNotNone(legs)
        self.assertEqual(drafter.last_attempts, 2)
        self.assertIn("not a", drafter.last_failures[0])
        self.assertIn("previous draft was refused", drafter.llm.prompts[1])

    def test_a_draft_through_a_building_is_retried_with_the_exact_failure(self):
        """The straight line (through the building beside the depot), sent twice, is refused both
        times and the result is None."""
        through = {"legs": [{"lat": START[0], "lon": START[1], "alt_m": 60},
                            {"lat": 40.7985, "lon": -73.955, "alt_m": 60}]}
        goal = (40.7985, -73.955)
        drafter = self.drafter(json.dumps(through), json.dumps(through))
        self.assertIsNone(drafter.draft(START, goal))
        self.assertEqual(drafter.last_attempts, 2)
        self.assertEqual(len(drafter.last_failures), 2)
        self.assertIn("hits", drafter.last_failures[0])
        retry = drafter.llm.prompts[1]
        self.assertIn("previous draft was refused", retry)
        self.assertIn("judge said", retry)

    def test_no_reply_means_no_second_ask(self):
        drafter = self.drafter(None, json.dumps(_hand_drawn()))
        self.assertIsNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 1)

    def test_the_draft_call_brings_its_own_budget(self):
        """A draft is not a 6 s filing-form question. Even on an idle Ollama it takes 9 s, and a
        passing answer up to 19 s."""
        drafter = self.drafter(json.dumps(_hand_drawn()))
        drafter.draft(START, GOAL)
        self.assertEqual(drafter.llm.budgets, [DRAFT_TIMEOUT_S])
        custom = ModelDrafter(ScriptedLlm(json.dumps(_hand_drawn())), self.planner, bbox=self.bbox,
                              timeout_s=12.5)
        custom.draft(START, GOAL)
        self.assertEqual(custom.llm.budgets, [12.5])

    def test_a_server_that_just_timed_out_is_not_asked_again_for_a_while(self):
        """Rather than wait 5.6 s after a timeout and stake another 30 s on the same server, this
        turn goes to A*."""
        import time

        drafter = self.drafter(json.dumps(_hand_drawn()), json.dumps(_hand_drawn()))
        # Right after a draft call was cut off (skip_until). A cut-off filing-form call
        # (llm.unreachable_at) has nothing to do with drafts.
        drafter.skip_until = time.monotonic() + drafter.backoff_s
        self.assertIsNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 0)
        self.assertIn("skipped", drafter.last_failures[0])
        drafter.skip_until = time.monotonic() - 1
        # Even with the filing-form call just cut off, the draft is asked
        drafter.llm.unreachable_at = time.monotonic()
        self.assertIsNotNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 1)

    def test_disabled_model_never_asks(self):
        llm = TieredLlm(base_url="", models={}, timeout_s=1, request_extra={}, record_dir="")
        drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
        self.assertFalse(drafter.enabled)
        self.assertIsNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 0)

    def test_the_operator_altitude_rule_lifts_a_leg_that_is_too_low(self):
        """Legs the model wrote at 40m stay put over the river; over buildings they rise to the
        lowest safe altitude."""
        form = _hand_drawn()
        for leg in form["legs"]:
            leg["alt_m"] = 40
        drafter = self.drafter(json.dumps(form))
        legs = drafter.draft(START, GOAL)
        self.assertIsNotNone(legs)
        self.assertTrue(all(40 <= leg["alt_m"] <= ALT_MAX_M for leg in legs))
        self.assertIsNone(first_breach(self.planner.airspace, legs))

    def test_the_brief_reads_the_map_from_the_judge(self):
        drafter = self.drafter()
        goal = (40.7985, -73.955)
        brief = drafter._brief(START, goal, self.bbox, {"reason": "leg 1 breaks the rules",
                                                       "forbids": None})
        self.assertIn(f"origin {START[0]:.5f},{START[1]:.5f} -> goal 40.79850,-73.95500", brief)
        self.assertIn("no-fly cell", brief)          # the Midtown KLGA 0ft band
        self.assertIn("clear", brief)                # which side is open
        self.assertIn("altitude capped", brief)      # the 300ft cells
        self.assertIn("runtime's refusal", brief)
        hits = drafter.obstacles(START, goal, 120.0)
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(len({v.id for _, v, _, _ in hits}), len(hits))   # nothing counted twice

    def test_describe_words(self):
        tall = Volume(id="bldg-1", name="BUILDING 157 m",
                      polygon=[(40.71, -73.97), (40.71, -73.969),
                               (40.711, -73.969), (40.711, -73.97)],
                      ceiling_m=157.0, clearance_m=50.0)
        self.assertIn("go around", describe(tall))
        self.assertIn("would need 208 m", describe(tall))       # 157 + 50 + 0.5, rounded up
        low = Volume(id="bldg-2", name="BUILDING 44 m", polygon=tall.polygon, ceiling_m=44.0,
                     clearance_m=50.0)
        self.assertIn("95 m or higher", describe(low))
        # Under a ceiling cell it can't climb over
        self.assertIn("go around", describe(low, allowed_m=60.0))
        cell = Volume(id="klga-0", name="KLGA cell 0 ft", polygon=tall.polygon, rule="forbidden")
        self.assertIn("every altitude", describe(cell))

    def test_a_deadline_clamps_each_ask_to_what_is_left(self):
        """With a deadline, the two asks together get only until then. With under 2 s left it
        doesn't ask at all."""
        import time

        drafter = self.drafter(json.dumps(_hand_drawn()))
        drafter.draft(START, GOAL, deadline=time.monotonic() + 10.0)
        self.assertLessEqual(drafter.llm.budgets[0], 10.0)
        self.assertGreater(drafter.llm.budgets[0], 9.0)
        spent = self.drafter(json.dumps(_hand_drawn()))
        self.assertIsNone(spent.draft(START, GOAL, deadline=time.monotonic() + 0.5))
        self.assertEqual(spent.last_attempts, 0)
        self.assertEqual(spent.llm.budgets, [])
        self.assertIn("budget exhausted", spent.last_failures[0])

    def test_a_retry_is_skipped_when_the_first_ask_took_longer_than_what_is_left(self):
        """Live runs: 11 retries cut off, 0 passed. Placing a same-sized ask for longer than the
        time left only waits for an answer that arrives after the deadline."""
        import time

        class Slow(ScriptedLlm):
            def ask(self, *args, **kwargs):
                reply = super().ask(*args, **kwargs)
                return None if reply is None else LlmReply(text=reply.text, model=reply.model,
                                                           latency_ms=7_000)

        drafter = ModelDrafter(Slow(json.dumps({"legs": "garbage"}),
                                    json.dumps(_hand_drawn())), self.planner, bbox=self.bbox)
        # 5 s to the deadline, and the first answer (not a form) reports taking 7 s → the same ask
        # again would only land after the deadline. It doesn't ask again
        drafter.draft(START, GOAL, deadline=time.monotonic() + 5.0)
        self.assertEqual(drafter.last_attempts, 1)
        self.assertEqual(len(drafter.llm.prompts), 1)
        self.assertIn("retry skipped", drafter.last_failures[-1])
        # Without a deadline (one budget per ask) it still asks twice
        again = ModelDrafter(Slow(json.dumps({"legs": "garbage"}), json.dumps(_hand_drawn())),
                             self.planner, bbox=self.bbox)
        self.assertIsNotNone(again.draft(START, GOAL))
        self.assertEqual(again.last_attempts, 2)


class GoAroundUnderACeilingCellTest(unittest.TestCase):
    """A building inside a ceiling cell. A 120m scan hits the cell itself and skips the whole
    cell, so the building needs a separate look.

    Live run (McCarren Park, 101m building bldg-t03281 inside the 90m cell uasfm-171132): the GO
    AROUND list was empty, and the 4B, given both the left and right points, zigzagged through
    between the buildings.
    """

    START, GOAL = (40.72060, -73.95200), (40.71819, -73.97575)

    def setUp(self):
        from shared.geo import box

        self.planner = OperatorPlanner()
        along = (40.71922, -73.96392)         # on the straight line, about 1.0 km from the start
        self.planner.airspace.add(Volume(
            id="cell-90", name="KLGA 300ft", rule="ceiling", ceiling_m=91.4,
            polygon=box(40.7150, -73.9700, 40.7260, -73.9520)))
        self.planner.airspace.add(Volume(
            id="bldg-101", name="BUILDING 101 m", rule="forbidden", ceiling_m=101.0,
            clearance_m=50.0,
            polygon=box(along[0] - 0.00017, along[1] - 0.00022,
                        along[0] + 0.00017, along[1] + 0.00022)))
        self.planner.airspace.add(Volume(
            id="bldg-60", name="BUILDING 60 m", rule="forbidden", ceiling_m=60.0, clearance_m=50.0,
            polygon=box(40.71880, -73.95700, 40.71910, -73.95660)))
        self.drafter = ModelDrafter(FixtureLlm(records=[]), self.planner, bbox=_bbox())

    def test_the_building_inside_the_cell_is_a_go_around_and_gets_one_side(self):
        around = self.drafter.go_arounds(self.START, self.GOAL)
        self.assertEqual([v.id for v, _ in around], ["bldg-101"],
                         "the 60m building can be cleared at 111m, so it is no go-around")
        self.assertTrue(self.drafter.must_go_around(*around[0]))
        # Measuring at 120m alone shows only the cell and misses the building — which is why it
        # is measured separately
        # The 120 m straight-line scan finds the building too — it used to stop at the cell entry
        # and miss the building inside, but leg_breaches collects everything a leg breaks, so the
        # cell and the building are both listed.
        self.assertIn("bldg-101", {v.id for _, v, _, _ in
                                   self.drafter.obstacles(self.START, self.GOAL, ALT_MAX_M)})
        brief = self.drafter._brief(self.START, self.GOAL, _bbox(), {})
        head, _, _ = brief.partition("The straight line at")
        self.assertIn("GO AROUND", head)
        self.assertIn("bldg-101", head)
        self.assertIn("would need 152 m, limit there 90 m", head)
        self.assertEqual(len([line for line in head.splitlines() if " of it (" in line]), 1,
                         "names only one side to pass")
        self.assertRegex(head, r"pass (NORTH|SOUTH)(-[A-Z]+)? of it \((left|right)\), e\.g\. via")


class FeedbackTest(unittest.TestCase):
    """The second ask gives, in numbers, what it hit and which way and how far to move aside.

    Recording (drafts_nano.json, Brooklyn Bridge Park): the real nano grazed the 77m building
    (bldg-t02419) on the DUMBO waterfront by 10m. It needs 77 + 50 + 0.5 = 128 m and the limit
    is 120 m, so it has to go around.
    """

    @classmethod
    def setUpClass(cls):
        cls.planner = _fleet_planner()
        cls.bbox = _bbox()
        records = [r for r in load_fixtures() if r.get("kind") == "draft"
                   and r.get("area") == "Brooklyn Bridge Park" and r.get("expect") == "fail"]
        if not records:
            raise unittest.SkipTest("no Brooklyn Bridge Park refusal recording")
        cls.record = records[0]
        cls.start = tuple(cls.record["start"])
        cls.goal = tuple(cls.record["goal"])
        # A hand-drawn route looping over the river, used as the second answer (the test first
        # checks that it passes the judge from seat 1 too)
        cls.clearing = {"tier": "nano", "model": "nemotron-3-nano",
                        "needle": "Your previous draft was refused",
                        "text": json.dumps(_hand_drawn()), "source": "hand-drawn"}
        legs, why = ModelDrafter.validate(_hand_drawn(), cls.start, cls.goal, cls.bbox)
        assert why is None, why
        assert first_breach(cls.planner.airspace, legs) is None

    def test_the_brief_lists_go_arounds_up_front_with_one_side_to_pass(self):
        drafter = ModelDrafter(FixtureLlm(records=[]), self.planner, bbox=self.bbox)
        brief = drafter._brief(self.start, self.goal, self.bbox, {})
        head, _, rest = brief.partition("The straight line at")
        self.assertIn("GO AROUND", head)
        self.assertIn("bldg-t02419", head)
        self.assertIn("would need 128 m", head)
        self.assertIn("pass SOUTH of it (left), e.g. via", head)
        self.assertIn("never alternate between left and right", head)
        self.assertIn("bldg-t02419", rest)             # also in the list read in order
        around = drafter.go_arounds(self.start, self.goal)
        self.assertTrue(all(drafter.must_go_around(v, at) for v, at in around))
        self.assertIn("bldg-t02419", {v.id for v, _ in around})

    def test_the_second_ask_names_the_building_its_roof_and_the_side_to_pass(self):
        llm = FixtureLlm(records=[self.record])          # answers only the first ask
        drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
        self.assertIsNone(drafter.draft(self.start, self.goal, {}))
        self.assertEqual(drafter.last_attempts, 2)
        self.assertEqual(len(llm.asked), 2)
        retry = llm.asked[1][1]
        feedback = retry.split("previous draft was refused")[1]
        self.assertIn("bldg-t02419", feedback)
        self.assertIn("roof 77 m", feedback)
        self.assertIn("would need 128 m", feedback)
        self.assertIn("above the 120 m limit", feedback)
        self.assertIn("MUST fly around it", feedback)
        self.assertRegex(feedback, r"pass (NORTH|SOUTH|EAST|WEST)(-[A-Z]+)? of it")
        self.assertIn("climbing cannot fix it", feedback)

    def test_a_draft_that_clears_after_feedback_is_returned_with_two_attempts(self):
        # The retry recording goes first: the first ask lacks its needle, so the first-ask
        # recording answers; on the second both match and the one in front wins.
        llm = FixtureLlm(records=[self.clearing, self.record])
        drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
        legs = drafter.draft(self.start, self.goal, {})
        self.assertIsNotNone(legs, drafter.last_failures)
        self.assertEqual(drafter.last_attempts, 2)
        self.assertEqual(llm.served, [self.record["_file"], "Your previous draft was refused"])
        self.assertIsNone(first_breach(self.planner.airspace, legs))
        self.assertEqual((legs[0]["lat"], legs[0]["lon"]), self.start)
        self.assertEqual((legs[-1]["lat"], legs[-1]["lon"]), self.goal)

    def test_pick_side_sticks_to_the_previous_side_when_it_is_open(self):
        near_left = {"left": {"compass": "south", "metres": 80, "point": (0, 0)},
                     "right": {"compass": "north", "metres": 80, "point": (0, 0)}}
        self.assertEqual(ModelDrafter.pick_side(near_left, None), "left")
        self.assertEqual(ModelDrafter.pick_side(near_left, "right"), "right")
        far_right = {"left": {"compass": "south", "metres": 80, "point": (0, 0)},
                     "right": {"compass": "north", "metres": 400, "point": (0, 0)}}
        # Switches when it is more than twice as far
        self.assertEqual(ModelDrafter.pick_side(far_right, "right"), "left")
        blocked = {"left": {"compass": "south", "metres": None, "point": None},
                   "right": {"compass": "north", "metres": None, "point": None}}
        self.assertIsNone(ModelDrafter.pick_side(blocked, "left"))


class RecordedRepliesTest(unittest.TestCase):
    """Recorded answers from a real Nemotron. The form must pass; the judge may or may not.

    A model-drawn route failing the judge is not a fault but what this design expects. What the
    test checks is that the answer gets through the code's checks to the judge, and that what
    passed really passes.
    """

    def setUp(self):
        self.records = [r for r in load_fixtures() if r.get("kind") == "draft"]
        if not self.records:
            self.skipTest(f"no recorded drafts ({FIXTURE_DIR})")
        self.planner = _fleet_planner()
        self.bbox = _bbox()

    def test_every_recorded_draft_parses_as_a_form(self):
        from shared.llm.client import parse_json_object

        for record in self.records:
            with self.subTest(file=record["_file"]):
                form = parse_json_object(record["text"])
                self.assertIsInstance(form, dict)
                self.assertIsInstance(form.get("legs"), list)

    def test_accepted_recordings_really_pass_the_judge_and_refused_ones_really_fail(self):
        seen_pass = False
        for record in self.records:
            start, goal = tuple(record["start"]), tuple(record["goal"])
            llm = FixtureLlm(records=[record])
            drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
            legs = drafter.draft(start, goal, {})
            with self.subTest(file=record["_file"], expect=record.get("expect")):
                if record.get("expect") == "pass":
                    self.assertIsNotNone(legs, drafter.last_failures)
                    self.assertIsNone(first_breach(self.planner.airspace, legs))
                    seen_pass = True
                else:
                    self.assertIsNone(legs)
                    self.assertTrue(drafter.last_failures)
        self.assertTrue(seen_pass,
                        "the fixture test means something only if one recording passes")

    def test_the_fixture_says_whether_the_straight_line_was_refused(self):
        """Where the straight line passes, the real flow never uses a draft. The record has to say
        so."""
        for record in self.records:
            straight = self.planner.straight(tuple(record["start"]), tuple(record["goal"]))
            refused = first_breach(self.planner.airspace, straight) is not None
            with self.subTest(file=record["_file"], area=record.get("area")):
                self.assertEqual(bool(record.get("straight_refused")), refused)


class RuntimeNeverReadsTheDrafterTest(unittest.TestCase):
    def test_no_runtime_file_mentions_the_drafter(self):
        """Who drew it is kept in the ledger only and never enters the judgement. Pinned by grep."""
        reports = RUNTIME_DIR / "store" / "reports"
        offenders = []
        for path in RUNTIME_DIR.rglob("*.py"):
            # A report reads the ledger back to people, provenance included. That package is the
            # one deliberate exemption, and it is keyed to the directory, not to a file name.
            if reports in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in ("drafter", "draft_attempts"):
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
