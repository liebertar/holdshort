"""A minimal MAVLink client, written by the team that wired the agent to the vehicle.

Every fleet that does this ends up with one of these, and every one of them is slightly
different. That is the point.
"""

import threading


class MavCommander:
    def __init__(self, endpoint: str):
        from pymavlink import mavutil

        self._mavutil = mavutil
        self.link = mavutil.mavlink_connection(endpoint)
        self.state: dict = {"state": "unknown"}
        self.ready = threading.Event()
        self._guard = threading.Lock()
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self) -> None:
        while True:
            message = self.link.recv_match(blocking=True, timeout=5)
            if message is None:
                continue
            kind = message.get_type()
            with self._guard:
                if kind == "GLOBAL_POSITION_INT":
                    self.state["lat"] = message.lat / 1e7
                    self.state["lon"] = message.lon / 1e7
                    self.state["alt_m"] = message.relative_alt / 1000.0
                elif kind == "BATTERY_STATUS" and message.battery_remaining >= 0:
                    self.state["battery"] = float(message.battery_remaining)
                elif kind == "VIBRATION":
                    worst = max(message.vibration_x, message.vibration_y, message.vibration_z)
                    self.state["vibration"] = round(min(1.0, worst / 60.0), 3)
                elif kind == "HEARTBEAT":
                    armed = bool(
                        message.base_mode & self._mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                    )
                    self.state["armed"] = armed
                    self.state["state"] = self._read_state(armed)
                    self.ready.set()

    def _read_state(self, armed: bool) -> str:
        if not armed:
            return "landed" if (self.state.get("alt_m") or 0.0) < 1.0 else "grounded"
        return "approaching" if self.state.get("assigned_pad") else "cruising"

    def telemetry(self, asset_id: str, model: str) -> dict:
        with self._guard:
            entry = dict(self.state)
        entry.update(
            id=asset_id, model=model, kind="drone",
            battery=entry.get("battery", 100.0),
            vibration=entry.get("vibration", 0.0),
            autonomy_health=entry.get("autonomy_health", 1.0),
            passengers=entry.get("passengers", 0),
            assigned_pad=entry.get("assigned_pad"),
        )
        return entry

    def send(self, action: str, params: dict, pads: dict) -> dict:
        if not self.ready.wait(timeout=20):
            return {"ok": False, "error": "autopilot has not reported in"}
        mav = self._mavutil.mavlink

        if action == "reserve_pad":
            pad = params.get("pad")
            target = pads.get(pad)
            if target is None:
                return {"ok": False, "error": f"unknown pad {pad}"}
            with self._guard:
                self.state["assigned_pad"] = pad
            self.link.mav.set_position_target_global_int_send(
                0, self.link.target_system, self.link.target_component,
                mav.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, 0b0000111111111000,
                int(target[0] * 1e7), int(target[1] * 1e7), 30.0,
                0, 0, 0, 0, 0, 0, 0, 0,
            )
            return {"ok": True}

        commands = {
            "land": mav.MAV_CMD_NAV_LAND,
            "depart": mav.MAV_CMD_NAV_TAKEOFF,
            "divert_ground": mav.MAV_CMD_NAV_RETURN_TO_LAUNCH,
            "disengage_autonomy": mav.MAV_CMD_DO_SET_MODE,
        }
        if action not in commands:
            return {"ok": True, "note": "ground equipment"}
        if action == "depart":
            with self._guard:
                self.state["assigned_pad"] = None
        self.link.mav.command_long_send(
            self.link.target_system, self.link.target_component,
            commands[action], 0, 0, 0, 0, 0, 0, 0, 30.0,
        )
        return {"ok": True}
