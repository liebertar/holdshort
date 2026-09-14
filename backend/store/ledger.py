"""Append-only. Written before the action, closed after it. No entry, no execution."""

import json
import threading
from pathlib import Path

from shared.models import Decision, LedgerEntry, Proposal


class Ledger:
    def __init__(self, path: str | Path = "ledger.jsonl", keep_in_memory: int = 200):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._recent: list[dict] = []
        self._keep = keep_in_memory

    def open_entry(self, proposal: Proposal, decision: Decision,
                   context: dict | None = None) -> LedgerEntry:
        entry = LedgerEntry(proposal=proposal.to_dict(), decision=decision.to_dict(),
                            context=dict(context or {}))
        self._append(entry.to_dict())
        return entry

    def close_entry(self, entry: LedgerEntry, outcome: str,
                    decision: Decision | None = None, context: dict | None = None) -> None:
        """Store the decision again as it stands after execution.

        The copy taken at open is still the pre-execution state. Closing with it would make
        the ledger read "outcome done, but never executed". A record that lies is no record.
        Context learned while executing (the id of the intent it created) is added to the
        closing line.
        """
        if decision is not None:
            entry.decision = decision.to_dict()
        if context:
            entry.context = {**entry.context, **context}
        entry.outcome = outcome
        self._append(entry.to_dict())

    def _append(self, payload: dict) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self._recent.append(payload)
            del self._recent[: max(0, len(self._recent) - self._keep)]

    def tail(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return list(reversed(self._recent[-limit:]))

    def read_all(self) -> list[dict]:
        """The whole file, in the order written.

        Reports are built from the file, not from memory: memory holds only 200 lines.
        """
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(encoding="utf-8") as handle:
                lines = [line for line in handle if line.strip()]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue    # half-written last line; a report must not make the ledger unreadable
        return out
