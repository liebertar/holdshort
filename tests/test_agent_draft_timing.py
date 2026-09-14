"""The choice starts the moment the refusal arrives; the model draft is the last resort.

The screen shows a refusal for 5.6 s. The planner's candidates and the model's choice run on a
worker thread from the refusal, the display delay still elapses, and the result is collected
afterwards. The model draft used to start at the refusal; now it waits until every candidate has
been refused — each drone has one model slot, and a 60 s draft queued in front of a 2 s choice
would make the aircraft stand for the draft. Within its own budget, one draft in flight at a
time, never taking the agent down. Nothing about provenance changes: params.drafter still says
who drew the line, and the runtime still judges it.
"""

import threading
import time
import unittest
from unittest import mock

from drone.agent import loop as loop_module
from drone.agent.drafter import ModelDrafter
from drone.agent.loop import GuardedAgent
from drone.agent.planner import OperatorPlanner
from drone.agent.propose import Proposer
from shared.llm.client import TieredLlm
from shared.models import Proposal
from tests.fixture_llm import FixtureLlm

HERE = (40.70178, -73.96920)
GOAL = (40.70600, -73.98000)
REFUSAL = {"verdict": "denied", "policy_hit": "airspace", "code": "airspace",
           "reason": "leg 1 breaks the rules", "forbids": "bldg-x", "detail": {}}
APPROVAL = {"verdict": "auto", "policy_hit": None, "reason": "", "detail": {}}
DRAWN = [{"lat": HERE[0], "lon": HERE[1], "alt_m": 60.0},
         {"lat": 40.70400, "lon": -73.97500, "alt_m": 60.0},
         {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 60.0}]


class SleepingDrafter:
    """Sleeps in place of a model. Records when it was called and the deadline it was given."""

    name = "nano:sleeper"

    def __init__(self, sleep_s: float, timeout_s: float = 5.0, legs=None):
        self.sleep_s = sleep_s
        self.timeout_s = timeout_s
        self.legs = DRAWN if legs is None else legs
        self.calls = 0
        self.started_at: float | None = None
        self.deadlines: list = []
        self.finished = threading.Event()
        self.last_attempts = 0
        self.last_latency_ms = 0
        self.last_breach = None

    def draft(self, start, goal, context=None, deadline=None):
        self.calls += 1
        self.started_at = time.monotonic()
        self.deadlines.append(deadline)
        self.last_attempts = 1
        time.sleep(self.sleep_s)
        self.finished.set()
        return self.legs


class TimedPlanner(OperatorPlanner):
    """Planner that notes when it started drawing candidates (empty airspace — answers in ms)."""

    def __init__(self):
        super().__init__()
        self.asked_at: list[float] = []

    def candidates(self, start, goal, context=None, budget_s=None):
        self.asked_at.append(time.monotonic())
        return super().candidates(start, goal, context, budget_s)


class FakeRuntime:
    """Stand-in for post_json. Replies in order and records when each filing arrived."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.filings: list[tuple[float, dict]] = []

    def __call__(self, url, payload, timeout=20.0, headers=None):
        self.filings.append((time.monotonic(), payload))
        return self.answers.pop(0) if self.answers else APPROVAL


def _agent(drafter, redraw_s: float) -> GuardedAgent:
    llm = TieredLlm(base_url="", models={}, timeout_s=1.0, request_extra={}, record_dir="")
    agent = GuardedAgent("drone-t", "http://runtime.test", Proposer(llm), llm)
    agent.planner = TimedPlanner()  # empty airspace: one candidate, the straight (shortest) line
    agent.drafter = drafter
    agent.redraw_s = redraw_s
    return agent


def _proposal() -> Proposal:
    return Proposal(asset_id="drone-t", action="fly_route", cost_usd=12.0,
                    blast_radius="schedule", rationale="test", params={})


TELEMETRY = {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0, "job_lat": GOAL[0],
             "job_lon": GOAL[1]}


def _file(agent: GuardedAgent, runtime: FakeRuntime, telemetry: dict | None = None):
    """post_json goes to the stand-in runtime, /state is empty, and the later position check
    reads the given telemetry."""
    fresh = dict(TELEMETRY if telemetry is None else telemetry)

    def get(url, **kwargs):
        return {} if url.endswith("/state") else fresh

    with mock.patch.object(loop_module, "post_json", runtime), \
            mock.patch.object(loop_module, "get_json", get):
        return agent._file_with_route(_proposal(), dict(TELEMETRY))


def _shortest(agent: GuardedAgent) -> list[dict]:
    return agent.planner.candidates(HERE, GOAL)[0]["legs"]


class ChoiceAtRefusalTimeTest(unittest.TestCase):
    def test_the_choice_starts_at_the_refusal_and_the_display_delay_still_elapses(self):
        drafter = SleepingDrafter(sleep_s=0.05, timeout_s=5.0)
        agent = _agent(drafter, redraw_s=0.4)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime)
        self.assertEqual(decision["verdict"], "auto")
        refused_at, straight = runtime.filings[0]
        redrawn_at, redrawn = runtime.filings[1]
        self.assertEqual(straight["params"]["drafter"], "straight")
        # Candidate drawing began right after the refusal — not after waiting 5.6 s (0.4 s here)
        self.assertLess(agent.planner.asked_at[0] - refused_at, 0.1)
        # Yet the time the screen shows the refusal still elapsed in full
        self.assertGreaterEqual(redrawn_at - refused_at, 0.4)
        self.assertEqual(redrawn["params"]["drafter"], "astar")      # no model, so the rules chose
        self.assertEqual(redrawn["params"]["route_choice"]["path"], "rules")
        self.assertEqual(redrawn["params"]["legs"], _shortest(agent))
        self.assertEqual(drafter.calls, 0, "no draft is asked for when a candidate passes")
        self.assertFalse(agent.draft_in_flight)

    def test_the_draft_waits_until_every_candidate_is_refused(self):
        drafter = SleepingDrafter(sleep_s=0.05, timeout_s=5.0)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        decision = _file(agent, runtime)
        self.assertEqual(decision["verdict"], "auto")
        candidate_at, _ = runtime.filings[1]
        drafted_at, drafted = runtime.filings[2]
        self.assertGreaterEqual(drafter.started_at, candidate_at,
                                "asks only once the candidate is refused")
        self.assertEqual(drafted["params"]["drafter"], "nano:sleeper")
        self.assertEqual(drafted["params"]["draft_attempts"], 1)
        self.assertEqual(drafted["params"]["legs"], DRAWN)
        # The deadline is the draft's start time plus one draft budget
        self.assertAlmostEqual(drafter.deadlines[0], drafter.started_at + 5.0, delta=0.1)
        self.assertFalse(agent.draft_in_flight)

    def test_a_slow_draft_that_finishes_inside_the_budget_is_used(self):
        drafter = SleepingDrafter(sleep_s=0.5, timeout_s=2.0)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        _file(agent, runtime)
        candidate_at, _ = runtime.filings[1]
        drafted_at, drafted = runtime.filings[2]
        self.assertGreaterEqual(drafted_at - candidate_at, 0.5)    # waited for the draft to finish
        self.assertLess(drafted_at - candidate_at, 1.5)
        self.assertEqual(drafted["params"]["drafter"], "nano:sleeper")
        self.assertFalse(agent.draft_in_flight)

    def test_a_draft_slower_than_its_budget_is_dropped_and_the_turn_ends(self):
        drafter = SleepingDrafter(sleep_s=1.0, timeout_s=0.4)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL)
        started = time.monotonic()
        decision = _file(agent, runtime)
        # Waited only up to the budget (0.4 s), not the 1 s the draft takes to finish.
        # The candidate was already refused, so this turn ends with that refusal and the next
        # turn files again from the start.
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual(decision["verdict"], "denied")
        self.assertEqual(len(runtime.filings), 2)
        self.assertTrue(agent.draft_in_flight)                     # still pending on the server
        self.assertTrue(drafter.finished.wait(2.0))
        agent._draft.future.result(timeout=1.0)
        self.assertFalse(agent.draft_in_flight)                    # clear once it finishes

    def test_only_one_draft_is_ever_in_flight_and_the_pool_does_not_grow(self):
        drafter = SleepingDrafter(sleep_s=0.6, timeout_s=0.2)
        agent = _agent(drafter, redraw_s=0.05)
        _file(agent, FakeRuntime(REFUSAL, REFUSAL))
        self.assertTrue(agent.draft_in_flight)
        # A new refusal while the last draft is still pending: no second ask
        second = FakeRuntime(REFUSAL, REFUSAL)
        _file(agent, second)
        self.assertEqual(drafter.calls, 1)
        self.assertEqual(len(second.filings), 2)
        self.assertTrue(drafter.finished.wait(2.0))
        agent._draft.future.result(timeout=1.0)
        # A refusal after it finished asks again. There is one thread and no new one appears.
        drafter.sleep_s = 0.01
        third = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        _file(agent, third)
        self.assertEqual(drafter.calls, 2)
        self.assertEqual(third.filings[2][1]["params"]["drafter"], "nano:sleeper")
        self.assertFalse(agent.draft_in_flight)
        # The worker thread is a daemon and finished ones don't linger — so Ctrl-C never waits
        # on a draft still pending on the server
        alive = [th for th in threading.enumerate() if th.name == "draft-drone-t"]
        self.assertLessEqual(len(alive), 1)
        self.assertTrue(all(th.daemon for th in threading.enumerate()
                            if th.name.startswith("draft-")))

    def test_a_traffic_refusal_never_starts_a_draft(self):
        """After a crossing refusal the model is not asked — it knows nothing of other aircraft."""
        drafter = SleepingDrafter(sleep_s=0.01)
        agent = _agent(drafter, redraw_s=0.05)
        traffic = {**REFUSAL, "policy_hit": "traffic", "code": "traffic",
                   "detail": {"blocked_asset": "drone-02", "blocked_until_tick": 900}}
        # Straight → crossing, +30m → crossing, delay → crossing (same tick, so the ladder ends)
        # → candidate (A*) → approval
        runtime = FakeRuntime(traffic, traffic, traffic, APPROVAL)
        _file(agent, runtime)
        self.assertEqual(drafter.calls, 0)
        self.assertEqual(runtime.filings[-1][1]["params"]["drafter"], "astar")
        self.assertFalse(agent.draft_in_flight)

    def test_a_crashing_drafter_does_not_take_the_agent_down(self):
        class Crashing(SleepingDrafter):
            def draft(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("boom")

        drafter = Crashing(sleep_s=0.0)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL)
        decision = _file(agent, runtime)
        self.assertEqual(drafter.calls, 1)
        self.assertEqual(decision["verdict"], "denied")
        self.assertEqual(len(runtime.filings), 2)
        self.assertFalse(agent.draft_in_flight)


class MovedWhileChoosingTest(unittest.TestCase):
    """The aircraft moved while the candidate was being chosen. The runtime refuses a route
    whose first point is far from where the aircraft is."""

    def test_a_small_move_re_anchors_the_first_leg_and_keeps_the_candidate(self):
        agent = _agent(SleepingDrafter(sleep_s=0.05), redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        nudged = {**TELEMETRY, "lat": HERE[0] + 0.0002}  # 22 m off, inside the corridor half-width
        _file(agent, runtime, telemetry=nudged)
        redrawn = runtime.filings[1][1]
        self.assertEqual(redrawn["params"]["drafter"], "astar")
        self.assertEqual((redrawn["params"]["legs"][0]["lat"], redrawn["params"]["legs"][0]["lon"]),
                         (round(nudged["lat"], 6), HERE[1]))
        self.assertEqual(redrawn["params"]["legs"][1:], _shortest(agent)[1:])

    def test_taking_off_meanwhile_drops_the_ground_route(self):
        agent = _agent(SleepingDrafter(sleep_s=0.05), redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime, telemetry={**TELEMETRY, "alt_m": 40.0})
        self.assertEqual(decision["verdict"], "denied",
                         "gives up this turn and files from the air next turn")
        self.assertEqual(len(runtime.filings), 1)

    def test_a_big_move_redraws_from_the_new_spot(self):
        agent = _agent(SleepingDrafter(sleep_s=0.05), redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        far = {**TELEMETRY, "lat": HERE[0] + 0.002}                # 220 m off
        _file(agent, runtime, telemetry=far)
        redrawn = runtime.filings[1][1]
        self.assertEqual(redrawn["params"]["legs"][0]["lat"], round(far["lat"], 6))
        self.assertNotEqual(redrawn["params"]["legs"][1:], _shortest(agent)[1:])
        self.assertNotIn("route_choice", redrawn["params"],
                         "the candidate from the old spot was dropped")


class BackoffStillWorksTest(unittest.TestCase):
    """No answer from the server means no draft asks for a while — still so now that the draft
    is the last resort."""

    def test_a_no_reply_sets_the_backoff_and_the_next_refusal_is_not_asked(self):
        llm = FixtureLlm(records=[])                       # None, whatever is asked
        planner = TimedPlanner()
        drafter = ModelDrafter(llm, planner, timeout_s=5.0, backoff_s=30.0)
        self.assertTrue(drafter.enabled)
        agent = _agent(drafter, redraw_s=0.05)
        agent.planner = planner
        first = FakeRuntime(REFUSAL, REFUSAL)
        _file(agent, first)
        self.assertEqual(len(first.filings), 2)            # straight, candidate; draft unanswered
        self.assertEqual(len(llm.asked), 1)
        self.assertGreater(drafter.skip_until, time.monotonic())
        self.assertFalse(agent.draft_in_flight)
        second = FakeRuntime(REFUSAL, REFUSAL)
        _file(agent, second)
        self.assertEqual(len(llm.asked), 1)                # backed off
        self.assertIn("skipped", drafter.last_failures[0])


if __name__ == "__main__":
    unittest.main()
