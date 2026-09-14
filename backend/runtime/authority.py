"""The envelope: what clears on its own, what a human must see, what is simply refused."""

from collections import defaultdict

from backend.runtime.policy import PolicyBook
from shared.config import Authority, Policy
from shared.models import Decision, Proposal, Verdict


class AuthorityCheck:
    def __init__(self, authority: Authority, policies: PolicyBook):
        self.authority = authority
        self.policies = policies
        self._spent_by_asset: dict[str, float] = defaultdict(float)
        self._spent_fleet = 0.0

    @property
    def fleet_spend(self) -> float:
        return round(self._spent_fleet, 2)

    def asset_spend(self, asset_id: str) -> float:
        return round(self._spent_by_asset[asset_id], 2)

    def new_round(self) -> None:
        """A new round starts the spend count over.

        The budget is discretion within one round. The simulator sets up fresh aircraft; if
        spend kept accumulating here alone, the limits would be full from the second round on
        and nothing would be approved.
        """
        self._spent_by_asset.clear()
        self._spent_fleet = 0.0

    def record_spend(self, proposal: Proposal) -> None:
        """Call only after execution. Money still awaiting approval is not spent yet."""
        self._spent_by_asset[proposal.asset_id] += proposal.cost_usd
        self._spent_fleet += proposal.cost_usd

    @staticmethod
    def policy_denial(proposal: Proposal, banned: Policy) -> Decision:
        """A refusal from a ban. Its end (until_tick) is carried as a value: the screen shows
        'until tick N', and the operator files again then."""
        return Decision(
            proposal.id,
            Verdict.DENIED,
            f"{banned.reason} ({banned.id})",
            policy_hit=banned.id,
            forbids=banned.forbid_resource or banned.forbid_action,
            code="policy",
            detail={"policy": banned.id, "until_tick": banned.active_until_tick},
        )

    def evaluate(self, proposal: Proposal, asset: dict, tick: int) -> Decision:
        problems = proposal.validate()
        if problems:
            return Decision(proposal.id, Verdict.DENIED, "; ".join(problems), code="invalid")

        banned: Policy | None = self.policies.hit(
            proposal.action, proposal.resource, asset, tick
        )
        if banned:
            return self.policy_denial(proposal, banned)

        if proposal.action in self.authority.human_required_actions:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"'{proposal.action}' is an action a human must look at",
                authority_hit="human_required_actions",
                code="human_action", detail={"action": proposal.action},
            )

        if proposal.blast_radius in self.authority.human_required_blast:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"blast radius is '{proposal.blast_radius}'",
                authority_hit="human_required_blast",
                code="human_blast", detail={"blast": proposal.blast_radius},
            )

        # Spending limits are the operator's call. With no limit in the config (null) the
        # runtime ignores money: with a limit left on, one aircraft ended up stuck waiting for
        # human approval after eight landing-site reservations.
        asset_after = self._spent_by_asset[proposal.asset_id] + proposal.cost_usd
        if self.authority.per_asset_usd is not None and asset_after > self.authority.per_asset_usd:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"over the aircraft limit: ${asset_after:.0f} > "
                f"${self.authority.per_asset_usd:.0f}",
                authority_hit="per_asset_usd",
                code="over_asset",
                detail={"spent": round(asset_after), "cap": round(self.authority.per_asset_usd)},
            )

        fleet_after = self._spent_fleet + proposal.cost_usd
        if self.authority.fleet_usd is not None and fleet_after > self.authority.fleet_usd:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"over the fleet limit: ${fleet_after:.0f} > ${self.authority.fleet_usd:.0f}",
                authority_hit="fleet_usd",
                code="over_fleet",
                detail={"spent": round(fleet_after), "cap": round(self.authority.fleet_usd)},
            )

        return Decision(proposal.id, Verdict.AUTO, "within limits", code="within_limits")
