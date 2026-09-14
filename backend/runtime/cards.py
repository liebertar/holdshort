"""Everything that raises, answers and takes down a card waiting for a person.

The one invariant this file buys: the card triple — _awaiting_human, _open_cards and the
decisions on the card path — is written only here. Elsewhere it is created in the constructor
and read by snapshot under _guard.
"""

from backend.intake.book import WeatherHold
from backend.intake.entries import FLEET_ASSET, INTAKE_ASSET, INTAKE_CHECKS
from backend.runtime.intents import Intent
from shared.models import Decision, Proposal, Verdict


class CardsMixin:
    def _park_for_human(self, proposal: Proposal, decision: Decision) -> Decision:
        """Put up a human card. Its ledger entry stays open until the human's answer (or the
        end of the round) closes it.

        In a live run one aircraft went over its limit and got a 'human' answer 309 times with
        not a single ledger line, and the card survived the round change. If the same card is
        already up, its decision is returned as is (the approval screen doesn't stack duplicate
        cards), but that is a judgement too, so one line is written (outcome waiting).
        """
        with self._guard:
            existing = next((waiting for waiting in self._awaiting_human.values()
                             if waiting.asset_id == proposal.asset_id
                             and waiting.action == proposal.action), None)
            if existing is None:
                self._awaiting_human[proposal.id] = proposal
        if existing is not None:
            repeat = Decision(proposal.id, Verdict.HUMAN,
                              f"the same card ({existing.id}) is already awaiting approval "
                              f"— {decision.reason}",
                              authority_hit=decision.authority_hit, code=decision.code,
                              detail={**decision.detail, "waiting_on": existing.id})
            self.ledger.close_entry(
                self.ledger.open_entry(proposal, repeat, self._context(proposal)), "waiting")
            self._checks.pop(proposal.id, None)
            return self._decisions[existing.id]
        self._open_cards[proposal.id] = self.ledger.open_entry(proposal, decision,
                                                              self._context(proposal))
        return decision

    def _close_card(self, card, proposal: Proposal, decision: Decision, outcome: str,
                    check: str = "human") -> None:
        """Close an open card entry. If it is missing (it shouldn't be), write a fresh pair."""
        if card is None:
            card = self.ledger.open_entry(proposal, decision, self._context(proposal))
        checks = list(card.context.get("checks_run") or []) + [check]
        self.ledger.close_entry(card, outcome, decision, {"tick": self.tick, "checks_run": checks})

    def approve(self, proposal_id: str, actor: str, allow: bool) -> Decision | None:
        with self._guard:
            proposal = self._awaiting_human.pop(proposal_id, None)
        if proposal is None:
            return None
        # A human's answer queues on the judgement lock too — an approval leads to rejudge,
        # execution and intent registration.
        with self._judging:
            return self._answer_card(proposal, proposal_id, actor, allow)

    def _answer_card(self, proposal: Proposal, proposal_id: str, actor: str,
                     allow: bool) -> Decision:
        decision = self._decisions[proposal_id]
        decision.approved_by = actor
        card = self._open_cards.pop(proposal_id, None)
        if proposal.action == "publish_notice":
            return self._confirm_notice(proposal, decision, actor, allow, card)
        if proposal.action == "publish_weather":
            return self._confirm_weather(proposal, decision, actor, allow, card)
        if proposal.action == "lift_weather_hold":
            return self._confirm_lift(proposal, decision, actor, allow, card)
        if proposal.action == "lost_link_notice":
            return self._confirm_lost_link(proposal, decision, actor, allow, card)
        if not allow:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} refused"
            self._close_card(card, proposal, decision, "denied")
            return decision
        decision.verdict = Verdict.AUTO
        decision.reason = f"{actor} approved"
        # The card's line closes with the human's answer; the rejudge and execution that follow
        # write their own lines.
        self._close_card(card, proposal, decision, "approved")
        if self._rejudge(proposal, decision):
            return decision   # airspace changed while waiting; approval can't revive the old route
        return self._queue_or_commit(proposal, decision)

    def _expire_cards(self, why: str) -> None:
        """When the round ends, human cards come down too: held notices as lapsed, the rest
        because the round changed.

        In a live run five cards from the previous round were still up after the change — no
        one should approve a previous round's over-limit card for the new round's aircraft.
        """
        for record in [r for r in list(self.notices.records.values()) if r.held]:
            self._lapse_held(record, why)
        with self._guard:
            cards = list(self._awaiting_human.items())
            self._awaiting_human.clear()
        for proposal_id, proposal in cards:
            decision = self._decisions.get(proposal_id) or Decision(proposal_id, Verdict.HUMAN, "")
            decision.verdict = Verdict.DENIED
            decision.reason = f"{why} before a human saw it — card taken down"
            decision.code = "card_lapsed"
            self._close_card(self._open_cards.pop(proposal_id, None), proposal, decision, "lapsed")

    def _lapse_held(self, record, why: str) -> None:
        """A held notice lapsed unapproved. It never applied, so only the record is closed."""
        self.notices.forget(record.id, f"{why} before confirmation")
        self._rule_close(record.id, "lapsed")
        with self._guard:
            waiting = next((pid for pid, p in self._awaiting_human.items()
                            if p.action == "publish_notice"
                            and p.params.get("notice_id") == record.id), None)
            if waiting is not None:
                self._awaiting_human.pop(waiting)
        entry = self._open_cards.pop(waiting, None) if waiting is not None else None
        if entry is None:
            return
        decision = self._decisions[waiting]
        decision.verdict = Verdict.DENIED
        decision.reason = f"{why} before a human confirmed — never applied"
        decision.code = "notice_lapsed"
        self.ledger.close_entry(entry, "lapsed", decision, {"tick": self.tick})

    def _hold_notice(self, item: dict, record, why: str | None = None) -> None:
        """Post a held notice to the approval screen; it blocks nothing until a human approves.

        Notices the model structured, and notices from outside the runtime's feed (search, manual
        input), come here. A held notice is shaped like a filing (action publish_notice), so the
        existing approval screen shows it as is."""
        held = Proposal(
            asset_id="airspace", action="publish_notice", cost_usd=0.0, blast_radius="none",
            rationale=f"{record.name} — {record.text}"[:180], author=record.source,
            params={"notice_id": record.id, "text": record.text, "notice": record.to_dict(),
                    "source": record.source},
        )
        decision = Decision(held.id, Verdict.HUMAN,
                            why or "a notice a model read applies only once a human confirms it",
                            authority_hit="model_notice", code="human_notice",
                            detail={"notice": record.id, "source": record.source})
        self._decisions[held.id] = decision
        with self._guard:
            self._awaiting_human[held.id] = held
        self._open_cards[held.id] = self.ledger.open_entry(
            held, decision, self._context(None, ["notice:grammar", "notice:model"]))

    def _raise_lift_card(self, hold: WeatherHold) -> None:
        """The 'lift early?' card on the approval screen. Approval lifts the hold on the spot;
        refusal keeps it until the window ends."""
        card = Proposal(asset_id=FLEET_ASSET, action="lift_weather_hold", cost_usd=0.0,
                        blast_radius="none", author="runtime",
                        rationale=f"{hold.reason} · until tick {hold.until_tick}"[:180],
                        params={"hold": hold.id, "reason": hold.reason,
                                "until_tick": hold.until_tick, "report": hold.report})
        decision = Decision(card.id, Verdict.HUMAN,
                            "lifting a weather hold before its window closes is a human's call",
                            authority_hit="weather_hold", code="human_lift",
                            detail={"hold": hold.id, "until_tick": hold.until_tick})
        self._decisions[card.id] = decision
        with self._guard:
            self._awaiting_human[card.id] = card
        self._open_cards[card.id] = self.ledger.open_entry(card, decision,
                                                           self._context(None, ["weather"]))
        hold.lift_card = card.id

    def _refresh_lift_card(self, hold: WeatherHold) -> None:
        """The hold's circumstances changed (a within-limits report came later, the window was
        extended). The hold doesn't lift itself; the card is made to state the current window
        and that fact."""
        later = hold.later_report
        with self._guard:
            standing = self._awaiting_human.get(hold.lift_card or "")
        if standing is None:
            self._raise_lift_card(hold)
            with self._guard:
                standing = self._awaiting_human.get(hold.lift_card or "")
        if standing is None:
            return
        standing.params = {**standing.params, "until_tick": hold.until_tick,
                           "later_report": later or {}}
        rationale = f"{hold.reason} · until tick {hold.until_tick}"
        if later:
            rationale += f" · later report within limits ({later.get('text', '')})"
        standing.rationale = rationale[:180]

    def _hold_weather(self, record, report, breaches: list[str], until_tick: int,
                      read_by: str) -> None:
        """Weather over limits, but not a grammar read of the runtime's feed (model, search,
        manual). Nothing is held until a human approves; the card states the window the hold
        would cover (until tick)."""
        held = Proposal(asset_id=INTAKE_ASSET, action="publish_weather", cost_usd=0.0,
                        blast_radius="none", author=read_by,
                        rationale=(f"WEATHER · {' · '.join(breaches)} · until tick {until_tick} "
                                   f"— {record.text}")[:180],
                        params={"item": record.id, "report": report.to_dict(),
                                "breaches": list(breaches), "until_tick": until_tick,
                                "source": read_by, "origin": record.source,
                                "text": record.text[:400]})
        decision = Decision(held.id, Verdict.HUMAN, self.intake.held_why(record, read_by),
                            authority_hit="model_weather", code="human_weather",
                            detail={"item": record.id, "source": read_by,
                                    "origin": record.source, "breaches": list(breaches),
                                    "until_tick": until_tick})
        self._decisions[held.id] = decision
        with self._guard:
            self._awaiting_human[held.id] = held
        self._open_cards[held.id] = self.ledger.open_entry(held, decision,
                                                           self._context(None, INTAKE_CHECKS))
        self.intake.held_weather[record.id] = {
            "id": record.id, "report": report.to_dict(), "breaches": list(breaches),
            "until_tick": until_tick, "source": read_by, "card": held.id,
            "text": record.text[:180]}
        self._rule_open(record.id, "weather_hold", self.tick, until_tick, applied=False)

    def _drop_card(self, proposal_id: str | None, code: str, why: str) -> None:
        """Take down one standing card (lapsed). No-op if a human already answered."""
        with self._guard:
            proposal = self._awaiting_human.pop(proposal_id or "", None)
        if proposal is None:
            return
        decision = self._decisions.get(proposal.id) or Decision(proposal.id, Verdict.HUMAN, "")
        decision.verdict = Verdict.DENIED
        decision.reason = why
        decision.code = code
        self._close_card(self._open_cards.pop(proposal.id, None), proposal, decision, "lapsed",
                         "weather:window")

    def _raise_link_card(self, asset: str, detail: dict, intent: Intent | None) -> None:
        """Lost-link notice on the approval screen. Approval releases the held space now;
        refusal keeps it until telemetry returns. When telemetry returns, the card comes down
        by itself."""
        until = detail.get("reserved_until_tick")
        card = Proposal(asset_id=asset, action="lost_link_notice", cost_usd=0.0,
                        blast_radius="schedule", author="runtime",
                        rationale=(f"no telemetry since tick {detail['since_tick']} · "
                                   f"{detail['behaviour']} · "
                                   + (f"space reserved until tick {until}" if until is not None
                                      else "nothing filed to reserve"))[:180],
                        params=dict(detail))
        decision = Decision(card.id, Verdict.HUMAN,
                            "releasing the space of an aircraft with a lost link early is "
                            "a human's call — approve and it goes now, refuse and it is "
                            "held until telemetry returns",
                            authority_hit="lost_link", code="human_lost_link", detail=dict(detail))
        self._decisions[card.id] = decision
        with self._guard:
            self._awaiting_human[card.id] = card
        self._open_cards[card.id] = self.ledger.open_entry(
            card, decision, self._context(None, ["link"], intent.id if intent else None))
        self._link_cards[asset] = card.id

    def _drop_link_card(self, asset: str, detail: dict) -> None:
        """Telemetry is back; take the lost-link card down (lapsed). No-op if a human already
        answered."""
        card_id = self._link_cards.pop(asset, None)
        with self._guard:
            proposal = self._awaiting_human.pop(card_id or "", None)
        if proposal is None:
            return
        decision = self._decisions.get(proposal.id) or Decision(proposal.id, Verdict.HUMAN, "")
        decision.verdict = Verdict.AUTO
        decision.reason = "telemetry is back — card taken down"
        decision.code = "link_restored"
        decision.detail = {**decision.detail, **detail}
        self._close_card(self._open_cards.pop(proposal.id, None), proposal, decision, "lapsed",
                         "link")
