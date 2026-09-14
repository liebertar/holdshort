"""4D intents, strategic deconfliction (F3548) and conformance.

Owner of `intents`: every intent is registered, retired and compared against telemetry here.
"""

from backend.runtime.form import ROUTED
from backend.runtime.intents import (
    ACCEPTED,
    PRESENCE,
    Intent,
    first_conflict,
    ground_conflict,
    hold,
    landing_conflict,
    schedule,
)
from shared.config import plural
from shared.models import Decision, Proposal, Verdict


class TrafficMixin:
    def _departure(self, proposal: Proposal) -> tuple[int, float]:
        """The tick this filing really departs and its altitude then: (tick, start altitude).

        On the ground: now + clearance confirmation (CLEARANCE_TICKS); if still loading or
        unloading, after that finishes; if the operator deferred departure
        (depart_after_tick), then. Airborne: now, from the current altitude.
        """
        state = self.telemetry.get(proposal.asset_id, {})
        altitude = float(state.get("alt_m") or 0.0)
        if altitude > 1.0:
            return self.tick, altitude
        work = max(0, int(state.get("work_ticks") or 0))
        depart = self.tick + max(self.performance.clearance_ticks, work)
        after = proposal.params.get("depart_after_tick")
        if after is not None:
            depart = max(depart, int(after))
        return depart, 0.0

    def _intend(self, proposal: Proposal) -> Intent:
        """The intent for one filing.

        The operator filed only the path; the timing is ours, computed from declared performance.
        """
        legs = proposal.params["legs"]
        depart, start_alt = self._departure(proposal)
        volumes, arrive = schedule(legs, depart, start_alt, self.performance)
        return Intent(
            asset=proposal.asset_id, proposal_id=proposal.id, volumes=volumes,
            start=(float(legs[0]["lat"]), float(legs[0]["lon"])),
            landing=(float(legs[-1]["lat"]), float(legs[-1]["lon"])),
            depart_tick=depart, arrive_tick=arrive, filed_tick=self.tick,
            contingency=self.performance.lost_link.behaviour,
        )

    def _check_traffic(self, proposal: Proposal, checks: list[str] | None = None) -> str | None:
        """Does this overlap another aircraft's live intent in space and time (F3548 strategic
        deconfliction)?

        First filed wins. The exception is an airborne aircraft's emergency re-filing (after a
        recall): it is not refused, and if the overlapping aircraft has not taken off yet, that
        intent is withdrawn. Re-filing from the ground is cheaper than hovering and waiting. If
        the other aircraft is airborne too, it cannot be withdrawn, so the filing is refused.
        """
        checks = checks if checks is not None else []
        if proposal.action not in ROUTED or not proposal.params.get("legs"):
            return None
        checks.append("traffic")
        intent = self._intend(proposal)
        asset = proposal.asset_id
        airborne = float(self.telemetry.get(asset, {}).get("alt_m") or 0.0) > 1.0
        last_leg = len(proposal.params["legs"]) - 1
        # Aircraft to withdraw. They are only chosen here; the actual withdrawal happens after
        # this filing executes (_on_committed) — if the check withdrew someone else's clearance
        # and this filing then never went out (human hold, limit, autopilot failure), the ground
        # aircraft would lose its route and the airborne one would be left without a clearance.
        withdraw: list[str] = []
        while True:
            others = self._others(asset, exclude=withdraw)
            conflict = first_conflict(intent.volumes, others)
            if conflict is None:
                checks.append("landing_site")
                conflict = landing_conflict(intent.landing, intent.arrive_tick, last_leg,
                                            others, self.tick)
            if conflict is None:
                conflict = ground_conflict(intent.landing, intent.arrive_tick, last_leg,
                                           self._occupants(asset, exclude=withdraw), self.tick)
            if conflict is None:
                proposal.params = {k: v for k, v in proposal.params.items() if k != "withdraw"}
                if withdraw:
                    proposal.params = {**proposal.params, "withdraw": withdraw}
                return None
            other = self.intents.get(conflict.asset)
            # A lost-link aircraft's intent cannot be withdrawn — withdrawal is a command to the
            # autopilot, and that aircraft cannot hear it. In that case this filing is refused.
            if (not airborne or conflict.asset in withdraw or other is None
                    or other.state != ACCEPTED or other.id != conflict.intent_id
                    or self.links.lost(conflict.asset)):
                break
            withdraw.append(other.asset)

        proposal.params = {
            **proposal.params,
            "blocked_kind": conflict.kind,
            "blocked_asset": conflict.asset,
            "blocked_intent": conflict.intent_id,
            "blocked_leg": conflict.leg,
            "blocked_at": {"lat": round(conflict.at[0], 6), "lon": round(conflict.at[1], 6)},
            "blocked_until_tick": conflict.until_tick,
        }
        if conflict.kind == "landing":
            return (f"{conflict.asset} holds the landing spot until tick {conflict.until_tick} — "
                    "no two aircraft on one landing area")
        return (f"leg {conflict.leg} overlaps the cleared route of {conflict.asset} "
                f"(tick {conflict.tick}, their corridor runs to tick {conflict.until_tick})")

    def _others(self, asset: str, exclude: list[str] | tuple[str, ...] = ()) -> list[Intent]:
        """Everything this filing must avoid: other aircraft's live intents plus the positions
        of airborne aircraft that have no intent.

        An airborne aircraft is always somewhere. If it dropped out of judgement for lacking an
        intent after a recall, withdrawal or declined job, filings through its position would
        be cleared. If telemetry says it is airborne, its position stands as an open column
        (presence) even when the registry has nothing.
        """
        live = [i for i in self.intents.others(asset) if i.asset not in exclude]
        covered = {i.asset for i in live}
        for other, state in self.telemetry.items():
            if other == asset or other in covered or other in exclude or other in self._released:
                # A lost-link aircraft whose reservation a human released: its telemetry is frozen
                # where the link dropped. The aircraft isn't there, and the human released it
                # knowing that.
                continue
            if float(state.get("alt_m") or 0.0) <= 1.0 or state.get("lat") is None:
                continue
            live.append(hold(other, (float(state["lat"]), float(state["lon"])),
                             float(state["alt_m"]), self.tick, self.performance, kind=PRESENCE))
        return live

    def _occupants(self, asset: str, exclude: list[str] | tuple[str, ...] = ()) \
            -> list[tuple[str, tuple[float, float], Intent | None]]:
        """Other aircraft standing on the ground: (aircraft, position, live intent or None).

        Intents of aircraft being withdrawn (exclude) count as absent — the aircraft itself
        still stands there.
        """
        found = []
        for other, state in self.telemetry.items():
            if other == asset or state.get("lat") is None:
                continue
            if float(state.get("alt_m") or 0.0) > 1.0:
                continue
            intent = self.intents.get(other)
            if intent is not None and (not intent.live or other in exclude):
                intent = None
            found.append((other, (float(state["lat"]), float(state["lon"])), intent))
        return found

    def _end_intent(self, asset: str, reason: str, exit_point: dict | None = None) -> Intent | None:
        """End an intent. If the aircraft is airborne, a holding spot (contingency) takes over.

        A recalled aircraft flies to the nearest way out (exit) and hovers there. That path and
        that spot are not empty — other filings must see them until a new clearance replaces
        them or the aircraft lands.
        """
        ended = self.intents.end(asset, reason)
        state = self.telemetry.get(asset) or {}
        altitude = float(state.get("alt_m") or 0.0)
        if altitude > 1.0 and state.get("lat") is not None:
            door = None
            if exit_point and exit_point.get("lat") is not None:
                door = (float(exit_point["lat"]), float(exit_point["lon"]))
            self.intents.accept(hold(asset, (float(state["lat"]), float(state["lon"])), altitude,
                                     self.tick, self.performance, exit_point=door,
                                     proposal_id=ended.proposal_id if ended else ""))
        return ended

    def _observe(self) -> None:
        """Advance intent states from telemetry.

        An aircraft that took off earlier than its cleared window is recorded in the ledger.
        """
        self.intents.observe(self.telemetry, self.tick)
        for intent, planned in self.intents.drain_nonconforming():
            self._ledger_nonconformance(intent, planned)

    def _ledger_nonconformance(self, intent: Intent, planned_tick: int) -> None:
        """The autopilot did not honour a deferred departure. The intent was re-registered at
        the actual departure; this records it.

        Nothing is undone — stopping an airborne aircraft is a recall, and that call isn't made
        here.
        """
        noted = Proposal(asset_id=intent.asset, action="conformance", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"took off before the cleared departure tick {planned_tick} "
                                   f"(tick {self.tick})",
                         params={"intent": intent.id, "planned_depart_tick": planned_tick,
                                 "actual_depart_tick": intent.depart_tick})
        decision = Decision(noted.id, Verdict.AUTO,
                            f"{intent.asset} took off "
                            f"{plural(planned_tick - self.tick, 'tick')} before its cleared "
                            "window — intent moved to the actual departure",
                            code="nonconforming",
                            detail={"resource": intent.asset, "intent": intent.id,
                                    "planned_depart_tick": planned_tick})
        entry = self.ledger.open_entry(noted, decision,
                                       self._context(None, ["conformance"], intent.id))
        self.ledger.close_entry(entry, "noted")

    def _withdraw(self, intent: Intent, for_proposal: Proposal) -> Decision:
        """Withdraw an intent that has not taken off. The runtime authors this decision; the
        autopilot only receives a waypoint clear.

        The grounded aircraft just loses its route and stays where it is (sim divert_ground).
        Its operator sees the route is gone and files again — by then the airborne one counts
        as having filed first.
        """
        retreat = Proposal(
            asset_id=intent.asset, action="divert_ground", cost_usd=0.0, blast_radius="cargo",
            rationale=f"gave way to the airborne re-filing of {for_proposal.asset_id}",
            author="runtime",
            params={"withdrawn_for": for_proposal.asset_id, "intent": intent.id},
        )
        decision = Decision(
            retreat.id, Verdict.AUTO,
            f"overlaps the airborne re-filing of {for_proposal.asset_id} — the route that has "
            f"not taken off is pulled back",
            policy_hit="traffic", code="withdrawn",
            detail={"resource": intent.asset, "for": for_proposal.asset_id, "intent": intent.id},
        )
        self._decisions[retreat.id] = decision
        entry = self.ledger.open_entry(retreat, decision,
                                       self._context(None, ["withdraw"], intent.id))
        decision.ledger_id = entry.id
        result = self.adapter.execute(intent.asset, "divert_ground", retreat.params, entry.id,
                                      blast=retreat.blast_radius)
        ok = bool(result.get("ok"))
        self.ledger.close_entry(entry, "done" if ok else f"failed: {result.get('error')}", decision)
        self._end_intent(intent.asset, "withdrawn")
        # Drop the withdrawn route's commit record from dedupe. Otherwise that aircraft's
        # re-filing is refused as "the same filing just executed" — even though what executed
        # was just withdrawn.
        for action in ROUTED:
            self._recent_commits.pop((intent.asset, action), None)
        return decision

    def _on_committed(self, proposal: Proposal, decision: Decision, entry) -> dict | None:
        """Right after a successful execution. For a route, (withdraw the chosen aircraft and)
        create the intent; for any other action, end that aircraft's intent.

        Withdrawal happens only here — a filing that did not execute withdraws no one.
        """
        if proposal.action in ROUTED and proposal.params.get("legs"):
            self.advisor.succeeded(proposal.asset_id)   # cleared, so the refusal streak ends
            # If the corridor enters a neighbourhood not yet asked about this round, the
            # pre-flight briefing asks about that cell (next poll, worker thread). Only the cell
            # is noted here — the approval thread does not wait on the network.
            self.briefing.corridor_cleared(proposal.params["legs"], proposal.asset_id)
            withdrew = []
            for other in proposal.params.get("withdraw") or []:
                standing = self.intents.get(other)
                if standing is not None and standing.state == ACCEPTED:
                    self._withdraw(standing, proposal)
                    withdrew.append(other)
            intent = self._intend(proposal)
            self.intents.accept(intent)
            proposal.params = {k: v for k, v in proposal.params.items() if k != "withdraw"}
            if withdrew:
                proposal.params = {**proposal.params, "withdrew": withdrew}
            entry.proposal = proposal.to_dict()   # closing line holds the filing after withdrawals
            return {"intent_id": intent.id, **({"withdrew": withdrew} if withdrew else {})}
        if proposal.action == "decline_job":
            # Declining the job after refusals ("no legal route"). Written after the decline
            # executes — writing it at filing time would add another advisory for a decline that
            # was refused as a duplicate. The order is gone, so the refusal streak ends too.
            if self.advisor.declined(proposal.asset_id):
                self._advise(proposal.asset_id, "decline_after_refusals")
            self.advisor.succeeded(proposal.asset_id)
        ended = self._end_intent(proposal.asset_id, proposal.action,
                                 exit_point=proposal.params.get("exit"))
        return {"intent_id": ended.id} if ended is not None else None
