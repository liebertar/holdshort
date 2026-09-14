"""What the tower took in, kept on disk: sqlite, stdlib only, one file.

The ledger is the record of decisions. This is the record of intake — every item that
arrived (from where, what it said, who read it, what became of it) and every rule that
came out of it (a weather hold, an incident circle, a restriction; from when, until when,
who lifted it). It exists so that a restart does not make the runtime read a searched page
or a typed line twice, and so that the report can list what the tower saw without parsing
the ledger for it. Nothing here judges: rows go in after code has decided.

It is best-effort. A broken or locked file must not stop the runtime or turn /state into a
500: a file that cannot be opened leaves the store in memory for the run, and a call that fails
returns an empty answer and leaves the file alone for a while. The runtime ledgers each change.
"""

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_PATH = ".run/intake.sqlite"
MEMORY = ":memory:"
# Sources whose items count as "seen" across a restart. A search result or a typed line is done
# once read — the same page must not come back as a human card after a restart. Simulator notices
# (resent with the same id every round) and METAR (the current observation must apply again every
# round) are left out.
DEDUPE_SOURCES = frozenset({"tavily", "manual"})
# Outcome of an item waiting for a human. Cards die with the process, so on start these rows are
# put back to be read again (reopen_waiting). Otherwise they would stay "seen" without a human
# ever having seen them, and would never be read. When the human answers or the window or round
# ends, decide_item replaces this value with how it ended.
WAITING = "held"
# Longest a single read or write waits on a locked file (seconds). Short, because the world thread
# calls it — with the sqlite default (5 s), every poll stalled 5 s while another connection held
# the file.
BUSY_TIMEOUT_S = 0.2
# Seconds after a failed call before the file is touched again. Calls in between return at once
# without waiting on it, so a locked file cannot stack up 0.2 s several times in one poll.
RETRY_AFTER_S = 30.0

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        kind TEXT,
        text TEXT NOT NULL,
        fetched_tick INTEGER,
        url TEXT,
        read_by TEXT,
        outcome TEXT,
        hints TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        from_tick INTEGER,
        until_tick INTEGER,
        applied INTEGER NOT NULL DEFAULT 0,
        lifted_by TEXT
    )""",
)
ITEM_COLUMNS = ("id", "source", "kind", "text", "fetched_tick", "url", "read_by", "outcome")
RULE_COLUMNS = ("id", "item_id", "kind", "from_tick", "until_tick", "applied", "lifted_by")
WAITING_COLUMNS = ("id", "source", "kind", "text", "url", "hints")
# Most rows the report carries. These tables grow like the ledger, so only the latest.
REPORT_ROWS = 200


class IntakeStore:
    """One connection, one lock. The world thread writes; the report (HTTP thread) reads.

    Best-effort only. These tables are a record, not a judgement, so a broken or locked file must
    not stop the runtime or turn /state into a 500. If the file cannot be opened, this run goes
    on in memory (open_error — the file is tried again on the next start); if a call fails
    mid-run, it returns an empty answer and the file is left alone for RETRY_AFTER_S (error —
    cleared by the next call that succeeds). Writing to the ledger is the runtime's job.
    """

    def __init__(self, path: str | Path | None = None):
        wanted = str(path or MEMORY)
        self.path = wanted
        self.open_error: str | None = None
        self.error: str | None = None
        self._failed_at: float | None = None
        self._lock = threading.Lock()
        try:
            self._db = _connect(wanted)
        except (sqlite3.Error, OSError) as error:
            # A broken file ("file is not a database"), one another process holds, or a place
            # that cannot be written. Judgement must go on — this run's record stays in memory.
            self.open_error = f"{wanted}: {type(error).__name__}: {error}"
            self.path = MEMORY
            self._db = _connect(MEMORY)

    @property
    def failing(self) -> bool:
        """True while records are not reaching the file (not opened, or the last call failed)."""
        return self.open_error is not None or self.error is not None

    def _run(self, work, default=None):
        """One read or write. On failure, return default and back off for RETRY_AFTER_S."""
        with self._lock:
            if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER_S:
                return default
            try:
                result = work(self._db)
                self._db.commit()
            except sqlite3.Error as error:
                self._failed_at, self.error = time.monotonic(), f"{type(error).__name__}: {error}"
                with contextlib.suppress(sqlite3.Error):
                    self._db.rollback()
                return default
            self._failed_at, self.error = None, None
            return result

    # ---------- items ----------

    def seen(self, item_id: str, source: str) -> bool:
        """Was this read and finished before the restart? Only for DEDUPE_SOURCES — the rest
        are read again every round. An item received but never finished (no outcome: reading
        stopped midway, or it lost its card while waiting for a human and reopen_waiting put it
        back) is not seen. If the record cannot be read, treat it as unseen — reading it once
        more beats never reading it."""
        if source not in DEDUPE_SOURCES:
            return False
        row = self._run(lambda db: db.execute("SELECT outcome FROM items WHERE id = ?",
                                              (item_id,)).fetchone())
        return row is not None and row[0] is not None

    def put_item(self, item_id: str, source: str, text: str, fetched_tick: int,
                 url: str = "", kind: str | None = None, hints: dict | None = None) -> None:
        """One row per received item. When the same id arrives again (the same notice next
        round), only the tick is rewritten.

        hints are the structured values outside the text (address, radius, window). They come
        back with the item when it is read again after a restart — from the text alone, an
        incident given by address cannot be read."""
        packed = json.dumps(hints, ensure_ascii=False, sort_keys=True) if hints else None
        self._run(lambda db: db.execute(
            "INSERT INTO items (id, source, kind, text, fetched_tick, url, hints) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET fetched_tick = excluded.fetched_tick, "
            "hints = excluded.hints, read_by = NULL, outcome = NULL",
            (item_id, source, kind, text[:2000], fetched_tick, url or None, packed)))

    def settle_item(self, item_id: str, kind: str | None, read_by: str, outcome: str) -> None:
        self._run(lambda db: db.execute(
            "UPDATE items SET kind = ?, read_by = ?, outcome = ? WHERE id = ?",
            (kind, read_by or None, outcome, item_id)))

    def decide_item(self, item_id: str, outcome: str) -> None:
        """How an item that waited for a human ended: approved · refused · lapsed · round ….
        Only waiting rows (held) change — a row the grammar read and applied at once stays
        "read" even after its rule is lifted."""
        self._run(lambda db: db.execute(
            "UPDATE items SET outcome = ? WHERE id = ? AND outcome = ?",
            (outcome, item_id, WAITING)))

    def reopen_waiting(self) -> list[dict]:
        """Once, on start. Items that were waiting for a human before the restart lost their
        cards — return them to be read again (raising their cards again) and clear their outcome
        so they are not "seen". Every open rule row is closed as restart — the in-memory weather
        holds, incident zones and held rules died with the process."""
        def work(db) -> list[dict]:
            rows = db.execute(f"SELECT {', '.join(WAITING_COLUMNS)} FROM items "
                              "WHERE outcome = ? ORDER BY rowid", (WAITING,)).fetchall()
            db.execute("UPDATE items SET outcome = NULL, read_by = NULL WHERE outcome = ?",
                       (WAITING,))
            db.execute("UPDATE rules SET lifted_by = 'restart' WHERE lifted_by IS NULL")
            return [_waiting_item(row) for row in rows]

        return self._run(work, [])

    def briefed(self, prefix: str, limit: int = REPORT_ROWS) -> list[dict]:
        """Finished items whose id starts with this prefix, hints included. Read-only; the
        tables are left as they are.

        The pre-flight briefing (runtime/briefing.py) calls this on restart. It neither fetches
        nor reads again what it already read, and re-applies the tightening rules that were in
        force then — a restart must not become a way to loosen rules (only a human or the window
        loosens). Rows that were waiting for a human come back through reopen_waiting, not here.
        """
        rows = self._run(lambda db: db.execute(
            "SELECT id, source, kind, text, url, read_by, outcome, hints FROM items "
            "WHERE id LIKE ? AND outcome IS NOT NULL ORDER BY rowid DESC LIMIT ?",
            (prefix.replace("%", "") + "%", limit)).fetchall(), [])
        out = []
        for row in reversed(rows):
            record = dict(zip(("id", "source", "kind", "text", "url", "read_by", "outcome",
                               "hints"), tuple(row), strict=True))
            try:
                record["hints"] = json.loads(record["hints"] or "{}")
            except ValueError:
                record["hints"] = {}
            out.append(record)
        return out

    # ---------- rules ----------

    def open_rule(self, item_id: str, kind: str, from_tick: int | None,
                  until_tick: int | None, applied: bool = True) -> int | None:
        """One rule row from an item. The returned id is how its lifting is recorded later
        (None if the row was not written)."""
        cursor = self._run(lambda db: db.execute(
            "INSERT INTO rules (item_id, kind, from_tick, until_tick, applied) VALUES (?,?,?,?,?)",
            (item_id, kind, from_tick, until_tick, int(applied))))
        return int(cursor.lastrowid) if cursor is not None else None

    def close_rule(self, rule_id: int | None, lifted_by: str,
                   until_tick: int | None = None) -> None:
        """The rule ended: a human lifted it (human), the window closed (window), the round
        changed (round), a human refused it (refused), it passed before being confirmed
        (lapsed), or the process restarted (restart)."""
        if rule_id is None:
            return
        if until_tick is None:
            self._run(lambda db: db.execute("UPDATE rules SET lifted_by = ? WHERE id = ?",
                                            (lifted_by, rule_id)))
        else:
            self._run(lambda db: db.execute(
                "UPDATE rules SET lifted_by = ?, until_tick = ? WHERE id = ?",
                (lifted_by, until_tick, rule_id)))

    def apply_rule(self, rule_id: int | None, from_tick: int | None = None) -> None:
        """A held rule took effect after human approval (applied 0 → 1)."""
        if rule_id is None:
            return
        if from_tick is None:
            self._run(lambda db: db.execute("UPDATE rules SET applied = 1 WHERE id = ?",
                                            (rule_id,)))
        else:
            self._run(lambda db: db.execute(
                "UPDATE rules SET applied = 1, from_tick = ? WHERE id = ?", (from_tick, rule_id)))

    def extend_rule(self, rule_id: int | None, until_tick: int) -> None:
        if rule_id is None:
            return
        self._run(lambda db: db.execute("UPDATE rules SET until_tick = ? WHERE id = ?",
                                        (until_tick, rule_id)))

    # ---------- reading ----------

    def items(self, limit: int = REPORT_ROWS) -> list[dict]:
        # Name the columns: a file that gained the hints column later has a different order.
        rows = self._run(lambda db: db.execute(
            f"SELECT {', '.join(ITEM_COLUMNS)} FROM items "
            "ORDER BY fetched_tick DESC, rowid DESC LIMIT ?", (limit,)).fetchall(), [])
        return [dict(zip(ITEM_COLUMNS, tuple(row), strict=True)) for row in reversed(rows)]

    def rules(self, limit: int = REPORT_ROWS) -> list[dict]:
        rows = self._run(lambda db: db.execute("SELECT * FROM rules ORDER BY id DESC LIMIT ?",
                                               (limit,)).fetchall(), [])
        out = []
        for row in reversed(rows):
            record = dict(zip(RULE_COLUMNS, tuple(row), strict=True))
            record["applied"] = bool(record["applied"])
            out.append(record)
        return out

    def counts(self) -> dict:
        """Header for the screen (/state.intake.store) and the report. None where a count
        failed; the reason is in error."""
        counted = self._run(lambda db: (db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                                        db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]))
        items, rules = counted if counted is not None else (None, None)
        return {"items": None if items is None else int(items),
                "rules": None if rules is None else int(rules), "path": self.path,
                "error": self.open_error or self.error}

    def report(self, limit: int = REPORT_ROWS) -> dict:
        """The intake record carried in the report (/ledger/report). The ledger records
        decisions; this records what came in."""
        return {**self.counts(), "items": self.items(limit), "rules": self.rules(limit)}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _waiting_item(row) -> dict:
    """One item to read again. Unpacks its hints and marks it reopened (so the ledger's
    received line says so)."""
    record = dict(zip(WAITING_COLUMNS, tuple(row), strict=True))
    try:
        hints = json.loads(record.pop("hints") or "{}")
    except ValueError:
        hints = {}
    item = {key: value for key, value in record.items() if value is not None}
    return {**(hints if isinstance(hints, dict) else {}), **item, "reopened": True}


def _connect(path: str) -> sqlite3.Connection:
    """Open the connection and create the tables. A broken file shows up here, on the first
    statement."""
    if path != MEMORY:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # One connection for all threads: an in-memory DB is a separate database per connection.
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=False)
    db.row_factory = sqlite3.Row
    try:
        for statement in SCHEMA:
            db.execute(statement)
        # A file from before the hints column: add the column instead of rebuilding the table
        # (old rows have no hints).
        if "hints" not in {row[1] for row in db.execute("PRAGMA table_info(items)")}:
            db.execute("ALTER TABLE items ADD COLUMN hints TEXT")
        db.commit()
    except sqlite3.Error:
        db.close()
        raise
    return db
