"""The tower: the one object the whole runtime is. Holds locks, limits, the arbiter,
the single commit path, the ledger.

Every attribute is born in the one constructor here and every mixin reaches it through
self. LOCK ORDER IS ALWAYS _judging THEN _guard, NEVER THE REVERSE.
"""

import json
import os
import threading
from pathlib import Path

from backend.adapters import build as build_adapter
from backend.intake.book import TowerIntake
from backend.intake.briefing import BriefingDesk
from backend.intake.desk import IntakeDeskMixin
from backend.intake.entries import IntakeEntriesMixin
from backend.intake.notice_flow import NoticeFlowMixin
from backend.intake.notices import NoticeBook
from backend.intake.rules import RulesMixin
from backend.intake.sources import SourcesMixin
from backend.intake.weather import WeatherMixin
from backend.runtime.advice import AdviceMixin
from backend.runtime.advisory import AdvisoryDesk
from backend.runtime.agents import AgentsMixin
from backend.runtime.arbiter import Arbiter
from backend.runtime.authority import AuthorityCheck
from backend.runtime.cards import CardsMixin
from backend.runtime.commit import Committer
from backend.runtime.commit_path import CommitPathMixin
from backend.runtime.intents import Intent, IntentRegistry, LinkWatch
from backend.runtime.judging import JudgingMixin
from backend.runtime.locks import LockTable
from backend.runtime.lostlink import LostLinkMixin
from backend.runtime.policy import PolicyBook
from backend.runtime.recall import RecallMixin
from backend.runtime.route_check import RouteCheckMixin
from backend.runtime.traffic import TrafficMixin
from backend.runtime.views import ViewsMixin
from backend.runtime.world import WorldMixin
from backend.store.intake_store import IntakeStore
from backend.store.ledger import Ledger
from shared import config as config_module
from shared.geo import Airspace
from shared.intake import Gazetteer
from shared.llm.client import TieredLlm
from shared.metar import MetarClient, MetarPoller
from shared.models import Decision, Proposal
from shared.notam import Clock
from shared.route import Router
from shared.tavily import FetchStatus, IntakePoller, TavilyClient

# The gazetteer intake uses to place things. Same file as the delivery addresses — the places an
# incident can "be" must be the same list as the places a delivery can go, and an address
# outside the list was invented by the model.
ADDRESS_FILE = os.getenv(
    "ADDRESS_FILE", str(Path(__file__).resolve().parent.parent.parent
                        / "configs/airspace/nyc_addresses.json"))


class Runtime(AdviceMixin, AgentsMixin, CardsMixin, CommitPathMixin, JudgingMixin,
              LostLinkMixin, RecallMixin, RouteCheckMixin, TrafficMixin, IntakeDeskMixin,
              IntakeEntriesMixin, NoticeFlowMixin, RulesMixin, SourcesMixin, ViewsMixin,
              WeatherMixin, WorldMixin):
    def __init__(self, config_path: str, sim_url: str, ledger_path: str, window_s: float = 1.5,
                 intake_db: str | None = None, metar: bool = False,
                 await_airspace: bool = False, briefing: bool = False):
        self.config = config_module.load(config_path)
        self.policies = PolicyBook(self.config.policies)
        self.authority = AuthorityCheck(self.config.authority, self.policies)
        self.locks = LockTable(self.config.resources)
        self.ledger = Ledger(ledger_path)
        self.llm = TieredLlm(models=vars(self.config.escalation))
        self.arbiter = Arbiter(self.llm)
        self.adapter = build_adapter(
            os.getenv("ADAPTER", "sim"), sim_url=sim_url, world="guarded",
            # Replies from the PX4 mirror (ADAPTER=composite) go to a line file next to the
            # ledger. The autopilot answers after the ledger line has closed, and another line
            # under the same id would make the UI and the report count one decision twice.
            # AUTOPILOT_LOG moves the file.
            journal_path=os.getenv("AUTOPILOT_LOG")
            or str(Path(ledger_path).with_name("autopilot.jsonl")),
        )
        self.committer = Committer(self.adapter, self.locks, self.ledger, self.authority)
        self.committer.on_committed = self._on_committed

        self.sim_url = sim_url
        self.pad_coords: dict[str, tuple[float, float]] = {}
        self.landing_areas: list[dict] = []   # delivery landing sites, passed to operators as is
        self.window_s = window_s
        self.tick = 0
        self.telemetry: dict = {}
        self.airspace = Airspace()
        self.router = Router(self.airspace)
        # A runtime that fetches its airspace from the simulator (the service) does not judge
        # until the fetch is complete. Under compose, aircraft filed before the runtime was up
        # and four routes were cleared against an empty airspace (version 0); judging against a
        # half-loaded one (version 14480) let the guarded wiring produce 19 restricted-airspace
        # incursions within 3 minutes. An empty airspace means "not known yet", not "no rules".
        # A runtime built in code (tests, harness) loads its airspace by hand and does not wait
        # — only the service (main) turns this on.
        self.await_airspace = await_airspace
        self.airspace_loaded = False
        self.zone_volumes: set[str] = set()   # zones from notices; removed when they end
        # Declared performance and the round's clock. Where and when a cleared route will be
        # (the intent) and NOTAM time windows are computed from these.
        self.performance = self.config.performance
        self.clock = Clock(self.performance.clock_epoch_z, self.performance.seconds_per_tick)
        self.intents = IntentRegistry()
        # Telemetry heartbeat. If an airborne aircraft's record goes unrefreshed for the declared
        # timeout_ticks, that is lost link — its intent (cleared route + landing column) stays
        # reserved and a human card goes up.
        self.links = LinkWatch(self.performance.lost_link.timeout_ticks)
        # Intents whose reservation was extended during lost link (for the conformance check
        # after recovery).
        self._dark: dict[str, Intent] = {}
        self._link_cards: dict[str, str] = {}    # aircraft → standing lost-link card (filing id)
        # Lost-link aircraft whose reservation a human released. No column (presence) goes up
        # at the spot where their telemetry froze, either.
        self._released: set[str] = set()
        # The world thread advances the heartbeat; the UI (HTTP thread) reads it.
        self._link_lock = threading.Lock()
        # What each aircraft process announced about itself (what writes its filings). A UI
        # label only; judgement ignores it.
        self.agents: dict[str, dict] = {}
        self.notices = NoticeBook(self.clock, self.llm)
        # Intake (weather, incidents, restrictions). Simulator notices, Tavily searches and manual
        # input enter the same book and pass grammar → model → code checks; weather becomes a
        # policy (takeoff halt), incidents become notices (zones).
        self.gazetteer = Gazetteer(_load_addresses(ADDRESS_FILE),
                                   lookup=lambda bid: getattr(self.airspace.get(bid), "polygon",
                                                              None))
        self.intake = TowerIntake(self.clock, self.llm, self.config.weather, self.gazetteer)
        # Record of what came in (sqlite). With no path it lives in memory, so tests don't share
        # what they have "seen". For the service, main() passes INTAKE_DB (default
        # .run/intake.sqlite).
        self.store = IntakeStore(intake_db)
        self._rule_ids: dict[str, int] = {}     # item id (the rule's basis) → rules table row
        self.tavily = TavilyClient.from_env()
        # METAR. Runs without a key. Only the service (main) turns it on — a plain instance
        # (tests) has it off and never touches the real network. None (source off) when
        # METAR=off or there are no stations.
        self.metar = MetarClient.from_env(self.config.intake.metar_stations) if metar else None
        self.metar_poller: MetarPoller | None = None
        self._metar_fetch: FetchStatus | None = None
        # starting (never fetched yet) | on | off. One line when it becomes unreachable (off),
        # one when it answers again (on) — written only on change. Writing every cycle would
        # fill the ledger with failures.
        self.metar_status = "starting" if self.metar is not None else "off"
        self.metar_fetch: dict | None = None
        self.metar_last_fetch_tick: int | None = None
        # Last observations received. Re-fed into the new round when the round changes
        # (_follow_round).
        self._metar_current: list[dict] = []
        self.intake_poller: IntakePoller | None = None
        self.intake_async = True
        self._reading_intake: set[str] = set()
        self._read_intake: list[tuple] = []
        self._intake_inbox: list[dict] = []     # left by search/manual input; world thread reads
        # Items that were waiting for a human before the restart. The cards died with the
        # process, so they are read again and the cards go back up — otherwise a report nobody
        # ever answered stays marked "seen" and is never read again.
        waiting = self.store.reopen_waiting()
        # Pre-flight briefing (Tavily). Only the service (main) turns it on — a runtime built in
        # code (tests, harness) has it off, like METAR. What the briefing already read is taken
        # here: waiting cards go back up (without re-reading), and rules that were in force are
        # read back from the record and applied as-is in the next round.
        self.briefing = BriefingDesk(self, config_path, enabled=briefing)
        self._intake_inbox.extend(self.briefing.adopt_waiting(waiting))
        self._intake_fetch: FetchStatus | None = None   # search thread's last cycle status
        # Runtime advisories. Counts consecutive refusals, checks the code-built options through
        # judgement and records them in the ledger. When a model writes the wording it runs on
        # its own thread — a refusal reply that waited on the model would stall the operator.
        self.advisor = AdvisoryDesk(self.llm)
        self.advisory_async = True
        # Model readings of notices outside the grammar also run off the world thread (tests
        # set this False and read inline).
        self.notice_async = True
        self._reading: set[str] = set()       # notice ids the model is reading
        self._read_notices: list[tuple] = []  # (round, notice, result) left by the reader thread
        # Both the world thread and the approval (HTTP) thread apply notices.
        self._notice_lock = threading.Lock()
        self._round = None                    # follows the simulator when it starts a new round
        self._contended: dict[str, list[tuple[Proposal, Decision, float]]] = {}
        self._awaiting_human: dict[str, Proposal] = {}
        # Human card (awaiting approval) filing id → its open ledger entry. Closed by the human's
        # answer, the end of the window or the end of the round.
        self._open_cards: dict[str, object] = {}
        self._decisions: dict[str, Decision] = {}
        self._checks: dict[str, list[str]] = {}   # filing id → names of the checks run so far
        # Counted on the world's clock, not the wall clock, so runs are reproducible.
        self._recent_commits: dict[tuple[str, str], int] = {}
        self.dedupe_ticks = int(os.getenv("DEDUPE_TICKS", "15"))
        self._guard = threading.Lock()
        # One at a time from judgement to intent registration. Each HTTP handler thread runs
        # file() on its own, so two filings 0.2 s apart both passed the traffic check before
        # either intent was registered (_on_committed) and both were cleared — in a live run
        # (rules mode) two recalled aircraft re-filed the same A* corridor, giving two losses of
        # separation on the runtime side. Lock order is always _judging → _guard (nothing takes
        # this while holding _guard). The world thread (recalls, notices) does not take it — the
        # clock must not stall behind a slow autopilot command, and an airborne aircraft left
        # without an intent mid-recall is covered by the presence column _others builds from
        # telemetry.
        self._judging = threading.RLock()

    def _context(self, proposal: Proposal | None, checks: list[str] | None = None,
                 intent_id: str | None = None) -> dict:
        """Judgement context for a ledger entry.

        The tick at the time, airspace version, policies in force and the order of checks.
        """
        if checks is None:
            checks = self._checks.get(proposal.id, []) if proposal is not None else []
        active = [p.id for p in self.policies.all()
                  if p.active_from_tick <= self.tick
                  and (p.active_until_tick is None or self.tick <= p.active_until_tick)]
        return {"tick": self.tick, "airspace_revision": self.airspace.revision,
                "policies": active, "intent_id": intent_id, "checks_run": list(checks)}


def _load_addresses(path: str) -> list[dict]:
    """Addresses from the gazetteer. No file means an empty list, and incidents that give an
    address are then unreadable."""
    try:
        return list(json.loads(Path(path).read_text(encoding="utf-8")).get("addresses") or [])
    except (OSError, ValueError, AttributeError):
        return []

