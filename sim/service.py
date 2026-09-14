"""Runs the clock and exposes the actuator. It never asks who is calling."""

import os
import threading
import time

from shared.http import JsonServer
from sim.world import Simulation


def main() -> None:
    simulation = Simulation(
        seed=int(os.getenv("SEED", "7")),
        fleet_limit=float(os.getenv("FLEET_LIMIT_USD", "500")),
        tick_seconds=float(os.getenv("TICK_SECONDS", "0.2")),
        lock_actuator=os.getenv("LOCK_ACTUATOR", "0") == "1",
        # With the screen open it must keep running; a finished round restarts by itself.
        # The 11 km service radius makes a one-way leg up to 570 ticks. Two drops and back to
        # the depot is about 2,000 ticks a trip, and a round needs two trips for the cycle to show.
        max_ticks=int(os.getenv("ROUND_TICKS", "5000")),   # Harlem round trip 4,040 ticks + margin
        # Model id of the direct wiring's agents. They have no way to reach the runtime (compose's
        # world network), so whoever starts them (scripts/dev.sh, compose) passes it in. Empty
        # means rules; unset means unknown.
        direct_model=os.getenv("DIRECT_MODEL"),
    )

    def clock() -> None:
        while True:
            simulation.step()
            time.sleep(simulation.tick_seconds)

    threading.Thread(target=clock, daemon=True).start()

    def state(body, query):
        name = query.get("world", "guarded")
        world = simulation.worlds.get(name)
        if world is None:
            return 404, {"error": f"unknown world {name}"}
        # Send the round number too: it is the only way the runtime knows when to clear its
        # per-round state (budget, locks).
        # Airspace volumes only on request — with buildings there are over 3,000, too many to
        # send every time.
        wants_volumes = query.get("volumes") in ("1", "true", "yes")
        return 200, {**world.snapshot(simulation.tick_count, volumes=wants_volumes),
                     "round": simulation.rounds}

    def reset(body, query):
        """Restart the round, for when you open the screen and want the scenario from the start.

        A demo rewind, not a command to the actuator. The runtime sees the round number change
        and resets its own state (budget, locks, notices).
        """
        simulation.reset()
        return 200, {"ok": True, "round": simulation.rounds, "tick": simulation.tick_count}

    def act(body, query):
        world = simulation.worlds.get(body.get("world", "guarded"))
        if world is None:
            return 404, {"error": "unknown world"}
        result = world.act(
            asset=body.get("asset", ""),
            action=body.get("action", ""),
            params=body.get("params") or {},
            ledger_id=body.get("ledger_id"),
            blast=body.get("blast", "none"),
            approved_by=body.get("approved_by"),
            tick=simulation.tick_count,
        )
        return 200, result

    def compare(body, query):
        # The approval screen's recall line is for the recall notice only. Giving the first
        # notice's tick would label the weather and incident windows as a recall too.
        recall = next((b for b in simulation.bulletins() if b.get("kind") == "recall"), None)
        return 200, {
            "tick": simulation.tick_count,
            "round": simulation.rounds,
            "recall_tick": recall["published_tick"] if recall else None,
            # Every notice currently posted; the screen shows which rules arrived, as is.
            # Zone notices carry only their sentence (text). The screen banner is drawn from the
            # runtime's /state notices; these are the originals of "what arrived".
            "bulletins": [{k: b.get(k) for k in ("id", "kind", "name", "reason", "text",
                                                  "published_tick", "until_tick",
                                                  "forbid_action", "applies_to",
                                                  "address", "radius_m", "building_id")}
                          for b in simulation.bulletins()],
            # For the screen, so the truth comes too: where a lost-link aircraft really is
            # (dark). The /state read by the runtime and the drone agents only carries the
            # record from the moment the link dropped.
            "worlds": {
                name: world.snapshot(simulation.tick_count, truth=True)
                for name, world in simulation.worlds.items()
            },
        }

    def rows(body, query):
        """A flat table Grafana takes as is. No nesting, so its config does not grow."""
        out = []
        for name, world in simulation.worlds.items():
            snapshot = world.snapshot(simulation.tick_count)
            crowded: dict[str, int] = {}
            for vehicle in snapshot["assets"].values():
                if vehicle["state"] in ("landed", "charging") and vehicle["assigned_pad"]:
                    crowded[vehicle["assigned_pad"]] = (
                        crowded.get(vehicle["assigned_pad"], 0) + 1
                    )
            for vehicle in snapshot["assets"].values():
                out.append({
                    "world": "runtime" if name == "guarded" else "direct",
                    "wiring": name,
                    "id": vehicle["id"],
                    "model": vehicle["model"],
                    "lat": vehicle["lat"],
                    "lon": vehicle["lon"],
                    "alt_m": vehicle["alt_m"],
                    "battery": vehicle["battery"],
                    "state": vehicle["state"],
                    "pad": vehicle["assigned_pad"] or "",
                    "spend_usd": vehicle["spend"],
                    "in_conflict": crowded.get(vehicle["assigned_pad"], 0) > 1,
                })
        return 200, out

    def scoreboard_rows(body, query):
        # The words the approval screen uses (frontend/approvals.html), counter for counter, so the
        # endpoint and the screen cannot drift apart unseen. The last two have no twin there.
        labels = [
            ("pad_conflicts", "Landing pad collisions"),
            ("post_recall_violations", "Banned acts after a recall"),
            ("weather_hold_takeoffs", "Takeoffs during a weather hold"),
            ("incident_incursions", "Flights into an incident scene"),
            ("unapproved_passenger_actions", "Unapproved passenger acts"),
            ("unrecorded_actions", "Acts with no record"),
            ("batteries_dead", "Stopped on a dead battery"),
            ("human_approvals", "Approved by a person"),
            ("spend_usd", "Fleet spend ($)"),
            ("over_fleet_limit_usd", "Over the fleet limit ($)"),
        ]
        guarded = simulation.worlds["guarded"].score.public()
        direct = simulation.worlds["direct"].score.public()
        return 200, [
            {"metric": label, "runtime": guarded[key], "direct": direct[key]}
            for key, label in labels
        ]

    def pad_rows(body, query):
        snapshot = simulation.worlds["guarded"].snapshot(simulation.tick_count)
        return 200, [
            {"pad": name.replace("pad:", ""), "lat": at["lat"], "lon": at["lon"]}
            for name, at in snapshot["pad_coords"].items()
        ]

    server = JsonServer(int(os.getenv("PORT", "8100")))
    server.add("GET", "/rows", rows)
    server.add("GET", "/scoreboard_rows", scoreboard_rows)
    server.add("GET", "/pad_rows", pad_rows)
    server.add("GET", "/state", state)
    server.add("POST", "/act", act)
    server.add("POST", "/reset", reset)
    server.add("GET", "/compare", compare)
    server.add("GET", "/bulletins", lambda body, query: (200, {
        "tick": simulation.tick_count, "bulletins": simulation.bulletins()
    }))
    server.add("GET", "/health", lambda body, query: (200, {"ok": True}))
    print(f"sim listening on :{os.getenv('PORT', '8100')}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
