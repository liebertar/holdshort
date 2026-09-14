"""One intake item written down: the ledger line, the sqlite row, the asset names.

The leaf of backend/intake. _ledger_intake is written from six methods in three modules and
the four constants are read from five, so both need one definition site that imports nothing
sideways.
"""

from shared.models import Decision, Proposal, Verdict

# Asset names for intake cards and policies. This is fleet and runtime work, not an aircraft's.
INTAKE_ASSET = "intake"
FLEET_ASSET = "fleet"
INTAKE_CHECKS = ["intake:grammar", "intake:model"]
# Structured values stored with an item (POST /intake hints, the address and radius of a
# simulator incident notice). Re-reading after a restart from the text alone cannot place an
# incident that arrived as an address.
INTAKE_HINT_KEYS = ("name", "address", "building_id", "radius_m", "until_tick")


class IntakeEntriesMixin:
    def _ledger_intake(self, record, item: dict, code: str, outcome: str, reason: str,
                       detail: dict | None = None) -> None:
        """One intake line. Everything received, read or unreadable must be in the ledger."""
        noted = Proposal(asset_id=INTAKE_ASSET, action="intake", cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=record.text[:180],
                         params={"item": record.id, "source": record.source,
                                 "url": record.url or None, "query": item.get("query"),
                                 "title": item.get("title"), "kind_hint": item.get("kind")})
        verdict = Verdict.DENIED if code == "intake_unreadable" else Verdict.AUTO
        decision = Decision(noted.id, verdict, reason, code=code,
                            detail={"item": record.id, "source": record.source,
                                    "url": record.url or None, **(detail or {})})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, INTAKE_CHECKS)), outcome)
        self._store_intake(record, item, code, detail or {})

    def _store_intake(self, record, item: dict, code: str, detail: dict) -> None:
        """The intake line in sqlite too: a row goes in on receipt and is updated with what it
        became once read."""
        if code == "intake_received":
            self.store.put_item(record.id, record.source, record.text, self.tick, record.url,
                                item.get("kind"), _hints_of(item))
            return
        outcome = ("unreadable" if code == "intake_unreadable"
                   else "window_closed" if detail.get("window_closed")
                   else "held" if detail.get("held") else "read")
        self.store.settle_item(record.id, record.kind, record.read_by, outcome)


def _hints_of(item: dict) -> dict:
    return {key: item[key] for key in INTAKE_HINT_KEYS if item.get(key) is not None}
