"""Run history again under a rule that did not exist yet.

This is the thing the ledger buys that nothing else does. Every commit recorded the request,
the asset's state, the policies in force and the limits at that moment. So a rule you are
thinking about writing can be tested against what actually happened, before you push it to
eight thousand aircraft.

A regulator writing a rule today finds out what it costs after it ships. An operator asked
to accept a rule has no way to price it. Both questions are the same query over this file.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from backend.runtime.authority import AuthorityCheck
from backend.runtime.policy import PolicyBook
from shared.config import Authority, Policy
from shared.models import Proposal, Verdict


@dataclass
class Change:
    ledger_id: str
    asset_id: str
    action: str
    cost_usd: float
    was: str
    now: str
    reason: str


@dataclass
class Verdicts:
    considered: int = 0
    unchanged: int = 0
    newly_denied: list = None
    newly_human: list = None
    newly_allowed: list = None

    def __post_init__(self):
        self.newly_denied = self.newly_denied or []
        self.newly_human = self.newly_human or []
        self.newly_allowed = self.newly_allowed or []

    @property
    def blocked_cost(self) -> float:
        return round(sum(c.cost_usd for c in self.newly_denied), 2)

    def summary(self) -> dict:
        return {
            "decisions seen": self.considered,
            "unchanged": self.unchanged,
            "newly denied": len(self.newly_denied),
            "newly to a human": len(self.newly_human),
            "newly allowed": len(self.newly_allowed),
            "blocked spend": self.blocked_cost,
        }


def read_commits(path: str | Path) -> list[dict]:
    """Only decisions that reached execution. What was refused never happened."""
    entries: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries[entry["id"]] = entry  # the last line for an id is its final state
    return [e for e in entries.values() if e.get("outcome") == "done"]


def replay(ledger_path: str | Path, authority: Authority,
           policies: list[Policy], telemetry: dict | None = None) -> Verdicts:
    """Judge past executions again under the new rules."""
    book = PolicyBook(policies)
    check = AuthorityCheck(authority, book)
    result = Verdicts()

    for entry in read_commits(ledger_path):
        raw = entry["proposal"]
        proposal = Proposal.from_dict(raw)
        asset = (telemetry or {}).get(proposal.asset_id, {})
        # The filing does not record what type the aircraft was back then, so it is passed in
        decision = check.evaluate(proposal, asset, tick=10**9)
        result.considered += 1

        was = entry["decision"]["verdict"]
        now = decision.verdict.value
        if now == was:
            result.unchanged += 1
            if decision.verdict is Verdict.AUTO:
                check.record_spend(proposal)
            continue

        change = Change(entry["id"], proposal.asset_id, proposal.action,
                        proposal.cost_usd, was, now, decision.reason)
        if decision.verdict is Verdict.DENIED:
            result.newly_denied.append(change)
        elif decision.verdict is Verdict.HUMAN:
            result.newly_human.append(change)
        else:
            result.newly_allowed.append(change)
            check.record_spend(proposal)
    return result
