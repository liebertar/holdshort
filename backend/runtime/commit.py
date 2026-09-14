"""The single door to the world.

Every path in this system funnels through commit(). It writes the ledger first, takes the
lock, calls the adapter, then closes the ledger. Agents cannot import this module: it is
not in their container image.
"""

from backend.runtime.authority import AuthorityCheck
from backend.runtime.locks import LockTable
from backend.store.ledger import Ledger
from shared.models import Decision, Proposal, Verdict

# Held resources are released when the aircraft actually leaves. Releasing them when loading
# starts (depart) let the next aircraft come down on top of one still sitting on the pad.
RELEASING_ACTIONS = {"fly_route", "divert_ground"}


class Committer:
    def __init__(
        self,
        adapter,
        locks: LockTable,
        ledger: Ledger,
        authority: AuthorityCheck,
    ):
        self.adapter = adapter
        self.locks = locks
        self.ledger = ledger
        self.authority = authority
        # Called right after a successful execution, before the ledger entry closes. The
        # runtime creates or ends the (4D) intent here, and the context it returns (intent_id)
        # goes on the closing line. A hook, because there must be exactly one commit path.
        self.on_committed = None

    def commit(self, proposal: Proposal, decision: Decision,
               context: dict | None = None) -> Decision:
        if decision.verdict is Verdict.DENIED:
            return decision

        if proposal.resource and not self.locks.acquire(
            proposal.resource, proposal.asset_id, proposal.id
        ):
            decision.verdict = Verdict.DENIED
            decision.reason = f"{proposal.resource} is in use by another aircraft"
            decision.code = "resource_held"
            decision.detail = {"resource": proposal.resource}
            return decision

        entry = self.ledger.open_entry(proposal, decision, context)
        decision.ledger_id = entry.id

        result = self.adapter.execute(
            proposal.asset_id,
            proposal.action,
            proposal.params,
            entry.id,
            blast=proposal.blast_radius,
            approved_by=decision.approved_by,
        )
        ok = bool(result.get("ok"))

        learned = None
        if ok:
            self.authority.record_spend(proposal)
            decision.committed = True
            if proposal.action in RELEASING_ACTIONS:
                self.locks.release_all(proposal.asset_id)
            if self.on_committed is not None:
                learned = self.on_committed(proposal, decision, entry)
        elif proposal.resource:
            self.locks.release(proposal.resource, proposal.asset_id)

        self.ledger.close_entry(
            entry, "done" if ok else f"failed: {result.get('error')}", decision, learned
        )
        return decision
