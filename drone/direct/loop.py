"""The agent that holds the actuator address. Same eyes, same hands, different wiring.

TRANSPORT=http     → the built-in city simulator's actuator endpoint
TRANSPORT=mavlink  → a real autopilot over MAVLink, through this package's own client

It is not a straw man. It keeps to its own per-vehicle budget, it reads the recall bulletin
and obeys it, it does not repeat commands, and it is shown the whole fleet's state, which
the guarded agent never sees. What it cannot do is know what the other vehicles intend, or
what the fleet has spent, or refuse an action the moment a recall lands. There is nowhere
to put those rules when every agent is its own island.
"""

import os
import time

from drone.agent.detect import detect
from drone.agent.loop import build_llm
from drone.agent.planner import OperatorPlanner
from drone.agent.propose import COSTS, Proposer
from shared.http import get_json, post_json
from shared.route import Router

PADS = ["pad:launch"]
PAD_COORDS = {   # the operator knows its own base's coordinates
    "pad:launch": (40.701783, -73.969168),   # emergency pad east of the yard (sim.world.PADS)
}


class DirectAgent:
    def __init__(self, asset_id: str, sim_url: str, proposer: Proposer,
                 per_asset_limit: float, bulletin_period_s: float,
                 transport: str = "http", mavlink_endpoint: str = ""):
        self.asset_id = asset_id
        self.sim_url = sim_url.rstrip("/")
        self.proposer = proposer
        self.per_asset_limit = per_asset_limit
        self.bulletin_period_s = bulletin_period_s
        self.transport = transport
        self.spend = 0.0
        self._round = None          # follows the simulator when it starts a new round
        self.banned_actions: set[str] = set()
        self.cooldown: dict[str, float] = {}
        self.repeat_s = float(os.getenv("REPEAT_COOLDOWN_S", "2"))
        self._last_bulletin_check = 0.0

        self.commander = None
        if transport == "mavlink":
            from drone.direct.mav_client import MavCommander

            self.commander = MavCommander(mavlink_endpoint)

    # ---------- what it can see ----------

    def observe(self) -> tuple[dict, dict]:
        """(own state, neighbours' states). Neighbours' intents are visible from neither side."""
        if self.commander is not None:
            mine = self.commander.telemetry(
                self.asset_id, os.getenv("VEHICLE_MODEL", "dv-x500")
            )
            neighbours = get_json(f"{self.sim_url}/state?world=direct") or {}
            return mine, neighbours.get("assets", {})
        state = get_json(f"{self.sim_url}/state?world=direct") or {}
        self._follow_round(state.get("round"))
        assets = state.get("assets") or {}
        return assets.get(self.asset_id) or {}, assets

    def _follow_round(self, round_number) -> None:
        """When the round changes, restart the own spending count too.

        If only the runtime side reset its budget each round while this one kept adding up,
        from the second round on the direct fleet would hit its limit and do nothing. That's
        not a difference in wiring but us crippling one side, and then the two wirings can't
        be compared.
        """
        if round_number is None or round_number == self._round:
            return
        self._round = round_number
        self.spend = 0.0
        self.banned_actions.clear()
        self.cooldown.clear()
        self._last_bulletin_check = 0.0

    def _refresh_bulletins(self, model: str) -> None:
        now = time.time()
        if now - self._last_bulletin_check < self.bulletin_period_s:
            return
        self._last_bulletin_check = now
        payload = get_json(f"{self.sim_url}/bulletins") or {}
        for item in payload.get("bulletins", []):
            if item.get("applies_to", {}).get("model") not in (None, model):
                continue
            if item.get("forbid_action"):
                self.banned_actions.add(item["forbid_action"])
            if item.get("forbid_resource"):
                self.banned_actions.add(item["forbid_resource"])

    def _free_looking_pad(self, neighbours: dict) -> str:
        """Only pads another aircraft is actually sitting on can be avoided.

        Positions are public through Remote ID, but the intent 'I have reserved that pad' is
        not. Between different companies there is no way at all to see each other's
        reservations. ASTM F3548 solved this for the airways; nobody has solved it for pads
        on the ground.
        """
        taken = {
            vehicle.get("assigned_pad")
            for vehicle_id, vehicle in neighbours.items()
            if vehicle_id != self.asset_id
            and vehicle.get("state") in ("landed", "charging")
        }
        open_pads = [p for p in PADS if p not in self.banned_actions]
        return next((p for p in open_pads if p not in taken),
                    open_pads[0] if open_pads else PADS[0])

    # ---------- what it does ----------

    def step(self) -> None:
        telemetry, neighbours = self.observe()
        if not telemetry or telemetry.get("state") == "unknown":
            return
        self._refresh_bulletins(telemetry.get("model", ""))

        concern = detect(telemetry)
        if concern is None:
            return

        proposal = self.proposer.write(
            concern, telemetry, self._free_looking_pad(neighbours),
            frozenset(self.banned_actions), tuple(PADS),
        )
        if proposal.action in self.banned_actions or (
            proposal.resource and proposal.resource in self.banned_actions
        ):
            return  # once it has seen the notice, it complies on its own
        cost = COSTS.get(proposal.action, 0.0)
        if self.spend + cost > self.per_asset_limit:
            return  # keeps its own limit; it has no way to know the fleet total
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # don't resend a command just sent
        self.cooldown[proposal.action] = time.time() + self.repeat_s

        self._attach_route(proposal, telemetry)
        result = self._act(proposal)
        if result and result.get("ok"):
            self.spend += result.get("cost_usd", cost)
        verdict = "ok" if (result or {}).get("ok") else "failed"
        print(f"[direct:{self.asset_id}] {proposal.action} ${cost:.0f} -> {verdict}", flush=True)

    @staticmethod
    def _attach_route(proposal, telemetry: dict) -> None:
        """A straight line to the destination, ignoring airspace — the very route this wiring
        files.

        The autopilot needs waypoints to take off (ready doesn't lift off without a route).
        Sending fly_route without a route left four aircraft sitting in the yard running up
        costs, with no wiring to compare. It's the same straight line the harness's direct
        side (tests/test_two_worlds.DirectSide) attaches.
        """
        if proposal.action not in ("fly_route", "reserve_pad"):
            return
        here = (telemetry.get("lat"), telemetry.get("lon"))
        goal = None
        if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
            goal = (telemetry["job_lat"], telemetry["job_lon"])
        elif proposal.action == "reserve_pad":
            goal = PAD_COORDS.get(proposal.resource or proposal.params.get("pad"))
        if here[0] is None or goal is None:
            return
        proposal.params = {**proposal.params, "legs": OperatorPlanner.straight_at(
            here, goal, Router.cruise_alt_default())}

    def _act(self, proposal) -> dict | None:
        if self.commander is not None:
            return self.commander.send(proposal.action, proposal.params, PAD_COORDS)
        return post_json(
            f"{self.sim_url}/act",
            {
                "world": "direct",
                "asset": self.asset_id,
                "action": proposal.action,
                "params": proposal.params,
                "blast": proposal.blast_radius,
            },
        )


def main() -> None:
    asset_id = os.environ["ASSET_ID"]
    period = float(os.getenv("AGENT_PERIOD_S", "0.6"))
    llm = build_llm()
    agent = DirectAgent(
        asset_id,
        os.getenv("SIM_URL", "http://sim:8100"),
        Proposer(llm),
        per_asset_limit=float(os.getenv("PER_ASSET_LIMIT_USD", "200")),
        bulletin_period_s=float(os.getenv("BULLETIN_PERIOD_S", "5")),
        transport=os.getenv("TRANSPORT", "http"),
        mavlink_endpoint=os.getenv("MAVLINK_ENDPOINT", ""),
    )
    print(f"agent {asset_id} up, holds the actuator "
          f"({agent.transport}, llm={'on' if llm.enabled else 'off'})", flush=True)
    while True:
        agent.step()
        time.sleep(period)


if __name__ == "__main__":
    main()
