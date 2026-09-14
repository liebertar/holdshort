"""The refusal advisory: what the runtime offers after it says no.

Owner of `advisor` and `advisory_async`. It sits in backend/runtime rather than in the intake
package because it is judgement going out, not a feed coming in — _judge_legs literally
re-runs check_route and _check_traffic, and _advise is called only from the judging path.
"""

import threading

from backend.runtime.advisory import Refusal, build_options
from backend.runtime.form import NOT_REFUSALS, ROUTED
from shared.models import Decision, Proposal, Verdict


class AdviceMixin:
    def _record_refusal(self, proposal: Proposal, decision: Decision) -> None:
        """One refusal of a route filing. Three in a row produce an advisory.

        Duplicate refusals don't count — the path isn't blocked; the same filing was sent twice.
        """
        if proposal.action not in ROUTED or decision.code in NOT_REFUSALS:
            return
        params = proposal.params or {}
        refusal = Refusal(
            asset=proposal.asset_id, tick=self.tick, code=decision.code,
            policy_hit=decision.policy_hit,
            blocked_kind=params.get("blocked_kind"), blocked_volume=params.get("blocked_volume"),
            blocked_asset=params.get("blocked_asset"),
            blocked_until_tick=params.get("blocked_until_tick"), proposal_id=proposal.id,
            action=proposal.action, legs=list(params.get("legs") or []),
            params={k: v for k, v in params.items()
                    if k != "legs" and not k.startswith("blocked_")},
            resource=proposal.resource,
        )
        if self.advisor.refused(proposal.asset_id, refusal):
            self._advise(proposal.asset_id, "refusals")

    def _judge_legs(self, refusal: Refusal, legs: list[dict]) -> str | None:
        """Why this route is blocked right now. Only checks advisory options; files nothing."""
        probe = Proposal(asset_id=refusal.asset, action=refusal.action, cost_usd=0.0,
                         blast_radius="none", rationale="advisory probe",
                         params={**refusal.params, "legs": legs},
                         resource=refusal.resource or refusal.params.get("pad"))
        checks: list[str] = []
        return self.check_route(probe, checks) or self._check_traffic(probe, checks)

    def _notice_until(self, volume_id: str | None) -> int | None:
        """The tick a notice zone lifts; None for permanent zones and buildings."""
        record = self.notices.get(volume_id or "")
        return record.until_tick if record is not None else None

    def _advise(self, asset: str, trigger: str) -> None:
        """Write one advisory. Options are judged against the current state; the wording (if a
        model is available) is written on a separate thread.
        """
        refusals = self.advisor.streak(asset)
        if not refusals:
            return
        airborne = float(self.telemetry.get(asset, {}).get("alt_m") or 0.0) > 1.0
        options = build_options(refusals, self._judge_legs, self._notice_until, airborne)
        context = self._context(None, ["advisory"])
        round_at = self._round

        def finish() -> None:
            params = self.advisor.compose(asset, trigger, refusals, options, airborne)
            if round_at != self._round:
                return      # round changed while the model answered; skip last round's advisory
            self._ledger_advisory(asset, params, context)

        if self.advisor.has_model and self.advisory_async:
            threading.Thread(target=finish, daemon=True, name=f"advisory-{asset}").start()
        else:
            finish()

    def _ledger_advisory(self, asset: str, params: dict, context: dict) -> None:
        """An advisory is a ledger entry (action advisory, outcome noted). Nothing is executed."""
        noted = Proposal(asset_id=asset, action="advisory", cost_usd=0.0, blast_radius="none",
                         author="runtime", rationale=params["summary"][:180], params=params)
        decision = Decision(noted.id, Verdict.AUTO, params["summary"], code="advisory",
                            detail={"resource": asset, "chosen": params["chosen"],
                                    "trigger": params["trigger"], "source": params["source"]})
        entry = self.ledger.open_entry(noted, decision, context)
        self.ledger.close_entry(entry, "noted")
        with self._guard:
            self.advisor.latest[asset] = {"asset": asset, "tick": context.get("tick"),
                                          "at": entry.at, "ledger_id": entry.id, **params}
