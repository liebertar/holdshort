"""Notices and incidents on their way into force, and out of it again.

Incidents are not in their own file because _apply_notices_locked applies both kinds from one
record list.
"""

import threading

from backend.intake.entries import FLEET_ASSET
from shared import config as config_module
from shared.models import Decision, Proposal, Verdict


class NoticeFlowMixin:
    def _settle_notice(self, item: dict, record) -> None:
        if record is None:
            self._ledger_notice(item, "unreadable", self.notices.unreadable.get(item["id"], ""))
        elif record.held:
            self._hold_notice(item, record)

    def _read_later(self, item: dict, bbox) -> None:
        """The model reads notices the grammar can't, off the world thread. The next poll
        collects the answer.

        Asking on the world thread froze the runtime's ticks and telemetry for the 5-27 s a
        30B-class model took to answer (filings that came in meanwhile were judged at old
        positions), and the 20 s timeout left three rounds out of four unread. One question per
        id at a time (_reading).
        """
        self._reading.add(item["id"])
        round_at = self._round

        def work() -> None:
            try:
                result = self.notices.compile_item(item, bbox)
            except Exception as error:  # noqa: BLE001 — recorded as unreadable
                result = (None, f"model read failed {error!r}")
            with self._guard:
                self._read_notices.append((round_at, item, result))

        threading.Thread(target=work, daemon=True, name=f"notice-{item['id']}").start()

    def _collect_read_notices(self) -> None:
        """Record reader-thread results on the world thread; drop answers from a past round."""
        with self._guard:
            arrived, self._read_notices = self._read_notices, []
        for round_at, item, result in arrived:
            self._reading.discard(item["id"])
            if round_at != self._round or self.notices.known(item["id"]):
                continue
            self._settle_notice(item, self.notices.settle(item, result))

    def _enforce_policy(self, item: dict) -> None:
        # Restrictions apply at once; only loosening is left to a human.
        policy = config_module.Policy(
            id=item["id"],
            reason=item.get("reason", item.get("kind", "")),
            forbid_action=item.get("forbid_action"),
            forbid_resource=item.get("forbid_resource"),
            applies_to=item.get("applies_to", {}),
            active_from_tick=0,
            active_until_tick=item.get("until_tick"),
        )
        self.policies.add(policy)
        self.revoke_under(policy)

    def _apply_notices(self, feed_ids: set[str] | None = None) -> None:
        """Open-window notices go into the airspace and recall flights; closed ones come out.

        The world thread (polling) and the approval thread (human approval) both call this.
        Without the lock, both pick the same notice out of due(), apply it twice and recall the
        same aircraft twice.
        """
        with self._notice_lock:
            self._apply_notices_locked(feed_ids)

    def _apply_notices_locked(self, feed_ids: set[str] | None) -> None:
        for record in self.notices.due(self.tick):
            record.applied = True
            self._rule_apply(record.id)
            self.airspace.add(record.volume)
            self.zone_volumes.add(record.id)
            self.recall_flights(record.volume)
            if record.id not in {p.id for p in self.policies.all()}:
                self.policies.add(config_module.Policy(
                    id=record.id, reason=record.volume.reason or record.name,
                    active_from_tick=record.from_tick or 0, active_until_tick=record.until_tick))
            if record.kind == "incident":
                self._ledger_incident(record)
        feed = feed_ids if feed_ids is not None else {r.id for r in self.notices.records.values()}
        for record in self.notices.lapsed(self.tick, feed):
            record.applied = False
            self._rule_close(record.id, "window", self.tick)
            self.airspace.remove(record.id)
            self.zone_volumes.discard(record.id)
            if record.id not in feed:
                self.notices.forget(record.id)
        for expired in self.zone_volumes - {r.id for r in self.notices.records.values()
                                            if r.applied}:
            self.airspace.remove(expired)
            self.zone_volumes.discard(expired)
        # Notices whose window closed (or that dropped off the list) while waiting for a human:
        # take down the card and banner, and leave a ledger line.
        for record in self.notices.stale_held(self.tick, feed):
            self._lapse_held(record, "the window closed" if record.id in feed
                             else "the notice was taken down")
        # Approved by a human but lapsed before it applied. Left alone, it would stay in
        # /state.notices until the round ends.
        for record in self.notices.stale_confirmed(self.tick, feed):
            self.notices.forget(record.id,
                                ("the window closed" if record.id in feed
                                 else "the notice was taken down")
                                + " after confirmation, before it applied")

    def _confirm_notice(self, proposal: Proposal, decision: Decision, actor: str,
                        allow: bool, card=None) -> Decision:
        """The human's answer. Closes the ledger entry opened at hold time (card) with it.

        Approving a notice whose window already closed leaves nothing to apply, so it is recorded
        as notice_lapsed rather than 'applied' (notice_published). In live runs this is a race
        within a single poll.
        """
        notice_id = proposal.params.get("notice_id", "")
        record = self.notices.get(notice_id)
        if allow and record is not None and record.until_tick is not None \
                and self.tick > record.until_tick:
            self.notices.forget(notice_id, "confirmed after the window closed")
            self._rule_close(notice_id, "lapsed")
            decision.verdict = Verdict.DENIED
            decision.reason = (f"{actor} confirmed, but the window had already closed at tick "
                               f"{record.until_tick} — never applied")
            decision.code = "notice_lapsed"
            self._close_card(card, proposal, decision, "lapsed", "notice:human")
            return decision
        record = self.notices.confirm(notice_id, actor, allow)
        if allow and record is not None:
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} confirmed the notice"
            decision.code = "notice_published"
        else:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} refused the notice" if record is not None \
                else "no such notice"
            decision.code = "notice_refused"
            self._rule_close(notice_id, "refused")
        self._close_card(card, proposal, decision,
                         "done" if allow and record is not None else "denied", "notice:human")
        self._apply_notices()
        return decision

    def _ledger_notice(self, item: dict, outcome: str, why: str) -> None:
        """An unreadable notice is a record too; why it never applied belongs in the ledger."""
        unread = Proposal(asset_id="airspace", action="publish_notice", cost_usd=0.0,
                          blast_radius="none", rationale=str(item.get("text") or "")[:180],
                          author="runtime", params={"notice_id": item["id"],
                                                    "text": item.get("text")})
        decision = Decision(unread.id, Verdict.DENIED, f"the notice could not be read — {why}",
                            code="notice_unreadable", detail={"notice": item["id"], "why": why})
        self.ledger.close_entry(
            self.ledger.open_entry(unread, decision,
                                   self._context(None, ["notice:grammar", "notice:model"])),
            outcome)

    def _take_incident(self, item: dict, record, report, read_by: str) -> None:
        """Incident → keep-out zone (circle), along the notice path: a grammar read of the
        runtime's feed applies this poll; anything else (model, search, manual) waits for a
        human. An incident whose window already closed is only recorded."""
        until_tick = self.intake.window_until(report.from_tick, report.until_tick, item, self.tick)
        detail = {"kind": "incident", "read_by": read_by, "incident": report.to_dict(),
                  "until_tick": until_tick}
        if until_tick <= self.tick:
            self._ledger_intake(record, item, "intake_read", "noted",
                                f"incident · {report.name} · window already closed at tick "
                                f"{until_tick}",
                                {**detail, "window_closed": True})
            return
        held = self.intake.must_hold(record, read_by)
        notice = self.intake.incident_record(record.id, report, record.text, read_by, until_tick,
                                             held)
        self.notices.records[record.id] = notice
        self._rule_open(record.id, "incident", report.from_tick or self.tick, until_tick,
                        applied=not held)
        self._ledger_intake(record, item, "intake_read", "noted",
                            f"incident · {report.name} · {report.radius_m:.0f} m",
                            {**detail, "held": held})
        if held:
            self._hold_notice({"id": record.id, "text": record.text}, notice,
                              self.intake.held_why(record, read_by))

    def _take_notice(self, item: dict, record, notice, read_by: str) -> None:
        """A restriction notice (FAA phrasing, or structured by the model). It goes into the
        notice book and takes the same path."""
        held = self.intake.must_hold(record, read_by)
        adopted = self.notices.adopt(record.id, str(item.get("name") or notice.name or record.id),
                                     {"kind": "notam", "until_tick": item.get("until_tick")},
                                     notice, read_by, held=held)
        self.intake.notice_ids.add(record.id)
        self._rule_open(record.id, "notice", adopted.from_tick or self.tick, adopted.until_tick,
                        applied=not held)
        self._ledger_intake(record, item, "intake_read", "noted",
                            f"restriction notice · {adopted.name}",
                            {"kind": "notice", "read_by": read_by, "held": held,
                             "notice": adopted.to_dict()})
        if held:
            self._hold_notice({"id": record.id, "text": record.text}, adopted,
                              self.intake.held_why(record, read_by))

    def _ledger_incident(self, record) -> None:
        """An incident zone applied. Written because, for a grammar read, this line is the only
        record."""
        tags = record.volume.tags or {}
        noted = Proposal(asset_id=FLEET_ASSET, action="incident_keepout", cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=record.name[:180],
                         params={"incident": record.id, "name": record.name,
                                 "centre": tags.get("centre"), "radius_m": tags.get("radius_m"),
                                 "until_tick": record.until_tick, "source": record.source})
        decision = Decision(noted.id, Verdict.AUTO,
                            f"{record.name} · {tags.get('radius_m')} m keep-out until tick "
                            f"{record.until_tick}", policy_hit=record.id, code="incident_keepout",
                            detail={"incident": record.id, "name": record.name,
                                    "until_tick": record.until_tick, "source": record.source,
                                    "radius_m": tags.get("radius_m")})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, ["incident"])), "noted")
