"""The read-only response builders: what the screen and the report show.

They stay in the domain rather than moving to the api layer for two reasons: snapshot takes
_guard, and a views mixin in api/ would make backend/runtime/tower.py import the controller.
"""

from backend.intake.book import incident_snapshot
from backend.store.reports.ledger import build_report, to_markdown


class ViewsMixin:
    def snapshot(self) -> dict:
        self._observe()
        with self._guard:
            pending = [p.to_dict() for p in self._awaiting_human.values()]
            waiting = {r: len(v) for r, v in self._contended.items()}
        return {
            "tick": self.tick,
            "config": self.config.name,
            # The model is visible but has no say: which server, how many calls, and how often
            # the rules stood in. Only the runtime's own calls (arbitration, notice structuring)
            # are counted here; the aircraft processes count their own.
            "llm": {"enabled": self.llm.enabled, "models": self.llm.models,
                    "host": self.llm.host, "calls": self.llm.stats_dict()},
            "locks": self.locks.snapshot(),
            "contended": waiting,
            "awaiting_human": pending,
            "policies": [vars(p) for p in self.policies.all()],
            # Where cleared routes will be, and when. The screen draws who is waiting for whom
            # from this.
            "intents": self.intents.snapshot(),
            # Notices in force. The banner comes from here, not the simulator: what is enforced
            # is what is shown. Held records ride along too: until a human approves, applied is
            # False and they block nothing. The banner shows them as 'waiting for a person'.
            "notices": self.notices.snapshot(),
            # Runtime advisories, the latest per aircraft. Information only; they change nothing.
            "advisories": self.advisor.snapshot(),
            # Intake: which sources are on and what was read, the active weather hold, and
            # incident zones.
            "intake": self._intake_snapshot(),
            "weather": self.intake.weather_snapshot(),
            "incidents": incident_snapshot(list(self.notices.records.values()), self.tick),
            # Pre-flight briefing (Tavily): where it came from (live|recorded|off), credits
            # spent, summary, and what was read with its sources.
            "briefing": self.briefing.snapshot(),
            # What writes each aircraft's filings (the registered model). A label, unrelated to
            # judgement.
            "agents": self.agents_snapshot(),
            # Telemetry heartbeat. A lost aircraft's space stays reserved.
            "links": self._links_snapshot(),
            # The real autopilot behind the runtime (ADAPTER=composite). Read-only: judgement
            # still uses only telemetry from the world of record (the simulator).
            "autopilots": self._autopilots_snapshot(),
            "spend": {
                "fleet": self.authority.fleet_spend,
                "fleet_limit": self.config.authority.fleet_usd,
                "per_asset_limit": self.config.authority.per_asset_usd,
                "by_asset": {
                    asset: self.authority.asset_spend(asset) for asset in self.telemetry
                },
            },
            "ledger": self.ledger.tail(25),
        }

    def report(self, asset: str | None = None, fmt: str = "json"):
        """The ledger folded into flights. Built only from the ledger file; memory holds just
        200 lines."""
        built = build_report(self.ledger.read_all(), self.tick, self.airspace.revision, asset)
        # Intake (sqlite) too: the same facts as the ledger's intake lines, folded into what
        # became what.
        built["intake"] = self.store.report()
        return to_markdown(built) if fmt == "md" else built

    def _autopilots_snapshot(self) -> dict:
        """Only an adapter with a mirror answers. Empty in the simulator-only wiring."""
        view = getattr(self.adapter, "autopilots", None)
        return view() if callable(view) else {}
