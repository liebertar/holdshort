"""The planner draws the candidates; this aircraft's own model picks one; the runtime judges it.

Measured: asking a 4B model to write waypoint coordinates rarely clears the judge — a long
Manhattan crossing is a search problem, and A* is better at it in milliseconds. Choosing
between routes that are already legal is a different kind of question: which one reads best
given the weather, the closed cells, the other aircraft's windows and the stops still to make.
That is a judgement, and it is the sort of thing these cards are trained to answer by calling
a tool.

So the model never writes geometry here. It calls choose_route(id, reason) with one of the
ids the planner handed it. Nothing about the guarantee changes: the chosen legs go to the
runtime exactly like A*'s, and the runtime refuses them for exactly the same reasons. If the
server has no tools, we ask again in JSON. If there is no model, no answer, or an answer that
names a route that does not exist, the rules choose and we count it.
"""

import os
import time
from dataclasses import dataclass, field

from shared.llm.client import LlmTier, TieredLlm, parse_json_object

CHOICE_TOOL = "choose_route"
# The reason is one sentence. It goes verbatim onto a screen card line and into the ledger, so
# a long one is cut.
REASON_CHARS = 160
# Budget (s) for one pick (tool call + a JSON re-ask if needed). It is collected after the
# 5.6 s refusal display, so finishing within that is free on screen. Measured (Ollama
# nemotron-3-nano:4b, idle server): 1.1-2.6 s.
CHOICE_TIMEOUT_S = 10.0
# Don't ask with less budget left than this; that's how long the first token takes.
MIN_ASK_S = 1.0
# A server that answers in text when given tools: after this many in a row, it's asked in
# JSON only. (Once may be a model slip, but asking twice every time keeps the aircraft
# standing that much longer.)
PLAIN_TEXT_LIMIT = 2

# Keep the system prompt short. Measured (Ollama nemotron-3-nano:4b, live brief of 820
# tokens): with a long system prompt that also spelled out preferences, Ollama couldn't parse
# the tool call the model wrote (35 tokens) and returned an empty answer (200, no content, no
# call) — live 2/2, reproduced 3/3. With the short prompt, 6/6 came back as tool calls. The
# chat template puts tool descriptions in the system slot, so the long prompt presumably
# crowded out those format instructions. Preferences go at the end of the brief.
SYSTEM_TOOLS = (
    "You pick one of the candidate routes for one uncrewed delivery drone by calling "
    f"{CHOICE_TOOL}. You cannot draw, change or approve routes; a runtime judges the one you pick."
)
SYSTEM_JSON = (
    "You pick one of the candidate routes for one uncrewed delivery drone. You cannot draw, "
    "change or approve routes; a runtime judges the one you pick. Reply with one JSON object "
    'and nothing else: {"id": "<one of the ids>", "reason": "one short sentence"}.'
)
PREFERENCE = ("Prefer a route that keeps clear of another aircraft's cleared corridor or an "
              "incident when one is near; otherwise a lower cruise if it costs little; otherwise "
              "the shortest.")
CLOSING_TOOLS = f"Call {CHOICE_TOOL} with one id and one short sentence why."
CLOSING_JSON = "Answer with one id and one short sentence why."


def choice_tool(ids: list[str]) -> dict:
    """The one tool the model can call. It executes nothing — it only states the pick."""
    return {
        "type": "function",
        "function": {
            "name": CHOICE_TOOL,
            "description": ("File one of the candidate routes for the runtime to judge. "
                            "Call this exactly once."),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": list(ids),
                           "description": "the id of the candidate you pick"},
                    "reason": {"type": "string",
                               "description": "one short sentence, why this one"},
                },
                "required": ["id", "reason"],
            },
        },
    }


@dataclass
class Choice:
    """One pick. path is how it was made: tools | json | rules."""

    chosen: str
    reason: str
    model: str = ""
    path: str = "rules"
    latency_ms: int = 0
    asked: bool = False
    fallback_reason: str | None = None

    @property
    def by_model(self) -> bool:
        return self.path in ("tools", "json")


@dataclass
class Outcome:
    """The result of one redraw: the candidates, the pick, and the planner's time."""

    candidates: list[dict] = field(default_factory=list)
    choice: Choice | None = None
    planned_ms: int = 0

    def ordered(self) -> list[dict]:
        """The pick first, then rule order. If one is refused, the next is filed."""
        by_id = {candidate["id"]: candidate for candidate in self.candidates}
        chosen = self.choice.chosen if self.choice else ""
        first = [by_id.pop(chosen)] if chosen in by_id else []
        return first + list(by_id.values())


def rule_choice(candidates: list[dict], situation: dict | None = None) -> Choice:
    """Pick without a model: (c) when a crossing is in play, otherwise (a).

    Without this rule, no route could be filed when there is no model. The fleet has to fly
    even on days the model doesn't answer, and what to pick then is settled — when another
    aircraft's corridor is in the way, the route that stays apart.
    """
    situation = situation or {}
    by_id = {candidate["id"]: candidate for candidate in candidates}
    shortest = by_id.get("a") or (candidates[0] if candidates else None)
    if shortest is None:
        return Choice("", "", path="rules", fallback_reason="no candidates")
    crowded = bool(situation.get("traffic_refusal")) or any(
        tag.startswith("near-traffic:") for tag in shortest.get("reason_tags") or [])
    clear = by_id.get("c")
    if crowded and clear is not None:
        return Choice(clear["id"], "rules: traffic is in the way, this one keeps clear",
                      path="rules")
    return Choice(shortest["id"], "rules: the shortest legal route", path="rules")


class RouteChooser:
    def __init__(self, llm: TieredLlm, tier: LlmTier = LlmTier.NANO,
                 timeout_s: float | None = None):
        self.llm = llm
        self.tier = tier
        self.timeout_s = (float(os.getenv("CHOICE_TIMEOUT_S", str(CHOICE_TIMEOUT_S)))
                          if timeout_s is None else float(timeout_s))
        # Tally of how picks were made, read by the measurement table (report) and by tests.
        self.counts = {"tools": 0, "json": 0, "rules": 0, "invalid": 0, "plain_text": 0,
                       "empty": 0, "unsupported": 0, "timeout": 0, "differs_from_rule": 0}
        self._plain_streak = 0

    @property
    def enabled(self) -> bool:
        return bool(self.llm.enabled and self.llm.model_for(self.tier))

    @property
    def model(self) -> str:
        return self.llm.model_for(self.tier)

    def choose(self, candidates: list[dict], situation: dict | None = None,
               deadline: float | None = None) -> Choice:
        """Pick one candidate. With no answer, or an unknown id, the rules pick and that is
        counted."""
        rules = rule_choice(candidates, situation)
        if len(candidates) < 2:
            return self._by_rules(rules, "one candidate" if candidates else "no candidates")
        if not self.enabled:
            return self._by_rules(rules, "no model")
        if deadline is None:
            deadline = time.monotonic() + self.timeout_s
        brief = choice_brief(candidates, situation)
        started = time.monotonic()
        if self.llm.tools_ok and self._plain_streak < PLAIN_TEXT_LIMIT:
            choice, why = self._by_tools(brief, candidates, deadline)
            if choice is not None:
                return self._counted(choice, rules, started)
            if why != "json":
                return self._by_rules(rules, why)
        choice, why = self._by_json(brief, candidates, deadline)
        if choice is not None:
            return self._counted(choice, rules, started)
        return self._by_rules(rules, why)

    # ---------- asking ----------

    def _by_tools(self, brief: str, candidates: list[dict],
                  deadline: float) -> tuple[Choice | None, str]:
        """Via tool calling. A returned reason of "json" means re-ask with the JSON form next."""
        budget = deadline - time.monotonic()
        if budget < MIN_ASK_S:
            return None, "no time"
        ids = [candidate["id"] for candidate in candidates]
        reply = self.llm.ask_tools(self.tier, SYSTEM_TOOLS, brief, [choice_tool(ids)],
                                   max_tokens=200, timeout_s=budget)
        if reply is None:
            if not self.llm.tools_ok:
                self.counts["unsupported"] += 1
                return None, "json"     # the server doesn't know tools; same question in JSON
            if self.llm.unreachable_within(1.0):
                self.counts["timeout"] += 1
                return None, "timeout"  # server unreachable; asking again just waits again
            # The server answered, but empty (200, no text, no tool call). Measured: Ollama
            # swallows the 4B's tool call like this when it can't parse it. The server is
            # there, so ask once more in JSON.
            self.counts["empty"] += 1
            self._plain_streak += 1
            return None, "json"
        if not reply.tool_calls:
            # Given tools, it answered in text. Drop that and re-ask in JSON.
            self.llm.discard(self.tier)
            self.counts["plain_text"] += 1
            self._plain_streak += 1
            return None, "json"
        call = next((c for c in reply.tool_calls if c["name"] == CHOICE_TOOL), None)
        if call is None:
            # Called a tool that doesn't exist. That's a wrong answer, not text, so no re-ask:
            # the rules pick.
            self.llm.discard(self.tier)
            self.counts["invalid"] += 1
            return None, "invalid tool"
        self._plain_streak = 0
        choice = self._validated(call["arguments"], candidates, reply, "tools")
        if choice is None:
            self.llm.discard(self.tier)
            self.counts["invalid"] += 1
            return None, "invalid id"
        return choice, ""

    def _by_json(self, brief: str, candidates: list[dict],
                 deadline: float) -> tuple[Choice | None, str]:
        budget = deadline - time.monotonic()
        if budget < MIN_ASK_S:
            return None, "no time"
        brief = brief.replace(CLOSING_TOOLS, CLOSING_JSON)
        reply = self.llm.ask(self.tier, SYSTEM_JSON, brief, max_tokens=160, json_object=True,
                             timeout_s=budget)
        if reply is None:
            self.counts["timeout"] += 1
            return None, "timeout"
        choice = self._validated(parse_json_object(reply.text), candidates, reply, "json")
        if choice is None:
            self.llm.discard(self.tier)
            self.counts["invalid"] += 1
            return None, "invalid id"
        return choice, ""

    def _validated(self, answer, candidates: list[dict], reply, path: str) -> Choice | None:
        """Form check. The id must be one we offered; the reason is cut to one sentence."""
        if not isinstance(answer, dict):
            return None
        # The model sometimes writes "(a)" or "a.". Keep letters and digits only and match
        # against our ids — the leniency goes as far as recognising the same id; an unknown id
        # is still dropped.
        chosen = "".join(ch for ch in str(answer.get("id") or "").lower() if ch.isalnum())
        if chosen not in {candidate["id"] for candidate in candidates}:
            return None
        reason = " ".join(str(answer.get("reason") or "").split())[:REASON_CHARS]
        return Choice(chosen, reason, model=reply.model, path=path,
                      latency_ms=reply.latency_ms, asked=True)

    # ---------- tallying ----------

    def _counted(self, choice: Choice, rules: Choice, started: float) -> Choice:
        self.counts[choice.path] += 1
        if choice.chosen != rules.chosen:
            self.counts["differs_from_rule"] += 1
        choice.latency_ms = int((time.monotonic() - started) * 1000)
        return choice

    def _by_rules(self, rules: Choice, why: str) -> Choice:
        self.counts["rules"] += 1
        rules.fallback_reason = why
        return rules


# ---------- what the model reads ----------


def choice_brief(candidates: list[dict], situation: dict | None = None) -> str:
    """Only what the pick needs, on one screen: this aircraft, why it's redrawing, what's in
    effect now, the candidate table."""
    situation = situation or {}
    lines = [_aircraft_line(situation)]
    if situation.get("concern"):
        lines.append(f"Task: {situation['concern']}.")
    if situation.get("refusal"):
        lines.append(f"The runtime refused the straight line — {situation['refusal']}.")
    lines.append(_now_line(situation))
    if situation.get("notices"):
        lines.append("In force: " + "; ".join(situation["notices"][:4]) + ".")
    if situation.get("traffic"):
        lines.append("Other aircraft already cleared: " + "; ".join(situation["traffic"][:4]) + ".")
    lines.append("Candidates (each one already passes the operator's airspace check; "
                 "the runtime judges the one you pick):")
    lines += [_candidate_line(candidate) for candidate in candidates]
    lines += [PREFERENCE, CLOSING_TOOLS]
    return "\n".join(lines)


def _aircraft_line(situation: dict) -> str:
    parts = [f"Aircraft {situation.get('asset') or '?'}"]
    if situation.get("model"):
        parts.append(f"({situation['model']})")
    where = "on the ground" if situation.get("airborne") is False else "airborne"
    parts.append(where)
    if situation.get("battery") is not None:
        parts.append(f"battery {float(situation['battery']):.0f}%")
    if situation.get("stops_left") is not None:
        parts.append(f"{int(situation['stops_left'])} stop(s) left this trip")
    return ", ".join([" ".join(parts[:3])] + parts[3:]) + "."


def _now_line(situation: dict) -> str:
    weather = situation.get("weather") or "no weather hold"
    return f"Now: tick {situation.get('tick', '?')}. Weather: {weather}."


def _candidate_line(candidate: dict) -> str:
    tags = ", ".join(candidate.get("reason_tags") or []) or "-"
    return (f"{candidate['id']}) {candidate.get('label', '')} — "
            f"{float(candidate.get('length_m') or 0) / 1000:.1f} km, "
            f"{len(candidate.get('legs') or [])} legs, cruise "
            f"{candidate.get('min_alt_m')}-{candidate.get('max_alt_m')} m; {tags}")


def route_choice_param(candidates: list[dict], choice: Choice) -> dict:
    """params.route_choice — what was picked from what, and why. Read by the screen and the
    ledger, not by the runtime."""
    return {
        "candidates": [{"id": c["id"], "label": c["label"], "legs_count": len(c["legs"]),
                        "length_m": c["length_m"], "max_alt_m": c["max_alt_m"],
                        "min_alt_m": c["min_alt_m"], "reason_tags": list(c["reason_tags"])}
                       for c in candidates],
        "chosen": choice.chosen, "reason": choice.reason, "model": choice.model,
        "path": choice.path,
    }


def situation_from_state(state: dict, telemetry: dict, refusal: dict | None, concern: str,
                         asset_id: str) -> dict:
    """The runtime's /state and our telemetry as a few lines for the model. Context, not
    judgement data."""
    state = state or {}
    hold = (state.get("weather") or {}).get("hold") or {}
    refused = refusal or {}
    return {
        "asset": asset_id,
        "model": telemetry.get("model"),
        "airborne": float(telemetry.get("alt_m") or 0.0) > 1.0,
        "battery": telemetry.get("battery"),
        "stops_left": telemetry.get("stops_left"),
        "concern": concern,
        "tick": state.get("tick", telemetry.get("tick")),
        "weather": (f"hold until tick {hold.get('until_tick')} ({hold.get('reason')})"
                    if hold else None),
        "notices": _notice_words(state),
        "traffic": _traffic_words(state, asset_id),
        "traffic_refusal": refused.get("policy_hit") == "traffic",
        "refusal": _refusal_words(refused),
    }


def _refusal_words(refusal: dict) -> str:
    hit = refusal.get("policy_hit")
    if not hit:
        return ""
    if hit == "traffic":
        detail = refusal.get("detail") or {}
        until = detail.get("blocked_until_tick")
        return (f"traffic: it overlaps {detail.get('blocked_asset') or 'another aircraft'}"
                + (f"'s corridor until tick {until}" if until is not None else "'s corridor"))
    return f"{hit}: {refusal.get('forbids') or 'a rule'} is in the way"


def _notice_words(state: dict) -> list[str]:
    """Applied notices only. One not yet confirmed by a person blocks nothing, so it's left
    out."""
    words = []
    for notice in state.get("notices") or []:
        if not notice.get("applied"):
            continue
        until = notice.get("until_tick")
        words.append(f"{notice.get('name') or notice.get('id')}"
                     + (f" (until tick {until})" if until is not None else ""))
    return words


def _traffic_words(state: dict, asset_id: str) -> list[str]:
    words = []
    for intent in state.get("intents") or []:
        if intent.get("asset") == asset_id or intent.get("state") not in ("accepted", "activated"):
            continue
        flying = "flying" if intent.get("state") == "activated" else "waiting to depart"
        words.append(f"{intent.get('asset')} ticks {intent.get('from_tick')}-"
                     f"{intent.get('to_tick')} ({flying})")
    return words


def keep_clear_from_state(state: dict, asset_id: str) -> dict:
    """What candidate (c) keeps clear of: other aircraft's approved routes, and the active
    zones and incident circles.

    /state has no corridor coordinates, so each intent's filing is looked up in the ledger
    tail (legs of approved filings). If it isn't found, that aircraft is left out — (c) is
    only an option, and the runtime judges crossings on the 4D intents anyway. Nothing
    missing here loosens the rules.
    """
    state = state or {}
    legs_by_proposal = {}
    for entry in state.get("ledger") or []:
        proposal = entry.get("proposal") or {}
        legs = (proposal.get("params") or {}).get("legs")
        if proposal.get("id") and legs:
            legs_by_proposal[proposal["id"]] = legs
    traffic = []
    for intent in state.get("intents") or []:
        if intent.get("asset") == asset_id or intent.get("state") not in ("accepted", "activated"):
            continue
        legs = legs_by_proposal.get(intent.get("proposal_id"))
        if legs:
            traffic.append({"id": intent["asset"], "legs": legs})
    keepouts = [{"id": notice.get("id"), "polygon": notice.get("polygon")}
                for notice in state.get("notices") or []
                if notice.get("applied") and notice.get("polygon")]
    keepouts += [{"id": incident.get("id"), "lat": (incident.get("centre") or [None, None])[0],
                  "lon": (incident.get("centre") or [None, None])[1],
                  "radius_m": incident.get("radius_m")}
                 for incident in state.get("incidents") or []
                 if incident.get("applied") and incident.get("centre")]
    return {"traffic": traffic, "keepouts": keepouts}
