"""Talks to real autopilots over MAVLink.

PX4 and ArduPilot are the assured layer. They fly the aircraft; nothing here and nothing
in any model touches attitude or thrust. This adapter only translates an approved proposal
into the mission-level command the autopilot already knows how to refuse or accept, which
is exactly the split ASTM F3269 describes.

A cleared route becomes a MAVLink mission item for item: take off where the aircraft stands,
one waypoint per judged leg at that leg's altitude, land at the destination. Nothing here
draws, shortens or smooths a route; the autopilot is handed exactly what the runtime judged.

Ground equipment (pads, chargers, money) is not an autopilot concern and does not appear
here. Those resources live in the runtime's lock table and authority envelope.
"""

import math
import os
import queue
import threading
import time
from collections import deque

# Landing points. A real deployment puts vertiport coordinates here.
PAD_COORDS = {
    "pad:P1": (47.397971, 8.546164),
    "pad:P2": (47.398500, 8.547500),
}
DEPOT_ALT_M = 30.0

# Above this height (m) the aircraft counts as airborne. If the autopilot reports its landed
# state, that comes first.
AIRBORNE_M = 1.0
# After this many seconds without a heartbeat the screen shows the link as lost. PX4 sends one
# every second by its own clock, but a SITL running slower than real time (0.66x on this Mac)
# makes that 1.5 s of wall clock, and losing one through Docker Desktop's address translation
# pushes it past 3 s. Matches the screen marking data stale from 5 s.
LINK_STALE_S = 5.0
# We send a heartbeat every second too. An autopilot on udpout learns our address from it.
GCS_HEARTBEAT_S = 1.0
# How many times to send the mission count (MISSION_COUNT) while no first request arrives.
# UDP loses packets.
MISSION_COUNT_TRIES = 3
# If the same item is requested again more than this many times, the protocol has gone wrong
# and the upload is abandoned. Asking again once or twice for a lost item is normal (UDP). An
# autopilot that keeps asking forever ties up the mirror's worker thread.
MISSION_ITEM_RESENDS = 5
STATUS_TEXT_KEEP = 6

# PX4 puts the main mode in bits 16-23 of HEARTBEAT.custom_mode, the sub mode in bits 24-31.
PX4_MAIN_MODES = {1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 4: "AUTO", 5: "ACRO", 6: "OFFBOARD",
                  7: "STABILIZED"}
PX4_AUTO_MODES = {1: "READY", 2: "TAKEOFF", 3: "LOITER", 4: "MISSION", 5: "RTL", 6: "LAND",
                  8: "FOLLOW_TARGET", 9: "PRECLAND"}
PX4_MAIN_AUTO = 4
PX4_AUTO_MISSION = 4
LANDED_STATES = {1: "on_ground", 2: "in_air", 3: "taking_off", 4: "landing"}
MISSION_REPLIES = ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK")
# Actions with nothing to send to the autopilot. Loading (depart) is ground work; the route
# mission does the takeoff.
NO_AUTOPILOT_COMMAND = {"depart": "loading is ground work; the route mission is the departure"}


def route_items(legs: list[dict], airborne: bool) -> list[dict]:
    """Approved route → autopilot mission, carried over as is: no point added or removed.

    legs[0] is where the aircraft was when it filed. On the ground, it takes off there to the
    first leg's altitude; in the air, that spot is flown as a waypoint too — so an autopilot
    running behind still follows the approved route from its start. It lands at the last
    point. The simulator also lands at the destination to unload.
    """
    points = [_point(leg) for leg in legs]
    if len(points) < 2:
        return []
    first, rest = points[0], points[1:]
    items = [{"command": "waypoint" if airborne else "takeoff", **first}]
    items += [{"command": "waypoint", **point} for point in rest]
    items.append({"command": "land", "lat": rest[-1]["lat"], "lon": rest[-1]["lon"],
                  "alt_m": 0.0})
    return items


def exit_items(exit_point: dict, alt_m: float) -> list[dict]:
    """Two-item recall mission: fly at the current altitude to the exit point the runtime
    gave (exit), and land there."""
    at = {"lat": float(exit_point["lat"]), "lon": float(exit_point["lon"])}
    return [{"command": "waypoint", **at, "alt_m": round(float(alt_m), 1)},
            {"command": "land", **at, "alt_m": 0.0}]


def px4_mode_name(custom_mode: int) -> str:
    main = (int(custom_mode) >> 16) & 0xFF
    sub = (int(custom_mode) >> 24) & 0xFF
    if main == PX4_MAIN_AUTO:
        return f"AUTO.{PX4_AUTO_MODES.get(sub, sub)}"
    return PX4_MAIN_MODES.get(main, f"MODE{main}")


def _point(leg: dict) -> dict:
    return {"lat": float(leg["lat"]), "lon": float(leg["lon"]),
            "alt_m": float(leg.get("alt_m") or 0.0)}


def _drain(inbox: queue.Queue) -> None:
    while True:
        try:
            inbox.get_nowait()
        except queue.Empty:
            return


def _blank_autopilot() -> dict:
    return {"mode": None, "landed": None, "mission_seq": None, "reached_seq": None,
            "last_beat": None, "status_text": deque(maxlen=STATUS_TEXT_KEEP)}


class MavlinkFleetAdapter:
    def __init__(self, endpoints: dict[str, str], world: str = "guarded",
                 ack_timeout_s: float = 3.0, link_timeout_s: float = 30.0):
        """endpoints: {"drone-01": "udpin:0.0.0.0:14540", ...} — one link per aircraft."""
        from pymavlink import mavutil  # only needed when this adapter is used

        self._mavutil = mavutil
        self._mav = mavutil.mavlink
        self.world = world
        self.endpoints = dict(endpoints)
        self.ack_timeout_s = ack_timeout_s
        self.link_timeout_s = link_timeout_s
        self.links = {
            asset_id: mavutil.mavlink_connection(endpoint)
            for asset_id, endpoint in endpoints.items()
        }
        self._state: dict[str, dict] = {
            asset_id: {"id": asset_id, "state": "unknown"} for asset_id in endpoints
        }
        # Kept apart from the judgement telemetry (_state). What only the autopilot knows:
        # mode, mission sequence, status text.
        self._autopilot: dict[str, dict] = {a: _blank_autopilot() for a in endpoints}
        # Each link is read by one thread only; replies are handed over through queues.
        self._acks: dict[str, queue.Queue] = {a: queue.Queue() for a in endpoints}
        self._mission_replies: dict[str, queue.Queue] = {a: queue.Queue() for a in endpoints}
        self._ready: dict[str, threading.Event] = {a: threading.Event() for a in endpoints}
        # (system, component) of the autopilot that takes commands; replaced by the values in
        # its heartbeat. PX4 only accepts mission messages addressed to its own ids (not 0 = all).
        self._targets: dict[str, tuple[int, int]] = {a: (1, 1) for a in endpoints}
        # One command is several round trips. Two overlapping would take each other's replies,
        # so one at a time.
        self._operations: dict[str, threading.RLock] = {a: threading.RLock() for a in endpoints}
        self._writes: dict[str, threading.Lock] = {a: threading.Lock() for a in endpoints}
        self._tick = 0
        self._guard = threading.Lock()
        self._closed = threading.Event()
        self._listeners = [
            threading.Thread(target=self._listen, args=(asset_id,), daemon=True,
                             name=f"mavlink-{asset_id}")
            for asset_id in self.links
        ]
        for listener in self._listeners:
            listener.start()

    def close(self) -> None:
        """Stops the listener threads and closes the links. Tests need this to free the ports."""
        self._closed.set()
        for listener in self._listeners:
            listener.join(timeout=2.0)
        for link in self.links.values():
            link.close()

    # ---------- telemetry ----------

    def _listen(self, asset_id: str) -> None:
        link = self.links[asset_id]
        next_beat = 0.0
        while not self._closed.is_set():
            if time.monotonic() >= next_beat:
                self._beat(asset_id)
                next_beat = time.monotonic() + GCS_HEARTBEAT_S
            try:
                message = link.recv_match(blocking=True, timeout=0.5)
            except Exception as error:  # noqa: BLE001 — a dead listener keeps showing stale state
                if self._closed.is_set():
                    return
                print(f"mavlink {asset_id}: {error!r}", flush=True)
                time.sleep(0.5)
                continue
            if message is not None:
                self._route(asset_id, message)

    def _beat(self, asset_id: str) -> None:
        """GCS heartbeat. udpin silently drops it while no peer has talked to us yet."""
        mav = self._mav
        try:
            with self._writes[asset_id]:
                self.links[asset_id].mav.heartbeat_send(
                    mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, mav.MAV_STATE_ACTIVE)
        except OSError:
            pass  # no receiver yet (udpout's ICMP refusal): try again on the next beat

    def _route(self, asset_id: str, message) -> None:
        kind = message.get_type()
        if kind == "COMMAND_ACK":
            self._acks[asset_id].put(message)
            return
        if kind in MISSION_REPLIES:
            self._mission_replies[asset_id].put(message)
            return
        if kind == "HEARTBEAT":
            if not self._from_vehicle(message):
                return
            with self._guard:
                self._targets[asset_id] = (message.get_srcSystem(), message.get_srcComponent())
                self._autopilot[asset_id]["last_beat"] = time.monotonic()
            self._ready[asset_id].set()
        self._absorb(asset_id, message)

    def _from_vehicle(self, message) -> bool:
        """Is this the vehicle's heartbeat? Other components (GCS, cameras) are not targeted."""
        return (message.type != self._mav.MAV_TYPE_GCS
                and message.autopilot != self._mav.MAV_AUTOPILOT_INVALID)

    def _absorb(self, asset_id: str, message) -> None:
        kind = message.get_type()
        with self._guard:
            entry = self._state[asset_id]
            extra = self._autopilot[asset_id]
            if kind == "GLOBAL_POSITION_INT":
                entry["lat"] = message.lat / 1e7
                entry["lon"] = message.lon / 1e7
                entry["alt_m"] = message.relative_alt / 1000.0
            elif kind == "BATTERY_STATUS" and message.battery_remaining >= 0:
                entry["battery"] = float(message.battery_remaining)
            elif kind == "SYS_STATUS" and message.battery_remaining >= 0:
                entry.setdefault("battery", float(message.battery_remaining))
            elif kind == "VIBRATION":
                worst = max(message.vibration_x, message.vibration_y, message.vibration_z)
                entry["vibration"] = round(min(1.0, worst / 60.0), 3)
            elif kind == "HEARTBEAT":
                entry["armed"] = bool(message.base_mode & self._mav.MAV_MODE_FLAG_SAFETY_ARMED)
                entry["state"] = self._read_state(entry)
                entry["autonomy_health"] = 1.0 if entry.get("guided", True) else 0.0
                extra["mode"] = self._mode_name(message)
            elif kind == "EXTENDED_SYS_STATE":
                extra["landed"] = LANDED_STATES.get(message.landed_state)
            elif kind == "MISSION_CURRENT":
                extra["mission_seq"] = int(message.seq)
            elif kind == "MISSION_ITEM_REACHED":
                extra["reached_seq"] = int(message.seq)
            elif kind == "STATUSTEXT":
                text = message.text
                text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
                extra["status_text"].append(text.rstrip("\x00"))
            entry.setdefault("model", os.getenv("VEHICLE_MODEL", "px4-sitl"))
            entry.setdefault("kind", "drone")
            entry.setdefault("battery", 100.0)
            entry.setdefault("vibration", 0.0)
            entry.setdefault("autonomy_health", 1.0)
            entry.setdefault("passengers", 0)
            entry.setdefault("assigned_pad", None)

    def _mode_name(self, message) -> str:
        if message.autopilot == self._mav.MAV_AUTOPILOT_PX4:
            return px4_mode_name(message.custom_mode)
        return self._mavutil.mode_string_v10(message)

    @staticmethod
    def _read_state(entry: dict) -> str:
        if not entry.get("armed"):
            return "landed" if (entry.get("alt_m") or 0.0) < 1.0 else "grounded"
        return "approaching" if entry.get("assigned_pad") else "cruising"

    def telemetry(self) -> dict:
        with self._guard:
            self._tick += 1
            return {"tick": self._tick, "assets": {k: dict(v) for k, v in self._state.items()}}

    def airborne(self, asset_id: str) -> bool:
        """Airborne? PX4's landed state (EXTENDED_SYS_STATE) comes first; without it, arming
        and altitude decide."""
        with self._guard:
            landed = self._autopilot[asset_id]["landed"]
            entry = self._state[asset_id]
            armed, alt = bool(entry.get("armed")), float(entry.get("alt_m") or 0.0)
        if landed is not None:
            return landed != "on_ground"
        return armed and alt > AIRBORNE_M

    def altitude(self, asset_id: str) -> float:
        with self._guard:
            return float(self._state[asset_id].get("alt_m") or 0.0)

    def link_up(self, asset_id: str, within_s: float = LINK_STALE_S) -> bool:
        """Heard a heartbeat within within_s? Always False if none was ever heard."""
        with self._guard:
            beat = self._autopilot[asset_id]["last_beat"]
        return beat is not None and time.monotonic() - beat < within_s

    def autopilot_view(self, asset_id: str) -> dict:
        """One autopilot's state for the screen. Read-only."""
        now = time.monotonic()
        with self._guard:
            entry, extra = self._state[asset_id], self._autopilot[asset_id]
            beat = extra["last_beat"]
            since = None if beat is None else round(now - beat, 1)
            link = "waiting" if since is None else ("up" if since < LINK_STALE_S else "lost")
            return {
                "endpoint": self.endpoints[asset_id], "link": link, "last_heartbeat_s": since,
                "lat": entry.get("lat"), "lon": entry.get("lon"), "alt_m": entry.get("alt_m"),
                "armed": entry.get("armed"), "mode": extra["mode"], "landed": extra["landed"],
                "battery": entry.get("battery"), "mission_seq": extra["mission_seq"],
                "reached_seq": extra["reached_seq"], "status_text": list(extra["status_text"]),
            }

    # ---------- commands ----------

    def execute(
        self,
        asset_id: str,
        action: str,
        params: dict,
        ledger_id: str,
        blast: str = "none",
        approved_by: str | None = None,
    ) -> dict:
        link = self.links.get(asset_id)
        if link is None:
            return {"ok": False, "error": f"no link for {asset_id}"}
        if action in NO_AUTOPILOT_COMMAND:
            return {"ok": True, "note": NO_AUTOPILOT_COMMAND[action]}
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            # Ground equipment is not the autopilot's business. The runtime only records it in
            # the ledger.
            return {"ok": True, "note": f"{action} is ground equipment, no autopilot command"}
        if not self._ready[asset_id].wait(timeout=self.link_timeout_s):
            return {"ok": False, "error": f"{asset_id} autopilot has not reported in"}
        return handler(link, asset_id, params)

    def _do_fly_route(self, link, asset_id: str, params: dict) -> dict:
        return self.fly_route(asset_id, params.get("legs") or [])

    def _do_reserve_pad(self, link, asset_id: str, params: dict) -> dict:  # noqa: D401
        # Route to a pad for a faulted aircraft. Legs from the runtime go into the mission as is.
        if params.get("legs"):
            return self.fly_route(asset_id, params["legs"])
        pad = params.get("pad")
        target = PAD_COORDS.get(pad)
        if target is None:
            return {"ok": False, "error": f"unknown pad {pad}"}
        with self._guard:
            self._state[asset_id]["assigned_pad"] = pad
        with self._writes[asset_id]:
            link.mav.set_position_target_global_int_send(
                0, *self._targets[asset_id],
                self._mav.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                0b0000111111111000,
                int(target[0] * 1e7), int(target[1] * 1e7), DEPOT_ALT_M,
                0, 0, 0, 0, 0, 0, 0, 0,
            )
        return {"ok": True, "sent": "set_position_target", "pad": pad}

    def _do_land(self, link, asset_id: str, params: dict) -> dict:
        return self.land_here(asset_id)

    def _do_divert_ground(self, link, asset_id: str, params: dict) -> dict:
        with self._guard:
            self._state[asset_id]["assigned_pad"] = None
        return self.recall(asset_id, params.get("exit"))

    def _do_disengage_autonomy(self, link, asset_id: str, params: dict) -> dict:
        """Hands control from autonomy to a human. This one line is what strands the passengers."""
        with self._guard:
            self._state[asset_id]["guided"] = False
        return self._command(asset_id, self._mav.MAV_CMD_DO_SET_MODE,
                             self._mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)

    # ---------- mission-level commands (the mirror calls these too) ----------

    def fly_route(self, asset_id: str, legs: list[dict]) -> dict:
        """Uploads the approved route as a mission and starts it at once (standalone wiring).
        The mirror calls the two separately."""
        items = route_items(legs, self.airborne(asset_id))
        if not items:
            return {"ok": False, "error": "a route needs at least two legs"}
        upload = self.upload_mission(asset_id, items)
        if not upload["ok"]:
            return upload
        return {**self.start_mission(asset_id, arm=not self.airborne(asset_id)),
                "items": len(items)}

    def recall(self, asset_id: str, exit_point: dict | None) -> dict:
        """Recalls the approval. In the air, flies to the runtime's exit point (exit) and lands.

        With no exit point (recalled outside the zone) it lands where it is. On the ground it
        clears the uploaded mission — an aircraft that has not taken off is left with no way to
        arm on that mission.
        """
        if not self.airborne(asset_id):
            return {**self.clear_mission(asset_id), "did": "cleared"}
        if exit_point and exit_point.get("lat") is not None and exit_point.get("lon") is not None:
            items = exit_items(exit_point, max(self.altitude(asset_id), AIRBORNE_M))
            upload = self.upload_mission(asset_id, items)
            if not upload["ok"]:
                return {**upload, "did": "exit_mission", "items": items, "uploaded": False}
            return {**self.start_mission(asset_id, arm=False), "did": "exit_mission",
                    "items": items, "uploaded": True}
        return {**self.land_here(asset_id), "did": "land_here"}

    def upload_mission(self, asset_id: str, items: list[dict]) -> dict:
        """MISSION_COUNT → (MISSION_REQUEST_INT n → MISSION_ITEM_INT n)… → MISSION_ACK.

        The autopilot asks for the items one by one. A repeated request for the same number is
        answered again (the item was lost). It is bounded: the upload is abandoned when one item
        is requested again more than MISSION_ITEM_RESENDS times, or when the whole exchange
        exceeds one reply's wait × (item count + MISSION_COUNT retries). Before it was bounded,
        an autopilot asking for the same number forever held the worker thread, and the recall
        queued behind it never went out (measured: 1,866 items sent in 25 s, not one recall).
        """
        if not items:
            return {"ok": False, "error": "empty mission"}
        replies = self._mission_replies[asset_id]
        deadline = time.monotonic() + self.ack_timeout_s * (len(items) + MISSION_COUNT_TRIES)
        with self._operations[asset_id]:
            _drain(replies)
            sent: dict[int, int] = {}
            tries = 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"ok": False, "error": "mission upload did not finish in time",
                            "count": len(items), "asked": len(sent)}
                if not sent:
                    if tries >= MISSION_COUNT_TRIES:
                        return {"ok": False, "error": "autopilot never asked for the mission items",
                                "count": len(items)}
                    self._send(asset_id, "mission_count_send", len(items))
                    tries += 1
                try:
                    reply = replies.get(timeout=min(self.ack_timeout_s, left))
                except queue.Empty:
                    if sent:
                        return {"ok": False, "error": "autopilot stopped asking for mission items",
                                "count": len(items), "asked": len(sent)}
                    continue
                if reply.get_type() == "MISSION_ACK":
                    accepted = reply.type == self._mav.MAV_MISSION_ACCEPTED
                    return {"ok": accepted, "result": int(reply.type), "count": len(items)}
                if 0 <= reply.seq < len(items):
                    sent[reply.seq] = sent.get(reply.seq, 0) + 1
                    if sent[reply.seq] > MISSION_ITEM_RESENDS + 1:
                        return {"ok": False,
                                "error": f"autopilot kept asking for mission item {reply.seq}",
                                "count": len(items), "asked": len(sent)}
                    self._send_item(asset_id, reply.seq, items[reply.seq])

    def _send_item(self, asset_id: str, seq: int, item: dict) -> None:
        mav = self._mav
        command = {"takeoff": mav.MAV_CMD_NAV_TAKEOFF, "waypoint": mav.MAV_CMD_NAV_WAYPOINT,
                   "land": mav.MAV_CMD_NAV_LAND}[item["command"]]
        # Altitude is relative to the takeoff spot (home). The sim's altitude is above ground
        # too, and SIH's ground is at home height. Yaw (param4) is NaN — the autopilot works
        # out the heading itself.
        self._send(asset_id, "mission_item_int_send", seq, mav.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                   command, 0, 1, 0.0, 0.0, 0.0, math.nan,
                   int(round(item["lat"] * 1e7)), int(round(item["lon"] * 1e7)),
                   float(item["alt_m"]))

    def start_mission(self, asset_id: str, arm: bool = True) -> dict:
        """Sets AUTO.MISSION, arms (if on the ground) and starts from the first item.

        If any step is refused it stops there and returns that answer as is. We gave the
        approval, but the autopilot decides whether it can take off now.
        """
        mav = self._mav
        steps = [("mode", mav.MAV_CMD_DO_SET_MODE,
                  (mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, PX4_MAIN_AUTO, PX4_AUTO_MISSION))]
        if arm:
            steps.append(("arm", mav.MAV_CMD_COMPONENT_ARM_DISARM, (1,)))
        steps.append(("start", mav.MAV_CMD_MISSION_START, (0, 0)))
        answered: dict[str, object] = {}
        with self._operations[asset_id]:
            for name, command, params in steps:
                reply = self._command(asset_id, command, *params)
                answered[name] = reply.get("result", "no answer")
                if not reply["ok"]:
                    why = "refused" if "result" in reply else "did not answer"
                    return {"ok": False, "error": f"autopilot {why} {name}",
                            "result": reply.get("result"), "steps": answered}
        return {"ok": True, "steps": answered}

    def clear_mission(self, asset_id: str) -> dict:
        replies = self._mission_replies[asset_id]
        with self._operations[asset_id]:
            _drain(replies)
            self._send(asset_id, "mission_clear_all_send")
            deadline = time.monotonic() + self.ack_timeout_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    reply = replies.get(timeout=remaining)
                except queue.Empty:
                    break
                if reply.get_type() == "MISSION_ACK":
                    return {"ok": reply.type == self._mav.MAV_MISSION_ACCEPTED,
                            "result": int(reply.type)}
        return {"ok": False, "error": "autopilot did not acknowledge the clear"}

    def land_here(self, asset_id: str) -> dict:
        # Position (param5/6) is NaN: land right here. Zero would be read as lat 0, lon 0.
        nan = math.nan
        return self._command(asset_id, self._mav.MAV_CMD_NAV_LAND, 0, 0, 0, nan, nan, nan, nan)

    def _send(self, asset_id: str, name: str, *args) -> None:
        with self._guard:
            target = self._targets[asset_id]
        with self._writes[asset_id]:
            getattr(self.links[asset_id].mav, name)(*target, *args)

    def _command(self, asset_id: str, command: int, *params: float) -> dict:
        """The autopilot may refuse. We approve; the autopilot accepts."""
        values = [float(value) for value in params] + [0.0] * (7 - len(params))
        acks = self._acks[asset_id]
        with self._operations[asset_id]:
            _drain(acks)  # clear out stale replies first
            self._send(asset_id, "command_long_send", command, 0, *values)
            deadline = time.monotonic() + self.ack_timeout_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    ack = acks.get(timeout=remaining)
                except queue.Empty:
                    break
                if ack.command != command or ack.result == self._mav.MAV_RESULT_IN_PROGRESS:
                    continue  # a late reply to another command, or still in progress
                accepted = ack.result == self._mav.MAV_RESULT_ACCEPTED
                return {"ok": accepted, "result": int(ack.result)}
        return {"ok": False, "error": "autopilot did not acknowledge"}


def from_env() -> "MavlinkFleetAdapter":
    """MAVLINK_ENDPOINTS='drone-01=udpin:0.0.0.0:14540,drone-02=udpin:0.0.0.0:14541'"""
    raw = os.environ["MAVLINK_ENDPOINTS"]
    endpoints = dict(pair.split("=", 1) for pair in raw.split(","))
    return MavlinkFleetAdapter(endpoints)
