"""The planner draws the candidates, the aircraft's own model chooses, the runtime judges.

Checked here, all offline:
- candidates: at most three, legal by construction (our copy's first_breach), distinct (no two
  within 100 m of each other everywhere), with the promised fields; (a) is today's A* route.
- tool calling in the client: tool_calls read whether arguments are a JSON string or an object;
  a server without tools (400) is remembered; 'required' falls back to 'auto' once; plain text
  is a reply without a tool call.
- the chooser: the tools path; JSON when the server has no tools or the model answers in prose;
  rules when there is no model, no answer, or an id that is not on the list — and it is counted.
- what the agent files: params.route_choice (exact shape), params.drafter, and params.model_trace
  for a model-written form, a rules form and a failed last-resort draft.
- recorded replies from the local Nemotron (tests/fixtures/llm/choices_nano.json) replay offline.
- the runtime never reads any of it.
"""

import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from drone.agent import loop as loop_module
from drone.agent.chooser import (
    CHOICE_TOOL,
    REASON_CHARS,
    RouteChooser,
    choice_brief,
    keep_clear_from_state,
    rule_choice,
    situation_from_state,
)
from drone.agent.detect import Concern
from drone.agent.loop import GuardedAgent
from drone.agent.planner import OperatorPlanner
from drone.agent.propose import Proposer
from drone.agent.trace import TRACE_BYTES, choice_part, form_part, model_trace, route_part
from shared.geo import first_breach
from shared.llm import client as client_module
from shared.llm.client import LlmReply, LlmTier, TieredLlm
from shared.models import Proposal
from shared.route import keep_clear_shapes, route_samples, same_route
from tests.fixture_llm import FIXTURE_DIR, FixtureLlm, load_fixtures

RUNTIME_DIR = pathlib.Path(__file__).resolve().parent.parent / "backend"
CHOICE_FIXTURES = FIXTURE_DIR / "choices_nano.json"

# 2.2 km north through empty airspace. Another aircraft's cleared corridor crosses the middle
# east to west.
HERE = (40.70000, -73.98000)
GOAL = (40.72000, -73.98000)
CROSSING = [{"lat": 40.71000, "lon": -73.98300, "alt_m": 90.0},
            {"lat": 40.71000, "lon": -73.97700, "alt_m": 90.0}]
STATE = {
    "tick": 812,
    "intents": [{"asset": "drone-02", "state": "activated", "from_tick": 800, "to_tick": 1200,
                 "proposal_id": "p_other"},
                {"asset": "drone-t", "state": "accepted", "from_tick": 0, "to_tick": 10,
                 "proposal_id": "p_mine"}],
    "ledger": [{"proposal": {"id": "p_other", "params": {"legs": CROSSING}}, "outcome": "done"},
               {"proposal": {"id": "p_mine", "params": {"legs": CROSSING}}, "outcome": "done"}],
    "notices": [{"id": "notam-x", "name": "NOTAM ZONE", "applied": True, "until_tick": 900,
                 "polygon": [[40.75, -73.99], [40.75, -73.98], [40.76, -73.98]]},
                {"id": "held-x", "name": "HELD", "applied": False, "polygon": [[1, 1], [1, 2]]}],
    "incidents": [{"id": "fire-x", "centre": [40.73, -73.97], "radius_m": 150, "applied": True}],
    "weather": {"hold": None},
}
TELEMETRY = {"id": "drone-t", "model": "dv-x500", "lat": HERE[0], "lon": HERE[1], "alt_m": 0.0,
             "job": "Morningside Park", "job_lat": GOAL[0], "job_lon": GOAL[1], "battery": 66.0,
             "stops_left": 2, "state": "loading"}
REFUSAL = {"verdict": "denied", "policy_hit": "airspace", "code": "airspace",
           "reason": "leg 1 breaks the rules", "forbids": "bldg-x", "detail": {}}
APPROVAL = {"verdict": "auto", "policy_hit": None, "reason": "", "detail": {}}
CANDIDATE_KEYS = ["id", "label", "legs", "length_m", "max_alt_m", "min_alt_m", "reason_tags"]


def _call(arguments: dict, name: str = CHOICE_TOOL) -> dict:
    return {"name": name, "arguments": arguments}


class ScriptedToolLlm(TieredLlm):
    """Hands out tool replies and JSON replies in order, and records what was asked.

    One tool reply is a {"name", "arguments"} dict (a tool call), a string (a prose-only
    answer), None (the server could not answer), or "unsupported" (the server knows no
    tools — 400).
    """

    def __init__(self, tool_replies=(), json_replies=(), model: str = "scripted-nano"):
        super().__init__(base_url="http://scripted", models={"nano": model}, timeout_s=1.0,
                         request_extra={}, record_dir="")
        self.tool_replies = list(tool_replies)
        self.json_replies = list(json_replies)
        self.tool_asks: list[dict] = []
        self.json_asks: list[str] = []

    def ask_tools(self, tier, system, user, tools, tool_choice="required", max_tokens=200,
                  timeout_s=None):
        self.tool_asks.append({"user": user, "tools": tools, "system": system})
        item = self.tool_replies.pop(0) if self.tool_replies else None
        if item == "unsupported":
            self._tools_ok = False
            return None
        if item is None:
            self.unreachable_at = time.monotonic()
            self.account(tier, None, 0)
            return None
        if isinstance(item, str):
            reply = LlmReply(text=item, model=self.model_for(tier), latency_ms=700)
        else:
            reply = LlmReply(text="", model=self.model_for(tier), latency_ms=700,
                             via="tool_calls",
                             tool_calls=[{"name": item["name"], "arguments": item["arguments"],
                                          "raw": json.dumps(item["arguments"])}])
        self.account(tier, reply, 700)
        return reply

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        self.json_asks.append(user)
        text = self.json_replies.pop(0) if self.json_replies else None
        if text is None:
            self.account(tier, None, 0)
            return None
        reply = LlmReply(text=text, model=self.model_for(tier), latency_ms=500)
        self.account(tier, reply, 500)
        return reply


class FakeRuntime:
    """Stand-in for post_json. Hands out answers in order and records the filings."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.filings: list[dict] = []

    def __call__(self, url, payload, timeout=20.0, headers=None):
        self.filings.append(payload)
        return self.answers.pop(0) if self.answers else APPROVAL


class StubDrafter:
    """Stand-in for the model drafter. Returns fixed legs (or None) and its record."""

    name = "nano:stub"

    def __init__(self, legs=None, breach=None, attempts=1, latency_ms=900):
        self.legs, self.breach = legs, breach
        self.attempts, self.latency_ms = attempts, latency_ms
        self.timeout_s = 5.0
        self.calls = 0
        self.last_attempts = self.last_latency_ms = 0
        self.last_breach = None

    def draft(self, start, goal, context=None, deadline=None):
        self.calls += 1
        self.last_attempts, self.last_latency_ms = self.attempts, self.latency_ms
        self.last_breach = self.breach
        return self.legs


class NoRoutePlanner(OperatorPlanner):
    """A copy with no legal route at all."""

    def candidates(self, start, goal, context=None, budget_s=None):
        return []

    def draw(self, start, goal):
        return None


def _agent(llm: TieredLlm, planner: OperatorPlanner | None = None, drafter=None) -> GuardedAgent:
    agent = GuardedAgent("drone-t", "http://runtime.test", Proposer(llm), llm)
    agent.planner = planner or OperatorPlanner()        # empty airspace: answers in milliseconds
    agent.drafter = drafter
    agent.redraw_s = 0.01
    return agent


def _proposal() -> Proposal:
    return Proposal(asset_id="drone-t", action="fly_route", cost_usd=12.0,
                    blast_radius="schedule", rationale="test", params={})


def _file(agent: GuardedAgent, runtime: FakeRuntime, state: dict | None = None,
          form: dict | None = None):
    """post_json goes to the stand-in runtime, /state to the given state, telemetry as is."""
    def get(url, timeout=5.0):
        return (state or {}) if url.endswith("/state") else dict(TELEMETRY)

    with mock.patch.object(loop_module, "post_json", runtime), \
            mock.patch.object(loop_module, "get_json", get):
        return agent._file_with_route(_proposal(), dict(TELEMETRY), form)


def _candidates(ids=("a", "b", "c"), a_tags=("detour",)) -> list[dict]:
    rows = {"a": ("shortest", 9800, 110.0), "b": ("lowest altitude", 11200, 72.0),
            "c": ("clear of traffic", 10400, 96.0)}
    return [{"id": i, "label": rows[i][0], "legs": [{"lat": 0, "lon": 0, "alt_m": 70}] * 4,
             "length_m": rows[i][1], "max_alt_m": rows[i][2], "min_alt_m": 70.0,
             "reason_tags": list(a_tags) if i == "a" else [i]} for i in ids]


# ---------- Candidates ----------


class CandidatesInOpenAirTest(unittest.TestCase):
    """Empty airspace: only what sets the candidates apart (no buildings)."""

    def setUp(self):
        self.planner = OperatorPlanner()
        self.context = {"traffic": [{"id": "drone-02", "legs": CROSSING}]}

    def test_the_shortest_is_todays_route_and_comes_first(self):
        found = self.planner.candidates(HERE, GOAL, self.context)
        today = [leg.to_dict() for leg in self.planner.router.plan(HERE, GOAL).legs]
        self.assertEqual(found[0]["id"], "a")
        self.assertEqual(found[0]["legs"], today)

    def test_the_clear_candidate_keeps_its_distance_from_the_other_corridor(self):
        found = {c["id"]: c for c in self.planner.candidates(HERE, GOAL, self.context)}
        self.assertIn("c", found)
        corridor = keep_clear_shapes(self.context)[0]

        def closest(candidate):
            return min(corridor.distance_m(*p) for p in route_samples(candidate["legs"]))

        self.assertLess(closest(found["a"]), 30.0, "the shortest cuts across the corridor")
        self.assertGreater(closest(found["c"]), 200.0, "(c) swings wide of the corridor")
        self.assertIn("near-traffic:drone-02", found["a"]["reason_tags"])
        self.assertIn("clear-of-traffic", found["c"]["reason_tags"])

    def test_nothing_to_avoid_and_nothing_lower_means_one_candidate(self):
        """In empty airspace (b) is the same route as (a) and drops out; with nothing to
        avoid there is no (c) either."""
        self.assertEqual([c["id"] for c in self.planner.candidates(HERE, GOAL, None)], ["a"])

    def test_a_zero_budget_leaves_only_the_shortest(self):
        found = self.planner.candidates(HERE, GOAL, self.context, budget_s=0)
        self.assertEqual([c["id"] for c in found], ["a"])

    def test_every_candidate_has_the_promised_fields(self):
        for candidate in self.planner.candidates(HERE, GOAL, self.context):
            self.assertEqual(sorted(candidate), CANDIDATE_KEYS)
            self.assertLessEqual(candidate["min_alt_m"], candidate["max_alt_m"])
            self.assertGreater(candidate["length_m"], 0)

    def test_unreadable_context_is_skipped_not_fatal(self):
        context = {"traffic": [{"id": "x", "legs": [{"lat": "?"}]}, "junk"],
                   "keepouts": [{"id": "y"}, {"id": "z", "polygon": [["a", "b"]]}]}
        self.assertEqual(keep_clear_shapes(context), [])
        self.assertEqual([c["id"] for c in self.planner.candidates(HERE, GOAL, context)], ["a"])


class CandidatesOverManhattanTest(unittest.TestCase):
    """Real airspace (34,000 buildings, the FAA grid): every candidate passes judgement and
    no two are alike."""

    @classmethod
    def setUpClass(cls):
        from sim.world import LANDING_AREAS, Simulation, seat_of, to_latlon
        cls.planner = OperatorPlanner()
        cls.planner.load(Simulation(seed=7).worlds["guarded"].snapshot(0, volumes=True)["volumes"])
        cls.start = tuple(round(v, 6) for v in to_latlon(*seat_of(0)))
        union = next(a for a in LANDING_AREAS if a["name"] == "Union Square")
        cls.goal = (union["lat"], union["lon"])
        straight = cls.planner.straight(cls.start, cls.goal)
        middle = ((cls.start[0] + cls.goal[0]) / 2, (cls.start[1] + cls.goal[1]) / 2)
        cls.context = {"traffic": [{"id": "drone-02", "legs": straight}],
                       "keepouts": [{"id": "fire", "lat": middle[0], "lon": middle[1],
                                     "radius_m": 150}]}
        cls.found = cls.planner.candidates(cls.start, cls.goal, cls.context, budget_s=20)

    def test_every_candidate_passes_the_same_judge_the_runtime_uses(self):
        self.assertGreaterEqual(len(self.found), 1)
        for candidate in self.found:
            self.assertIsNone(first_breach(self.planner.airspace, candidate["legs"]),
                              candidate["id"])
            first, last = candidate["legs"][0], candidate["legs"][-1]
            self.assertAlmostEqual(first["lat"], self.start[0], places=5)
            self.assertAlmostEqual(last["lon"], self.goal[1], places=5)
            self.assertIsNone(self.planner.airspace.landing_breach(last["lat"], last["lon"]))

    def test_no_two_candidates_are_the_same_route(self):
        self.assertLessEqual(len(self.found), 3)
        for index, first in enumerate(self.found):
            for second in self.found[index + 1:]:
                self.assertFalse(same_route(first["legs"], second["legs"]),
                                 f"{first['id']} and {second['id']} are the same route")

    def test_there_is_more_than_one_route_to_choose_from(self):
        """On a real run this trip produced all three (4.2/4.3/4.5 km). With no options
        there is nothing to choose."""
        self.assertGreaterEqual(len(self.found), 2, self.planner.router.last_timings)


# ---------- Client: tool calling ----------


def completion(content=None, **message):
    return {"choices": [{"message": {"role": "assistant", "content": content, **message}}]}


def tool_completion(arguments, name=CHOICE_TOOL):
    return completion("", tool_calls=[{"id": "call_1", "type": "function",
                                       "function": {"name": name, "arguments": arguments}}])


class ToolCallingClientTest(unittest.TestCase):
    TOOLS = [{"type": "function", "function": {"name": CHOICE_TOOL, "parameters": {}}}]

    def _client(self, **kwargs) -> TieredLlm:
        return TieredLlm(base_url="http://model.test/v1", api_key="k",
                         models={"nano": "nano-id"}, timeout_s=3.0,
                         request_extra={"reasoning_effort": "none"},
                         record_dir=kwargs.get("record_dir", ""))

    def _capture(self, responses):
        calls = []

        def fake(url, payload, timeout=20.0, headers=None):
            calls.append(payload)
            return responses.pop(0)

        return calls, mock.patch.object(client_module, "post_json_status", fake)

    def test_string_arguments_are_read_as_json(self):
        llm = self._client()
        calls, patch = self._capture([(200, tool_completion('{"id": "b", "reason": "lower"}'))])
        with patch:
            reply = llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS)
        self.assertEqual(reply.via, "tool_calls")
        self.assertEqual(reply.tool_calls[0]["name"], CHOICE_TOOL)
        self.assertEqual(reply.tool_calls[0]["arguments"], {"id": "b", "reason": "lower"})
        self.assertEqual(calls[0]["tools"], self.TOOLS)
        self.assertEqual(calls[0]["tool_choice"], "required")
        self.assertEqual(calls[0]["reasoning_effort"], "none")
        self.assertNotIn("response_format", calls[0])
        self.assertEqual(llm.stats["nano"].ok, 1)

    def test_object_arguments_are_taken_as_they_are(self):
        llm = self._client()
        _, patch = self._capture([(200, tool_completion({"id": "c", "reason": "clear"}))])
        with patch:
            reply = llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS)
        self.assertEqual(reply.tool_calls[0]["arguments"], {"id": "c", "reason": "clear"})

    def test_garbage_arguments_are_none_not_a_crash(self):
        llm = self._client()
        _, patch = self._capture([(200, tool_completion("not json at all"))])
        with patch:
            reply = llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS)
        self.assertIsNone(reply.tool_calls[0]["arguments"])

    def test_plain_text_is_a_reply_without_a_tool_call(self):
        llm = self._client()
        _, patch = self._capture([(200, completion("I would pick b."))])
        with patch:
            reply = llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS)
        self.assertEqual(reply.text, "I would pick b.")
        self.assertEqual(reply.tool_calls, [])

    def test_required_refused_is_tried_once_as_auto(self):
        llm = self._client()
        calls, patch = self._capture([(400, None), (200, tool_completion('{"id": "a"}'))])
        with patch:
            reply = llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS)
        self.assertEqual([c["tool_choice"] for c in calls], ["required", "auto"])
        self.assertTrue(llm.tools_ok)
        self.assertEqual(reply.tool_calls[0]["arguments"], {"id": "a"})

    def test_a_server_without_tools_is_remembered_and_not_counted(self):
        llm = self._client()
        calls, patch = self._capture([(400, None), (400, None)])
        with patch:
            self.assertIsNone(llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS))
            self.assertIsNone(llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS))
        self.assertFalse(llm.tools_ok)
        self.assertEqual(len(calls), 2, "the second one is not even asked")
        self.assertEqual((llm.stats["nano"].ok, llm.stats["nano"].fallback), (0, 0))

    def test_a_timeout_is_counted_and_not_retried(self):
        llm = self._client()
        calls, patch = self._capture([(0, None)])
        with patch:
            self.assertIsNone(llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS))
        self.assertEqual(len(calls), 1)
        self.assertTrue(llm.tools_ok)
        self.assertEqual(llm.stats["nano"].fallback, 1)
        self.assertTrue(llm.unreachable_within(60))

    def test_the_record_keeps_the_tool_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            llm = self._client(record_dir=folder)
            _, patch = self._capture([(200, tool_completion('{"id": "b", "reason": "x"}'))])
            with patch:
                llm.ask_tools(LlmTier.NANO, "s", "u", self.TOOLS)
            saved = json.loads(next(pathlib.Path(folder).glob("*.json")).read_text())
        self.assertEqual(saved["tool_calls"][0]["arguments"], {"id": "b", "reason": "x"})
        self.assertEqual(saved["tools"], [CHOICE_TOOL])


# ---------- Choosing ----------


class ChooserTest(unittest.TestCase):
    def test_the_tools_path(self):
        llm = ScriptedToolLlm([_call({"id": "b", "reason": "Lower cruise over the river."})])
        chooser = RouteChooser(llm)
        choice = chooser.choose(_candidates(), {})
        self.assertEqual((choice.chosen, choice.path, choice.model),
                         ("b", "tools", "scripted-nano"))
        self.assertEqual(choice.reason, "Lower cruise over the river.")
        self.assertEqual(chooser.counts["tools"], 1)
        self.assertEqual(chooser.counts["differs_from_rule"], 1)
        tool = llm.tool_asks[0]["tools"][0]["function"]
        self.assertEqual(tool["name"], CHOICE_TOOL)
        self.assertEqual(tool["parameters"]["properties"]["id"]["enum"], ["a", "b", "c"])
        self.assertEqual(tool["parameters"]["required"], ["id", "reason"])

    def test_the_reason_is_one_sentence_long_at_most(self):
        llm = ScriptedToolLlm([_call({"id": "a", "reason": "word " * 200})])
        choice = RouteChooser(llm).choose(_candidates(), {})
        self.assertEqual(len(choice.reason), REASON_CHARS)

    def test_an_id_written_loosely_is_still_that_id(self):
        llm = ScriptedToolLlm([_call({"id": " (C) ", "reason": "clear"})])
        self.assertEqual(RouteChooser(llm).choose(_candidates(), {}).chosen, "c")

    def test_an_id_that_is_not_on_the_list_is_discarded_and_counted(self):
        llm = ScriptedToolLlm([_call({"id": "d", "reason": "made up"})])
        chooser = RouteChooser(llm)
        choice = chooser.choose(_candidates(), {})
        self.assertEqual((choice.chosen, choice.path), ("a", "rules"))
        self.assertEqual(choice.fallback_reason, "invalid id")
        self.assertEqual(chooser.counts["invalid"], 1)
        self.assertEqual((llm.stats["nano"].ok, llm.stats["nano"].fallback), (0, 1))
        self.assertEqual(llm.json_asks, [], "a wrong id is not asked again; the rules pick")

    def test_a_call_to_some_other_tool_is_not_a_choice(self):
        llm = ScriptedToolLlm([_call({"id": "b"}, name="fly_there")])
        chooser = RouteChooser(llm)
        choice = chooser.choose(_candidates(), {})
        self.assertEqual(choice.path, "rules")
        self.assertEqual(chooser.counts["invalid"], 1)

    def test_prose_instead_of_a_tool_call_asks_again_in_json(self):
        llm = ScriptedToolLlm(["I think c is best."],
                              ['{"id": "c", "reason": "clear of drone-02"}'])
        chooser = RouteChooser(llm)
        choice = chooser.choose(_candidates(), {})
        self.assertEqual((choice.chosen, choice.path), ("c", "json"))
        self.assertEqual(chooser.counts["plain_text"], 1)
        self.assertEqual(len(llm.json_asks), 1)

    def test_a_server_without_tools_is_asked_in_json_and_not_asked_tools_again(self):
        llm = ScriptedToolLlm(["unsupported"], ['{"id": "b", "reason": "low"}',
                                                '{"id": "a", "reason": "short"}'])
        chooser = RouteChooser(llm)
        self.assertEqual(chooser.choose(_candidates(), {}).path, "json")
        self.assertEqual(chooser.choose(_candidates(), {}).path, "json")
        self.assertEqual(len(llm.tool_asks), 1)
        self.assertEqual(chooser.counts["unsupported"], 1)

    def test_prose_twice_in_a_row_stops_offering_tools(self):
        llm = ScriptedToolLlm(["b", "b"], ['{"id": "b", "reason": "x"}'] * 3)
        chooser = RouteChooser(llm)
        for _ in range(3):
            chooser.choose(_candidates(), {})
        self.assertEqual(len(llm.tool_asks), 2)
        self.assertEqual(len(llm.json_asks), 3)

    def test_json_garbage_falls_back_to_rules(self):
        llm = ScriptedToolLlm(["unsupported"], ["the second one"])
        choice = RouteChooser(llm).choose(_candidates(), {})
        self.assertEqual((choice.chosen, choice.path, choice.fallback_reason),
                         ("a", "rules", "invalid id"))

    def test_no_answer_goes_to_rules_without_a_second_ask(self):
        llm = ScriptedToolLlm([None])
        chooser = RouteChooser(llm)
        choice = chooser.choose(_candidates(), {})
        self.assertEqual((choice.path, choice.fallback_reason), ("rules", "timeout"))
        self.assertEqual(llm.json_asks, [])

    def test_without_a_model_the_rules_pick_a_or_c_when_traffic_is_there(self):
        chooser = RouteChooser(TieredLlm(base_url="", models={}, request_extra={},
                                         record_dir=""))
        quiet = chooser.choose(_candidates(), {})
        self.assertEqual((quiet.chosen, quiet.path, quiet.fallback_reason),
                         ("a", "rules", "no model"))
        self.assertEqual(chooser.choose(_candidates(), {"traffic_refusal": True}).chosen, "c")
        crowded = _candidates(a_tags=("detour", "near-traffic:drone-02"))
        self.assertEqual(chooser.choose(crowded, {}).chosen, "c")
        self.assertEqual(chooser.choose(_candidates(("a", "b")), {"traffic_refusal": True})
                         .chosen, "a", "(a) when there is no (c)")

    def test_one_candidate_is_not_worth_a_question(self):
        llm = ScriptedToolLlm([_call({"id": "a", "reason": "x"})])
        choice = RouteChooser(llm).choose(_candidates(("a",)), {})
        self.assertEqual((choice.path, choice.fallback_reason), ("rules", "one candidate"))
        self.assertEqual(llm.tool_asks, [])

    def test_rule_choice_on_nothing(self):
        self.assertEqual(rule_choice([], {}).chosen, "")


class SituationTest(unittest.TestCase):
    def test_the_brief_names_the_aircraft_the_refusal_and_every_candidate(self):
        situation = situation_from_state(STATE, TELEMETRY, REFUSAL,
                                         "has a delivery to Morningside Park, no cleared route",
                                         "drone-t")
        brief = choice_brief(_candidates(), situation)
        for words in ("drone-t", "on the ground", "66%", "2 stop(s) left", "Morningside Park",
                      "airspace: bldg-x", "tick 812", "no weather hold", "NOTAM ZONE",
                      "drone-02 ticks 800-1200 (flying)", "a) shortest", "b) lowest altitude",
                      "c) clear of traffic", "9.8 km", CHOICE_TOOL):
            self.assertIn(words, brief)
        self.assertNotIn("HELD", brief, "a notice blocks nothing until a person confirms it")
        self.assertNotIn("drone-t ticks", brief, "its own intent is not someone else's window")

    def test_a_weather_hold_and_a_traffic_refusal_read_as_words(self):
        state = {**STATE, "weather": {"hold": {"until_tick": 2700, "reason": "gusts 14 m/s > 12"}}}
        traffic = {"policy_hit": "traffic",
                   "detail": {"blocked_asset": "drone-02", "blocked_until_tick": 1200}}
        situation = situation_from_state(state, TELEMETRY, traffic, "", "drone-t")
        self.assertTrue(situation["traffic_refusal"])
        brief = choice_brief(_candidates(), situation)
        self.assertIn("hold until tick 2700 (gusts 14 m/s > 12)", brief)
        self.assertIn("overlaps drone-02's corridor until tick 1200", brief)

    def test_what_to_keep_clear_of_comes_from_other_aircraft_and_applied_rules_only(self):
        context = keep_clear_from_state(STATE, "drone-t")
        self.assertEqual(context["traffic"], [{"id": "drone-02", "legs": CROSSING}])
        ids = [item["id"] for item in context["keepouts"]]
        self.assertEqual(ids, ["notam-x", "fire-x"])
        self.assertEqual(keep_clear_from_state({}, "drone-t"), {"traffic": [], "keepouts": []})


# ---------- What goes on the filing ----------


class FiledChoiceTest(unittest.TestCase):
    def test_the_models_choice_is_filed_with_route_choice_in_its_exact_shape(self):
        llm = ScriptedToolLlm([_call({"id": "c", "reason": "Keeps clear of drone-02's corridor."})])
        agent = _agent(llm)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime, STATE)
        self.assertEqual(decision["verdict"], "auto")
        straight, chosen = runtime.filings[0]["params"], runtime.filings[1]["params"]
        self.assertEqual(straight["drafter"], "straight")
        self.assertNotIn("route_choice", straight)
        self.assertEqual(chosen["drafter"], "choice:scripted-nano")
        choice = chosen["route_choice"]
        self.assertEqual(sorted(choice), ["candidates", "chosen", "model", "path", "reason"])
        self.assertEqual((choice["chosen"], choice["path"], choice["model"]),
                         ("c", "tools", "scripted-nano"))
        self.assertEqual(choice["reason"], "Keeps clear of drone-02's corridor.")
        self.assertEqual([row["id"] for row in choice["candidates"]], ["a", "c"])
        for row in choice["candidates"]:
            self.assertEqual(sorted(row), ["id", "label", "legs_count", "length_m", "max_alt_m",
                                           "min_alt_m", "reason_tags"])
        candidate_c = agent.planner.candidates(HERE, GOAL, keep_clear_from_state(STATE,
                                                                                 "drone-t"))[1]
        self.assertEqual(chosen["legs"], candidate_c["legs"])
        self.assertEqual(choice["candidates"][1]["legs_count"], len(candidate_c["legs"]))
        route = chosen["model_trace"]["route"]
        self.assertEqual((route["source"], route["choice"]["chosen"], route["draft"]),
                         ("choice", "c", None))
        self.assertIn("drone-02", llm.tool_asks[0]["user"])

    def test_without_a_model_the_rules_choose_and_the_filing_says_astar(self):
        llm = TieredLlm(base_url="", models={}, request_extra={}, record_dir="")
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        _file(_agent(llm), runtime, STATE)
        chosen = runtime.filings[1]["params"]
        self.assertEqual(chosen["drafter"], "astar")
        self.assertEqual((chosen["route_choice"]["path"], chosen["route_choice"]["model"],
                          chosen["route_choice"]["chosen"]), ("rules", "", "c"))
        self.assertEqual(chosen["model_trace"]["route"]["source"], "astar")

    def test_when_the_chosen_route_is_refused_the_next_candidate_goes_by_rules(self):
        llm = ScriptedToolLlm([_call({"id": "c", "reason": "clear"})])
        runtime = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        decision = _file(_agent(llm), runtime, STATE)
        self.assertEqual(decision["verdict"], "auto")
        second = runtime.filings[2]["params"]
        self.assertEqual(second["route_choice"]["chosen"], "a")
        self.assertEqual(second["route_choice"]["path"], "rules")
        self.assertIn("refused", second["route_choice"]["reason"])
        self.assertEqual(second["drafter"], "astar")

    def test_the_model_draft_is_asked_only_after_every_candidate_is_refused(self):
        drawn = [{"lat": HERE[0], "lon": HERE[1], "alt_m": 80.0},
                 {"lat": 40.71, "lon": -73.99, "alt_m": 80.0},
                 {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 80.0}]
        drafter = StubDrafter(legs=drawn, attempts=1, latency_ms=900)
        llm = ScriptedToolLlm([_call({"id": "a", "reason": "short"})])
        runtime = FakeRuntime(REFUSAL, REFUSAL, REFUSAL, APPROVAL)
        _file(_agent(llm, drafter=drafter), runtime, STATE)
        self.assertEqual(drafter.calls, 1)
        last = runtime.filings[3]["params"]
        self.assertEqual((last["drafter"], last["legs"], last["draft_attempts"]),
                         ("nano:stub", drawn, 1))
        self.assertNotIn("route_choice", last)
        route = last["model_trace"]["route"]
        self.assertEqual(route["source"], "draft")
        self.assertEqual(route["draft"], {"asked": True, "latency_ms": 900, "breach": None,
                                          "used": True})
        self.assertEqual(route["choice"]["chosen"], "a")

    def test_a_failed_draft_is_on_the_decline_it_led_to(self):
        drafter = StubDrafter(legs=None, breach="crossed bldg-t02452, roof 114 m", attempts=2,
                              latency_ms=1800)
        llm = TieredLlm(base_url="", models={}, request_extra={}, record_dir="")
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        _file(_agent(llm, planner=NoRoutePlanner(), drafter=drafter), runtime, STATE)
        declined = runtime.filings[1]
        self.assertEqual(declined["action"], "decline_job")
        self.assertEqual(declined["params"]["model_trace"]["route"], {
            "source": "astar", "choice": None,
            "draft": {"asked": True, "latency_ms": 1800,
                      "breach": "crossed bldg-t02452, roof 114 m", "used": False}})

    def test_every_filing_in_a_turn_carries_the_same_form_trace(self):
        form = form_part("scripted-nano", "has a delivery to Morningside Park, no cleared route",
                         "fly_route", "Morningside is due next.", 540, True, None)
        llm = ScriptedToolLlm([_call({"id": "c", "reason": "clear"})])
        runtime = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        _file(_agent(llm), runtime, STATE, form=form)
        self.assertEqual(len(runtime.filings), 3)
        for filing in runtime.filings:
            trace = filing["params"]["model_trace"]
            self.assertEqual(trace["form"], form)
            self.assertLessEqual(len(json.dumps(trace, ensure_ascii=False).encode()), TRACE_BYTES)


class FormTraceTest(unittest.TestCase):
    CONCERN = Concern("needs_route", "normal", "delivering to Morningside Park, battery 66%")

    def _write(self, llm) -> dict:
        proposer = Proposer(llm)
        proposer.write(self.CONCERN, TELEMETRY, "pad:launch", frozenset(), ("pad:launch",))
        return proposer.last_trace

    def test_a_model_written_form(self):
        class FormLlm(ScriptedToolLlm):
            def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
                reply = LlmReply(text='{"action": "fly_route", "pad": null, '
                                      '"rationale": "Morningside is due next."}',
                                 model="scripted-nano", latency_ms=540)
                self.account(tier, reply, 540)
                return reply

        trace = self._write(FormLlm())
        self.assertEqual(trace, {
            "model": "scripted-nano",
            "concern": "has a delivery to Morningside Park, no cleared route",
            "action": "fly_route", "rationale": "Morningside is due next.", "latency_ms": 540,
            "used": True, "fallback_reason": None})

    def test_rules_wrote_it_because_the_model_named_an_action_it_may_not_take(self):
        trace = self._write(ScriptedToolLlm(json_replies=['{"action": "charge"}']))
        self.assertEqual((trace["model"], trace["used"], trace["fallback_reason"]),
                         ("", False, "invalid action"))
        self.assertEqual(trace["action"], "fly_route")

    def test_rules_wrote_it_because_there_is_no_model(self):
        trace = self._write(TieredLlm(base_url="", models={}, request_extra={}, record_dir=""))
        self.assertEqual((trace["model"], trace["fallback_reason"]), ("", "no model"))

    def test_rules_wrote_it_because_the_server_did_not_answer(self):
        class Silent(ScriptedToolLlm):
            def ask(self, tier, system, user, max_tokens=400, json_object=False,
                    timeout_s=None):
                self.unreachable_at = time.monotonic()
                self.account(tier, None, 6000)
                return None

        trace = self._write(Silent())
        self.assertEqual((trace["model"], trace["fallback_reason"]), ("", "timeout"))

    def test_the_trace_stays_under_one_kilobyte_and_keeps_its_keys(self):
        form = form_part("m", "concern " * 200, "fly_route", "from here " * 400, 1, True, None)
        route = route_part("choice", choice_part(_candidates(), "c", "because " * 100),
                           {"asked": True, "latency_ms": 1, "breach": "x" * 900, "used": False})
        trace = model_trace(form, route)
        self.assertLessEqual(len(json.dumps(trace, ensure_ascii=False).encode()), TRACE_BYTES)
        self.assertEqual(sorted(trace["form"]), sorted(form))
        self.assertEqual(sorted(trace["route"]), ["choice", "draft", "source"])
        self.assertEqual(trace["route"]["choice"]["chosen"], "c")

    def test_an_unknown_route_source_is_a_bug_not_a_label(self):
        with self.assertRaises(ValueError):
            route_part("vibes")


class AirspaceRefreshTest(unittest.TestCase):
    """If the airspace copy cannot be fetched, the last copy stays. Swapping in an empty one
    would draw a city with no buildings."""

    VOLUME = {"id": "bldg-z", "name": "Z", "polygon": [[40.71, -73.981], [40.71, -73.979],
                                                       [40.711, -73.979], [40.711, -73.981]],
              "ceiling_m": 60.0, "clearance_m": 50.0, "rule": "forbidden"}

    def _step(self, agent, answer):
        telemetry = {"airspace_revision": 7, "state": "grounded"}   # no concerns: just a refetch

        def get(url, timeout=5.0):
            return answer if url.endswith("/airspace") else telemetry

        with mock.patch.object(loop_module, "get_json", get):
            agent.step()

    def test_a_failed_fetch_keeps_the_old_copy_and_asks_again_next_turn(self):
        agent = _agent(TieredLlm(base_url="", models={}, request_extra={}, record_dir=""))
        before = agent.planner
        before.load([self.VOLUME])
        self._step(agent, None)
        self.assertIs(agent.planner, before)
        self.assertIsNone(agent.airspace_revision, "no revision kept, so it refetches next turn")
        self._step(agent, {"volumes": []})
        self.assertIs(agent.planner, before)

    def test_a_good_fetch_swaps_the_copy_and_records_the_revision(self):
        agent = _agent(TieredLlm(base_url="", models={}, request_extra={}, record_dir=""))
        self._step(agent, {"volumes": [self.VOLUME], "pads": {}, "landing_areas": []})
        self.assertEqual(agent.airspace_revision, 7)
        self.assertIsNotNone(agent.planner.airspace.get("bldg-z"))


# ---------- Recorded replies ----------


class FixtureToolLlm(FixtureLlm):
    """Serves recorded tool replies (kind=choice) by needle. None when nothing matches."""

    def ask_tools(self, tier, system, user, tools, tool_choice="required", max_tokens=200,
                  timeout_s=None):
        self.asked.append((tier.value, user))
        for record in self.records:
            if record.get("kind") == "choice" and record["needle"] in user:
                self.served.append(record.get("_file", record["needle"]))
                reply = LlmReply(text=record.get("text") or "", model=record.get("model"),
                                 latency_ms=int(record.get("latency_ms") or 0),
                                 via="tool_calls" if record.get("tool_calls") else "content",
                                 tool_calls=list(record.get("tool_calls") or []))
                self.account(tier, reply, reply.latency_ms)
                return reply
        self.account(tier, None, 0)
        return None


class RecordedChoicesTest(unittest.TestCase):
    """Choices the local Nemotron (nemotron-3-nano:4b) really made on a live run, replayed
    offline."""

    @classmethod
    def setUpClass(cls):
        cls.records = [r for r in load_fixtures() if r.get("kind") == "choice"]

    def test_the_fixture_file_is_there_and_holds_tool_calls(self):
        self.assertTrue(CHOICE_FIXTURES.exists())
        self.assertGreaterEqual(len(self.records), 3)
        self.assertTrue(all(r.get("tool_calls") for r in self.records))

    def test_every_recorded_choice_names_one_of_its_candidates(self):
        for record in self.records:
            candidates = _candidates(tuple(record["candidate_ids"]))
            chooser = RouteChooser(FixtureToolLlm(records=[record], model=record["model"]))
            choice = chooser.choose(candidates, {"concern": record["needle"]})
            self.assertEqual(choice.path, "tools", record["_file"])
            self.assertIn(choice.chosen, record["candidate_ids"])
            self.assertEqual(choice.chosen, record["chosen"])
            self.assertLessEqual(len(choice.reason), REASON_CHARS)


class RuntimeNeverReadsTheChoiceTest(unittest.TestCase):
    def test_no_runtime_file_mentions_the_choice_or_the_trace(self):
        """The choice and the trace are operator-side labels. Once judgement starts reading
        them, the model has leaked into the judgement."""
        for path in RUNTIME_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in ("route_choice", "model_trace", CHOICE_TOOL):
                self.assertNotIn(word, text, f"{path.name} reads {word}")


if __name__ == "__main__":
    unittest.main()
