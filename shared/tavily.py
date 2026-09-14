"""The tower's eyes on the live city: search, extract, map, crawl, research. Stdlib only.

Tavily answers a question with pages (title, url, content), pulls the clean text out of a
page (extract), walks an official site (map/crawl), and runs a multi-step research task that
comes back as JSON in a schema we hand it (research). The runtime turns every page into an
intake item and reads it like any other text: the grammar first, then the Super tier, then
code validates. The client itself decides nothing — it fetches, counts what it spent and
reduces. When it cannot fetch it says so: a source that is quietly failing looks exactly like
a quiet day, and the tower must be able to tell the two apart.

Nothing here runs on the world thread, and every call is bounded twice: by a timeout, and by
the credit book. A round has a budget; when it is gone the next call does not leave the
process at all (BudgetExhausted) — an empty wallet is not an outage.
"""

import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API_ROOT = "https://api.tavily.com"
TAVILY_URL = f"{API_ROOT}/search"
# Pages per query. However many come back, a model reads them, and one model call takes tens
# of seconds.
MAX_RESULTS = 5
# Snippet length. The text goes to the model, and its start also shows in the on-screen source
# banner.
SNIPPET_CHARS = 400
# Length of the failure reason kept in the ledger and on screen.
ERROR_CHARS = 120
# Body text read per page. A notice fits in a few KB; taking a long page whole only bloats the
# model prompt and the sqlite rows.
CONTENT_CHARS = 8000
# Credits available per round. Tavily prices by call type (basic search 1, extract 1 per 5
# pages, map 1 per 10 pages, crawl = map + extract, research mini 4-110 per request). A call
# over the limit never leaves the process.
DEFAULT_BUDGET = 20.0
# Minimum credits reserved before starting a research call (mini's per-request floor). The
# actual cost is booked from the usage in the reply — so one research call can overrun the
# remaining budget, and later calls are then blocked. That is the honest answer we have to
# Tavily reporting the cost only afterwards.
RESEARCH_RESERVE = 4.0
RESEARCH_TIMEOUT_S = float(os.getenv("TAVILY_RESEARCH_TIMEOUT_S", "120"))
RESEARCH_POLL_S = 3.0
# Number of recorded calls kept (/state.briefing.calls).
KEPT_CALLS = 12


class SearchFailed(Exception):
    """One call failed — unreachable, refused (401 etc.), too slow, a broken reply, a bad URL."""


class BudgetExhausted(SearchFailed):
    """This round's credits are used up. Not a failure but a call not sent — it isn't written
    to the ledger as a source failure."""


@dataclass
class FetchStatus:
    """One cycle's result. Handed to the runtime with the items, and written to the ledger
    only when it flips between failure and recovery."""

    ok: bool
    error: str = ""
    calls: int = 0
    failures: int = 0
    credits: float = 0.0
    skipped: int = 0          # calls not sent for lack of budget

    def to_dict(self) -> dict:
        return {"ok": self.ok, "error": self.error or None, "calls": self.calls,
                "failures": self.failures, "credits": round(self.credits, 2),
                "skipped": self.skipped}


@dataclass
class Call:
    """One call and its cost. estimated means the reply carried no cost and we computed it."""

    op: str
    credits: float
    ok: bool
    estimated: bool = False
    detail: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"op": self.op, "credits": round(self.credits, 2), "ok": self.ok,
                "estimated": self.estimated, "detail": self.detail[:80], "at": self.at}


class CreditBook:
    """What search spent this round. The limit refills every round (new_round).

    Tavily puts the cost in the reply (include_usage). If it's missing, the cost is computed
    from the price list — booking an unknown cost as 0 would make the limit no limit.
    """

    def __init__(self, budget: float | None = None):
        self.budget = (float(os.getenv("TAVILY_BUDGET_PER_ROUND", str(DEFAULT_BUDGET)))
                       if budget is None else float(budget))
        self.used = 0.0          # spent this round
        self.total = 0.0         # spent since the process started
        self.blocked = 0         # calls not sent for lack of budget
        self.pending = 0.0       # estimate for calls sent but not yet priced
        self.calls: list[Call] = []
        self._lock = threading.Lock()
        # The share this thread has reserved; the same thread's next book() releases it — a
        # call runs start to finish on one thread (send → reply → price), one call per thread
        # at a time.
        self._held = threading.local()

    def allow(self, estimate: float) -> bool:
        """If the budget allows, reserve the estimate and return True.

        Only checking, and booking the cost after the reply, lets the briefing and the search
        poller (same book) both see the same remaining share and both go — measured: four calls
        went out on a limit of 3 and spent 4. Reserving blocks the later one.
        """
        amount = max(0.0, estimate)
        with self._lock:
            self._release_locked()     # drop this thread's earlier unpriced reservation
            if self.used + self.pending + amount > self.budget:
                return False
            self.pending += amount
            self._held.amount = amount
            return True

    def release(self) -> None:
        """Release this thread's reservation unpriced (the call died with an exception midway)."""
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        held = getattr(self._held, "amount", 0.0)
        if held:
            self.pending = max(0.0, self.pending - held)
            self._held.amount = 0.0

    def book(self, op: str, credits: float, ok: bool = True, estimated: bool = False,
             detail: str = "") -> None:
        with self._lock:
            self._release_locked()     # the actual cost replaces the reserved estimate
            self.used += max(0.0, credits)
            self.total += max(0.0, credits)
            self.calls.append(Call(op, credits, ok, estimated, detail))
            del self.calls[:-KEPT_CALLS]

    def block(self, op: str) -> None:
        with self._lock:
            self.blocked += 1
            self.calls.append(Call(op, 0.0, ok=False, detail="budget"))
            del self.calls[:-KEPT_CALLS]

    def new_round(self) -> None:
        with self._lock:
            self.used = 0.0
            self.blocked = 0

    @property
    def left(self) -> float:
        with self._lock:
            return max(0.0, self.budget - self.used - self.pending)

    def to_dict(self) -> dict:
        with self._lock:
            return {"used": round(self.used, 2), "total": round(self.total, 2),
                    "budget": self.budget, "blocked": self.blocked,
                    "calls": [call.to_dict() for call in self.calls]}


def _ceil_units(count: int, per_credit: int) -> float:
    """The price list's '1 credit per N pages'. Zero pages cost 0."""
    return float(math.ceil(count / per_credit)) if count > 0 else 0.0


class TavilyClient:
    recorded = False

    def __init__(self, api_key: str, base_url: str = TAVILY_URL, timeout_s: float = 15.0,
                 credits: CreditBook | None = None, record_dir: str | None = None):
        self.api_key = api_key
        self.base_url = base_url
        # The base URL minus /search is the root for the other endpoints (extract, map, crawl,
        # research).
        self.root = base_url[: -len("/search")] if base_url.endswith("/search") else base_url
        self.timeout_s = timeout_s
        self.research_timeout_s = RESEARCH_TIMEOUT_S
        self.credits = credits if credits is not None else CreditBook()
        self.record_dir = os.getenv("TAVILY_RECORD_DIR", "") if record_dir is None else record_dir
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self._recorded_files = 0
        self._books = threading.Lock()

    @classmethod
    def from_env(cls) -> "TavilyClient | None":
        """None without a key — the source is then off and records nothing."""
        key = os.getenv("TAVILY_API_KEY", "").strip()
        if not key:
            return None
        return cls(key, os.getenv("TAVILY_URL", TAVILY_URL),
                   float(os.getenv("TAVILY_TIMEOUT_S", "15")))

    # ---------- endpoints ----------

    def search(self, query: str, **options) -> list[dict]:
        """One query, reduced to intake items. SearchFailed if unreachable or too slow."""
        return reduce_results(query, self.search_raw(query, **options))

    def search_raw(self, query: str, topic: str = "general", time_range: str | None = None,
                   max_results: int = MAX_RESULTS, include_domains: list[str] | None = None,
                   search_depth: str = "basic") -> dict:
        """The raw reply. The briefing also weighs published_date and score when choosing."""
        payload = {"api_key": self.api_key, "query": query, "max_results": max_results,
                   "search_depth": search_depth, "include_answer": False, "include_usage": True}
        if topic and topic != "general":
            payload["topic"] = topic
        if time_range:
            payload["time_range"] = time_range
        if include_domains:
            payload["include_domains"] = list(include_domains)
        estimate = 2.0 if search_depth == "advanced" else 1.0
        body = self._call("search", self.base_url, payload, estimate, detail=query)
        self._settle("search", body, estimate, len(_results(body)), detail=query)
        return body

    def extract(self, urls: list[str], extract_depth: str = "basic", fmt: str = "text",
                query: str | None = None) -> dict:
        """Page body text. A search snippet (content) is only a few lines, so heights, radii
        and time windows get cut off."""
        wanted = [str(url) for url in urls if str(url).strip()]
        if not wanted:
            return {"results": [], "failed_results": []}
        payload = {"urls": wanted, "extract_depth": extract_depth, "format": fmt,
                   "include_usage": True}
        if query:
            payload["query"] = query
        factor = 2.0 if extract_depth == "advanced" else 1.0
        estimate = _ceil_units(len(wanted), 5) * factor
        body = self._call("extract", f"{self.root}/extract", payload, estimate,
                          detail=wanted[0])
        got = len(_results(body))
        self._settle("extract", body, _ceil_units(got, 5) * factor, got, detail=wanted[0])
        return body

    def map_site(self, url: str, instructions: str | None = None, max_depth: int = 1,
                 max_breadth: int = 20, limit: int = 20,
                 select_paths: list[str] | None = None) -> dict:
        """An official site's page list. Seeing what's there comes before choosing what to read."""
        payload = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth,
                   "limit": limit, "include_usage": True}
        if instructions:
            payload["instructions"] = instructions
        if select_paths:
            payload["select_paths"] = list(select_paths)
        factor = 2.0 if instructions else 1.0
        body = self._call("map", f"{self.root}/map", payload,
                          _ceil_units(limit, 10) * factor, detail=url)
        found = len(_results(body))
        self._settle("map", body, _ceil_units(found, 10) * factor, found, detail=url)
        return body

    def crawl_site(self, url: str, instructions: str | None = None, max_depth: int = 1,
                   max_breadth: int = 20, limit: int = 10, extract_depth: str = "basic",
                   fmt: str = "text", select_paths: list[str] | None = None) -> dict:
        """Walk an official site down to the page text. The FAA's TFR list and city agency
        notice pages come this way."""
        payload = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth,
                   "limit": limit, "extract_depth": extract_depth, "format": fmt,
                   "include_usage": True}
        if instructions:
            payload["instructions"] = instructions
        if select_paths:
            payload["select_paths"] = list(select_paths)
        walk = 2.0 if instructions else 1.0
        read = 2.0 if extract_depth == "advanced" else 1.0
        estimate = _ceil_units(limit, 10) * walk + _ceil_units(limit, 5) * read
        body = self._call("crawl", f"{self.root}/crawl", payload, estimate, detail=url)
        found = len(_results(body))
        self._settle("crawl", body,
                     _ceil_units(found, 10) * walk + _ceil_units(found, 5) * read, found,
                     detail=url)
        return body

    def research(self, question: str, output_schema: dict | None = None, model: str = "mini",
                 output_length: str = "short", include_domains: list[str] | None = None,
                 deadline_s: float | None = None, poll_s: float = RESEARCH_POLL_S) -> dict:
        """One self-driven, multi-step research task. The answer follows our schema.

        After creating it (201 pending), polls until done — call this only from a worker
        thread. The cost varies per request (mini 4-110) and arrives in the finished reply's
        usage. Only the floor is reserved up front, so one expensive task can overrun the
        remaining budget; if it does, the next call is blocked.
        """
        payload = {"input": question, "model": model, "stream": False,
                   "output_length": output_length}
        if output_schema:
            payload["output_schema"] = output_schema
        if include_domains:
            payload["include_domains"] = list(include_domains)
        body = self._call("research", f"{self.root}/research", payload, RESEARCH_RESERVE,
                          timeout_s=min(self.timeout_s * 2, 60.0), detail=question[:60])
        if body.get("status") == "completed" or body.get("content") is not None:
            self._settle("research", body, RESEARCH_RESERVE, 1, detail=question[:60])
            return body
        request_id = str(body.get("request_id") or "")
        if not request_id:
            self.credits.book("research", RESEARCH_RESERVE, ok=False, estimated=True)
            self._fail("research: no request_id in the reply")
        deadline = time.monotonic() + float(deadline_s or self.research_timeout_s)
        while time.monotonic() < deadline:
            time.sleep(max(0.05, poll_s))
            body = self._get(f"{self.root}/research/{urllib.parse.quote(request_id)}")
            status = str(body.get("status") or "")
            if status == "completed":
                self._settle("research", body, RESEARCH_RESERVE, 1, detail=question[:60])
                return body
            if status == "failed":
                self.credits.book("research", _usage(body) or 0.0, ok=False, estimated=False)
                self._fail("research failed")
        self.credits.book("research", RESEARCH_RESERVE, ok=False, estimated=True)
        self._fail("research timed out")
        return {}       # _fail always raises; this line is for the reader

    # ---------- one trip outside ----------

    def _call(self, op: str, url: str, payload: dict, estimate: float,
              timeout_s: float | None = None, detail: str = "") -> dict:
        """One call. The budget is checked first; only then does it leave the process.

        The Request is built inside the try too. A bad URL makes Request, not urlopen, raise
        ValueError, and if that escapes, the polling thread dies on its first call and the
        source stays 'on' but never asks again.
        """
        if not self.credits.allow(estimate):
            self.credits.block(op)
            raise BudgetExhausted(f"{op}: this round's {self.credits.budget:.0f} credits are spent")
        with self._books:
            self.calls += 1
        try:
            body = self._send(op, url, payload, timeout_s, detail)
            self._write_record(op, payload, body)
        except BaseException:
            self.credits.release()     # no reply to price; don't leak the reservation
            raise
        return body

    def _send(self, op: str, url: str, payload: dict, timeout_s: float | None,
              detail: str) -> dict:
        try:
            request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                             method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(request,
                                        timeout=timeout_s or self.timeout_s) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            self._spent_nothing(op, f"HTTP {error.code}", detail)
        except urllib.error.URLError as error:
            self._spent_nothing(op, f"unreachable: {error.reason}", detail)
        except (TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
            self._spent_nothing(op, f"{type(error).__name__}: {error}", detail)
        return {}

    def _get(self, url: str) -> dict:
        """Poll a research task's progress. Part of the same call, so no second budget check."""
        try:
            request = urllib.request.Request(url, method="GET")
            request.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            self._spent_nothing("research", f"HTTP {error.code}", url)
        except urllib.error.URLError as error:
            self._spent_nothing("research", f"unreachable: {error.reason}", url)
        except (TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
            self._spent_nothing("research", f"{type(error).__name__}: {error}", url)
        return {}

    def _settle(self, op: str, body: dict, fallback: float, got: int, detail: str = "") -> None:
        """What this call cost: the reply's usage first, else computed from the price list."""
        credits = _usage(body)
        self.credits.book(op, fallback if credits is None else credits, ok=True,
                          estimated=credits is None, detail=f"{detail} ({got})")

    def _spent_nothing(self, op: str, why: str, detail: str) -> None:
        """Refused, cut off or too slow. Nothing is charged, but the failure counts."""
        self.credits.book(op, 0.0, ok=False, detail=f"{detail}: {why}"[:80])
        self._fail(why)

    def _fail(self, why: str) -> None:
        with self._books:
            self.failures += 1
            self.last_error = why[:ERROR_CHARS]
        raise SearchFailed(self.last_error)

    def _write_record(self, op: str, payload: dict, body: dict) -> None:
        """With TAVILY_RECORD_DIR set, save real replies to files — raw material for fixtures.

        The key is left out. The files have the tests/fixtures/tavily shape, so a useful reply
        moved there as is gets replayed by recorded mode.
        """
        if not self.record_dir or not body:
            return
        with self._books:
            self._recorded_files += 1
            serial = self._recorded_files
        request = {key: value for key, value in payload.items() if key != "api_key"}
        record = {
            "fixture": {"kind": "recorded", "note": "real Tavily response recorded by skynet",
                        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "calls": [{"op": op, "request": request, "response": body}],
        }
        try:
            folder = Path(self.record_dir)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}-{os.getpid()}-{serial:04d}-{op}.json"
            (folder / name).write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except OSError as error:
            print(f"cannot write to TAVILY_RECORD_DIR: {error}", flush=True)


def _results(body: dict) -> list:
    found = body.get("results") if isinstance(body, dict) else None
    return found if isinstance(found, list) else []


def _usage(body: dict) -> float | None:
    """The cost the reply states. Some servers don't know include_usage, so None if absent."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None
    credits = usage.get("credits")
    return float(credits) if isinstance(credits, (int, float)) else None


def reduce_results(query: str, body: dict) -> list[dict]:
    """Reply to intake items. id is a hash of the url (or of the title without one) — the same
    page isn't read twice."""
    items = []
    for raw in _results(body):
        if not isinstance(raw, dict):
            continue
        title = " ".join(str(raw.get("title") or "").split())
        url = str(raw.get("url") or "")
        snippet = " ".join(str(raw.get("content") or "").split())[:SNIPPET_CHARS]
        if not title and not snippet:
            continue
        key = hashlib.sha1((url or title).encode("utf-8")).hexdigest()[:12]
        items.append({
            "id": f"tavily-{key}", "source": "tavily", "query": query, "title": title,
            "url": url, "snippet": snippet, "fetched_at": time.time(),
            "text": f"{title}. {snippet}".strip(". ") if title else snippet,
        })
    return items


# ---------- recorded Tavily ----------

FIXTURE_DIR = str(Path(__file__).resolve().parent.parent / "tests/fixtures/tavily")


def load_tavily_fixtures(folder: str | Path | None = None) -> list[dict]:
    """Fixture files as a call list. Hand-written scenes and recordings of real replies have
    the same shape."""
    root = Path(folder or os.getenv("TAVILY_FIXTURE_DIR") or FIXTURE_DIR)
    calls = []
    if not root.is_dir():
        return calls
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"tavily fixture {path.name}: {error}", flush=True)
            continue
        note = dict(payload.get("fixture") or {})
        note.setdefault("file", path.name)
        for call in payload.get("calls") or []:
            if not isinstance(call, dict) or not call.get("op"):
                continue
            match = dict(call.get("match") or {})
            if not match and isinstance(call.get("request"), dict):
                # A recorded real call; its request says what it answered.
                request = call["request"]
                if request.get("query"):
                    match = {"query": str(request["query"])}
                elif request.get("url"):
                    match = {"url": str(request["url"])}
            calls.append({"op": str(call["op"]), "match": match,
                          "response": call.get("response") or {}, "fixture": note})
    return calls


def _matches(match: dict, subject: str) -> bool:
    """Does this fixture answer this query (or URL)? With no conditions it answers anything."""
    lowered = subject.lower()
    exact = match.get("query") or match.get("url")
    if exact and str(exact).lower() == lowered:
        return True
    terms_all = [str(term).lower() for term in match.get("all") or []]
    terms_any = [str(term).lower() for term in match.get("any") or []]
    terms_none = [str(term).lower() for term in match.get("none") or []]
    if not terms_all and not terms_any and not terms_none:
        return not exact
    if any(term not in lowered for term in terms_all):
        return False
    if terms_any and not any(term in lowered for term in terms_any):
        return False
    return not any(term in lowered for term in terms_none)


class RecordedTavily:
    """Tavily without a key. Replies from tests/fixtures/tavily, shaped like the real thing.

    It exists so demos and tests run the same scenes without a key. Everything from here is
    marked recorded (screen, ledger, logs) and never passes itself off as a live answer.
    """

    recorded = True

    def __init__(self, folder: str | Path | None = None, credits: CreditBook | None = None):
        self.fixtures = load_tavily_fixtures(folder)
        self.credits = credits if credits is not None else CreditBook()
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.timeout_s = 0.0
        self.asked: list[tuple[str, str]] = []      # (op, what was asked) — tests check it

    # Recordings spend no credits: calls are counted, at a cost of 0.
    def _note(self, op: str, subject: str) -> None:
        self.calls += 1
        self.asked.append((op, subject))
        self.credits.book(op, 0.0, ok=True, detail=f"recorded: {subject}"[:80])

    def _pick(self, op: str, subject: str) -> list[dict]:
        return [call for call in self.fixtures
                if call["op"] == op and _matches(call["match"], subject)]

    def search(self, query: str, **options) -> list[dict]:
        return reduce_results(query, self.search_raw(query, **options))

    def search_raw(self, query: str, topic: str = "general", time_range: str | None = None,
                   max_results: int = MAX_RESULTS, include_domains: list[str] | None = None,
                   search_depth: str = "basic") -> dict:
        self._note("search", query)
        results, seen = [], set()
        for call in self._pick("search", query):
            for raw in _results(call["response"]):
                url = str(raw.get("url") or "")
                if url in seen:
                    continue
                seen.add(url)
                results.append({**raw, "fixture": call["fixture"]})
        return {"query": query, "results": results[:max_results], "usage": {"credits": 0}}

    def extract(self, urls: list[str], extract_depth: str = "basic", fmt: str = "text",
                query: str | None = None) -> dict:
        self._note("extract", ", ".join(urls)[:80])
        found, missing = [], []
        for url in urls:
            hit = None
            for call in self._pick("extract", url):
                for raw in _results(call["response"]):
                    if str(raw.get("url") or "") == url:
                        hit = {**raw, "fixture": call["fixture"]}
                        break
                if hit is not None:
                    break
            if hit is None:
                missing.append({"url": url, "error": "no fixture"})
            else:
                found.append(hit)
        return {"results": found, "failed_results": missing, "usage": {"credits": 0}}

    def map_site(self, url: str, instructions: str | None = None, **options) -> dict:
        self._note("map", url)
        results = []
        for call in self._pick("map", url):
            results += [raw for raw in _results(call["response"])]
        return {"base_url": url, "results": results, "usage": {"credits": 0}}

    def crawl_site(self, url: str, instructions: str | None = None, **options) -> dict:
        self._note("crawl", url)
        results = []
        for call in self._pick("crawl", url):
            results += [{**raw, "fixture": call["fixture"]} for raw in _results(call["response"])
                        if isinstance(raw, dict)]
        return {"base_url": url, "results": results, "usage": {"credits": 0}}

    def research(self, question: str, output_schema: dict | None = None, **options) -> dict:
        self._note("research", question[:80])
        for call in self._pick("research", question):
            body = dict(call["response"])
            body.setdefault("status", "completed")
            body["fixture"] = call["fixture"]
            return body
        return {"status": "completed", "content": {"hazards": []}, "sources": []}


class IntakePoller:
    """Each cycle, runs the query list and hands over the results — on its own thread, so the
    world thread never waits.

    deliver(items, status) — the status is handed over even with no items. Given only an empty
    list, the receiver couldn't tell "a quiet day" from "a day we couldn't reach".
    """

    def __init__(self, client: TavilyClient, queries: list[str], period_s: float, deliver):
        self.client = client
        self.queries = list(queries)
        self.period_s = period_s
        self.deliver = deliver
        self.fetches = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, daemon=True, name="intake-tavily")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            self._stop.wait(self.period_s)

    def fetch_once(self) -> list[dict]:
        found, errors, skipped = [], [], 0
        for query in self.queries:
            if self._stop.is_set():
                break
            try:
                found += self.client.search(query)
            except BudgetExhausted:
                # No budget isn't a dead source. Skip this cycle; try again next round.
                skipped += 1
            except SearchFailed as error:
                errors.append(str(error))
        self.fetches += 1
        status = FetchStatus(ok=not errors, error=errors[0] if errors else "",
                             calls=self.client.calls, failures=self.client.failures,
                             credits=self.client.credits.used, skipped=skipped)
        try:
            self.deliver(found, status)
        except Exception as error:  # noqa: BLE001 — a failed handover doesn't stop the next cycle
            print(f"intake deliver: {error!r}", flush=True)
        return found
