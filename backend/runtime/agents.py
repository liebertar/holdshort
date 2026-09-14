"""Which aircraft have announced themselves, and when one goes stale.

Owner of `agents`. The staleness rule only means anything next to the registration it ages.
"""

import os

from shared.models import AgentIdentity

# Drop an aircraft from /state.agents when its registration goes unrenewed for this many ticks.
# Aircraft re-announce every 30 s (drone/agent/loop.py REGISTER_PERIOD_S) — at 0.2 s/tick,
# 600 ticks is 2 minutes, i.e. more than two missed announcements.
AGENT_STALE_TICKS = int(os.getenv("AGENT_STALE_TICKS") or "600")
# Fields of one /state.agents row.
AGENT_FIELDS = ("model", "host", "world", "last_seen_tick", "display", "base_url_port", "model_ok")


class AgentsMixin:
    def register_agent(self, body: dict) -> tuple[int, dict]:
        """POST /agents/register. An aircraft process says what writes its filings. Only a
        label."""
        try:
            identity = AgentIdentity.from_dict(body or {})
        except ValueError as error:
            return 400, {"error": str(error)}
        if identity.world != "guarded":
            return 400, {"error": "the direct world does not file with the runtime — the "
                                  "simulator loads that model per aircraft (DIRECT_MODEL)"}
        # The world (telemetry) knows the fleet roster. Accepting a name off the roster turns the
        # screen's 'drones · … ×4' into ×5. Before the world has arrived, 503: the aircraft
        # registers again a few seconds later.
        fleet = set(self.telemetry)
        if not fleet:
            return 503, {"error": "no world received yet — say so again shortly",
                         "retry": True}
        if identity.asset_id not in fleet:
            return 404, {"error": f"{identity.asset_id} is not in this fleet"}
        with self._guard:
            self.agents[identity.asset_id] = {**identity.to_dict(), "last_seen_tick": self.tick}
        return 200, {"ok": True, "display": identity.display, "tick": self.tick}

    def agents_snapshot(self) -> dict:
        """/state.agents. Drops aircraft not heard from for AGENT_STALE_TICKS: a screen still
        showing a dead process's model name would be lying."""
        with self._guard:
            for asset in [a for a, row in self.agents.items()
                          if self.tick - int(row["last_seen_tick"]) > AGENT_STALE_TICKS]:
                del self.agents[asset]
            return {asset: {key: row.get(key) for key in AGENT_FIELDS}
                    for asset, row in self.agents.items()}
