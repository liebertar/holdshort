"""Turns a concern into a request form.

Both worlds use this same file. The agents in the unguarded world are exactly as capable
and exactly as well behaved; what they lack is a place to put the rules.
"""

import time

from drone.agent.detect import Concern
from drone.agent.trace import concern_words, form_part
from shared.llm.client import LlmTier, TieredLlm, parse_json_object
from shared.models import Proposal

# No charging. Battery management is the operator's business, and this cycle (load → deliver →
# pick up → return) has no charger.
# With it on the list, the model wrote "charge" for aircraft standing in the yard, and the
# autopilot refused 61 times with "not on a pad".
ALLOWED_ACTIONS = {"decline_job", "fly_route", "reserve_pad", "disengage_autonomy", "depart"}
# The model may pick only actions that fit the concern. In the delivery cycle that's deliver,
# depart or decline; emergency landing (reserve_pad) and disengaging autonomy only with a fault
# concern. The 4B wrote 'reserve landing pad' seven times for an aircraft in the yard, trying to
# land on top of the aircraft next to it, and every one was refused. Route recall
# (divert_ground) is the runtime's to use, so it isn't listed.
ROUTINE_ACTIONS = {"fly_route", "depart", "decline_job"}
FAULT_ACTIONS = {"motor_fault": {"reserve_pad"}, "needs_pad": {"reserve_pad"},
                 "autonomy_fault": {"disengage_autonomy"}}


def allowed_for(concern: Concern) -> set[str]:
    return ROUTINE_ACTIONS | FAULT_ACTIONS.get(concern.kind, set())

COSTS = {"decline_job": 0.0, "fly_route": 12.0, "reserve_pad": 28.0, "charge": 22.0,
         "fast_charge": 60.0, "divert_ground": 35.0, "disengage_autonomy": 0.0, "depart": 0.0}

BLAST = {"decline_job": "none", "fly_route": "schedule", "reserve_pad": "schedule",
         "charge": "none", "fast_charge": "none", "divert_ground": "cargo",
         "disengage_autonomy": "public", "depart": "none"}

def system_for(pads: tuple[str, ...]) -> str:
    """The form description. Pad names are used exactly as the runtime reported them —
    written down here, they'd silently drift the moment a name changed (they had drifted)."""
    choices = "|".join(f'"{pad}"' for pad in pads) or "null"
    return (
        "You watch one uncrewed vehicle. You cannot act. You may only fill in a request form "
        "that a runtime will judge. Reply with one JSON object and nothing else: "
        '{"action": one of ' + str(sorted(ALLOWED_ACTIONS)) + f', "pad": {choices}|null, '
        '"rationale": "one short sentence"}. Never invent an action outside the list.'
    )


def by_rule(
    concern: Concern, telemetry: dict, pad: str, banned: frozenset[str] = frozenset()
) -> Proposal:
    asset_id = telemetry.get("id", "?")
    if concern.kind == "needs_route":
        action, chosen_pad = "fly_route", None
    elif concern.kind in ("charged", "needs_reload"):
        action, chosen_pad = "depart", None
    elif concern.kind == "autonomy_fault":
        action, chosen_pad = "disengage_autonomy", None
    elif concern.kind in ("motor_fault", "needs_pad"):
        action, chosen_pad = "reserve_pad", pad
    else:
        action, chosen_pad = "reserve_pad", pad

    return _build(asset_id, action, chosen_pad, concern.detail, "rules")


def possible_now(action: str, telemetry: dict) -> bool:
    """Can the aircraft physically do this now? The same test the simulator refuses by."""
    state = str(telemetry.get("state") or "")
    airborne = float(telemetry.get("alt_m") or 0.0) > 1.0
    if action in ("charge", "fast_charge"):
        return state in ("landed", "charging")
    if action == "depart":
        return not airborne
    return True


def _build(asset_id: str, action: str, pad: str | None, rationale: str, author: str) -> Proposal:
    return Proposal(
        asset_id=asset_id,
        action=action,
        cost_usd=COSTS.get(action, 0.0),
        blast_radius=BLAST.get(action, "none"),
        rationale=rationale,
        params={"pad": pad} if pad else {},
        resource=pad,
        author=author,
    )


class Proposer:
    def __init__(self, llm: TieredLlm):
        self.llm = llm
        # Who wrote the last filing (trace.form_part). The drone agent puts this in the
        # filing's model_trace.
        self.last_trace: dict | None = None

    def write(
        self, concern: Concern, telemetry: dict, pad: str,
        banned: frozenset[str] = frozenset(), pads: tuple[str, ...] = ()
    ) -> Proposal:
        known = tuple(pads) or (pad,)
        fallback = by_rule(concern, telemetry, pad, banned)
        tier = LlmTier.SUPER if concern.urgency == "high" else LlmTier.NANO
        words = concern_words(concern, telemetry)
        started = time.monotonic()
        reply = self.llm.ask(tier, system_for(known),
                             self._brief(concern, telemetry, pad), max_tokens=160,
                             json_object=True)
        if reply is None:
            waited = int((time.monotonic() - started) * 1000)
            self.last_trace = form_part("", words, fallback.action, fallback.rationale, waited,
                                        False, self._silence_reason(tier))
            return fallback

        form = parse_json_object(reply.text)
        problem = None
        if not form:
            problem = "not a form"
        elif form.get("action") not in allowed_for(concern):
            problem = "invalid action"  # drop an action that doesn't fit this concern
        elif form["action"] in banned:
            problem = "banned action"   # drop a pick that is already banned
        elif not possible_now(form["action"], telemetry):
            # Something the aircraft can't do right now (charge when not on a pad, take off
            # when airborne). The form is valid, but the autopilot would refuse the filing — in
            # a live run the 4B wrote 'charge' 61 times for an aircraft in the yard, every one
            # failed with "not on a pad", and meanwhile that aircraft loaded nothing.
            # This is the operator's common sense, not judgement.
            problem = "impossible now"
        if problem is not None:
            self.llm.discard(tier)
            self.last_trace = form_part("", words, fallback.action, fallback.rationale,
                                        reply.latency_ms, False, problem)
            return fallback

        chosen_pad = form.get("pad") if form.get("action") == "reserve_pad" else None
        if chosen_pad is not None and chosen_pad not in known:
            chosen_pad = pad  # unknown pad; the form is valid, so fall back to the nearest
        rationale = str(form.get("rationale") or concern.detail)[:180]
        self.last_trace = form_part(reply.model, words, form["action"], rationale,
                                    reply.latency_ms, True, None)
        return _build(telemetry.get("id", "?"), form["action"], chosen_pad, rationale, reply.model)

    def _silence_reason(self, tier: LlmTier) -> str:
        """Why there was no answer: "no model" without a model, "timeout" if the server
        couldn't be reached (timeouts included)."""
        if not (self.llm.enabled and self.llm.model_for(tier)):
            return "no model"
        return "timeout" if self.llm.unreachable_within(5.0) else "no reply"

    @staticmethod
    def _brief(concern: Concern, telemetry: dict, pad: str) -> str:
        return (
            f"vehicle={telemetry.get('id')} model={telemetry.get('model')} "
            f"state={telemetry.get('state')} battery={telemetry.get('battery')}% "
            f"vibration={telemetry.get('vibration')} "
            f"autonomy={telemetry.get('autonomy_health')} "
            f"passengers={telemetry.get('passengers')} cargo={telemetry.get('cargo')}\n"
            f"concern={concern.kind} ({concern.urgency}): {concern.detail}\n"
            f"nearest free-looking pad: {pad}"
        )
