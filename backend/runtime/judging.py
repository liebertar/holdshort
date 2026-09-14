"""The judgement pipeline and the Decision each refusal is built from.

Owner of _checks and _decisions on the filing path. `file` takes _judging; the lock order is
always _judging then _guard, never the reverse.
"""

from backend.runtime.authority import AuthorityCheck
from backend.runtime.form import ROUTED
from shared import config as config_module
from shared.models import Decision, Proposal, Verdict


class JudgingMixin:
    @property
    def ready(self) -> bool:
        """Whether judgement can start.

        A service that awaits airspace is ready once it has all of it; any other runtime from
        the start.
        """
        return self.airspace_loaded or not self.await_airspace

    def file(self, raw: dict) -> Decision:
        """Judge one filing and execute it if cleared.

        One at a time from judgement to intent registration (_judging).
        """
        with self._judging:
            return self._judge_and_commit(raw)

    def _judge_and_commit(self, raw: dict) -> Decision:
        proposal = Proposal.from_dict({**raw, "world": "guarded"})
        asset = self.telemetry.get(proposal.asset_id, {})
        # Checks for this filing. When an operator re-files under the same id (straight line →
        # rewrite) the list starts over — one ledger line must describe one filing. A later
        # rejudge appends to this list.
        checks = self._checks[proposal.id] = []
        self._observe()

        if not self.ready:
            # A gate before judgement: nothing is cleared until the whole airspace is in.
            # policy_hit stays empty — an operator that learned "this action is banned" from it
            # would stop filing that action even after the airspace arrived.
            checks.append("airspace_loaded")
            return self._deny(proposal, Decision(
                proposal.id, Verdict.DENIED,
                "the runtime has not received the whole airspace yet — nothing to judge with, "
                "refused, file again shortly",
                code="airspace_not_loaded",
                detail={"airspace_revision": self.airspace.revision}))

        checks.append("dedupe")
        seen_at = self._recent_commits.get((proposal.asset_id, proposal.action))
        if seen_at is not None and self.tick - seen_at < self.dedupe_ticks:
            # The same filing arriving back to back goes out once; otherwise it is billed twice.
            # This is a judgement too, so it goes in the ledger — without it, "why did that
            # filing get no answer?" has no answer.
            decision = Decision(proposal.id, Verdict.DENIED,
                                "the same filing was executed a moment ago", code="duplicate")
            return self._deny(proposal, decision)

        if self.links.lost(proposal.asset_id):
            # An aircraft with a lost link cannot hear commands. A gate before judgement — not
            # recorded on filings that pass; it goes in the check list only on refusal.
            checks.append("link")
            return self._deny(proposal, self._dark_denial(proposal))
        blocked = self.check_route(proposal, checks)
        if blocked:
            return self._deny(proposal, self._airspace_denial(proposal, blocked))
        unknown = self._contingency_problem(proposal, checks)
        if unknown:
            return self._deny(proposal, unknown)
        blocked = self._check_traffic(proposal, checks)
        if blocked:
            return self._deny(proposal, self._traffic_denial(proposal, blocked))

        checks.append("authority")
        decision = self.authority.evaluate(proposal, asset, self.tick)
        self._decisions[proposal.id] = decision

        if decision.verdict is Verdict.DENIED:
            return self._deny(proposal, decision)
        if decision.verdict is Verdict.HUMAN:
            return self._park_for_human(proposal, decision)
        return self._queue_or_commit(proposal, decision)

    def _deny(self, proposal: Proposal, decision: Decision) -> Decision:
        self._decisions[proposal.id] = decision
        self.ledger.close_entry(
            self.ledger.open_entry(proposal, decision, self._context(proposal)), "denied")
        self._checks.pop(proposal.id, None)
        self._record_refusal(proposal, decision)
        return decision

    @staticmethod
    def _airspace_denial(proposal: Proposal, blocked: str) -> Decision:
        return Decision(proposal.id, Verdict.DENIED, blocked, policy_hit="airspace",
                        forbids=proposal.params.get("blocked_volume"), code="airspace")

    @staticmethod
    def _traffic_denial(proposal: Proposal, blocked: str) -> Decision:
        """Traffic refusal. The code is airspace (the UI replays it like other refusals); the
        policy_hit is traffic.

        What the operator needs to know (the other aircraft, the tick its volume clears) rides
        in detail and goes back in the reply — params stay in the ledger only, not the reply.
        """
        params = proposal.params
        return Decision(
            proposal.id, Verdict.DENIED, blocked, policy_hit="traffic",
            forbids=params.get("blocked_asset"), code="airspace",
            detail={key: params.get(key) for key in (
                "blocked_kind", "blocked_asset", "blocked_leg", "blocked_at",
                "blocked_until_tick", "blocked_intent")},
        )

    def _dark_denial(self, proposal: Proposal) -> Decision:
        """A filing for an aircraft with a lost link. Commands can't reach it, so a clearance
        could not be executed.

        policy_hit stays empty — an operator that learned "this action is banned" from it would
        never file that action again, even after the link returns. Once the link is back, the
        same filing is judged as usual.
        """
        link = self.links.links[proposal.asset_id]
        return Decision(proposal.id, Verdict.DENIED,
                        f"{proposal.asset_id} has had no link since tick {link.since_tick} — "
                        "the aircraft cannot hear commands", code="lost_link_refused",
                        detail={"resource": proposal.asset_id, "since_tick": link.since_tick,
                                "last_seen_tick": link.last_seen_tick})

    def _contingency_problem(self, proposal: Proposal, checks: list[str]) -> Decision | None:
        """Judge the lost-link contingency volume. An unknown contingency behaviour is refused.

        For continue_and_land the contingency volume is the cleared route itself plus the
        landing column at the destination, so the route/columns/landing checks just passed are
        that judgement — here we only check that the declared behaviour is that one. Other
        behaviours such as return_to_launch create a different volume (the straight line back),
        and clearing without judging it means flying an unjudged path the moment the link drops.
        """
        if proposal.action not in ROUTED or not proposal.params.get("legs"):
            return None
        checks.append("contingency")
        lost_link = self.performance.lost_link
        if lost_link.known:
            return None
        known = ", ".join(config_module.KNOWN_LOST_LINK_BEHAVIOURS)
        return Decision(proposal.id, Verdict.DENIED,
                        f"the volume of lost-link behaviour {lost_link.behaviour!r} cannot be "
                        f"judged (known: {known})", policy_hit="lost_link",
                        code="contingency_unknown",
                        detail={"behaviour": lost_link.behaviour,
                                "known": list(config_module.KNOWN_LOST_LINK_BEHAVIOURS)})

    def _rejudge(self, proposal: Proposal, decision: Decision) -> str | None:
        """Judge again right before execution, against the current airspace and intents. If
        blocked, close it as a refusal and return the reason.

        Judgement happens once, at filing (file). When a zone notice arrived while a filing
        waited for human approval or sat in a resource queue, the later execution sent a route
        judged against the old airspace into a closed zone. "Every executed route passed
        judgement against the runtime's airspace" must hold at execution time. Judgement takes
        milliseconds, so running it every time is cheaper than remembering the airspace version
        and re-checking only on change.
        """
        checks = self._checks.setdefault(proposal.id, [])
        checks.append("rejudge")
        fresh = self._fresh_refusal(proposal, checks)
        if fresh is None:
            return None
        decision.verdict = Verdict.DENIED
        decision.reason = fresh.reason
        decision.policy_hit = fresh.policy_hit
        decision.forbids = fresh.forbids
        decision.code = fresh.code
        decision.detail = fresh.detail
        self.ledger.close_entry(
            self.ledger.open_entry(proposal, decision, self._context(proposal)), "denied")
        self._record_refusal(proposal, decision)
        return decision.reason

    def _fresh_refusal(self, proposal: Proposal, checks: list[str]) -> Decision | None:
        """The reason to refuse right before execution, or None.

        Checked in order: link → airspace → traffic → policy. If the link dropped while waiting
        for human approval or an arbitration grant, that approval must not send a command to an
        aircraft that cannot hear it. With an adapter that reports a command as received once
        sent (MAVLink says ok on send), the new route ended the lost-link reservation, and
        another aircraft's crossing was cleared through the path the aircraft was actually
        still flying. A ban (weather hold, airworthiness directive) may also have arrived
        meanwhile, so the same policy check as at filing runs once more.
        """
        if self.links.lost(proposal.asset_id):
            checks.append("link")
            return self._dark_denial(proposal)
        blocked = self.check_route(proposal, checks)
        if blocked:
            return self._airspace_denial(proposal, blocked)
        traffic = self._check_traffic(proposal, checks)
        if traffic:
            return self._traffic_denial(proposal, traffic)
        checks.append("policy")
        banned = self.policies.hit(proposal.action, proposal.resource,
                                   self.telemetry.get(proposal.asset_id, {}), self.tick)
        return AuthorityCheck.policy_denial(proposal, banned) if banned else None
