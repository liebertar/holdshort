"""The world thread: pulling telemetry in, loading the airspace, following the round.

Sole writer of tick, telemetry, airspace_loaded, pad_coords and landing_areas, and of the
round reset. The only module on the world-pull path that imports get_json: it is the patch
target in tests/test_locks_and_arbiter.py, and a second importer here would make that patch
silently miss.
"""

import threading
import time

from backend.intake.book import INTAKE_PERIOD_S
from backend.intake.sources import METAR_PERIOD_S
from shared.geo import Volume
from shared.http import get_json
from shared.metar import MetarPoller
from shared.tavily import IntakePoller

# The single startup request that fetches the airspace (some 30,000 volumes) from the
# simulator. The world thread has nothing to do without it, so it waits generously — a short
# timeout re-downloads the large response from scratch every time.
AIRSPACE_FETCH_TIMEOUT_S = 30.0
# Service-area box margin (about 2 km). A model-structured notice outside it was invented.
SERVICE_MARGIN_DEG = 0.02


class WorldMixin:
    def _pull_world(self) -> None:
        state = self.adapter.telemetry()
        if state:
            self.tick = state.get("tick", self.tick)
            self.telemetry = state.get("assets", {})
            self._follow_round(state.get("round"))
            self._observe()
            self.watch_links()

        if not self.airspace_loaded:
            self._load_airspace()

        bulletins = get_json(f"{self.sim_url}/bulletins?world=guarded") or {}
        self.absorb(bulletins.get("bulletins", []))

    def _load_airspace(self) -> None:
        """Fetch the simulator's airspace in one go. Load it all at once, then start judging.

        If the fetch fails or comes back empty, try again on the next poll. When volumes were
        added one by one, HTTP threads judged against a half-filled airspace (Airspace.add_all
        loads with a single swap).
        """
        world = get_json(f"{self.sim_url}/state?world=guarded&volumes=1",
                         timeout=AIRSPACE_FETCH_TIMEOUT_S) or {}
        volumes = [Volume.from_dict(raw) for raw in world.get("volumes") or []]
        if not volumes:
            return
        self.pad_coords = {name: (at["lat"], at["lon"])
                           for name, at in (world.get("pad_coords") or {}).items()}
        self.landing_areas = list(world.get("landing_areas") or [])
        self.airspace.add_all(volumes)
        self.airspace_loaded = True

    def _follow_round(self, round_number) -> None:
        """On a new round, clear the per-round state — budget, locks, dedupe, intents, notices.

        The ledger stays. It is history, and a new round does not undo it.
        Notice policies are cleared too; reposted under the same id, they apply again then.
        """
        if round_number is None or round_number == self._round:
            return
        self._round = round_number
        self.authority.new_round()
        for asset_id in list(self.telemetry):
            self.locks.release_all(asset_id)
        self._recent_commits.clear()
        for volume_id in self.zone_volumes:
            self.airspace.remove(volume_id)
        self.zone_volumes.clear()
        self.policies.clear()
        self.intents.clear()
        # Links are per-round too — the new round's aircraft are freshly placed and their
        # heartbeat stamps count from scratch. Standing lost-link cards come down with the other
        # cards in _expire_cards below.
        with self._link_lock:
            self.links.clear()
        self._dark.clear()
        self._link_cards.clear()
        self._released.clear()
        # Aircraft registrations survive the round (the processes keep running); only their
        # ticks move onto the new round's clock.
        with self._guard:
            for row in self.agents.values():
                row["last_seen_tick"] = min(int(row["last_seen_tick"]), self.tick)
        self._expire_cards("the round changed")
        self.notices.clear()
        # An open weather hold gets a closing line. Without one, the report lists that hold as
        # "open" forever.
        if self.intake.hold is not None:
            self._ledger_hold_end(self.intake.hold, "weather_hold_closed",
                                  f"closed by the round change (window ran to tick "
                                  f"{self.intake.hold.until_tick})")
        self._close_rules("round")
        self.intake.clear()
        # METAR is the observation in force now. A new round does not stop the gusts, so the
        # last observation goes into the new round's first poll — waiting for the next cycle (up
        # to METAR_PERIOD_S later) would let the new round's aircraft take off into the gusts.
        with self._guard:
            self._intake_inbox.extend(dict(item) for item in self._metar_current)
        with self._guard:
            self.advisor.clear()

    def service_bbox(self) -> tuple[float, float, float, float] | None:
        """Box around landing sites and pads + margin. Model-structured notices must lie inside."""
        points = [(a["lat"], a["lon"]) for a in self.landing_areas] + list(self.pad_coords.values())
        if not points:
            return None
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        return (min(lats) - SERVICE_MARGIN_DEG, min(lons) - SERVICE_MARGIN_DEG,
                max(lats) + SERVICE_MARGIN_DEG, max(lons) + SERVICE_MARGIN_DEG)

    def background(self) -> None:
        """Pulls the world in. Never on the arbitration thread (see settle_forever below)."""
        while True:
            # A failure is retried next cycle. If this thread dies, the runtime keeps judging
            # against a stale world, the worst state there is: silently wrong.
            try:
                self._pull_world()
            except Exception as error:  # noqa: BLE001 — staying alive comes first
                print(f"runtime background: {error!r}", flush=True)
            time.sleep(0.25)

    def settle_forever(self) -> None:
        """A thread that only arbitrates resources, so a slow model never stops the world clock.

        With arbitration on the world-update thread, ticks, positions and notices go 3 s stale
        while Ultra thinks for 3 s, and filings arriving meanwhile are judged at old positions.
        Arbitration waits window_s before acting anyway, so running it apart delays nothing but
        arbitration.
        """
        while True:
            try:
                self._settle_contended()
            except Exception as error:  # noqa: BLE001
                print(f"runtime arbiter: {error!r}", flush=True)
            time.sleep(0.25)

    def start_background(self) -> list[threading.Thread]:
        threads = [threading.Thread(target=self.background, daemon=True, name="world"),
                   threading.Thread(target=self.settle_forever, daemon=True, name="arbiter")]
        for thread in threads:
            thread.start()
        # Search only with a key, on its own thread. Results go into the inbox for the world
        # thread to read on its next poll; the world thread never waits on the network.
        if self.tavily is not None and self.intake_poller is None:
            self.intake_poller = IntakePoller(self.tavily, self.config.intake.queries,
                                              INTAKE_PERIOD_S, self.take_in)
            threads.append(self.intake_poller.start())
        # METAR on its own thread too. It runs without a key; when unreachable, the source
        # gets one 'off' line.
        if self.metar is not None and self.metar_poller is None:
            self.metar_poller = MetarPoller(self.metar, METAR_PERIOD_S, self.take_metar)
            threads.append(self.metar_poller.start())
        return threads
