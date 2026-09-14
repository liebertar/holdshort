"""The three ways clearance already given is taken back: a weather hold, a new zone, a banned
resource.

Called from intake through self, but every line here is adapter commands, intent retirement,
lock release and ledger. Named recall rather than enforcement to keep it apart from intake's
_enforce_policy, which is a different act.
"""

from backend.intake.book import HOLD_POLICY_PREFIX, WeatherHold
from backend.runtime.form import ROUTED
from backend.runtime.intents import ACCEPTED
from shared.geo import Airspace, first_breach, nearest_exit
from shared.models import Decision, Proposal, Verdict


class RecallMixin:
    def _ground_for_hold(self, hold: WeatherHold) -> None:
        """Pull back routes cleared on the ground but not yet flown. Airborne aircraft are left
        alone: they need to come down.

        Policies block only new filings. Departing on a route already cleared (25 ticks after
        the clearance is confirmed) is not a filing, so unless pulled back it takes off during
        the hold.
        """
        for asset_id, state in list(self.telemetry.items()):
            if float(state.get("alt_m") or 0.0) > 1.0:
                continue
            # The telemetry route is from the last poll. A route cleared on the very tick the
            # hold opened wasn't there yet, so it was never pulled back and took off 25 ticks
            # later, mid-hold (one 'takeoff during hold' on the runtime side).
            # Go by the intent the runtime recorded: an intent that is cleared but hasn't
            # departed gets pulled back.
            waiting = self.intents.get(asset_id)
            undeparted = waiting is not None and waiting.state == ACCEPTED
            if not state.get("route") and not undeparted:
                continue
            retreat = Proposal(asset_id=asset_id, action="divert_ground", cost_usd=0.0,
                               blast_radius="cargo", author="runtime",
                               rationale=f"{hold.reason} — the route that has not taken "
                                         f"off is pulled back",
                               params={"hold": hold.id})
            decision = Decision(retreat.id, Verdict.AUTO,
                                f"{hold.reason} — {asset_id}'s cleared route is pulled back; "
                                f"it has not taken off",
                                policy_hit=HOLD_POLICY_PREFIX, code="recalled",
                                detail={"resource": asset_id, "policy": HOLD_POLICY_PREFIX,
                                        "until_tick": hold.until_tick})
            standing = self.intents.get(asset_id)
            intent_id = standing.id if standing is not None and standing.live else None
            entry = self.ledger.open_entry(retreat, decision,
                                           self._context(None, ["weather"], intent_id))
            decision.ledger_id = entry.id
            result = self.adapter.execute(asset_id, "divert_ground", retreat.params, entry.id,
                                          blast=retreat.blast_radius)
            ok = bool(result.get("ok"))
            self.ledger.close_entry(entry, "done" if ok else f"failed: {result.get('error')}",
                                    decision)
            self._end_intent(asset_id, "weather_hold")
            for action in ROUTED:
                self._recent_commits.pop((asset_id, action), None)

    def recall_flights(self, volume) -> list[Decision]:
        """Re-judge routes already cleared and in flight against a new zone.

        Refusing is not enough: a flight cleared before the rule arrived doesn't know the rule
        and keeps flying. Having an enforcement point means undoing what has already happened.
        A recalled aircraft's route intent ends; that route won't be flown any more, so it must
        not block anyone else. The way out, and a place to hover there (contingency), take its
        place.
        """
        pulled = []
        for asset_id, telemetry in self.telemetry.items():
            if self.links.lost(asset_id):
                # A recall can't reach an aircraft with a lost link, so none is sent. Its space
                # stays reserved and judgement keeps avoiding that volume.
                continue
            legs = [{"lat": telemetry.get("lat"), "lon": telemetry.get("lon"),
                     "alt_m": telemetry.get("alt_m", 0.0)}]
            legs += [{"lat": leg["lat"], "lon": leg["lon"], "alt_m": leg.get("alt_m", 0.0)}
                     for leg in (telemetry.get("route") or [])]
            if len(legs) < 2 or legs[0]["lat"] is None:
                continue
            if first_breach(Airspace([volume], default_ceiling_m=None), legs) is None:
                continue
            # An aircraft inside is sent to the nearest point outside. Stopped in place, it
            # stays in the closed zone, where every route is forbidden from its first point
            # and nothing can be redrawn.
            exit_point = nearest_exit(volume, legs[0]["lat"], legs[0]["lon"])
            params = ({"exit": {"lat": exit_point[0], "lon": exit_point[1]}, "volume": volume.id}
                      if exit_point else {"volume": volume.id})
            retreat = Proposal(
                asset_id=asset_id, action="divert_ground", cost_usd=35.0,
                blast_radius="cargo", rationale=f"{volume.name} ({volume.id})",
                author="runtime", params=params,
            )
            decision = Decision(
                retreat.id, Verdict.AUTO,
                f"route in flight recalled by {volume.id}",
                policy_hit=volume.id, code="recalled",
                detail={"resource": asset_id, "policy": volume.id},
            )
            # The adapter is handed the ledger id (a string). Passing the ledger entry object
            # itself made the HTTP adapter fail to serialise it to JSON, which killed the
            # background thread; from then on the runtime kept serving old positions and every
            # filing started from the wrong place. Tests using only the local adapter missed it.
            standing = self.intents.get(asset_id)
            intent_id = standing.id if standing is not None and standing.live else None
            entry = self.ledger.open_entry(retreat, decision,
                                           self._context(None, ["recall"], intent_id))
            decision.ledger_id = entry.id
            result = self.adapter.execute(asset_id, "divert_ground", params, entry.id,
                                          blast=retreat.blast_radius)
            ok = bool(result.get("ok"))
            self.ledger.close_entry(entry, "done" if ok else f"failed: {result.get('error')}",
                                    decision)
            self._end_intent(asset_id, "recalled", exit_point=params.get("exit"))
            for action in ROUTED:
                self._recent_commits.pop((asset_id, action), None)
            pulled.append(decision)
        return pulled

    def revoke_under(self, policy) -> Decision | None:
        """A ban arrived on a resource already held: revoke it and divert the aircraft.

        This is what separates it from merely refusing: an enforcement point undoes what has
        already happened. The diversion goes out without checking limits, since leaving a
        closed zone is not a budget question. Who ordered it and why still goes on the ledger.
        """
        if not policy.forbid_resource:
            return None
        hold = self.locks.holder(policy.forbid_resource)
        if hold is None or self.links.lost(hold.asset_id):
            return None     # no holder, or its link is lost so it can't hear the divert order

        self.locks.release(policy.forbid_resource, hold.asset_id)
        retreat = Proposal(
            asset_id=hold.asset_id,
            action="divert_ground",
            cost_usd=35.0,
            blast_radius="cargo",
            rationale=f"{policy.reason} ({policy.id})",
            author="runtime",
        )
        decision = Decision(
            retreat.id, Verdict.AUTO, f"{policy.forbid_resource} recalled by {policy.id}",
            policy_hit=policy.id, code="recalled",
            detail={"resource": policy.forbid_resource, "policy": policy.id},
        )
        self._decisions[retreat.id] = decision
        return self.committer.commit(retreat, decision, self._context(None, ["revoke"]))
