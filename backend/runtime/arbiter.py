"""Picks one proposal out of several that already passed authority.

It never authors an action. It returns an index into a list the runtime built, plus one
sentence saying why, which goes on the record. If the model answers with anything else,
or does not answer, the rule below decides instead.
"""

from dataclasses import dataclass

from shared.llm.client import LlmTier, TieredLlm, parse_choice, parse_json_object
from shared.models import BLAST_RANK, Proposal

# No money here. authority has already checked the budget; bringing it up again would record
# the model picking the cheaper option as if it were a safety judgement. All that is left is
# who gets the resource first.
SYSTEM = (
    "You are an arbiter for an uncrewed fleet. Several requests want the same single "
    "resource at the same time. Every one of them has already passed the safety checks; "
    "you only decide the order. Choose exactly one. Reply with one JSON object and nothing "
    'else: {"choice": <the number from the list>, "reason": "one short sentence"}. '
    "Do not invent new actions or conditions."
)
REASON_LIMIT = 140


@dataclass
class Choice:
    proposal: Proposal
    how: str            # single | rule:… | ultra:<model id>
    reason: str = ""    # the model's one-line reason; empty when the rule picked


def by_rule(candidates: list[Proposal], telemetry: dict) -> tuple[Proposal, str]:
    """Wider blast radius first, then lower battery, then earlier filing."""

    def key(proposal: Proposal):
        asset = telemetry.get(proposal.asset_id, {})
        return (
            -BLAST_RANK.get(proposal.blast_radius, 0),
            asset.get("battery", 100.0),
            proposal.filed_at,
        )

    winner = sorted(candidates, key=key)[0]
    return winner, "rule:blast>battery>filed"


def parse_verdict(text: str, option_count: int) -> tuple[int, str] | None:
    """{"choice": n, "reason": "…"} or a bare number. None for prose or an out-of-range pick."""
    form = parse_json_object(text)
    if form is not None and "choice" in form:
        try:
            index = int(form["choice"]) - 1
        except (TypeError, ValueError):
            return None
        if not 0 <= index < option_count:
            return None
        return index, str(form.get("reason") or "")[:REASON_LIMIT]
    index = parse_choice(text, option_count)
    return None if index is None else (index, "")


class Arbiter:
    def __init__(self, llm: TieredLlm):
        self.llm = llm

    def pick(self, candidates: list[Proposal], telemetry: dict) -> Choice:
        if len(candidates) == 1:
            return Choice(candidates[0], "single")

        fallback, fallback_reason = by_rule(candidates, telemetry)
        verdict = self._ask(candidates, telemetry)
        if verdict is None:
            return Choice(fallback, fallback_reason)

        picked, reason, model = verdict
        return Choice(candidates[picked], f"ultra:{model}", reason)

    def choose(self, candidates: list[Proposal], telemetry: dict) -> tuple[Proposal, str]:
        choice = self.pick(candidates, telemetry)
        return choice.proposal, choice.how

    def _ask(self, candidates: list[Proposal], telemetry: dict):
        lines = []
        for index, proposal in enumerate(candidates, start=1):
            asset = telemetry.get(proposal.asset_id, {})
            lines.append(
                f"{index}. asset={proposal.asset_id} action={proposal.action} "
                f"impact={proposal.blast_radius} battery={asset.get('battery', '?')}% "
                f"passengers={asset.get('passengers', 0)} why={proposal.rationale}"
            )
        reply = self.llm.ask(
            LlmTier.ULTRA,
            SYSTEM,
            "Resource: " + (candidates[0].resource or "?") + "\n" + "\n".join(lines),
            max_tokens=160,
            json_object=True,
        )
        if reply is None:
            return None
        verdict = parse_verdict(reply.text, len(candidates))
        if verdict is None:
            self.llm.discard(LlmTier.ULTRA)
            return None
        index, reason = verdict
        return index, reason, reply.model
