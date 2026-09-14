"""Pre-flight briefing: Tavily finds, the grammar reads, code judges, a person loosens.

No network anywhere. A small HTTP server stands in for the live Tavily API (search, extract,
map, crawl, research); the recorded fixtures in tests/fixtures/tavily serve the demo scenes.
What is checked here is what the owner asked for: trust by domain, the credit cap, no page read
twice across a restart, a crane that refuses a route through it and passes one 50 m above, an
event circle that pulls back a cleared corridor, a closed park that refuses landing, recorded
versus live labelling, a refused key ledgered once, and a world thread that never waits.
"""

import datetime
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from backend.intake.briefing import (
    CLOSED_CEILING_M,
    RECORDED_PREFIX,
    cell_of,
    domain_of,
    trusted_domain,
)
from backend.runtime.tower import Runtime
from drone.agent.planner import OperatorPlanner
from shared.intake import (
    Gazetteer,
    UnknownPlace,
    eastern_offset_hours,
    from_briefing_form,
    hazard_problems,
    read_hazard,
    read_window,
)
from shared.models import Proposal
from shared.tavily import (
    BudgetExhausted,
    CreditBook,
    IntakePoller,
    RecordedTavily,
    SearchFailed,
    TavilyClient,
    load_tavily_fixtures,
)
from sim import world as sim_world
from tests.fixture_llm import FixtureLlm

CONFIG = "configs/fleet.yaml"
DAY = datetime.date(2026, 9, 22)          # the day the recordings were made (fixture as_of)
BBOX = (40.669, -74.037, 40.836, -73.917)
DEPOT = (40.7019, -73.9700)
ST_NICHOLAS = (40.8155, -73.949)
CRANE_AT = (40.799327, -73.968752)        # 2701 Broadway in the gazetteer
TAVILY_ENV = ("TAVILY_API_KEY", "TAVILY_RECORDED", "TAVILY_BUDGET_PER_ROUND",
              "TAVILY_RECORD_DIR", "TAVILY_FIXTURE_DIR", "BRIEFING_DATE", "TAVILY_URL")
FIXTURES = "tests/fixtures/tavily"


def gazetteer() -> Gazetteer:
    with open("configs/airspace/nyc_addresses.json", encoding="utf-8") as handle:
        return Gazetteer(json.load(handle)["addresses"])


def fixture_text(name: str, op: str = "extract") -> str:
    with open(f"{FIXTURES}/{name}", encoding="utf-8") as handle:
        payload = json.load(handle)
    call = next(c for c in payload["calls"] if c["op"] == op)
    first = call["response"]["results"][0]
    return first.get("raw_content") or first.get("content")


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


def fleet(**extra) -> dict:
    """Two grounded aircraft. First drop-offs match seed 7: north Central Park, Morningside."""
    return {
        "drone-01": {"lat": DEPOT[0], "lon": DEPOT[1], "alt_m": 0.0,
                     "job_lat": 40.7985, "job_lon": -73.955},
        "drone-03": {"lat": DEPOT[0], "lon": DEPOT[1] + 0.0004, "alt_m": 0.0,
                     "job_lat": 40.804, "job_lon": -73.95824},
        **extra,
    }


def briefed_runtime(db: str | None = None, tavily=None, llm=None, telemetry=None,
                    tick: int = 10):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0, intake_db=db,
                          briefing=True)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.notice_async = False
    runtime.intake_async = False
    runtime.advisory_async = False
    runtime.briefing.run_async = False
    runtime.tavily = tavily
    if llm is not None:
        runtime.llm = llm
        runtime.notices.llm = llm
        runtime.intake.llm = llm
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.tick = tick
    runtime.telemetry = fleet() if telemetry is None else telemetry
    return runtime, adapter


def ledger_lines(runtime) -> list[dict]:
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [entry for entry in (json.loads(line) for line in handle)
                if entry["outcome"] != "pending"]


def codes(runtime) -> list[str]:
    return [entry["decision"]["code"] for entry in ledger_lines(runtime)]


def items_by_kind(runtime) -> dict:
    return {item["kind"]: item for item in runtime.snapshot()["briefing"]["items"]}


def route(asset, points, alt_m=100.0):
    return Proposal(asset_id=asset, action="fly_route", cost_usd=12.0, blast_radius="schedule",
                    rationale="", params={"legs": [{"lat": lat, "lon": lon, "alt_m": alt_m}
                                                   for lat, lon in points]})


def pending(runtime, action="publish_notice"):
    return [p for p in runtime.snapshot()["awaiting_human"] if p["action"] == action]


class QuietEnv(unittest.TestCase):
    """Keeps the developer shell's keys out of the tests. Each test clears the Tavily environment
    and sets only what it needs."""

    env: dict = {}

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in TAVILY_ENV:
            os.environ.pop(key, None)
        os.environ.update(self.env)


# ---------- Fake Tavily (stand-in for the live API) ----------

class FakeTavilyApi(http.server.BaseHTTPRequestHandler):
    """/search /extract /map /crawl /research and GET /research/<id>. Answers live in class
    attributes."""

    status = 200
    delay_s = 0.0
    seen: list = []
    search_results: list = []
    pages: dict = {}
    crawl_results: list = []
    map_results: list = []
    research_content: dict = {"hazards": []}
    usage: dict = {}
    polls = 0

    @classmethod
    def reset(cls):
        cls.status, cls.delay_s, cls.seen, cls.polls = 200, 0.0, [], 0
        cls.search_results, cls.pages, cls.crawl_results, cls.map_results = [], {}, [], []
        cls.research_content = {"hazards": []}
        cls.usage = {}

    def _send(self, status: int, payload: dict | None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _usage(self, op: str) -> dict:
        credits = type(self).usage.get(op)
        return {} if credits is None else {"usage": {"credits": credits}}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        path = self.path.split("?")[0]
        cls = type(self)
        cls.seen.append((path, body, self.headers.get("Authorization")))
        time.sleep(cls.delay_s)
        if cls.status != 200:
            self._send(cls.status, None)
            return
        if path.endswith("/search"):
            self._send(200, {"query": body.get("query"), "results": cls.search_results,
                             **self._usage("search")})
        elif path.endswith("/extract"):
            found = [{"url": url, "raw_content": cls.pages[url]} for url in body.get("urls", [])
                     if url in cls.pages]
            missing = [{"url": url, "error": "not found"} for url in body.get("urls", [])
                       if url not in cls.pages]
            self._send(200, {"results": found, "failed_results": missing,
                             **self._usage("extract")})
        elif path.endswith("/map"):
            self._send(200, {"base_url": body.get("url"), "results": cls.map_results,
                             **self._usage("map")})
        elif path.endswith("/crawl"):
            self._send(200, {"base_url": body.get("url"), "results": cls.crawl_results,
                             **self._usage("crawl")})
        elif path.endswith("/research"):
            self._send(201, {"request_id": "r-1", "status": "pending", "input": body.get("input")})
        else:
            self._send(404, {"error": "no route"})

    def do_GET(self):
        cls = type(self)
        cls.polls += 1
        cls.seen.append((self.path, {}, self.headers.get("Authorization")))
        if cls.polls < 2:
            self._send(202, {"request_id": "r-1", "status": "in_progress"})
            return
        self._send(200, {"request_id": "r-1", "status": "completed",
                         "content": cls.research_content, "sources": [],
                         **self._usage("research")})

    def log_message(self, *args):
        pass


def start_fake():
    FakeTavilyApi.reset()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTavilyApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/search"


class FakeServerCase(QuietEnv):
    env = {"BRIEFING_DATE": DAY.isoformat()}

    def setUp(self):
        super().setUp()
        self.server, self.url = start_fake()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def client(self, budget: float = 100.0, record_dir: str = "") -> TavilyClient:
        client = TavilyClient("tvly-test", self.url, timeout_s=2.0, credits=CreditBook(budget),
                              record_dir=record_dir)
        client.research_timeout_s = 5.0
        return client

    def posts(self, suffix: str = "") -> list:
        return [seen for seen in FakeTavilyApi.seen
                if seen[0].endswith(suffix) and not seen[0].startswith("/research/")]


# ---------- Client ----------

class ClientTest(FakeServerCase):
    def test_each_endpoint_sends_its_shape_and_books_what_it_cost(self):
        FakeTavilyApi.search_results = [{"title": "a", "url": "https://www.nyc.gov/a",
                                         "content": "x"}]
        FakeTavilyApi.pages = {"https://www.nyc.gov/a": "page"}
        FakeTavilyApi.map_results = [f"https://tfr.faa.gov/{n}" for n in range(12)]
        FakeTavilyApi.crawl_results = [{"url": "https://tfr.faa.gov/1", "raw_content": "tfr"}]
        FakeTavilyApi.usage = {"search": 1, "extract": 1, "crawl": 3, "research": 16}
        client = self.client()
        client.search_raw("crane near Broadway", topic="news", time_range="week")
        client.extract(["https://www.nyc.gov/a", "https://www.nyc.gov/missing"])
        client.map_site("https://tfr.faa.gov/", limit=20)
        client.crawl_site("https://tfr.faa.gov/", instructions="New York TFRs", limit=6)
        answer = client.research("hazards", {"type": "object", "properties": {}}, poll_s=0.01)
        self.assertEqual(answer["status"], "completed")
        # map returned no usage — it is counted at 1 per 10 pages (12 pages → 2).
        self.assertEqual(client.credits.used, 1 + 1 + 2 + 3 + 16)
        estimated = [call for call in client.credits.calls if call.estimated]
        self.assertEqual([call.op for call in estimated], ["map"])
        search = self.posts("/search")[0]
        self.assertEqual((search[1]["topic"], search[1]["time_range"], search[1]["include_usage"],
                          search[2]), ("news", "week", True, "Bearer tvly-test"))
        self.assertEqual(self.posts("/extract")[0][1]["urls"],
                         ["https://www.nyc.gov/a", "https://www.nyc.gov/missing"])
        self.assertEqual(self.posts("/crawl")[0][1]["instructions"], "New York TFRs")
        research = self.posts("/research")[0][1]
        self.assertEqual((research["model"], research["stream"]), ("mini", False))
        self.assertIn("output_schema", research)
        self.assertEqual(FakeTavilyApi.polls, 2, "polled GET /research/<id> until it finished")

    def test_an_empty_wallet_stops_the_call_before_it_leaves_the_process(self):
        FakeTavilyApi.usage = {"search": 1}
        client = self.client(budget=3)
        for n in range(3):
            client.search_raw(f"q{n}")
        with self.assertRaises(BudgetExhausted):
            client.search_raw("q3")
        self.assertEqual(len(self.posts("/search")), 3, "no query went out past the cap")
        self.assertEqual((client.credits.used, client.credits.blocked), (3.0, 1))
        client.credits.new_round()
        client.search_raw("q4")
        self.assertEqual(len(self.posts("/search")), 4, "a new round refills it")

    def test_two_threads_sharing_one_wallet_cannot_overspend_it(self):
        """The briefing and the search poller share one credit book. Checking the balance and
        booking the cost only once the answer came back let both see the same remaining share and
        go out together (four calls on a cap of 3, 4 credits). The share is reserved before a
        call goes out."""
        FakeTavilyApi.usage = {"search": 1}
        FakeTavilyApi.delay_s = 0.3
        client = self.client(budget=3)

        def spend():
            for n in range(3):
                try:
                    client.search_raw(f"q{n}")
                except BudgetExhausted:
                    pass

        threads = [threading.Thread(target=spend) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(self.posts("/search")), 3, "no query went out past the cap")
        self.assertEqual((client.credits.used, client.credits.pending, client.credits.blocked),
                         (3.0, 0.0, 3))

    def test_a_refused_key_is_a_failure_with_its_code_and_costs_nothing(self):
        FakeTavilyApi.status = 401
        client = self.client()
        with self.assertRaises(SearchFailed) as raised:
            client.search_raw("q")
        self.assertNotIsInstance(raised.exception, BudgetExhausted)
        self.assertEqual((str(raised.exception), client.failures, client.credits.used),
                         ("HTTP 401", 1, 0.0))

    def test_live_answers_are_recorded_without_the_key_and_replay_as_fixtures(self):
        FakeTavilyApi.search_results = [{"title": "Crane notice", "url": "https://www.nyc.gov/c",
                                         "content": "tower crane"}]
        with tempfile.TemporaryDirectory() as folder:
            client = self.client(record_dir=folder)
            client.search_raw("tower crane permit near Broadway Manhattan")
            files = os.listdir(folder)
            self.assertEqual(len(files), 1)
            with open(os.path.join(folder, files[0]), encoding="utf-8") as handle:
                saved = handle.read()
            self.assertNotIn("tvly-test", saved, "the key is not written to the recording")
            replay = RecordedTavily(folder)
            body = replay.search_raw("tower crane permit near Broadway Manhattan")
            self.assertEqual([r["url"] for r in body["results"]], ["https://www.nyc.gov/c"])
            self.assertEqual(replay.search_raw("something else")["results"], [])

    def test_the_intake_poller_does_not_call_an_empty_wallet_a_failed_source(self):
        runtime, _ = briefed_runtime()
        client = self.client(budget=0)
        runtime.tavily = client
        poller = IntakePoller(client, ["q1", "q2"], period_s=60.0, deliver=runtime.take_in)
        status = []
        poller.deliver = lambda items, fetched: status.append(fetched)
        poller.fetch_once()
        self.assertEqual((status[0].ok, status[0].skipped), (True, 2))
        self.assertEqual(self.posts("/search"), [])


# ---------- Grammar ----------

class GrammarTest(unittest.TestCase):
    def setUp(self):
        self.gazetteer = gazetteer()
        self.areas = sim_world.LANDING_AREAS

    def read(self, name, op="extract"):
        return read_hazard(fixture_text(name, op), self.gazetteer, self.areas, DAY)

    def test_the_four_rule_kinds_and_an_advisory_read_from_their_pages(self):
        crane = self.read("dob-crane-2701-broadway.json")
        self.assertEqual((crane.kind, crane.address, crane.centre), ("crane", "2701 Broadway",
                                                                     CRANE_AT))
        self.assertAlmostEqual(crane.height_m, 230 * 0.3048, places=2)
        tfr = self.read("faa-tfr-unga.json")
        self.assertEqual((tfr.kind, tfr.radius_m), ("restriction", 1852.0))
        self.assertAlmostEqual(tfr.centre[0], 40.75778, places=4)
        self.assertAlmostEqual(tfr.centre[1], -73.975, places=4)
        self.assertAlmostEqual(tfr.ceiling_m, 3000 * 0.3048, places=1)
        self.assertEqual((tfr.window.start.isoformat(), tfr.window.end.isoformat()),
                         ("2026-09-21T10:00:00+00:00", "2026-09-26T04:00:00+00:00"))
        closure = self.read("nycparks-st-nicholas-closure.json")
        self.assertEqual((closure.kind, closure.landing_area), ("closure", "la-stnicholas"))
        self.assertEqual((closure.window.start.hour, closure.window.end.hour), (9, 16),
                         "05:00~12:00 EDT is 09:00~16:00Z")
        rally = self.read("news-union-square-rally.json", op="search")
        self.assertEqual((rally.kind, rally.place, rally.radius_m),
                         ("event", "Union Square", 300.0))
        advisory = self.read("nws-wind-advisory.json")
        self.assertEqual(advisory.kind, "weather")
        self.assertIsNone(advisory.rule_kind, "a weather advisory is information, not a rule")
        self.assertAlmostEqual(advisory.numbers["gust_mps"], 45 * 0.44704, places=1)

    def test_irrelevant_pages_are_read_as_nothing(self):
        self.assertIsNone(self.read("irrelevant-rooftop-bars.json", op="search"))
        self.assertIsNone(self.read("irrelevant-yankees.json", op="search"))

    def test_code_refuses_numbers_places_and_windows_that_do_not_add_up(self):
        low = read_hazard("A tower crane at 2701 Broadway will reach a height of 5 feet. "
                          "September 22, 2026.", self.gazetteer, self.areas, DAY)
        self.assertIn("crane height outside 10~400 m", hazard_problems(low, BBOX))
        tall = read_hazard("A tower crane at 2701 Broadway will reach a height of 1500 feet.",
                           self.gazetteer, self.areas, DAY)
        self.assertTrue(hazard_problems(tall, BBOX))
        nowhere = read_hazard("A tower crane at 99999 Imaginary Street rises 200 feet tall.",
                              self.gazetteer, self.areas, DAY)
        self.assertIsNone(nowhere, "an address missing from the gazetteer is not a place")
        not_ours = read_hazard("Central Park will be closed on September 22, 2026 from 5 a.m. "
                               "to 9 a.m.", self.gazetteer, self.areas, DAY)
        self.assertIsNone(not_ours, "closing a park that is not our landing site is not a rule")
        wide = read_hazard("Parade at Union Square Park within 9000 meters radius of the "
                           "stage, September 22 from 5 a.m. to 7 a.m.", self.gazetteer,
                           self.areas, DAY)
        self.assertEqual(wide.radius_m, 300.0, "an out-of-range radius falls back to the default")
        far = read_hazard("Temporary flight restriction: Radius: 1 nautical miles. Latitude: "
                          "41.2000, Longitude: -73.9000. September 22, 2026 at 0900 UTC to "
                          "September 22, 2026 at 1100 UTC", self.gazetteer, self.areas, DAY)
        self.assertIn("a place outside the service area", hazard_problems(far, BBOX))

    def test_new_york_time_follows_daylight_saving(self):
        self.assertEqual(eastern_offset_hours(DAY), -4)
        self.assertEqual(eastern_offset_hours(datetime.date(2026, 12, 1)), -5)
        window = read_window("closed from 5:00 a.m. to 12:00 p.m.", DAY)
        self.assertEqual((window.start.hour, window.end.hour), (9, 16))
        winter = read_window("closed from 5:00 a.m. to 12:00 p.m.", datetime.date(2026, 12, 1))
        self.assertEqual((winter.start.hour, winter.end.hour), (10, 17))
        self.assertIsNone(read_window("no times here at all", DAY))

    def test_a_model_form_is_placed_by_the_gazetteer_not_by_the_model(self):
        with self.assertRaises(UnknownPlace):
            from_briefing_form({"kind": "crane", "address": "1 Imaginary Plaza", "height_ft": 200},
                               self.gazetteer, self.areas, DAY)
        with self.assertRaises(UnknownPlace):
            from_briefing_form({"kind": "closure", "park": "Prospect Park"}, self.gazetteer,
                               self.areas, DAY)
        closure = from_briefing_form({"kind": "closure", "park": "Tompkins Square Park",
                                      "start": "2026-09-22T05:00", "end": "2026-09-22T09:00",
                                      "timezone": "local"}, self.gazetteer, self.areas, DAY)
        self.assertEqual((closure.landing_area, closure.window.start.hour),
                         ("la-tompkins", 9))


# ---------- Trust ----------

class TrustTest(QuietEnv):
    def test_trust_is_by_the_exact_domain_or_below_it(self):
        trusted = ("faa.gov", "weather.gov", "nyc.gov", "nycgovparks.org")
        for url in ("https://tfr.faa.gov/x", "https://forecast.weather.gov/x",
                    "https://www.nyc.gov/site/fdny/x", "https://www.nycgovparks.org/x",
                    "https://nyc.gov/x"):
            self.assertTrue(trusted_domain(url, trusted), url)
        for url in ("https://evilnyc.gov/x", "https://nyc.gov.example.com/x",
                    "https://www.nycgovparks.org.evil.test/x", "https://eastvillage.example/x",
                    "not a url", ""):
            self.assertFalse(trusted_domain(url, trusted), url)
        self.assertEqual(domain_of("https://WWW.NYC.GOV/a?b=1"), "www.nyc.gov")

    def test_official_grammar_readings_apply_at_once_and_the_rest_wait_for_a_person(self):
        runtime, _ = briefed_runtime()
        runtime.absorb([])
        kinds = items_by_kind(runtime)
        self.assertEqual({kind: item["status"] for kind, item in kinds.items()},
                         {"restriction": "applied", "closure": "applied", "event": "held",
                          "weather": "info"})
        self.assertEqual((kinds["restriction"]["domain"], kinds["closure"]["domain"],
                          kinds["event"]["domain"]),
                         ("tfr.faa.gov", "www.nycgovparks.org", "eastvillage-bulletin.example"))
        self.assertEqual({kinds[k]["trust"] for k in ("restriction", "closure")}, {"official"})
        self.assertEqual(kinds["event"]["trust"], "unofficial")
        # The official rules went into the airspace; the unofficial event stands only as a card.
        self.assertIsNotNone(runtime.airspace.get(kinds["restriction"]["id"]))
        self.assertIsNotNone(runtime.airspace.get(kinds["closure"]["id"]))
        self.assertIsNone(runtime.airspace.get(kinds["event"]["id"]))
        cards = pending(runtime)
        self.assertEqual([card["params"]["notice_id"] for card in cards], [kinds["event"]["id"]])
        self.assertEqual(cards[0]["params"]["notice"]["citation"]["domain"],
                         "eastvillage-bulletin.example")
        self.assertIn("is not an official source", runtime._decisions[cards[0]["id"]].reason)
        self.assertEqual(runtime.snapshot()["briefing"]["ignored"], 2, "the two irrelevant pages")

    def test_every_rule_carries_its_source_into_the_ledger_the_store_and_the_state(self):
        runtime, _ = briefed_runtime()
        runtime.absorb([])
        rules = [e for e in ledger_lines(runtime) if e["decision"]["code"] == "briefing_rule"]
        self.assertEqual(len(rules), 3)
        for entry in rules:
            detail = entry["decision"]["detail"]
            for key in ("source_url", "title", "domain", "fetched_at", "read_by"):
                self.assertTrue(detail.get(key), (key, detail))
        stored = {row["id"]: row for row in runtime.store.briefed("brief-")}
        for item in runtime.snapshot()["briefing"]["items"]:
            citation = stored[item["id"]]["hints"]["briefing"]["citation"]
            self.assertEqual(citation["source_url"], item["url"])
        tfr = next(n for n in runtime.snapshot()["notices"] if n["kind"] == "restriction")
        self.assertEqual(tfr["citation"]["domain"], "tfr.faa.gov")

    def test_a_model_reading_waits_for_a_person_even_from_an_official_domain(self):
        os.environ["BRIEFING_DATE"] = DAY.isoformat()
        server, url = start_fake()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        page = "https://www.nycgovparks.org/parks/tompkins-square-park/alerts"
        FakeTavilyApi.search_results = [{"title": "Tompkins Square Park alert", "url": page,
                                         "content": "Heads up for Tuesday morning."}]
        FakeTavilyApi.pages = {page: "Lawn restoration: expect a closure of the lawn and plaza "
                                     "at Tompkins Square Park early Tuesday, before sunrise, "
                                     "while crews reseed."}
        llm = FixtureLlm([{"tier": "super", "needle": "Lawn restoration",
                           "text": json.dumps({"kind": "closure", "park": "Tompkins Square Park",
                                               "start": "2026-09-22T05:00",
                                               "end": "2026-09-22T09:00",
                                               "timezone": "local"})}])
        client = TavilyClient("tvly-test", url, timeout_s=2.0, credits=CreditBook(100))
        runtime, _ = briefed_runtime(tavily=client, llm=llm)
        runtime.briefing.settings.research = False
        runtime.absorb([])
        item = items_by_kind(runtime)["closure"]
        self.assertEqual((item["status"], item["trust"], item["read_by"]),
                         ("held", "official", "model:nemotron-3-nano"))
        self.assertIsNone(runtime.airspace.get(item["id"]), "the model puts nothing in force")
        self.assertEqual(len(pending(runtime)), 1)


# ---------- What the rules do in judgement ----------

class RuleTest(FakeServerCase):
    def crane_runtime(self):
        FakeTavilyApi.search_results = [
            {"title": "Crane Notice - 2701 Broadway", "url": "https://www.nyc.gov/crane",
             "content": "A tower crane at 2701 Broadway."}]
        FakeTavilyApi.pages = {"https://www.nyc.gov/crane":
                               fixture_text("dob-crane-2701-broadway.json")}
        runtime, adapter = briefed_runtime(tavily=self.client())
        runtime.briefing.settings.crawl = ()
        runtime.briefing.settings.research = False
        runtime.absorb([])
        return runtime, adapter

    def test_a_crane_refuses_a_route_through_it_and_passes_one_fifty_metres_above(self):
        runtime, _ = self.crane_runtime()
        crane = items_by_kind(runtime)["crane"]
        volume = runtime.airspace.get(crane["id"])
        self.assertEqual((volume.ceiling_m, volume.clearance_m), (round(230 * 0.3048, 3), 50.0))
        west, east = (CRANE_AT[0], CRANE_AT[1] - 0.008), (CRANE_AT[0], CRANE_AT[1] + 0.008)
        through = route("drone-09", [west, east], alt_m=100.0)
        self.assertIn("breaks the rules", runtime.check_route(through))
        self.assertEqual(through.params["blocked_volume"], crane["id"])
        above = route("drone-09", [west, east], alt_m=121.0)   # above 70 m rooftop + 50 m margin
        self.assertIsNone(runtime.check_route(above))

    def test_a_closed_park_refuses_landing_but_not_takeoff_or_overflight(self):
        runtime, _ = briefed_runtime()
        runtime.absorb([])
        closure = items_by_kind(runtime)["closure"]
        volume = runtime.airspace.get(closure["id"])
        self.assertEqual((volume.floor_m, volume.ceiling_m), (0.0, CLOSED_CEILING_M))
        landing = route("drone-09", [(40.80, -73.96), ST_NICHOLAS], alt_m=60.0)
        self.assertIn("cannot touch down", runtime.check_route(landing))
        self.assertEqual(landing.params["blocked_kind"], "landing")
        takeoff = route("drone-09", [ST_NICHOLAS, (40.80, -73.96)], alt_m=60.0)
        self.assertIsNone(runtime.check_route(takeoff), "leaving a closed park is not blocked")
        over = route("drone-09", [(40.825, -73.945), (40.805, -73.953)], alt_m=80.0)
        self.assertIsNone(runtime.check_route(over), "nor is flying over it")

    def test_the_operator_planner_declines_a_closed_park_without_any_change(self):
        """The aircraft-side planner loads the /airspace volumes as they are and applies the
        landing check to the destination."""
        runtime, _ = briefed_runtime()
        runtime.absorb([])
        planner = OperatorPlanner()
        planner.load([volume.to_dict() for volume in runtime.airspace.all()])
        self.assertIsNone(planner.draw((40.80, -73.96), ST_NICHOLAS))
        self.assertIsNotNone(planner.draw((40.80, -73.96), (40.804, -73.95824)))

    def test_a_drone_flying_to_a_park_that_closes_is_pulled_back(self):
        flying = {"drone-02": {"lat": 40.805, "lon": -73.955, "alt_m": 80.0,
                               "route": [{"lat": ST_NICHOLAS[0], "lon": ST_NICHOLAS[1],
                                          "alt_m": 80.0}]}}
        runtime, adapter = briefed_runtime(telemetry=fleet(**flying))
        runtime.absorb([])
        runtime.absorb([])
        self.assertIn(("drone-02", "divert_ground"), [(a, action) for a, action, _ in adapter.sent])
        recalled = [e for e in ledger_lines(runtime) if e["decision"]["code"] == "recalled"]
        self.assertEqual(recalled[0]["decision"]["policy_hit"], items_by_kind(runtime)
                         ["closure"]["id"])

    def test_an_event_circle_a_person_confirms_pulls_back_a_cleared_corridor(self):
        flying = {"drone-02": {"lat": 40.7300, "lon": -73.9950, "alt_m": 80.0,
                               "route": [{"lat": 40.7420, "lon": -73.9850, "alt_m": 80.0}]}}
        runtime, adapter = briefed_runtime(telemetry=fleet(**flying), tick=2300)
        runtime.absorb([])
        self.assertEqual(adapter.sent, [], "nothing is blocked before a person has looked")
        card = pending(runtime)[0]
        runtime.approve(card["id"], "controller", allow=True)
        self.assertIn(("drone-02", "divert_ground"), [(a, action) for a, action, _ in adapter.sent])
        runtime.absorb([])
        event = items_by_kind(runtime)["event"]
        self.assertEqual(event["status"], "approved")
        refused = route("drone-09", [(40.7300, -73.9950), (40.7420, -73.9850)], alt_m=80.0)
        self.assertIn("breaks the rules", runtime.check_route(refused))


# ---------- Recorded and live answers ----------

class SourceTest(FakeServerCase):
    def test_without_a_key_the_briefing_runs_recorded_and_says_so_everywhere(self):
        runtime, _ = briefed_runtime()
        self.assertIsNone(runtime.tavily)
        runtime.absorb([])
        state = runtime.snapshot()["briefing"]
        self.assertEqual((state["source"], state["day"], state["credits_used"]),
                         ("recorded", "2026-09-22", 0.0))
        self.assertTrue(state["items"])
        self.assertTrue(all(item["recorded"] for item in state["items"]))
        self.assertTrue(all(item["id"].startswith(RECORDED_PREFIX) for item in state["items"]))
        run = next(e for e in ledger_lines(runtime) if e["decision"]["code"] == "briefing_run")
        self.assertEqual(run["decision"]["detail"]["source"], "recorded")
        stored = runtime.store.briefed("brief-")
        self.assertTrue(all(row["hints"]["briefing"]["citation"]["recorded"] for row in stored))
        self.assertIn("(recorded)", state["summary"])

    def test_with_a_key_the_briefing_is_live_and_says_so(self):
        tfr_url = "https://tfr.faa.gov/tfr3/?page=detail_6_0922"
        FakeTavilyApi.search_results = [{"title": "FDC 6/0922", "url": tfr_url,
                                         "content": "VIP TFR"}]
        FakeTavilyApi.pages = {tfr_url: fixture_text("faa-tfr-unga.json")}
        runtime, _ = briefed_runtime(tavily=self.client())
        runtime.absorb([])
        state = runtime.snapshot()["briefing"]
        self.assertEqual(state["source"], "live")
        item = items_by_kind(runtime)["restriction"]
        self.assertEqual((item["recorded"], item["status"]), (False, "applied"))
        self.assertFalse(item["id"].startswith(RECORDED_PREFIX))
        self.assertGreater(state["credits_used"], 0)

    def test_a_junk_key_is_ledgered_once_the_briefing_falls_back_and_recovery_is_one_line(self):
        FakeTavilyApi.status = 401
        runtime, _ = briefed_runtime(tavily=self.client())
        runtime.absorb([])
        state = runtime.snapshot()["briefing"]
        self.assertEqual(state["source"], "recorded")
        self.assertIn("HTTP 401", state["fallback"]["why"])
        self.assertTrue(state["items"], "the recording took over the scene")
        self.assertTrue(all(item["recorded"] for item in state["items"]))
        for _ in range(2):
            runtime.briefing.request_run()
            runtime.tick += 1
            runtime.absorb([])
        failed = [e for e in ledger_lines(runtime) if e["decision"]["code"] ==
                  "intake_source_failed"]
        self.assertEqual(len(failed), 1, "one line per failure, when it changes")
        self.assertEqual(failed[0]["decision"]["detail"]["source"], "briefing")
        FakeTavilyApi.status = 200
        runtime.briefing.request_run()
        runtime.tick += 1
        runtime.absorb([])
        self.assertEqual(codes(runtime).count("intake_source_recovered"), 1)
        self.assertEqual(runtime.snapshot()["briefing"]["source"], "live")

    def test_recorded_off_without_a_key_turns_the_briefing_off(self):
        os.environ["TAVILY_RECORDED"] = "0"
        runtime, _ = briefed_runtime()
        runtime.absorb([])
        self.assertEqual(runtime.snapshot()["briefing"]["source"], "off")
        self.assertEqual([c for c in codes(runtime) if c.startswith("briefing")], [])
        self.assertEqual(runtime.briefing.request_run()[0], 503)

    def test_a_briefing_that_is_not_switched_on_does_nothing(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
        runtime.landing_areas = sim_world.LANDING_AREAS
        runtime.absorb([])
        self.assertEqual(runtime.snapshot()["briefing"]["source"], "off")
        self.assertEqual(codes(runtime), [])


# ---------- Budget ----------

class BudgetTest(FakeServerCase):
    def test_the_briefing_never_spends_past_the_round_budget(self):
        FakeTavilyApi.usage = {"search": 1, "extract": 1, "crawl": 2, "research": 30}
        FakeTavilyApi.search_results = [{"title": "x", "url": "https://www.nyc.gov/x",
                                         "content": "nothing here"}]
        FakeTavilyApi.pages = {"https://www.nyc.gov/x": "nothing here"}
        client = self.client(budget=5)
        runtime, _ = briefed_runtime(tavily=client)
        runtime.absorb([])
        state = runtime.snapshot()["briefing"]
        self.assertLessEqual(state["credits_used"], state["budget"])
        self.assertEqual(state["budget"], 5.0)
        self.assertGreater(client.credits.blocked, 0, "the remaining calls were not sent")
        paid = [call for call in client.credits.calls if call.ok and call.op != "research"]
        self.assertEqual(len(self.posts()), len(paid), "calls sent = calls booked")
        self.assertEqual(self.posts("/research"), [], "research never started for lack of budget")
        # The next round refills it.
        runtime._round = 2
        runtime.absorb([])
        self.assertGreater(len(self.posts()), len(paid))


# ---------- Restart ----------

class RestartTest(QuietEnv):
    def setUp(self):
        super().setUp()
        folder = tempfile.mkdtemp()
        self.db = os.path.join(folder, "intake.sqlite")

    def test_a_restart_reads_no_page_twice_and_keeps_the_rules_it_had(self):
        first, _ = briefed_runtime(db=self.db)
        first.absorb([])
        before = {k: v["id"] for k, v in items_by_kind(first).items()}
        self.assertTrue(any(op == "extract" for op, _ in first.briefing.recorded_client().asked))
        second, _ = briefed_runtime(db=self.db)
        second.absorb([])
        asked = second.briefing.recorded_client().asked
        self.assertEqual([what for op, what in asked if op == "extract"], [],
                         "pages already read were not fetched again")
        self.assertEqual([c for c in codes(second) if c == "briefing_item"], [],
                         "nothing was read again")
        for kind in ("restriction", "closure"):
            self.assertIsNotNone(second.airspace.get(before[kind]),
                                 f"the {kind} rule stayed in force across the restart")

    def test_a_waiting_card_comes_back_once_and_not_after_a_person_answers_it(self):
        first, _ = briefed_runtime(db=self.db)
        first.absorb([])
        event_id = items_by_kind(first)["event"]["id"]
        self.assertEqual([c["params"]["notice_id"] for c in pending(first)], [event_id])
        second, _ = briefed_runtime(db=self.db)
        second.absorb([])
        cards = pending(second)
        self.assertEqual([c["params"]["notice_id"] for c in cards], [event_id],
                         "no card was lost, and only one came back")
        second.approve(cards[0]["id"], "controller", allow=True)
        second.absorb([])
        self.assertEqual(items_by_kind(second)["event"]["status"], "approved")
        third, _ = briefed_runtime(db=self.db, tick=2300)
        third.absorb([])
        self.assertEqual(pending(third), [], "a card a person answered does not come back")
        self.assertEqual(items_by_kind(third)["event"]["status"], "approved")
        self.assertIsNotNone(third.airspace.get(event_id),
                             "a rule a person confirmed stays in force")


# ---------- World thread ----------

class WorldThreadTest(FakeServerCase):
    def test_the_world_thread_never_waits_for_tavily(self):
        FakeTavilyApi.delay_s = 0.6
        FakeTavilyApi.search_results = [{"title": "x", "url": "https://example.test/x",
                                         "content": "nothing"}]
        runtime, _ = briefed_runtime(tavily=self.client())
        runtime.briefing.run_async = True
        runtime.briefing.settings.crawl = ()
        runtime.briefing.settings.research = False
        runtime.briefing.settings.max_queries_per_run = 2
        slowest = 0.0
        for _ in range(15):
            runtime.tick += 1
            started = time.monotonic()
            runtime.absorb([])
            slowest = max(slowest, time.monotonic() - started)
            time.sleep(0.02)
        self.assertLess(slowest, 0.2, f"absorb waited on Tavily ({slowest:.2f}s)")
        deadline = time.monotonic() + 10.0
        while runtime.snapshot()["briefing"]["runs"] == 0 and time.monotonic() < deadline:
            runtime.tick += 1
            runtime.absorb([])
            time.sleep(0.05)
        state = runtime.snapshot()["briefing"]
        self.assertEqual((state["runs"], state["source"], state["running"]), (1, "live", False))
        self.assertEqual(len(self.posts("/search")), 2)


# ---------- Corridor ----------

class CorridorTest(QuietEnv):
    def test_a_cleared_corridor_asks_about_new_neighbourhoods_once_per_round(self):
        start = (40.7890, -73.9760)
        telemetry = fleet(**{"drone-05": {"lat": start[0], "lon": start[1], "alt_m": 0.0}})
        runtime, _ = briefed_runtime(telemetry=telemetry)
        runtime.absorb([])
        self.assertNotIn("crane", items_by_kind(runtime),
                         "that neighbourhood was not asked about at the start of the round")
        corridor = [start, CRANE_AT, (40.8090, -73.9630)]
        decision = runtime.file(route("drone-05", corridor, alt_m=121.0).to_dict())
        self.assertEqual(decision.verdict.value, "auto", decision.reason)
        self.assertIn(cell_of(*CRANE_AT, 1.0), runtime.briefing.pending_cells)
        for _ in range(3):
            runtime.tick += 13
            runtime.absorb([])
        crane = items_by_kind(runtime)["crane"]
        self.assertEqual((crane["status"], crane["place"]), ("applied", "2701 Broadway"))
        runs = [e["decision"]["detail"]["trigger"] for e in ledger_lines(runtime)
                if e["decision"]["code"] == "briefing_run"]
        self.assertEqual(runs[0], "round")
        self.assertIn("corridor", runs)
        # A corridor through the same cells is not asked about again this round.
        runtime.briefing.corridor_cleared([{"lat": p[0], "lon": p[1]} for p in corridor])
        self.assertEqual(runtime.briefing.pending_cells, {})
        # A new round asks about the neighbourhood again.
        runtime._round = 2
        runtime.absorb([])
        runtime.briefing.corridor_cleared([{"lat": p[0], "lon": p[1]} for p in corridor])
        self.assertIn(cell_of(*CRANE_AT, 1.0), runtime.briefing.pending_cells)


# ---------- Screen ----------

class StateTest(QuietEnv):
    def test_state_briefing_has_the_documented_shape_and_post_run_queues_one(self):
        runtime, _ = briefed_runtime()
        runtime.absorb([])
        state = runtime.snapshot()["briefing"]
        for key in ("last_run_tick", "credits_used", "budget", "source", "summary", "items"):
            self.assertIn(key, state)
        for item in state["items"]:
            self.assertTrue({"id", "kind", "place", "summary", "url", "domain", "trust",
                             "rule_id", "until_tick"} <= set(item))
        self.assertEqual(state["runs"], 1)
        status, body = runtime.briefing.request_run()
        self.assertEqual((status, body["queued"]), (200, True))
        runtime.tick += 1
        runtime.absorb([])
        self.assertEqual(runtime.snapshot()["briefing"]["runs"], 2)
        self.assertEqual(runtime.snapshot()["briefing"]["last_trigger"], "manual")

    def test_a_model_summary_that_names_a_domain_it_was_not_given_is_thrown_away(self):
        llm = FixtureLlm([{"tier": "super", "needle": "two sentences",
                           "text": "A crane and a TFR are active per reuters.com. Fly safe."}])
        runtime, _ = briefed_runtime(llm=llm)
        runtime.absorb([])
        state = runtime.snapshot()["briefing"]
        self.assertEqual(state["summary_by"], "template")
        good = FixtureLlm([{"tier": "super", "needle": "two sentences",
                            "text": "An FAA TFR from tfr.faa.gov covers Midtown East and "
                                    "St. Nicholas Park is closed per www.nycgovparks.org. "
                                    "A march at Union Square waits for a person."}])
        runtime, _ = briefed_runtime(llm=good)
        runtime.absorb([])
        self.assertEqual(runtime.snapshot()["briefing"]["summary_by"], "model:nemotron-3-nano")


class FixtureShapeTest(unittest.TestCase):
    def test_every_fixture_says_it_is_a_fixture_and_has_tavilys_shape(self):
        calls = load_tavily_fixtures(FIXTURES)
        self.assertGreaterEqual(len(calls), 12)
        for call in calls:
            note = call["fixture"]
            self.assertEqual(note.get("kind"), "hand-written", note)
            self.assertIn("Not a real notice", note.get("note", ""))
            self.assertIn(call["op"], ("search", "extract", "crawl", "map", "research"))
            if call["op"] != "research":
                self.assertIsInstance(call["response"].get("results"), list)


# ---------- Harness: the guarded wiring stays all zero with the briefing on ----------

class HarnessWithBriefingTest(unittest.TestCase):
    """One seed-7 round (first 3200 ticks) with the recorded briefing on. Rules only tighten, so
    violations must be 0. The direct wiring does not know the runtime, so this test leaves it
    alone."""

    TICKS = 3200

    @classmethod
    def setUpClass(cls):
        from tests import test_two_worlds as harness

        cls._env = mock.patch.dict(os.environ, {}, clear=False)
        cls._env.start()
        for key in TAVILY_ENV:
            os.environ.pop(key, None)
        original = harness.Runtime
        cls.runtimes = []

        def with_briefing(*args, **kwargs):
            runtime = original(*args, briefing=True, **kwargs)
            runtime.briefing.run_async = False
            cls.runtimes.append(runtime)
            return runtime

        harness.Runtime = with_briefing
        try:
            with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
                cls.ledger_path = handle.name
            cls.guarded, cls.direct, cls.trace = harness.run(cls.ledger_path, ticks=cls.TICKS)
        finally:
            harness.Runtime = original

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()

    def test_guarded_stays_all_zero_with_the_briefing_on(self):
        for key in ("airspace_violations", "ceiling_breaches", "zone_incursions",
                    "separation_losses", "site_conflicts", "pad_conflicts", "unrecorded_actions",
                    "incident_incursions", "weather_hold_takeoffs", "post_recall_violations"):
            self.assertEqual(self.guarded[key], 0, key)
        self.assertGreater(self.guarded["deliveries"], 0, "the fleet still delivers")

    def test_the_briefing_ran_and_its_rules_were_in_the_judge(self):
        with open(self.ledger_path, encoding="utf-8") as handle:
            entries = [json.loads(line) for line in handle]
        done = [e for e in entries if e["outcome"] != "pending"]
        rules = [e for e in done if e["decision"]["code"] == "briefing_rule"]
        kinds = {e["decision"]["detail"]["kind"] for e in rules}
        self.assertTrue({"restriction", "closure"} <= kinds, kinds)
        runs = [e["decision"]["detail"]["trigger"] for e in done
                if e["decision"]["code"] == "briefing_run"]
        self.assertEqual(runs[0], "round")
        self.assertIn("corridor", runs, "an approved corridor asked about a new neighbourhood")
        state = self.runtimes[0].snapshot()["briefing"]
        self.assertEqual(state["source"], "recorded")


if __name__ == "__main__":
    unittest.main()
