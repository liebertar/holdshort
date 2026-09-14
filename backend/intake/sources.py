"""Where the two fetch threads drop their results, and whether each source is answering.

Search and METAR share one ledger-line builder (_ledger_source_change, also called by the
briefing desk) and one /state block, so both callers sit next to it.
"""

import os

from backend.intake.entries import INTAKE_ASSET
from shared.metar import DEFAULT_PERIOD_S as METAR_DEFAULT_PERIOD_S
from shared.metar import SOURCE as METAR_SOURCE
from shared.models import Decision, Proposal, Verdict
from shared.tavily import FetchStatus

# How often (s) to fetch METAR. Observations come hourly (specials in between).
METAR_PERIOD_S = float(os.getenv("METAR_PERIOD_S") or METAR_DEFAULT_PERIOD_S)
# Names used in source failure/recovery lines.
SOURCE_NAMES = {"tavily": "search", METAR_SOURCE: "METAR"}


class SourcesMixin:
    def take_in(self, items: list[dict], status: FetchStatus | None = None) -> None:
        """The search thread drops off its results and cycle status. Nothing is recorded or
        read here; the next poll does that. Only a successful cycle advances last_fetch_tick:
        advanced by a failed, empty cycle, the screen would read 'just asked, found nothing'."""
        with self._guard:
            self._intake_inbox.extend(dict(item) for item in items)
            if status is not None:
                self._intake_fetch = status
            if status is None or status.ok:
                self.intake.last_fetch_tick = self.tick

    def _note_fetch(self) -> None:
        """Search source failure ↔ recovery, one line per change only: a line every cycle would
        fill the ledger with failures."""
        with self._guard:
            status, self._intake_fetch = self._intake_fetch, None
        if status is None:
            return
        self.intake.fetch = status.to_dict()
        failed_now = not status.ok
        if failed_now == self.intake.source_failed:
            return
        self.intake.source_failed = failed_now
        self._ledger_source_change("tavily", status, failed_now)

    def _ledger_source_change(self, source: str, status: FetchStatus, failed_now: bool) -> None:
        """One line per source failure ↔ recovery. Search (tavily) and METAR share it."""
        name = SOURCE_NAMES.get(source, source)
        noted = Proposal(asset_id=INTAKE_ASSET, action="intake_source", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"{source} · {status.error or 'ok'}"[:180],
                         params={"source": source, **status.to_dict()})
        decision = Decision(
            noted.id, Verdict.DENIED if failed_now else Verdict.AUTO,
            (f"cannot reach the {name} source — {status.error}" if failed_now
             else f"the {name} source answers again (after {status.failures} failures)"),
            code="intake_source_failed" if failed_now else "intake_source_recovered",
            detail={"source": source, **status.to_dict()})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, ["intake:source"])),
            "failed" if failed_now else "noted")

    def take_metar(self, items: list[dict], status: FetchStatus | None = None) -> None:
        """The METAR thread drops off observations and cycle status. Recording and reading
        happen on the next poll (world thread)."""
        with self._guard:
            self._intake_inbox.extend(dict(item) for item in items)
            if status is not None:
                self._metar_fetch = status
            if status is None or status.ok:
                self.metar_last_fetch_tick = self.tick
                # A failed cycle doesn't clear the last observations: a gust hold is lifted by
                # the window or a human, never by a network outage.
                self._metar_current = [dict(item) for item in items]

    def _note_metar_fetch(self) -> None:
        """METAR source on ↔ off. One line (off) when it can't be reached, one (on) when it
        answers again."""
        with self._guard:
            status, self._metar_fetch = self._metar_fetch, None
        if status is None:
            return
        self.metar_fetch = status.to_dict()
        now = "on" if status.ok else "off"
        was, self.metar_status = self.metar_status, now
        if now == was or (was == "starting" and status.ok):
            return      # the first success is no news: it simply started on
        self._ledger_source_change(METAR_SOURCE, status, failed_now=not status.ok)

    def _intake_snapshot(self) -> dict:
        out = self.intake.snapshot(self.tavily is not None)
        out["sources"]["metar"] = self.metar_status
        out["metar"] = {"stations": list(self.metar.stations) if self.metar is not None else [],
                        "period_s": METAR_PERIOD_S, "last_fetch_tick": self.metar_last_fetch_tick,
                        "fetch": self.metar_fetch}
        out["store"] = self.store.counts()
        return out
