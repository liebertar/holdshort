"""One process per vehicle. It files requests. That is the whole of it.

The only address this process knows is the runtime's. It cannot reach an actuator: there is
no actuator client in this package, none in its container image, and in compose it is not
even on the network the vehicles live on.

The other wiring — an agent holding the actuator address — lives in the `drone.direct`
package, which is built into a different image.
"""

import math
import os
import threading
import time
import urllib.parse
from concurrent.futures import Future
from concurrent.futures import TimeoutError as DraftTimeout
from dataclasses import dataclass

from drone.agent.chooser import (
    Choice,
    Outcome,
    RouteChooser,
    keep_clear_from_state,
    route_choice_param,
    situation_from_state,
)
from drone.agent.detect import detect
from drone.agent.drafter import ModelDrafter, service_bbox
from drone.agent.planner import OperatorPlanner
from drone.agent.propose import Proposer
from drone.agent.trace import choice_part, draft_part, form_part, model_trace, route_part
from shared.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON, TRAFFIC_LATERAL_M
from shared.http import get_json, post_json
from shared.llm.client import LlmTier, TieredLlm
from shared.route import Router

FALLBACK_PADS = ["pad:launch"]
# How often (s) the agent re-announces itself to the runtime. The runtime drops an agent it
# hasn't heard from for AGENT_STALE_TICKS (default 600 ticks, 2 min at 0.2 s/tick) — once the
# process has died, a screen still showing its model name would be lying.
REGISTER_PERIOD_S = 30.0
# Seconds before re-announcing when registration wasn't accepted (no runtime yet, or 503
# because it hasn't received the world). Waiting 30 s leaves aircraft on screen without a model
# name for the first half minute.
REGISTER_RETRY_S = 3.0
# Delay from a refusal to the redraw. Equal to how long the screen shows a refusal
# (frontend/map-route.mjs stageLife('rejected'):
# draw 2.4 + judge 0.6 + red 1.6 + fade 1.0 = 5.6 s). It is the operator's time to read the
# refusal reason before redrawing, and it keeps the refusal and approval displays from
# overlapping in real time, so the simulator only has to wait one approval's worth
# (CLEARANCE_TICKS).
REDRAW_DELAY_S = 5.6
# Resolution ladder for crossing refusals. First raise the same route by this much (the other
# corridor spans ±25 m vertically, so 30 m clears it); failing that, delay departure until the
# other corridor is free. A delay is refiled at most this many times, once per new refusal
# naming a different tick — ticks only move forward, so it ends; after that comes an A* redraw
# → decline.
ALTITUDE_SHIFT_M = 30.0
MAX_DELAY_TRIES = 3
ROUTE_REFUSALS = ("airspace", "traffic")
# Longest wait (s) for drawing and choosing candidates. Just a safety line — the planner takes
# 40-50 s for a Manhattan run from an empty memo, usually under 1 s after that, and the model
# has a 10 s budget. Hitting it abandons this turn and refiles from scratch next turn.
CHOICE_WAIT_S = 300.0
# Time allowed to read runtime state (notices, weather, other aircraft's windows) when
# choosing. Without it, the pick is made with no (c) candidate.
STATE_TIMEOUT_S = 3.0
# Time (s) allowed to fetch the airspace copy. /airspace is large (34,000 buildings) and four
# aircraft fetch it at once. The default 5 s cut it off, the truncated reply was taken as an
# empty copy, and the planner drew a city with no buildings.
AIRSPACE_TIMEOUT_S = 60.0


@dataclass
class PendingDraft:
    """One draft handed to a worker thread when a refusal arrived. Waited on only until its
    deadline (monotonic)."""

    future: Future
    drafter: ModelDrafter
    deadline: float

    @property
    def in_flight(self) -> bool:
        return not self.future.done()


class GuardedAgent:
    """Submits filings. That is all it does."""

    def __init__(self, asset_id: str, runtime_url: str, proposer: Proposer,
                 llm: TieredLlm | None = None):
        self.asset_id = asset_id
        self.runtime_url = runtime_url.rstrip("/")
        self.proposer = proposer
        self.llm = llm or proposer.llm
        self.pad_index = 0
        self.pads: dict = {}
        self.banned: set[str] = set()
        self.cooldown: dict[str, float] = {}
        self.repeat_s = float(os.getenv("REPEAT_COOLDOWN_S", "2"))
        self.denial_s = float(os.getenv("DENIAL_COOLDOWN_S", "6"))
        self.redraw_s = float(os.getenv("REDRAW_DELAY_S", str(REDRAW_DELAY_S)))
        self.banned_retry_s = float(os.getenv("BANNED_RETRY_S", "20"))
        self.planner = OperatorPlanner()
        # Route drafts drawn by the model. None without a model, and then only A* is used.
        # Whoever drew it, the runtime judges — a draft just rides in the filing as legs.
        self.service_bbox = None
        self.drafter = self._build_drafter()
        # This aircraft's model for picking one candidate: the same model (NANO) that writes
        # filings — the screen's model_ok depends on who wrote the filings and these picks
        # (ModelHealth).
        self.chooser = RouteChooser(self.llm)
        # A draft starts on a worker thread the moment a refusal arrives, so the model's drawing
        # time overlaps the 5.6 s the screen shows the refusal — it used to ask only after the
        # full 5.6 s and stood still that much longer. One draft per aircraft at a time: if the
        # last draft is still pending on the server, this refusal goes to A* (no queueing
        # behind it). The thread is a daemon — the interpreter always waits for
        # ThreadPoolExecutor workers at exit, so Ctrl-C didn't finish until a draft pending on
        # the server (up to 60 s) came back.
        self._draft: PendingDraft | None = None
        # The candidate pick in flight (Future), plus this turn's filing trace and draft count.
        self._choice = None
        self._form: dict | None = None
        self._draft_attempts = 0
        # How often the model was asked and its answer used, and how often it was asked but the
        # rules answered instead (filings and route picks only). Registration (ModelHealth)
        # uses these to decide between the model name and rules on screen.
        self.model_answers = 0
        self.model_misses = 0
        self.airspace_revision = None
        # The height our aircraft wants to fly. If the allowed ceiling is lower, the runtime
        # refuses, and the planner then redraws with each leg lowered.
        self.preferred_alt_m = float(os.getenv("CRUISE_ALT_M", str(Router.cruise_alt_default())))

    def _count_form(self, trace: dict | None) -> None:
        """Count one filing. One the rules wrote for lack of a model (no model) never asked
        anything, so it isn't counted."""
        if not trace or trace.get("fallback_reason") == "no model":
            return
        self._count(bool(trace.get("used")))

    def _count_choice(self, choice) -> None:
        """Count one route pick. Picks the rules made without asking the model aren't counted."""
        if choice is not None and (choice.asked or choice.by_model):
            self._count(choice.by_model)

    def _count(self, used: bool) -> None:
        if used:
            self.model_answers += 1
        else:
            self.model_misses += 1

    def _build_drafter(self) -> ModelDrafter | None:
        drafter = ModelDrafter(self.llm, self.planner, bbox=self.service_bbox)
        return drafter if drafter.enabled else None

    @property
    def draft_in_flight(self) -> bool:
        return self._draft is not None and self._draft.in_flight

    def _open_pad(self) -> str:
        names = sorted(self.pads) or FALLBACK_PADS
        open_pads = [pad for pad in names if pad not in self.banned] or names
        return open_pads[self.pad_index % len(open_pads)]

    def telemetry(self) -> dict:
        return get_json(f"{self.runtime_url}/telemetry/{self.asset_id}") or {}

    def _destination(self, telemetry: dict, proposal) -> tuple | None:
        if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
            return (telemetry["job_lat"], telemetry["job_lon"])
        if proposal.action == "reserve_pad" and proposal.resource:
            pads = self.pads or {}
            at = pads.get(proposal.resource)
            return (at["lat"], at["lon"]) if at else None
        return None

    def _file_with_route(self, proposal, telemetry: dict, form: dict | None = None):
        """File the shortest straight line first. If it breaks the rules, the runtime says
        where, then the planner draws up to three legal candidates and this aircraft's model
        picks one. The runtime judges the pick again — approval isn't ours to give."""
        self._form = form if form is not None else _rules_form(proposal)
        self._draft_attempts = 0
        here = (telemetry.get("lat"), telemetry.get("lon"))
        goal = self._destination(telemetry, proposal)
        if here[0] is None or goal is None:
            return self._file(proposal.to_dict(), None)

        if float(telemetry.get("alt_m") or 0.0) > 1.0:
            # Lost the route in flight (recall). Time spent hovering is pure noise, so skip the
            # straight-line ritual and the pick and draw a detour straight from our airspace
            # copy — A* answers in milliseconds.
            legs = self.planner.draw(here, goal)
            if not legs:
                return self._nothing_legal(proposal, telemetry, here, None, None, None)
            return self._file_legs(proposal, legs, "astar", route_part("astar"), airborne=True)

        proposal.params = {**proposal.params, "legs": self.planner.straight(here, goal),
                           "drafter": "straight", "draft_attempts": 0}
        straight = route_part("straight")
        decision = self._file(proposal.to_dict(), straight)
        if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
            return decision
        if decision.get("policy_hit") == "traffic":
            # Overlaps another aircraft's corridor. The route is fine, so try another height or
            # time.
            decision = self._resolve_traffic(proposal, proposal.params["legs"], decision,
                                             airborne=False, route=straight)
            if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
                return decision
        # Told to redraw. The planner draws candidates now, as the refusal arrives, and the
        # model picks one (worker thread). The 5.6 s the screen shows the refusal covers that
        # time — the aircraft is busy on the ground and stays put.
        refused_at = time.monotonic()
        if decision.get("policy_hit") == "airspace":
            self.planner.note_refusal(decision.get("forbids"))
        pending = self._start_choice(here, goal, telemetry, decision)
        time.sleep(self.redraw_s)
        if pending is None:
            return decision      # the last pick is still running; skip this turn
        outcome = self._collect_choice(pending, refused_at)
        self._count_choice(outcome.choice if outcome else None)
        airborne_now, moved_to = self._position_now()
        if airborne_now:
            return decision      # airborne now: a route drawn on the ground is moot; skip turn
        return self._file_candidates(proposal, telemetry, here, goal, moved_to, outcome, decision)

    def _file_candidates(self, proposal, telemetry: dict, here, goal, moved_to,
                         outcome: Outcome, refusal):
        """File the model's pick first. If refused, the remaining candidates in turn; if all
        are refused, the draft.

        Every candidate passed judgement on our copy, but the runtime's copy may be newer (a
        zone that just closed), and the crossing check, which also looks at times, can't be
        done here. So a refusal doesn't mean 'wrong candidate', and filing the rest is right.
        """
        if moved_to is not None and _distance_m(here, moved_to) > TRAFFIC_LATERAL_M:
            # Moved far while waiting; candidates drawn from the old spot are a different route.
            legs = self.planner.draw(moved_to, goal)
            if not legs:
                return self._nothing_legal(proposal, telemetry, here, None, None, refusal)
            return self._file_legs(proposal, legs, "astar", route_part("astar"), airborne=False)
        base = proposal.rationale
        decision = refusal
        for index, candidate in enumerate(outcome.ordered()):
            legs = (self._anchored(candidate["legs"], here, moved_to, goal) if moved_to
                    else candidate["legs"])
            if not legs:
                continue
            filed = (outcome.choice if index == 0 and outcome.choice is not None
                     else _next_best(candidate, decision))
            route = route_part("choice" if filed.by_model else "astar",
                               choice=choice_part(outcome.candidates, filed.chosen, filed.reason))
            proposal.rationale = f"{base} · candidate {candidate['id']} ({candidate['label']})"
            decision = self._file_legs(
                proposal, legs, f"choice:{filed.model}" if filed.by_model else "astar", route,
                airborne=False,
                extra={"route_choice": route_choice_param(outcome.candidates, filed)})
            if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
                return decision
        proposal.rationale = base
        return self._after_candidates(proposal, telemetry, here, goal, moved_to, outcome, decision)

    def _after_candidates(self, proposal, telemetry: dict, here, goal, moved_to,
                          outcome: Outcome, decision):
        """No candidate got through (or there were none). The model's draft is now the last
        resort."""
        chosen = outcome.choice
        choice = (choice_part(outcome.candidates, chosen.chosen, chosen.reason)
                  if chosen is not None else None)
        legs, draft, drew = self._last_resort_draft(moved_to or here, goal, decision)
        if legs:
            proposal.rationale = f"{proposal.rationale} · model draft"
            return self._file_legs(proposal, legs, drew, route_part("draft", choice, draft),
                                   airborne=False)
        if outcome.candidates:
            # Legal routes existed and the runtime refused them all. Abandon this turn and
            # refile from scratch next turn (by then the other aircraft may have passed or the
            # zone lifted).
            return decision
        return self._nothing_legal(proposal, telemetry, here, choice, draft, decision)

    def _nothing_legal(self, proposal, telemetry: dict, here, choice, draft, decision):
        """Our copy has no route that keeps to the rules."""
        if proposal.action != "fly_route" or self.planner.start_blocked(here, telemetry):
            # Having no route to the pad is not the same as being unable to take the order.
            # We used to decline deliveries because the charger was blocked.
            # A blocked start (inside a closed zone) isn't the order's problem either — wait
            # until we're out.
            return decision
        return self._file({**proposal.to_dict(), "action": "decline_job", "cost_usd": 0.0,
                           "blast_radius": "none", "params": {}, "resource": None,
                           "rationale": f"{proposal.rationale} · no legal route"},
                          route_part("astar", choice, draft))

    def _file_legs(self, proposal, legs: list[dict], drafter: str, route: dict,
                   airborne: bool, extra: dict | None = None):
        """File one route. Who drew it is recorded in the filing; the runtime doesn't read
        that value."""
        # The previous filing's route_choice belongs to that filing. Left on a draft or A*
        # filing, it would label a route nobody picked as 'the pick'.
        kept = {key: value for key, value in proposal.params.items() if key != "route_choice"}
        proposal.params = {**kept, "legs": legs, "drafter": drafter,
                           "draft_attempts": self._draft_attempts, **(extra or {})}
        filed = {**proposal.to_dict(),
                 "rationale": f"{proposal.rationale} · redrawn {len(legs)} legs"}
        decision = self._file(filed, route)
        if decision and decision.get("policy_hit") == "traffic":
            # The filed route overlaps another aircraft's corridor. The route is fine, so try
            # another height or time.
            decision = self._resolve_traffic(proposal, legs, decision, airborne, route)
        return decision

    def _file(self, payload: dict, route: dict | None):
        """Submit one filing. Every filing carries model_trace — a label the screen card
        reads; the runtime's judgement doesn't read it (judgement looks at legs)."""
        params = {**(payload.get("params") or {}),
                  "model_trace": model_trace(self._form, route)}
        return self._send({**payload, "params": params})

    def _send(self, payload: dict):
        """Send one filing to the runtime. This is the only wire — the offline harness
        (tests/test_two_worlds.py) swaps just this for an in-process call and keeps the rest
        of the flow."""
        return post_json(f"{self.runtime_url}/proposals", payload)

    def _runtime_state(self) -> dict:
        """One read of the runtime's /state. Empty if unreadable — that only drops candidate
        (c) and the model's context."""
        return get_json(f"{self.runtime_url}/state", timeout=STATE_TIMEOUT_S) or {}

    def _position_now(self) -> tuple[bool, tuple[float, float] | None]:
        """Aircraft position after waiting on a draft: (airborne?, current position or None)."""
        now = self.telemetry()
        if now.get("lat") is None or now.get("lon") is None:
            return False, None
        return float(now.get("alt_m") or 0.0) > 1.0, (float(now["lat"]), float(now["lon"]))

    def _anchored(self, legs: list[dict], here, moved_to, goal) -> list[dict] | None:
        """If the aircraft moved during the 20-60 s draft wait, move the first point to where
        it is now.

        The runtime refuses a first point more than TRAFFIC_LATERAL_M from the aircraft (live
        run: a draft came back while the aircraft was taking off on its previously approved
        route, was filed from the old start 70 m away, and was refused). If it moved further
        than that, a line drawn from the old spot is a different route, so A* redraws it.
        """
        gap = _distance_m(here, moved_to)
        if gap < 1.0:
            return legs
        if gap > TRAFFIC_LATERAL_M:
            return self.planner.draw(moved_to, goal)
        return [{**legs[0], "lat": round(moved_to[0], 6), "lon": round(moved_to[1], 6)}] + legs[1:]

    def _resolve_traffic(self, proposal, legs: list[dict], refusal: dict, airborne: bool,
                         route: dict | None = None):
        """Resolution ladder for a crossing refusal: altitude +30 m → delayed departure.
        Returns the last answer.

        The route is right; the timing is the problem. First refile the same route 30 m higher
        (only if every leg stays under the ceiling). If that still overlaps, refile with
        departure delayed to the tick the other corridor clears (blocked_until_tick, from the
        refusal). The autopilot waits ready on the ground until that tick, and the screen says
        whom it is waiting for. An airborne aircraft can't be delayed (that would be a hover in
        the air, not a ground hold), so only altitude is tried.
        """
        detail = refusal.get("detail") or {}
        other = detail.get("blocked_asset") or refusal.get("forbids")
        lifted = self.planner.lift(legs, ALTITUDE_SHIFT_M)
        decision = refusal
        if lifted is not None:
            decision = self._file({
                **proposal.to_dict(),
                "params": {**proposal.params, "legs": lifted, "resolution": "altitude",
                           "altitude_shift_m": ALTITUDE_SHIFT_M, "holding_for": None},
                "rationale": (f"{proposal.rationale} · +{ALTITUDE_SHIFT_M:.0f} m over "
                              f"{other}'s corridor"),
            }, route)
            if not decision or decision.get("policy_hit") != "traffic":
                return decision
            detail = decision.get("detail") or {}
            other = detail.get("blocked_asset") or other
        if airborne:
            return decision
        until = detail.get("blocked_until_tick")
        for _ in range(MAX_DELAY_TRIES):
            if until is None:
                break
            decision = self._file({
                **proposal.to_dict(),
                "params": {**proposal.params, "legs": legs, "resolution": "delay",
                           "holding_for": other, "depart_after_tick": int(until)},
                "rationale": f"{proposal.rationale} · depart after {other} passes "
                             f"(tick {int(until)})",
            }, route)
            if not decision or decision.get("policy_hit") != "traffic":
                return decision
            detail = decision.get("detail") or {}
            later = detail.get("blocked_until_tick")
            if later is None or int(later) <= int(until):
                break
            until, other = later, detail.get("blocked_asset") or other
        return decision

    def _start_choice(self, here, goal, telemetry: dict, refusal: dict):
        """As a refusal arrives, draw candidates on a worker thread and have the model pick.
        None if not started.

        Reads the runtime's /state once to build what candidate (c) keeps clear of (other
        aircraft's approved corridors, active zones) and the model's context (weather, notices,
        other aircraft's windows, remaining stops). Candidates still come without /state — (c)
        is dropped and the pick is between (a) and (b). This is not judgement data.
        """
        if self._choice is not None and not self._choice.done():
            return None
        planner, chooser, asset = self.planner, self.chooser, self.asset_id
        concern = (self._form or {}).get("concern", "")

        def work() -> Outcome:
            state = self._runtime_state()
            began = time.monotonic()
            candidates = planner.candidates(here, goal, keep_clear_from_state(state, asset))
            planned_ms = int((time.monotonic() - began) * 1000)
            if not candidates:
                return Outcome([], None, planned_ms)
            situation = situation_from_state(state, telemetry, refusal, concern, asset)
            return Outcome(candidates, chooser.choose(candidates, situation), planned_ms)

        self._choice = self._in_background(work)
        return self._choice

    def _collect_choice(self, pending, refused_at: float) -> Outcome:
        """Collect the candidates and the pick. Empty if that fails — next comes the draft,
        then a decline.

        The planner can take a while (40-50 s for a Manhattan run from an empty memo). The
        aircraft waits on the ground meanwhile — as it already did with A* — and the screen
        shows it as 'redrawing after a refusal'.
        """
        try:
            left = max(0.0, refused_at + CHOICE_WAIT_S - time.monotonic())
            outcome = pending.result(timeout=left)
            self._log_choice(outcome)
            return outcome
        except DraftTimeout:
            print(f"[{self.asset_id}] route choice did not finish in time", flush=True)
        except Exception as error:  # noqa: BLE001 - a failed pick is refiled next turn
            print(f"[{self.asset_id}] route choice failed: {error!r}", flush=True)
        return Outcome()

    def _log_choice(self, outcome: Outcome) -> None:
        """One line for the candidates and the pick. What was chosen from what, and how, has
        to be in the live-run log to be measured."""
        ids = ",".join(candidate["id"] for candidate in outcome.candidates) or "-"
        choice = outcome.choice
        how = "" if choice is None else (
            f" chose {choice.chosen} by {choice.path}"
            + (f" ({choice.fallback_reason})" if choice.fallback_reason else "")
            + (f" in {choice.latency_ms} ms" if choice.asked else ""))
        print(f"[choice:{self.asset_id}] candidates {ids} drawn in {outcome.planned_ms} ms"
              f"{how}", flush=True)

    def _last_resort_draft(self, here, goal, refusal: dict | None):
        """No candidate got through. Only now is the model asked to draw a new route.

        This used to be the first thing done when a refusal arrived. Now the candidate pick
        takes that slot and the draft goes only after every candidate is refused — each
        aircraft has one model server slot, so asking both at once queues the pick behind the
        draft (up to 60 s) and the aircraft stands still that much longer.
        Returns (draft legs or None, draft record for the screen card or None, who drew it).
        """
        pending = self._start_draft(here, goal, refusal or {}, time.monotonic())
        if pending is None:
            return None, None, ""
        legs = None
        try:
            legs = pending.future.result(timeout=max(0.0, pending.deadline - time.monotonic()))
        except DraftTimeout:
            pass          # out of budget; drop the draft
        except Exception as error:  # noqa: BLE001 - a dead draft is refiled next turn
            print(f"[{self.asset_id}] draft failed: {error!r}", flush=True)
        drafter = pending.drafter
        self._draft_attempts = drafter.last_attempts
        return legs, draft_part(drafter.last_attempts > 0, drafter.last_latency_ms,
                                drafter.last_breach, bool(legs)), drafter.name

    def _start_draft(self, here, goal, refusal: dict, refused_at: float) -> PendingDraft | None:
        """Ask the model for one draft (worker thread). None if not asked.

        The model reads the refusal reason and drafts a route. The draft has to pass the form,
        box, altitude and length checks and judgement on our airspace copy (all inside the
        drafter, same thread); after two failures it's None. Either way the runtime judges
        again, so a wild line from the model never gets flown.
        Not asked after a crossing refusal — the model doesn't know about other aircraft, and
        what needs fixing is the time, not the route (the ladder handles that).
        Deadline: start time + one draft budget. The drafter fits both questions inside it.
        """
        drafter = self.drafter
        if drafter is None or refusal.get("policy_hit") == "traffic":
            return None
        if self.draft_in_flight:
            # The last refusal's draft is still pending on the server. Queueing another behind
            # it makes both late.
            return None
        context = {"reason": refusal.get("reason"), "forbids": refusal.get("forbids")}
        deadline = refused_at + drafter.timeout_s
        future = self._in_background(drafter.draft, here, goal, context, deadline)
        self._draft = PendingDraft(future=future, drafter=drafter, deadline=deadline)
        return self._draft

    def _in_background(self, work, *args) -> Future:
        """Run work(*args) on one daemon thread and return a Future."""
        future: Future = Future()

        def run() -> None:
            try:
                future.set_result(work(*args))
            except BaseException as error:  # noqa: BLE001 - passed on; the main thread handles it
                future.set_exception(error)

        threading.Thread(target=run, daemon=True, name=f"draft-{self.asset_id}").start()
        return future

    def _refresh_airspace(self, revision) -> bool:
        """Our airspace copy is stale (a zone closed or lifted). Fetch a new one and draw from
        it.

        If the fetch fails (timeout, no runtime, empty reply), keep the old copy and don't
        record the revision — fetch again next turn. It used to swap in an empty copy on
        failure and record the revision too, so until the revision changed again the planner
        drew a city with no buildings: in a live run a 'straight, 4 legs, 70 m' candidate to
        Morningside came out, and the runtime refused all of them at 48 m above a 22 m roof
        (50 m clearance required).
        """
        world = get_json(f"{self.runtime_url}/airspace", timeout=AIRSPACE_TIMEOUT_S) or {}
        if not world.get("volumes"):
            print(f"[{self.asset_id}] airspace copy not refreshed (revision {revision}); "
                  "keeping the last one and retrying", flush=True)
            return False
        self.planner = OperatorPlanner()
        self.planner.load(world["volumes"])
        self.pads = world.get("pads", {})
        # Service area = bounding box of the landing sites and pads. A model draft that leaves
        # it is dropped.
        corners = ([(a["lat"], a["lon"]) for a in world.get("landing_areas", [])]
                   + [(p["lat"], p["lon"]) for p in self.pads.values()])
        self.service_bbox = service_bbox(corners) or self.service_bbox
        self.drafter = self._build_drafter()
        self.airspace_revision = revision
        return True

    def step(self) -> None:
        telemetry = self.telemetry()
        if not telemetry:
            return
        if telemetry.get("airspace_revision") != self.airspace_revision:
            self._refresh_airspace(telemetry.get("airspace_revision"))
        concern = detect(telemetry)
        if concern is None:
            return
        proposal = self.proposer.write(
            concern, telemetry, self._open_pad(), frozenset(self.banned),
            tuple(sorted(self.pads) or FALLBACK_PADS),
        )
        self._count_form(self.proposer.last_trace)
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # don't keep pushing what was just refused
        self.cooldown[proposal.action] = time.time() + self.repeat_s
        # Who wrote the filing (model or rules, and why) rides on every filing this turn.
        decision = self._file_with_route(proposal, telemetry, self.proposer.last_trace)
        if decision and decision.get("verdict") in ("denied", "human", "queued"):
            self.cooldown[proposal.action] = time.time() + self.denial_s
        route_refusal = bool(decision) and decision.get("policy_hit") in ROUTE_REFUSALS
        if decision and decision.get("verdict") == "denied" and decision.get("policy_hit") \
                and not route_refusal:
            # An action blocked by a directive (an airworthiness directive, say). Only refiling
            # tells whether it was lifted, but refiling every time plasters the screen with
            # refusals. Refile only now and then.
            self.cooldown[proposal.action] = time.time() + self.banned_retry_s
        if decision and decision.get("verdict") == "denied":
            if decision.get("policy_hit"):
                # With an enforcement point, we learn on the spot what was forbidden.
                # Mistaking a blocked resource for a blocked action means never filing again.
                # A blocked route or time (airspace, crossing) isn't a blocked action, so it
                # isn't learned here.
                if not route_refusal:
                    self.banned.add(decision.get("forbids") or proposal.action)
            elif proposal.resource:
                self.pad_index += 1
        _report(self.asset_id, "guarded", proposal, decision)


def _rules_form(proposal) -> dict:
    """Called without a filing trace (tests, direct calls): record it as written by the rules."""
    return form_part("", "", proposal.action, proposal.rationale, 0, False, "rules")


def _next_best(candidate: dict, refusal: dict | None) -> Choice:
    """The 'pick' when the next candidate is filed after the chosen one was refused. The rules
    make it, not the model."""
    why = (refusal or {}).get("policy_hit") or "refused"
    return Choice(candidate["id"], f"rules: the previous candidate was refused ({why})",
                  path="rules")


def _distance_m(a, b) -> float:
    return math.hypot((float(b[0]) - float(a[0])) * METRES_PER_DEG_LAT,
                      (float(b[1]) - float(a[1])) * METRES_PER_DEG_LON)


def _report(asset_id: str, mode: str, proposal, outcome) -> None:
    verdict = (outcome or {}).get("verdict") or ("ok" if (outcome or {}).get("ok") else "?")
    print(f"[{mode}:{asset_id}] {proposal.action} ${proposal.cost_usd:.0f} -> {verdict}",
          flush=True)


def build_llm() -> TieredLlm:
    # The aircraft side waits only 6 s. Without an answer the rules write the filing and A*
    # draws the route. Waiting longer than the refusal display (5.6 s) makes the aircraft look
    # frozen on screen.
    return TieredLlm(models={
        "nano": os.getenv("MODEL_NANO", ""),
        "super": os.getenv("MODEL_SUPER", ""),
        "ultra": os.getenv("MODEL_ULTRA", ""),
    }, timeout_s=float(os.getenv("LLM_TIMEOUT_S") or "6"))


def identity(asset_id: str, llm: TieredLlm, model_ok: bool | None = None) -> dict:
    """Self-introduction sent to the runtime: only what this process writes filings with
    (model, server).

    A model that can't be called isn't listed — with no server address, or Nebius without a
    key, every call fails and the rules write the filings. A model name on screen would then
    be a lie, so model is left empty and host is off. A sound configuration that gets no
    answers is reported by model_ok (ModelHealth) — when False, the runtime shows rules
    instead of the name.
    """
    model = llm.model_for(LlmTier.NANO) if llm.enabled else ""
    host = llm.host if llm.host in ("ollama", "nebius", "other") else "off"
    if host == "nebius" and not llm.api_key:
        model = ""
    port = None
    if model:
        parsed = urllib.parse.urlparse(llm.base_url)
        port = parsed.port or {"https": 443, "http": 80}.get(parsed.scheme)
    return {"asset_id": asset_id, "world": "guarded", "model": model,
            "host": host if model else "off", "base_url_port": port,
            "model_ok": model_ok if model else None}


class ModelHealth:
    """Is this aircraft's model currently making its decisions (filings, route picks)?
    Evaluated once per registration.

    True if, since the last registration, the model was asked and its answer used even once;
    False if it was asked but the rules answered every time (timeout, non-form answer, unknown
    candidate); unchanged if it wasn't asked in between. None at first (unknown) — the runtime
    trusts the configured model name and shows it.

    Route drafts aren't counted. A draft is the last resort after every candidate is refused,
    so it often failing is normal. Looking at the LLM stats as a whole, as before, flipped the
    screen to rules in a window full of draft failures even though the model wrote every
    filing (measured 2026-09-11, drone-01 and 03).
    """

    def __init__(self, counts):
        self.counts = counts          # () -> (model answers used, asked but rules answered)
        self.state: bool | None = None
        self._counted = (0, 0)

    def check(self) -> bool | None:
        answered, missed = self.counts()
        if answered > self._counted[0]:
            self.state = True
        elif missed > self._counted[1]:
            self.state = False
        self._counted = (answered, missed)
        return self.state


class Registration:
    """POST /agents/register: every REGISTER_PERIOD_S once accepted, otherwise after
    REGISTER_RETRY_S.

    Runs on its own thread (start). On the same line as filing (step), a slow runtime would
    make registration hold up filings 3 s at a time — registration is a screen label and
    filings come first. The payload is rebuilt with describe() on every send: whether the
    model has been answering changes.
    """

    def __init__(self, runtime_url: str, describe, period_s: float = REGISTER_PERIOD_S,
                 retry_s: float = REGISTER_RETRY_S):
        self.url = f"{runtime_url.rstrip('/')}/agents/register"
        self.describe = describe
        self.period_s = period_s
        self.retry_s = retry_s
        self.payload: dict | None = None
        self.sent_at: float | None = None
        self.accepted = False
        self._stop = threading.Event()

    def maybe_send(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        wait = self.period_s if self.accepted else self.retry_s
        if self.sent_at is not None and now - self.sent_at < wait:
            return False
        self.send(now)
        return True

    def send(self, now: float | None = None) -> bool:
        self.sent_at = time.monotonic() if now is None else now
        self.payload = self.describe()
        # Short wait: registration is a screen label, not judgement.
        answer = post_json(self.url, self.payload, timeout=3.0)
        self.accepted = bool(answer and answer.get("ok"))
        return self.accepted

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, daemon=True, name="register")
        thread.start()
        return thread

    def run(self) -> None:
        while not self._stop.is_set():
            self.send()
            self._stop.wait(self.period_s if self.accepted else self.retry_s)

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    asset_id = os.environ["ASSET_ID"]
    period = float(os.getenv("AGENT_PERIOD_S", "0.6"))
    llm = build_llm()
    runtime_url = os.getenv("RUNTIME_URL", "http://runtime:8000")
    agent = GuardedAgent(asset_id, runtime_url, Proposer(llm))
    health = ModelHealth(lambda: (agent.model_answers, agent.model_misses))
    registration = Registration(runtime_url, lambda: identity(asset_id, llm, health.check()),
                                float(os.getenv("REGISTER_PERIOD_S") or REGISTER_PERIOD_S))
    print(f"agent {asset_id} up, files to runtime "
          f"(llm={'on' if llm.enabled else 'off'}, host={llm.host}, "
          f"model={identity(asset_id, llm)['model'] or 'rules'}, "
          f"chooser={'nano' if agent.chooser.enabled else 'rules'}, "
          f"drafter={'nano' if agent.drafter else 'astar'})", flush=True)
    registration.start()
    while True:
        agent.step()
        time.sleep(period)


if __name__ == "__main__":
    main()
