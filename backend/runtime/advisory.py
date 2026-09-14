"""A tower advisory: what the desk tells an operator whose filings keep being refused.

It is information, not a decision. After a run of refusals the runtime lists the options the
code can see — hold until the other corridor clears, lift the same legs, wait for the notice
window, decline, ask a person — and has the deterministic judge check each one where a check
applies. A model (the Super tier, if configured) may write the two-sentence summary and pick
one id out of that list; anything else it says is discarded and a rule picks instead. The
advisory goes on the ledger and into /state, and changes nothing: the operator still files
whatever it files, and the same judge reads it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from shared.llm.client import LlmTier, TieredLlm, parse_json_object

# One advisory per this many refusals in a row. Two is normal: still inside the rewrite ladder
# (straight → detour → altitude).
ADVISORY_AFTER = 3
# The same height as the operator's crossing-resolution ladder (drone/agent/loop.py
# ALTITUDE_SHIFT_M). The runtime does not import the aircraft package, so the value is kept
# separately — if the two drift apart, the advisory suggests a route the operator cannot file.
CLIMB_M = 30.0
# Recent refusals carried in an advisory: enough for the sixth refusal's advisory to carry all
# six.
KEEP_REFUSALS = ADVISORY_AFTER * 2
SUMMARY_LIMIT = 400
# Time budget (seconds) when a model writes the advisory. The refusal reply goes out after it,
# so a long one keeps the operator waiting.
ADVISORY_TIMEOUT_S = 12.0

ADVISORY_SYSTEM = (
    "You are the tower desk for an uncrewed delivery fleet. One aircraft's route filings keep "
    "being refused by a deterministic judge. You are given the refusals and a fixed list of "
    "options that the code has already checked against the same judge. You decide nothing and "
    "change nothing: pick ONE option id from the list and write a two-sentence summary for the "
    'controller. Reply with one JSON object and nothing else: {"choice": "<option id>", '
    '"summary": "two sentences"}. Never invent an option that is not in the list.'
)


@dataclass
class Refusal:
    """What the advisory needs from one refusal. Field names match the ledger's."""

    asset: str
    tick: int
    code: str
    policy_hit: str | None
    blocked_kind: str | None
    blocked_volume: str | None
    blocked_asset: str | None
    blocked_until_tick: int | None
    proposal_id: str
    action: str
    legs: list = field(default_factory=list)        # last filed route; input to the climb option
    params: dict = field(default_factory=dict)      # filing params without legs (pad etc.)
    resource: str | None = None                     # filing's resource (landing pad): the
                                                    # endpoint check's destination

    @property
    def traffic(self) -> bool:
        return self.policy_hit == "traffic" or self.blocked_kind in ("traffic", "landing")

    @property
    def signature(self) -> tuple:
        """Is it the same block? A refusal by the same other aircraft or zone until the same
        tick is a repeat, not news."""
        return (self.action, self.code, self.policy_hit, self.blocked_kind, self.blocked_volume,
                self.blocked_asset, self.blocked_until_tick)

    def to_dict(self) -> dict:
        return {"tick": self.tick, "code": self.code, "policy_hit": self.policy_hit,
                "blocked_kind": self.blocked_kind, "blocked_volume": self.blocked_volume,
                "blocked_asset": self.blocked_asset,
                "blocked_until_tick": self.blocked_until_tick, "proposal": self.proposal_id}


@dataclass
class Option:
    id: str
    label: str
    legal: bool
    why: str
    until_tick: int | None = None
    shift_m: float | None = None

    def to_dict(self) -> dict:
        out = {"id": self.id, "label": self.label, "legal": self.legal, "why": self.why}
        if self.until_tick is not None:
            out["until_tick"] = self.until_tick
        if self.shift_m is not None:
            out["shift_m"] = self.shift_m
        return out


def lifted(legs: list[dict], shift_m: float = CLIMB_M) -> list[dict]:
    return [{**leg, "alt_m": round(float(leg.get("alt_m") or 0.0) + shift_m, 1)} for leg in legs]


def build_options(refusals: list[Refusal], judge: Callable[[Refusal, list[dict]], str | None],
                  notice_until: Callable[[str | None], int | None],
                  airborne: bool) -> list[Option]:
    """The options the code builds. Where the judge applies, it checks them — nothing runs.

    The order is the rule's priority: ground hold → climb → notice window → decline → a person.
    judge(refusal, legs) is why that route is blocked right now (None if it is not);
    notice_until(volume_id) is the tick that zone closes, if it comes from a notice.
    """
    options: list[Option] = []
    last = refusals[-1] if refusals else None

    crossing = next((r for r in reversed(refusals) if r.traffic and r.blocked_until_tick), None)
    if crossing is not None:
        until = int(crossing.blocked_until_tick)
        options.append(Option(
            "hold", f"hold on the ground until tick {until}", not airborne,
            (f"{crossing.blocked_asset} clears that volume at tick {until}" if not airborne
             else "the aircraft is airborne — it cannot hold on the ground"),
            until_tick=until))

    if last is not None and len(last.legs) >= 2:
        higher = lifted(last.legs)
        problem = judge(last, higher)
        options.append(Option(
            "climb", f"climb +{CLIMB_M:.0f} m on the last filed legs", problem is None,
            problem or f"the same legs pass the judge {CLIMB_M:.0f} m higher", shift_m=CLIMB_M))

    notice = next(((r, notice_until(r.blocked_volume)) for r in reversed(refusals)
                   if r.blocked_volume and notice_until(r.blocked_volume) is not None), None)
    if notice is not None:
        refusal, until = notice
        options.append(Option(
            "notice_window", f"wait for the notice window to close at tick {until}", True,
            f"{refusal.blocked_volume} lapses at tick {until}", until_tick=int(until)))

    options.append(Option("decline", "decline the job", True,
                          "no aircraft flies; the order goes back to dispatch"))
    options.append(Option("escalate", "escalate to a person", True,
                          "a controller looks at the refusals"))
    return options


def rule_pick(options: list[Option]) -> str:
    """The first legal option in the order above. The last two are always legal, so one exists."""
    return next(o.id for o in options if o.legal)


def template_summary(asset: str, refusals: list[Refusal], options: list[Option],
                     chosen: str, trigger: str) -> str:
    codes = ", ".join(sorted({r.blocked_kind or r.policy_hit or r.code for r in refusals}))
    label = next((o.label for o in options if o.id == chosen), chosen)
    if trigger == "decline_after_refusals":
        head = f"{asset} declined the order after {len(refusals)} refusals ({codes})."
    else:
        head = f"{asset} was refused {len(refusals)} times in a row ({codes})."
    return f"{head} The rules suggest: {label}."


def parse_advice(text: str, options: list[Option]) -> tuple[str | None, str]:
    """(option id or None, summary) from the model's answer. The id is None when it is not on
    the list or the judge blocked it."""
    form = parse_json_object(text)
    if not form:
        return None, ""
    legal = {o.id for o in options if o.legal}
    # The list reads "[hold] …", so models echo the brackets (in a real run, 155 of 160 advisories
    # answered "[hold]" and fell back to the rule's pick). Brackets and quotes are formatting, not
    # the choice, so strip them first.
    choice = str(form.get("choice") or "").strip().strip("[]()\"'` ").strip().lower()
    summary = " ".join(str(form.get("summary") or "").split())[:SUMMARY_LIMIT]
    return (choice if choice in legal else None), summary


class AdvisoryDesk:
    """Counts refusals in a row per aircraft, and writes the advisory when one is due."""

    def __init__(self, llm: TieredLlm | None):
        self.llm = llm
        self.streaks: dict[str, list[Refusal]] = {}
        self.latest: dict[str, dict] = {}       # aircraft → latest advisory (for the screen)
        # Per aircraft, how many refusals each block (signature) drew. Three of the same block get
        # one advisory, and that block never gets another — in a real run, refilings arrived every
        # few ticks while someone else held the landing pad, so every third one wrote "hold on the
        # ground until tick X" again: 190 in 65 min, each calling the 30B once.
        self._by_block: dict[str, dict[tuple, int]] = {}

    @property
    def has_model(self) -> bool:
        return (self.llm is not None and self.llm.enabled
                and bool(self.llm.model_for(LlmTier.SUPER)))

    def refused(self, asset: str, refusal: Refusal) -> bool:
        """Adds one refusal. True if this one makes an advisory due.

        One advisory on the third refusal by the same block (same other aircraft, zone and
        until-tick). That block gets no second one; a different block that reaches three gets
        its own — while the same aircraft blocks until the same tick, the same thing is not
        repeated every third time. An approval resets every count.
        """
        streak = self.streaks.setdefault(asset, [])
        streak.append(refusal)
        counts = self._by_block.setdefault(asset, {})
        counts[refusal.signature] = counts.get(refusal.signature, 0) + 1
        return counts[refusal.signature] == ADVISORY_AFTER

    def succeeded(self, asset: str) -> None:
        """An approval was executed (or the order was declined). The streak ends."""
        self.streaks.pop(asset, None)
        self._by_block.pop(asset, None)

    def declined(self, asset: str) -> bool:
        """A decline-the-order filing arrived. After refusals, that makes an advisory due."""
        return bool(self.streaks.get(asset))

    def streak(self, asset: str) -> list[Refusal]:
        return list(self.streaks.get(asset, []))[-KEEP_REFUSALS:]

    def clear(self) -> None:
        self.streaks.clear()
        self.latest.clear()
        self._by_block.clear()

    def snapshot(self) -> list[dict]:
        return [self.latest[asset] for asset in sorted(self.latest)]

    def compose(self, asset: str, trigger: str, refusals: list[Refusal],
                options: list[Option], airborne: bool) -> dict:
        """The advisory body. With a model, ask it for the summary and the pick; if the answer
        is off the list or missing, the rule picks.
        """
        fallback = rule_pick(options)
        chosen, summary, model, source = fallback, "", "", "rules"
        if self.has_model:
            reply = self.llm.ask(LlmTier.SUPER, ADVISORY_SYSTEM,
                                 self._brief(asset, refusals, options, airborne),
                                 max_tokens=240, json_object=True, timeout_s=ADVISORY_TIMEOUT_S)
            if reply is not None:
                model = reply.model
                picked, said = parse_advice(reply.text, options)
                if picked is None:
                    # An answer off the list. Drop the summary too — it may describe an option
                    # that does not exist.
                    self.llm.discard(LlmTier.SUPER)
                else:
                    chosen, summary, source = picked, said, "super"
        if not summary:
            summary = template_summary(asset, refusals, options, chosen, trigger)
        return {"trigger": trigger, "refusals": [r.to_dict() for r in refusals],
                "options": [o.to_dict() for o in options], "chosen": chosen,
                "summary": summary, "model": model, "source": source}

    @staticmethod
    def _brief(asset: str, refusals: list[Refusal], options: list[Option],
               airborne: bool) -> str:
        lines = [f"Aircraft: {asset} (airborne: {'yes' if airborne else 'no'})",
                 "Refusals, oldest first:"]
        for index, r in enumerate(refusals, start=1):
            what = r.blocked_kind or r.policy_hit or r.code
            who = r.blocked_asset or r.blocked_volume or "-"
            until = f" until tick {r.blocked_until_tick}" if r.blocked_until_tick else ""
            lines.append(f"{index}. tick {r.tick}: {r.action} refused ({what}) by {who}{until}")
        lines.append("Options (ids in brackets):")
        for o in options:
            verdict = "legal" if o.legal else f"NOT legal: {o.why}"
            lines.append(f"- [{o.id}] {o.label} — {verdict}")
        return "\n".join(lines)
