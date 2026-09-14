import unittest

from backend.runtime.authority import AuthorityCheck
from backend.runtime.policy import PolicyBook
from shared.config import Authority, Policy
from shared.models import Proposal, Verdict


def make(**kwargs) -> Proposal:
    base = dict(
        asset_id="drone-01", action="charge", cost_usd=12.0,
        blast_radius="none", rationale="low battery",
    )
    return Proposal(**{**base, **kwargs})


class AuthorityTest(unittest.TestCase):
    def setUp(self):
        self.policies = PolicyBook()
        self.authority = AuthorityCheck(
            Authority(
                per_asset_usd=200,
                fleet_usd=500,
                human_required_blast=["passenger", "public"],
                human_required_actions=["disengage_autonomy"],
            ),
            self.policies,
        )
        self.asset = {"model": "dv-x500"}

    def test_clears_under_limit(self):
        self.assertIs(self.authority.evaluate(make(), self.asset, 0).verdict, Verdict.AUTO)

    def test_policy_denies_before_any_limit(self):
        self.policies.add(Policy("recall-1", "fire reports", forbid_action="fast_charge",
                                 applies_to={"model": "dv-x500"}))
        decision = self.authority.evaluate(
            make(action="fast_charge", cost_usd=0.0), self.asset, 0
        )
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "recall-1")

    def test_policy_ignores_other_models(self):
        self.policies.add(Policy("recall-1", "fire reports", forbid_action="fast_charge",
                                 applies_to={"model": "dv-x500"}))
        decision = self.authority.evaluate(
            make(action="fast_charge"), {"model": "dv-hexa"}, 0
        )
        self.assertIs(decision.verdict, Verdict.AUTO)

    def test_passenger_blast_needs_a_human_even_when_free(self):
        decision = self.authority.evaluate(
            make(action="disengage_autonomy", cost_usd=0.0, blast_radius="passenger"),
            self.asset, 0,
        )
        self.assertIs(decision.verdict, Verdict.HUMAN)

    def test_fleet_limit_binds_before_per_asset_limits_are_reached(self):
        # 200 per aircraft, 600 for three. The fleet limit of 500 binds first.
        for asset in ("drone-01", "drone-02"):
            for _ in range(10):
                proposal = make(asset_id=asset, cost_usd=20.0)
                if self.authority.evaluate(proposal, self.asset, 0).verdict is Verdict.AUTO:
                    self.authority.record_spend(proposal)
        decision = self.authority.evaluate(make(asset_id="drone-03", cost_usd=200.0),
                                           self.asset, 0)
        self.assertIs(decision.verdict, Verdict.HUMAN)
        self.assertEqual(decision.authority_hit, "fleet_usd")

    def test_a_closed_zone_denies_the_resource_inside_it(self):
        """A no-fly zone comes down as 'do not use that pad'. It is a resource, not an action."""
        self.policies.add(Policy("nofly-1", "hospital medevac", forbid_resource="pad:P2"))
        blocked = make(action="reserve_pad", cost_usd=28.0, resource="pad:P2")
        allowed = make(action="reserve_pad", cost_usd=28.0, resource="pad:P1")
        self.assertIs(self.authority.evaluate(blocked, self.asset, 0).verdict, Verdict.DENIED)
        self.assertIs(self.authority.evaluate(allowed, self.asset, 0).verdict, Verdict.AUTO)

    def test_an_empty_policy_forbids_nothing(self):
        self.policies.add(Policy("empty", "forbids nothing"))
        self.assertIs(self.authority.evaluate(make(), self.asset, 0).verdict, Verdict.AUTO)

    def test_malformed_proposal_is_refused(self):
        decision = self.authority.evaluate(make(blast_radius="nonsense"), self.asset, 0)
        self.assertIs(decision.verdict, Verdict.DENIED)


if __name__ == "__main__":
    unittest.main()
