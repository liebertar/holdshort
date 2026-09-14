"""The weather hold, from the report that opens it to the word that lifts it.

Its human cards live in backend/runtime/cards.py: the card triple has one writer.
"""

from backend.intake.book import HOLD_POLICY_PREFIX, WeatherHold
from backend.intake.entries import FLEET_ASSET
from shared.models import Decision, Proposal, Verdict


class WeatherMixin:
    def _take_weather(self, item: dict, record, report, read_by: str) -> None:
        """Within limits: record only (attached to the lift card if a hold is on). Over limits:
        at once for a grammar read off the runtime's feed; anything else (model, search, manual)
        waits for a human. A report whose window already closed is only recorded."""
        breaches = self.intake.breaches(report)
        until_tick = self.intake.hold_until(report, item, self.tick)
        detail = {"kind": "weather", "read_by": read_by, "breaches": breaches,
                  "report": report.to_dict(), "until_tick": until_tick}
        if not breaches:
            self.intake.note_report(record.id, report, breaches, self.tick, read_by)
            self._ledger_intake(record, item, "intake_read", "noted",
                                "weather report · within limits", detail)
            if self.intake.hold is not None:
                self._refresh_lift_card(self.intake.hold)
            return
        if until_tick <= self.tick:
            # A report for a past window. Opening a hold and lifting it in the same poll would
            # only pull back cleared routes that haven't taken off, for nothing.
            self._ledger_intake(record, item, "intake_read", "noted",
                                f"weather report · out of limits · window already closed "
                                f"at tick {until_tick}",
                                {**detail, "window_closed": True})
            return
        must_hold = self.intake.must_hold(record, read_by)
        if self.intake.hold is not None:
            # Already holding. If the runtime's feed reports a later end, extend to it and move
            # the card to that window.
            hold = self.intake.hold
            if until_tick > hold.until_tick and not must_hold:
                hold.until_tick = until_tick
                self._rule_extend(hold.id, until_tick)
                for policy in hold.policies():
                    self.policies.add(policy)
                self._refresh_lift_card(hold)
                detail["extended_until"] = until_tick
            self.intake.note_report(record.id, report, breaches, self.tick, read_by)
            self._ledger_intake(record, item, "intake_read", "noted",
                                "weather report · out of limits (already holding)", detail)
            return
        if must_hold:
            self._ledger_intake(record, item, "intake_read", "noted",
                                "weather report · out of limits (waiting for a human)",
                                {**detail, "held": True})
            self._hold_weather(record, report, breaches, until_tick, read_by)
            return
        self._ledger_intake(record, item, "intake_read", "noted",
                            "weather report · out of limits", detail)
        self._open_weather_hold(record.id, report, breaches, until_tick, "grammar")

    def _open_weather_hold(self, record_id: str, report, breaches: list[str], until_tick: int,
                           source: str) -> WeatherHold:
        """Ground stop. Policies apply now, and cleared routes not yet airborne are pulled back.
        Only a human or the window lifts it."""
        hold = self.intake.open_hold(record_id, report, breaches, until_tick, self.tick, source)
        self._rule_apply(record_id, "weather_hold", self.tick, until_tick)
        for policy in hold.policies():
            self.policies.add(policy)
        noted = Proposal(asset_id=FLEET_ASSET, action="weather_hold", cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=hold.reason[:180],
                         params={"hold": hold.to_dict()})
        decision = Decision(noted.id, Verdict.AUTO, hold.reason, policy_hit=HOLD_POLICY_PREFIX,
                            code="weather_hold",
                            detail={"until_tick": until_tick, "since_tick": self.tick,
                                    "source": source, "breaches": list(breaches),
                                    "report": report.to_dict()})
        entry = self.ledger.open_entry(noted, decision, self._context(None, ["weather"]))
        self.ledger.close_entry(entry, "noted")
        self._ground_for_hold(hold)
        self._raise_lift_card(hold)
        return hold

    def _confirm_weather(self, proposal: Proposal, decision: Decision, actor: str,
                         allow: bool, card=None) -> Decision:
        """The human's answer. Approval opens the hold from then on, on that human's word;
        refusal only leaves a record."""
        key = str(proposal.params.get("item") or "")
        held = self.intake.held_weather.pop(key, None)
        report = _report_from(held or proposal.params)
        until_tick = int((held or proposal.params).get("until_tick") or self.tick)
        if not (allow and held is not None and self.intake.hold is None
                and self.tick <= until_tick):
            # Nothing holds on this report: the window passed, it was refused, or already holding.
            self._rule_close(key, "lapsed" if allow and self.tick > until_tick
                             else "already_held" if allow else "refused")
        if allow and self.tick > until_tick:
            decision.verdict = Verdict.DENIED
            decision.reason = (f"{actor} confirmed, but the window had already closed at tick "
                               f"{until_tick}")
            decision.code = "weather_lapsed"
            self._close_card(card, proposal, decision, "lapsed", "weather:human")
            return decision
        if allow and held is not None and self.intake.hold is None:
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} confirmed the weather report — takeoffs stopped"
            decision.code = "weather_confirmed"
            self._close_card(card, proposal, decision, "done", "weather:human")
            self._open_weather_hold(held["id"], report, list(held["breaches"]), until_tick, "human")
            return decision
        if allow:
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} confirmed — already holding, recorded only"
            decision.code = "weather_confirmed"
            self._close_card(card, proposal, decision, "done", "weather:human")
            return decision
        decision.verdict = Verdict.DENIED
        decision.reason = f"{actor} refused the weather report"
        decision.code = "weather_refused"
        self._close_card(card, proposal, decision, "denied", "weather:human")
        return decision

    def _confirm_lift(self, proposal: Proposal, decision: Decision, actor: str, allow: bool,
                      card=None) -> Decision:
        """The answer to 'lift early?'. Approval removes the policies; refusal leaves them until
        the window closes."""
        hold = self.intake.hold
        if hold is not None:
            hold.lift_card = None
        if allow and hold is not None and hold.id == proposal.params.get("hold"):
            self._lift_hold(hold, "human")
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} lifted the weather hold"
            decision.code = "weather_hold_lifted"
            self._close_card(card, proposal, decision, "done", "weather:human")
            return decision
        decision.verdict = Verdict.DENIED
        decision.reason = (f"{actor} did not lift it — the hold stands until the window closes"
                           if hold is not None else "there is no hold to lift")
        decision.code = "lift_refused"
        self._close_card(card, proposal, decision, "denied", "weather:human")
        return decision

    def _lift_hold(self, hold: WeatherHold, lifted_by: str) -> None:
        for policy_id in hold.policy_ids:
            self.policies.remove(policy_id)
        self.intake.close_hold()
        self._rule_close(hold.id, lifted_by, self.tick)

    def _tick_intake(self) -> None:
        """Sweep up ended windows: lift the hold and take its card down, and forget held weather
        that lapsed without a human."""
        hold = self.intake.hold
        if hold is not None and self.tick > hold.until_tick:
            self._lift_hold(hold, "window")
            self._ledger_hold_end(hold, "weather_hold_expired",
                                  f"the window closed at tick {hold.until_tick} — hold lifted")
            if hold.lift_card:
                self._drop_card(hold.lift_card, "weather_hold_expired",
                                "the window closed — hold lifted")
        for key, held in list(self.intake.held_weather.items()):
            if self.tick > int(held.get("until_tick") or self.tick):
                self.intake.held_weather.pop(key, None)
                self._rule_close(key, "lapsed")
                self._drop_card(held.get("card"), "weather_lapsed",
                                "the window closed before a human confirmed")

    def _ledger_hold_end(self, hold: WeatherHold, code: str, reason: str) -> None:
        """Line for a hold that ended without a human (window closed, round changed). The ledger
        report closes the hold on this line."""
        noted = Proposal(asset_id=FLEET_ASSET, action="weather_hold", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"{hold.reason} — {reason}"[:180],
                         params={"hold": hold.to_dict()})
        decision = Decision(noted.id, Verdict.AUTO, reason, code=code,
                            detail={"hold": hold.id, "until_tick": hold.until_tick})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, ["weather"])), "noted")


def _report_from(source: dict):
    """Turn a report kept as a dict on a card or the held list back into a WeatherReport."""
    from shared.intake import WeatherReport

    raw = dict(source.get("report") or {})
    return WeatherReport(wind_mps=raw.get("wind_mps"), gust_mps=raw.get("gust_mps"),
                         visibility_m=raw.get("visibility_m"),
                         precipitation=raw.get("precipitation"), from_tick=raw.get("from_tick"),
                         until_tick=raw.get("until_tick"), text=str(raw.get("text") or ""))
