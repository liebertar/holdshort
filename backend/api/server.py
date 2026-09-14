"""The runtime process: the route table in front of the tower, and nothing else.

The whole controller layer. Response shaping and body validation stay in the domain packages:
under a mixin composition, an api-side mixin would have to be a base of Runtime, and the
domain would then import the controller.
"""

import os

from backend.runtime.tower import Runtime
from backend.store.intake_store import DEFAULT_PATH as STORE_DEFAULT_PATH
from shared.http import JsonServer


def main() -> None:
    runtime = Runtime(
        config_path=os.getenv("CONFIG", "configs/fleet.yaml"),
        sim_url=os.getenv("SIM_URL", "http://sim:8100"),
        ledger_path=os.getenv("LEDGER_PATH", "ledger.jsonl"),
        window_s=float(os.getenv("ARBITRATION_WINDOW_S", "1.5")),
        # Intake record. Under compose: /data/intake.sqlite (same volume as the ledger).
        intake_db=os.getenv("INTAKE_DB") or STORE_DEFAULT_PATH,
        metar=True,
        await_airspace=True,
        # Pre-flight briefing. Without a key it plays recordings (tests/fixtures/tavily) and
        # the screen shows 'recorded'.
        briefing=True,
    )
    runtime.start_background()

    server = JsonServer(int(os.getenv("PORT", "8000")))
    server.add("POST", "/proposals", lambda body, query: (200, runtime.file(body).to_dict()))
    # Aircraft processes introduce themselves (model, server), again every 30 s.
    server.add("POST", "/agents/register", lambda body, query: runtime.register_agent(body))
    server.add(
        "POST",
        "/approve",
        lambda body, query: _approval(runtime, body, allow=True),
    )
    server.add("POST", "/deny", lambda body, query: _approval(runtime, body, allow=False))
    server.add("GET", "/state", lambda body, query: (200, runtime.snapshot()))
    # Send the airspace revision along, so when a zone newly closes the operator refreshes its
    # copy and routes around it from the start; otherwise it files a straight line into the
    # closed zone and only finds out from the refusal.
    # The tick goes along too: a refiling that delays departure (depart_after_tick) must be
    # stated in the world's clock.
    server.add(
        "GET",
        "/telemetry/{asset}",
        lambda body, query, asset: (200, {**runtime.telemetry.get(asset, {}),
                                          "airspace_revision": runtime.airspace.revision,
                                          "tick": runtime.tick}
                                    if asset in runtime.telemetry else {}),
    )
    # Landing sites go along too; the operator computes its service area (the box model drafts
    # must not leave) from them. Data of the same kind as pad coordinates, unrelated to
    # judgement.
    server.add("GET", "/airspace", lambda body, query: (200, {
        "volumes": [v.to_dict() for v in runtime.airspace.all()],
        "pads": {n: {"lat": a[0], "lon": a[1]} for n, a in runtime.pad_coords.items()},
        "landing_areas": runtime.landing_areas,
    }))
    # ready means ready to judge (all airspace received); the compose healthcheck waits on it
    # before starting the aircraft. Alive (ok) and able to judge (ready) are different
    # questions, so both are here.
    server.add("GET", "/health", lambda body, query: (200, {
        "ok": True, "tick": runtime.tick, "ready": runtime.ready,
        "airspace_revision": runtime.airspace.revision}))
    # Intake input (demo, manual injection): queues one sentence in the inbox and returns.
    # The world thread does the reading.
    server.add("POST", "/intake", lambda body, query: runtime.submit_intake(body))
    # Run the pre-flight briefing again; the worker thread asks on the next poll. 503 when
    # off, 409 while one is running.
    server.add("POST", "/briefing/run", lambda body, query: runtime.briefing.request_run())
    # Ledger report. ?asset=<id> for one aircraft, ?format=md for a human-readable table.
    server.add("GET", "/ledger/report", lambda body, query: (
        200, runtime.report(query.get("asset") or None,
                            "md" if query.get("format") == "md" else "json")))
    print(f"runtime listening on :{os.getenv('PORT', '8000')}", flush=True)
    server.serve_forever()


def _approval(runtime: Runtime, body: dict, allow: bool):
    decision = runtime.approve(
        body.get("proposal_id", ""), body.get("actor", "controller"), allow=allow
    )
    if decision is None:
        return 404, {"error": "no such filing"}
    return 200, decision.to_dict()


if __name__ == "__main__":
    main()
