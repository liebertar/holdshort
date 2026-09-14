"""The single commit path and the contention queue in front of it.

The only toucher of `_contended`, and (with revoke_under) of `committer` and `locks`.
"""

import time

from shared.models import Decision, Proposal, Verdict


class CommitPathMixin:
    def _queue_or_commit(self, proposal: Proposal, decision: Decision) -> Decision:
        if not proposal.resource:
            committed = self.committer.commit(proposal, decision, self._context(proposal))
            if committed.committed:
                self._recent_commits[(proposal.asset_id, proposal.action)] = self.tick
            self._checks.pop(proposal.id, None)
            return committed

        with self._guard:
            waiting = self._contended.setdefault(proposal.resource, [])
            standing = next(
                (d for p, d, _ in waiting if p.asset_id == proposal.asset_id), None
            )
            if standing is not None:
                # Already queued. The same aircraft does not queue twice for the same resource.
                return standing
            waiting.append((proposal, decision, time.time()))

        decision.verdict = Verdict.QUEUED
        decision.reason = f"waiting for {proposal.resource} to be assigned"
        return decision

    def _settle_contended(self) -> None:
        now = time.time()
        with self._guard:
            ready = [
                resource
                for resource, waiting in self._contended.items()
                if waiting and now - waiting[0][2] >= self.window_s
            ]
            batches = {resource: self._contended.pop(resource) for resource in ready}

        if batches:
            with self._judging:
                self._settle_batches(batches)

    def _settle_batches(self, batches: dict) -> None:
        """Grant queued resources.

        This rejudges, executes and registers intents, so it holds the same lock as file()
        (_judging).
        """
        for resource, waiting in batches.items():
            # The airspace may have changed while queued. Blocked routes don't enter arbitration.
            waiting = [item for item in waiting if self._rejudge(item[0], item[1]) is None]
            if not waiting:
                continue
            held = self.locks.holder(resource)
            candidates = [item[0] for item in waiting]
            if held and held.asset_id not in {p.asset_id for p in candidates}:
                for proposal, decision, _ in waiting:
                    decision.verdict = Verdict.DENIED
                    decision.reason = f"{resource} is in use by {held.asset_id}"
                    decision.code = "resource_held"
                    decision.detail = {"resource": resource, "holder": held.asset_id}
                    self.ledger.close_entry(
                        self.ledger.open_entry(proposal, decision, self._context(proposal)),
                        "denied")
                    self._record_refusal(proposal, decision)
                continue

            choice = self.arbiter.pick(candidates, self.telemetry)
            winner, how = choice.proposal, choice.how
            # The model's reason for its pick goes on the record. What it picked is one number,
            # and if that number is out of range the rule picked instead — the reason explains,
            # it does not decide.
            detail = {"resource": resource}
            if choice.reason:
                detail["arbiter_reason"] = choice.reason
            for proposal, decision, _ in waiting:
                if proposal.id == winner.id:
                    decision.arbiter = how if len(candidates) > 1 else None
                    decision.verdict = Verdict.AUTO
                    decision.reason = f"{resource} assigned"
                    decision.code = "resource_granted"
                    decision.detail = dict(detail)
                    self._checks.setdefault(proposal.id, []).append("arbiter")
                    self.committer.commit(proposal, decision, self._context(proposal))
                    if decision.committed:
                        self._recent_commits[(proposal.asset_id, proposal.action)] = self.tick
                    self._checks.pop(proposal.id, None)
                else:
                    decision.verdict = Verdict.DENIED
                    decision.arbiter = how
                    decision.reason = f"{winner.asset_id} was given {resource}"
                    decision.detail = dict(detail)
                    self.ledger.close_entry(
                        self.ledger.open_entry(proposal, decision, self._context(proposal)),
                        "denied")
                    self._record_refusal(proposal, decision)
