"""The simulated city stays the world of record; one guarded aircraft is also flown by a real PX4.

The simulator decides everything the runtime judges with — positions, the cargo cycle, the
scoreboards — for all four aircraft. One of them (MAVLINK_MIRROR, default drone-01) is also
flown by a PX4 autopilot. Every command the runtime executes for that aircraft goes to the
simulator first and then to PX4. The simulator's answer is the answer the runtime gets; PX4's
answer is written beside it and never changes a verdict.

PX4 is a mirror that proves the command path, not the source of truth: the same cleared route
the map shows is uploaded as a real mission, a recall or a weather hold reaches a real
autopilot, a refused filing never arms it. Nothing judged is read back from PX4.
"""

import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path

from backend.adapters.fleet_sim import FleetSimAdapter
from backend.adapters.mavlink_fleet import route_items

DEFAULT_MIRROR = "drone-01"
DEFAULT_ENDPOINT = "udpin:0.0.0.0:14540"
# The sim aircraft counts as departed above this altitude (m). PX4 arms at that point.
LIFTOFF_M = 1.0
# One worker thread sends the mirror's commands in order. Once the queue holds this many, new
# commands are dropped and recorded — the world thread must never wait on PX4.
JOB_QUEUE_LIMIT = 32
COMMAND_LOG_KEEP = 16
# A link the worker may send on: an autopilot heard from within this many seconds. Looser than
# the screen's "lost" threshold — dropping the arm command over one or two missed heartbeats
# once wiped out a whole mirrored flight. An autopilot that has truly gone quiet is caught by
# the per-step reply deadline (ACK).
LINK_SEND_S = 15.0
# Actions that carry a route. reserve_pad only comes on a fault; if it has legs, it flies them.
ROUTE_ACTIONS = ("fly_route", "reserve_pad")
# Actions passed straight to the autopilot. The rest (loading, declining, charging) happen on
# the ground, so there is nothing to send.
PASS_THROUGH = ("land", "disengage_autonomy")
NO_COMMAND_WHY = {
    "depart": "loading is ground work — the route mission does the taking off",
    "decline_job": "turning a delivery down is the operator's business",
}
GROUND_WORK_WHY = "ground equipment — nothing to send to the autopilot"


class AutopilotJournal:
    """A line file (JSONL) of PX4's answers, kept beside the ledger.

    Why not the ledger file: PX4 answers after the ledger line has closed. Another line under
    the same ledger id would make the screen's recent log and the report's flight folding
    count the same decision twice.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as error:
                print(f"autopilot journal: {error!r}", flush=True)


class AutopilotMirror:
    """The PX4 mirror for one aircraft.

    The world thread puts work on the queue and returns at once. A single worker thread sends
    the queue to the autopilot in order and records the answers. Neither the verdict nor the
    ledger outcome waits for them.
    """

    def __init__(self, asset_id: str, autopilot, journal: AutopilotJournal | None = None):
        self.asset_id = asset_id
        self.autopilot = autopilot
        self.journal = journal
        self._jobs: queue.Queue = queue.Queue(maxsize=JOB_QUEUE_LIMIT)
        self._guard = threading.Lock()
        self._commands: deque = deque(maxlen=COMMAND_LOG_KEEP)
        self._mission: dict | None = None
        # A route uploaded and waiting for the sim aircraft to lift off (ledger id). Loading,
        # approval checks and deferred departures happen in the world of record. Arming right
        # after upload would have PX4 leave while the aircraft on the map is still loading.
        self._pending_start: str | None = None
        self._world_airborne = False
        threading.Thread(target=self._work, daemon=True, name=f"mirror-{asset_id}").start()

    # ---------- world-thread side: nothing here waits ----------

    def submit(self, action: str, params: dict, ledger_id: str) -> dict:
        # Copy the route as it is when queued. If the original changed before the worker sends
        # it, a route other than the judged one would be uploaded.
        legs = [dict(leg) for leg in params.get("legs") or []]
        if action in ROUTE_ACTIONS and legs:
            with self._guard:
                start_now = self._world_airborne
                self._pending_start = None if start_now else ledger_id
            return self._enqueue(ledger_id, action,
                                 lambda: self._fly(ledger_id, legs, start_now))
        if action == "divert_ground":
            with self._guard:
                self._pending_start = None  # a route recalled before liftoff is never armed
            exit_point = params.get("exit")
            return self._enqueue(ledger_id, action, lambda: self._recall(ledger_id, exit_point))
        if action in PASS_THROUGH:
            return self._enqueue(ledger_id, action,
                                 lambda: self._pass_through(action, params, ledger_id))
        return self._record(ledger_id, action, True, "no_command",
                            NO_COMMAND_WHY.get(action, GROUND_WORK_WHY))

    def skip(self, ledger_id: str, action: str, why: str) -> dict:
        """A command the world of record did not take. What did not happen there does not
        happen in the mirror either."""
        return self._record(ledger_id, action, None, "not_sent", why)

    def observe_world(self, asset_state: dict) -> None:
        """One telemetry row for the sim aircraft. Starts the waiting route the moment it
        lifts off."""
        airborne = float(asset_state.get("alt_m") or 0.0) > LIFTOFF_M
        with self._guard:
            self._world_airborne = airborne
            ready = self._pending_start if airborne else None
            if ready is not None:
                self._pending_start = None
        if ready is not None:
            self._enqueue(ready, "start", lambda: self._start(ready))

    def view(self) -> dict:
        base = self.autopilot.autopilot_view(self.asset_id)
        with self._guard:
            mission = dict(self._mission) if self._mission else None
            return {**base, "role": "mirror", "world_airborne": self._world_airborne,
                    "pending_start": self._pending_start, "mission": mission,
                    "commands": list(self._commands)}

    def _enqueue(self, ledger_id: str, action: str, job) -> dict:
        try:
            self._jobs.put_nowait((ledger_id, action, job))
        except queue.Full:
            return self._record(ledger_id, action, False, "dropped",
                                "the PX4 worker is backed up — nothing was sent")
        return {"ok": None, "result": "queued", "detail": "sending to PX4"}

    # ---------- worker-thread side ----------

    def _work(self) -> None:
        while True:
            ledger_id, action, job = self._jobs.get()
            if not self.autopilot.link_up(self.asset_id, within_s=LINK_SEND_S):
                # Sending on a dead link waits out every step's reply deadline, and the
                # commands behind it queue up.
                self._record(ledger_id, action, False, "link_down", "no PX4 heartbeat")
                continue
            try:
                ok, result, detail = job()
            except Exception as error:  # noqa: BLE001 — a dead worker silently loses later commands
                ok, result, detail = False, "error", repr(error)
            self._record(ledger_id, action, ok, result, detail)

    def _fly(self, ledger_id: str, legs: list[dict], start_now: bool):
        items = route_items(legs, self.autopilot.airborne(self.asset_id))
        upload = self.autopilot.upload_mission(self.asset_id, items)
        with self._guard:
            self._mission = {"ledger_id": ledger_id, "items": items,
                             "uploaded": bool(upload["ok"]), "started": False}
        if not upload["ok"]:
            return False, "upload_failed", upload
        if not start_now:
            return True, "uploaded", {"items": len(items),
                                      "start": "when the simulated aircraft takes off"}
        return self._start(ledger_id)

    def _start(self, ledger_id: str):
        with self._guard:
            mission = self._mission
        if mission is None or mission["ledger_id"] != ledger_id or not mission["uploaded"]:
            return False, "not_started", "no mission for this route is uploaded"
        reply = self.autopilot.start_mission(
            self.asset_id, arm=not self.autopilot.airborne(self.asset_id))
        with self._guard:
            mission["started"] = bool(reply["ok"])
        return bool(reply["ok"]), "started" if reply["ok"] else "start_refused", reply

    def _recall(self, ledger_id: str, exit_point: dict | None):
        reply = self.autopilot.recall(self.asset_id, exit_point)
        did = reply.get("did", "recall")
        with self._guard:
            if did == "exit_mission":
                self._mission = {"ledger_id": ledger_id, "items": reply.get("items") or [],
                                 "uploaded": bool(reply.get("uploaded")),
                                 "started": bool(reply["ok"])}
            elif did == "cleared" and reply["ok"]:
                self._mission = None
        return bool(reply["ok"]), did, reply

    def _pass_through(self, action: str, params: dict, ledger_id: str):
        reply = self.autopilot.execute(self.asset_id, action, params, ledger_id)
        return bool(reply.get("ok")), "sent", reply

    def _record(self, ledger_id: str, action: str, ok, result: str, detail) -> dict:
        outcome = {"ok": ok, "result": result, "detail": detail}
        record = {"ledger_id": ledger_id, "asset": self.asset_id, "action": action,
                  **outcome, "at": round(time.time(), 3)}
        with self._guard:
            self._commands.appendleft(record)
        if self.journal is not None:
            self.journal.write(record)
        return outcome


class CompositeAdapter:
    """Simulator (world of record) + one PX4 mirror. To the runtime it looks like one adapter."""

    def __init__(self, world, mirror: AutopilotMirror):
        self.world = world
        self.mirror = mirror

    def execute(
        self,
        asset_id: str,
        action: str,
        params: dict,
        ledger_id: str,
        blast: str = "none",
        approved_by: str | None = None,
    ) -> dict:
        # The world of record goes first. Its answer is what the runtime gets, and it decides
        # the ledger outcome.
        result = self.world.execute(asset_id, action, params, ledger_id, blast=blast,
                                    approved_by=approved_by)
        if asset_id != self.mirror.asset_id:
            return result
        if not result.get("ok"):
            return {**result, "autopilot": self.mirror.skip(ledger_id, action,
                                                            "the simulator did not take it")}
        return {**result, "autopilot": self.mirror.submit(action, dict(params or {}), ledger_id)}

    def telemetry(self) -> dict:
        # The telemetry judgements use is the simulator's. PX4's position is exposed only
        # through /state.autopilots.
        state = self.world.telemetry()
        seen = ((state or {}).get("assets") or {}).get(self.mirror.asset_id)
        if seen is not None:
            self.mirror.observe_world(seen)
        return state

    def autopilots(self) -> dict:
        """/state.autopilots. Read-only — judgement never looks at these values."""
        return {self.mirror.asset_id: self.mirror.view()}


def from_env(sim_url: str, world: str = "guarded", journal_path: str | None = None):
    """ADAPTER=composite. MAVLINK_MIRROR (default drone-01), MAVLINK_ENDPOINT (default
    udpin:0.0.0.0:14540).

    Without pymavlink, or if the port cannot be opened, it runs on the simulator alone. A
    missing mirror must not stop the round — the mirror only proves the command path; the
    world of record is the simulator.
    """
    sim = FleetSimAdapter(sim_url, world=world)
    asset = os.getenv("MAVLINK_MIRROR") or DEFAULT_MIRROR
    endpoint = os.getenv("MAVLINK_ENDPOINT") or DEFAULT_ENDPOINT
    try:
        from backend.adapters.mavlink_fleet import MavlinkFleetAdapter

        autopilot = MavlinkFleetAdapter({asset: endpoint}, world=world, link_timeout_s=1.0)
    except (ImportError, OSError) as error:
        print(f"composite: no PX4 mirror, simulator only ({error!r})", flush=True)
        return sim
    journal = AutopilotJournal(journal_path) if journal_path else None
    print(f"composite: {asset} also flown by PX4 ({endpoint}) — the simulator judges", flush=True)
    return CompositeAdapter(sim, AutopilotMirror(asset, autopilot, journal))
