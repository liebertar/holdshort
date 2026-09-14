"""The pre-flight briefing: what is happening today at the places this fleet is flying to.

Structured feeds tell the tower about airspace. They do not tell it that a tower crane went up
on Broadway last week, that a park it lands in is closed this morning, or that the FAA put a
VIP restriction over the East Side for the General Assembly. Those live in prose on official
pages, so the tower reads them: at the start of a round, and again whenever a corridor it has
just cleared enters a neighbourhood nobody has asked about yet (one question per ~1 km cell
per round).

Nothing here decides anything. Tavily finds and fetches; the grammar in core/intake reads;
the Super tier only structures what the grammar cannot; code validates every number against
the tower's own gazetteer and ranges; and only then does a rule exist. A rule from an official
domain that the grammar read applies at once, because it can only tighten. Everything else —
another domain, or anything a model read — waits on the approval screen for a person. Every
rule carries where it came from: url, title, domain, when it was fetched, who read it.

The work happens on its own thread: the world thread hands over a plan and picks up the result
at a later poll. When there is no key (or the key is refused) the same scenes come from
recorded fixtures, and everything says "recorded" — on screen, in the ledger, in the store.
"""

import datetime
import hashlib
import math
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

import yaml

from backend.intake.notices import NoticeRecord
from shared.config import plural
from shared.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON, Volume
from shared.intake import (
    BRIEFING_SYSTEM,
    EVIDENCE_CHARS,
    Hazard,
    Window,
    from_briefing_form,
    hazard_problems,
    read_hazard,
    utc_to_eastern,
    worth_a_model,
)
from shared.intake import UnknownPlace as UnknownBriefingPlace
from shared.llm.client import LlmTier, parse_json_object
from shared.models import Decision, Proposal, Verdict
from shared.notam import circle
from shared.tavily import (
    CONTENT_CHARS,
    BudgetExhausted,
    FetchStatus,
    RecordedTavily,
    SearchFailed,
)

# Asset name on the briefing's ledger lines: the work is the runtime's own, not an aircraft's.
BRIEFING_ASSET = "briefing"
BRIEFING_CHECKS = ["briefing:grammar", "briefing:model", "briefing:validate"]
# Item id prefixes. Recorded scenes get their own, so that once a key is added a recording can't
# pass for a live answer.
LIVE_PREFIX = "brief-"
RECORDED_PREFIX = "brief-rec-"
# Ceiling of a closure zone. Being below the ground, it catches no altitude (routes and take-off
# columns pass as before) and trips only the landing check (Airspace.landing_breach, which looks
# only at ground-level forbidden zones around the spot). Landing in a closed park is blocked;
# taking off from one is not.
CLOSED_CEILING_M = -1.0
# Radius of the column used to recall aircraft bound for a closed landing site. It catches only
# routes that would land on that spot.
CLOSURE_COLUMN_M = 10.0
# Pages fetched in full per run (extract costs 1 credit per 5 pages).
EXTRACT_MAX = 5
# Length and sentence count of the summary a person reads.
SUMMARY_CHARS = 400
SUMMARY_SENTENCES = 2
# Seconds the model gets to structure one page; on the worker thread, so unrelated to ticks.
BRIEFING_TIMEOUT_S = float(os.getenv("BRIEFING_TIMEOUT_S", "60"))
KEPT_ITEMS = 40

DEFAULT_TRUSTED = ("faa.gov", "weather.gov", "noaa.gov", "nyc.gov", "nycgovparks.org",
                   "cityofnewyork.us", "mta.info", "parks.ny.gov", "ny.gov")
DEFAULT_QUERIES = {
    "round": [
        "FAA temporary flight restriction New York City drone {date}",
        "National Weather Service New York City wind advisory {date}",
    ],
    "seasonal": {
        9: ["United Nations General Assembly {year} flight restrictions East Side Manhattan"],
    },
    "park": ["{park} closure {date}"],
    "street": ["tower crane permit near {street} {borough}"],
}
# The borough of each landing site. Without a borough in the query, cranes from some other city
# come back.
BOROUGH = {"la-bbp": "Brooklyn", "la-mccarren": "Brooklyn", "la-bushwick": "Brooklyn",
           "la-hunters": "Queens", "la-gantry": "Queens", "la-governors": "New York"}
DEFAULT_BOROUGH = "Manhattan"

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "hazards": {
            "type": "array",
            "description": "Hazards to low-altitude drone flight, one object each",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "description": "crane, event, closure, restriction or weather"},
                    "place": {"type": "string", "description": "where, in plain words"},
                    "address": {"type": "string", "description": "street address for a crane"},
                    "park": {"type": "string", "description": "park name for a closure"},
                    "venue": {"type": "string", "description": "venue for an event"},
                    "lat": {"type": "number", "description": "centre latitude of a restriction"},
                    "lon": {"type": "number", "description": "centre longitude of a restriction"},
                    "radius_m": {"type": "number", "description": "radius in metres"},
                    "height_ft": {"type": "number", "description": "crane height in feet"},
                    "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                    "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                    "timezone": {"type": "string", "description": "UTC or local"},
                    "summary": {"type": "string", "description": "one short sentence"},
                    "source_url": {"type": "string", "description": "the page this came from"},
                },
            },
        },
    },
    "required": ["hazards"],
}
SUMMARY_SYSTEM = (
    "You write two sentences for a drone tower's pre-flight briefing screen. Use only the "
    "hazards and the source domains listed in the message; never add advice, a hazard or a "
    "domain that is not listed. Plain text, no markup, at most 60 words."
)


class _Unset:
    def __init__(self, label: str):
        self.label = label

    def __repr__(self) -> str:
        return f"<{self.label}>"


# A desk that has not seen a round yet, and a reading never applied. Both must differ from None:
# the round number is None in the tests and the harness.
_UNSET = _Unset("no round yet")
NEVER = _Unset("never applied")


# ---------- Settings ----------

@dataclass
class BriefingSettings:
    """The briefing section of configs/fleet.yaml. Without one, the defaults below apply."""

    trusted_domains: tuple = DEFAULT_TRUSTED
    cell_km: float = 1.0
    max_cells_per_run: int = 3
    corridor_debounce_ticks: int = 12
    corridor_min_gap_ticks: int = 50
    closure_radius_m: float = 60.0
    crane_radius_m: float = 30.0
    crane_clearance_m: float = 50.0
    max_window_ticks: int = 6000
    extract_max: int = EXTRACT_MAX
    research: bool = True
    research_model: str = "mini"
    queries: dict = field(default_factory=lambda: dict(DEFAULT_QUERIES))
    crawl: tuple = ()
    max_queries_per_run: int = 8

    @classmethod
    def load(cls, config_path: str | None) -> "BriefingSettings":
        raw = {}
        if config_path:
            try:
                with open(config_path, encoding="utf-8") as handle:
                    raw = (yaml.safe_load(handle) or {}).get("briefing") or {}
            except (OSError, ValueError) as error:
                print(f"briefing: could not read the settings: {error}", flush=True)
        settings = cls()
        for key, value in raw.items():
            if key == "queries" and isinstance(value, dict):
                merged = {**DEFAULT_QUERIES, **value}
                merged["seasonal"] = {int(month): list(items) for month, items
                                      in (value.get("seasonal")
                                          or DEFAULT_QUERIES["seasonal"]).items()}
                settings.queries = merged
            elif key == "crawl" and isinstance(value, list):
                settings.crawl = tuple(value)
            elif key == "trusted_domains" and isinstance(value, list):
                settings.trusted_domains = tuple(str(item).lower().strip() for item in value)
            elif hasattr(settings, key) and not isinstance(value, (dict, list)):
                setattr(settings, key, type(getattr(settings, key))(value))
        # TAVILY_BUDGET_PER_ROUND in the environment sets each round's credits; not repeated here.
        return settings


def domain_of(url: str) -> str:
    """The URL's host. Trust goes by domain: who published the text is what the rule weighs."""
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host.lower()


def trusted_domain(url: str, trusted: tuple) -> bool:
    """Is it an official source? The host must be exactly that domain or below it.

    'nyc.gov.example.com' belongs to someone else.
    """
    host = domain_of(url)
    return any(host == name or host.endswith("." + name) for name in trusted)


# ---------- One reading ----------

@dataclass
class Citation:
    """Where the rule came from. Copied as-is to the ledger, store, screen and approval card."""

    url: str = ""
    title: str = ""
    domain: str = ""
    fetched_at: float = 0.0
    read_by: str = ""
    trust: str = "unofficial"      # official | unofficial
    query: str = ""
    recorded: bool = False
    fixture: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"source_url": self.url, "title": self.title, "domain": self.domain,
                "fetched_at": self.fetched_at, "read_by": self.read_by, "trust": self.trust,
                "query": self.query, "recorded": self.recorded,
                **({"fixture": self.fixture} if self.fixture else {})}

    @classmethod
    def from_dict(cls, raw: dict) -> "Citation":
        return cls(url=str(raw.get("source_url") or raw.get("url") or ""),
                   title=str(raw.get("title") or ""), domain=str(raw.get("domain") or ""),
                   fetched_at=float(raw.get("fetched_at") or 0.0),
                   read_by=str(raw.get("read_by") or ""),
                   trust=str(raw.get("trust") or "unofficial"),
                   query=str(raw.get("query") or ""), recorded=bool(raw.get("recorded")),
                   fixture=dict(raw.get("fixture") or {}))


@dataclass
class Reading:
    """What one page yielded and what happened after. Remembered across rounds.

    status: applied (in force) · held (waiting for a person) · approved (a person confirmed) ·
    refused (a person refused) · lapsed (passed without an answer) · info (not a rule) ·
    none (irrelevant) · invalid (failed the checks) · unreadable.
    """

    item_id: str
    hazard: Hazard
    citation: Citation
    status: str = "info"
    text: str = ""
    why: str = ""
    from_tick: int | None = None
    until_tick: int | None = None
    confirmed_by: str | None = None
    round_applied: object = field(default_factory=lambda: NEVER)

    @property
    def held(self) -> bool:
        return self.status in ("held", "lapsed")

    def to_hints(self) -> dict:
        return {"briefing": {"hazard": self.hazard.to_dict(), "citation": self.citation.to_dict(),
                             "status": self.status, "why": self.why,
                             "confirmed_by": self.confirmed_by}}

    @classmethod
    def from_hints(cls, item_id: str, hints: dict, text: str = "") -> "Reading | None":
        raw = (hints or {}).get("briefing")
        if not isinstance(raw, dict) or not isinstance(raw.get("hazard"), dict):
            return None
        return cls(item_id=item_id, hazard=Hazard.from_dict(raw["hazard"]),
                   citation=Citation.from_dict(raw.get("citation") or {}),
                   status=str(raw.get("status") or "info"), text=text,
                   why=str(raw.get("why") or ""), confirmed_by=raw.get("confirmed_by"))

    def to_dict(self, tick: int = 0) -> dict:
        return {"id": self.item_id, "kind": self.hazard.kind, "place": self.hazard.place,
                "summary": self.hazard.detail or self.why, "url": self.citation.url,
                "domain": self.citation.domain, "title": self.citation.title,
                "trust": self.citation.trust, "read_by": self.citation.read_by,
                "recorded": self.citation.recorded, "status": self.status,
                "rule_id": self.item_id if self.status in ("applied", "approved", "held") else None,
                "from_tick": self.from_tick, "until_tick": self.until_tick,
                "fetched_at": self.citation.fetched_at, "why": self.why,
                "note": self.hazard.note}

    def active_in(self, round_key) -> bool:
        """Is the rule in force this round, or due to be (including held for a person)?"""
        return self.round_applied is not NEVER and self.round_applied == round_key


@dataclass
class BriefingNotice(NoticeRecord):
    """A notice the briefing made. It takes the notice book's usual path, carrying its source."""

    citation: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**super().to_dict(), "citation": dict(self.citation)}


@dataclass
class Finding:
    """One page's result from the worker thread. The world thread turns it into a rule."""

    item_id: str
    citation: Citation
    text: str
    hazard: Hazard | None = None
    why: str = ""
    remembered: bool = False


@dataclass
class RunPlan:
    """What one briefing asks. The world thread plans it; the worker thread runs it."""

    trigger: str                 # round | corridor | manual
    round: object = None
    tick: int = 0
    day: datetime.date = None
    queries: tuple = ()
    crawls: tuple = ()
    places: tuple = ()
    cells: tuple = ()
    bbox: tuple | None = None
    landing_areas: tuple = ()
    known: frozenset = frozenset()
    research: bool = False
    window_text: str = ""


@dataclass
class RunResult:
    plan: RunPlan
    source: str = "recorded"
    fallback: dict | None = None
    status: FetchStatus | None = None
    findings: list = field(default_factory=list)
    summary: str = ""
    summary_by: str = ""
    domains: tuple = ()
    errors: list = field(default_factory=list)
    credits: float = 0.0
    calls: int = 0
    ignored: int = 0


# ---------- Reading (worker thread) ----------

@dataclass
class Reader:
    """Everything needed to read one page. Reading runs only on the worker thread."""

    gazetteer: object
    landing_areas: tuple
    day: datetime.date
    bbox: tuple | None
    llm: object = None

    def read(self, title: str, text: str) -> tuple[Hazard | None, str, str]:
        """(hazard, read by, why unreadable). Grammar → Super if needed → code checks."""
        body = f"{title}. {text}".strip(". ") if title else text
        if not body.strip():
            return None, "", "empty page"
        hazard = read_hazard(body, self.gazetteer, list(self.landing_areas), self.day)
        read_by = "grammar"
        if hazard is None:
            if not worth_a_model(body):
                return Hazard(kind="none", detail="not about flying"), "grammar", ""
            hazard, read_by, why = self._ask_model(body)
            if hazard is None:
                return None, read_by, why
        problems = hazard_problems(hazard, self.bbox)
        if problems:
            return None, read_by, "; ".join(problems)
        return hazard, read_by, ""

    def _ask_model(self, body: str) -> tuple[Hazard | None, str, str]:
        if not self._can_compile():
            return None, "", "the grammar could not read it and there is no model to structure it"
        reply = self.llm.ask(
            LlmTier.SUPER, BRIEFING_SYSTEM,
            f"Today is {self.day.isoformat()} in New York. Landing areas the tower uses: "
            f"{', '.join(area['name'] for area in self.landing_areas)}.\nPage: {body[:3000]}",
            max_tokens=500, json_object=True, timeout_s=BRIEFING_TIMEOUT_S)
        if reply is None:
            return None, "", "no answer from the model"
        form = parse_json_object(reply.text)
        try:
            hazard = None if form is None else from_briefing_form(
                form, self.gazetteer, list(self.landing_areas), self.day, body)
        except UnknownBriefingPlace as error:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", str(error)
        if hazard is None:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", "the model's answer is not in the form"
        return hazard, f"model:{reply.model}", ""

    def from_form(self, form: dict, text: str, read_by: str) -> tuple[Hazard | None, str, str]:
        """One form filled in by Tavily research. Same checks as anything a model read."""
        try:
            hazard = from_briefing_form(form, self.gazetteer, list(self.landing_areas),
                                        self.day, text)
        except UnknownBriefingPlace as error:
            return None, read_by, str(error)
        if hazard is None:
            return None, read_by, "the research answer is not in the form"
        problems = hazard_problems(hazard, self.bbox)
        if problems:
            return None, read_by, "; ".join(problems)
        return hazard, read_by, ""

    def _can_compile(self) -> bool:
        return (self.llm is not None and getattr(self.llm, "enabled", False)
                and bool(self.llm.model_for(LlmTier.SUPER)))


def brief_id(url: str, extra: str = "", recorded: bool = False) -> str:
    digest = hashlib.sha1(f"{url}|{extra}".encode()).hexdigest()[:12]
    return f"{RECORDED_PREFIX if recorded else LIVE_PREFIX}{digest}"


def execute(plan: RunPlan, client, reader: Reader, settings: BriefingSettings) -> RunResult:
    """One briefing: search → official pages in full → read → research → summary.

    All on the worker thread. With no budget left, that call is not made (BudgetExhausted) and
    the run carries on: an empty wallet is not a fault. Failures are counted and passed on as
    the status.
    """
    result = RunResult(plan=plan, source="recorded" if client.recorded else "live")
    docs: dict[str, dict] = {}
    errors: list[str] = []
    successes = 0
    skipped = 0

    for crawl in plan.crawls:
        try:
            body = client.crawl_site(str(crawl.get("url")),
                                     instructions=crawl.get("instructions"),
                                     limit=int(crawl.get("limit") or 6))
            successes += 1
        except BudgetExhausted:
            skipped += 1
            continue
        except SearchFailed as error:
            errors.append(f"crawl {crawl.get('url')}: {error}")
            continue
        for raw in body.get("results") or []:
            _note_doc(docs, plan, raw.get("url"), raw.get("title") or "",
                      raw.get("raw_content") or "", f"crawl {crawl.get('url')}", raw, client)

    for query in plan.queries:
        try:
            body = client.search_raw(query["text"], topic=query.get("topic", "general"),
                                     time_range=query.get("time_range"))
            successes += 1
        except BudgetExhausted:
            skipped += 1
            continue
        except SearchFailed as error:
            errors.append(f"search {query['text']}: {error}")
            continue
        for raw in body.get("results") or []:
            _note_doc(docs, plan, raw.get("url"), raw.get("title") or "",
                      raw.get("content") or "", query["text"], raw, client)

    _fetch_pages(docs, client, settings, errors, plan.known)

    for doc in docs.values():
        if doc["item_id"] in plan.known:
            result.findings.append(Finding(doc["item_id"], doc["citation"], doc["text"],
                                           remembered=True))
            continue
        hazard, read_by, why = reader.read(doc["title"], doc["text"])
        doc["citation"].read_by = read_by
        if hazard is not None and hazard.kind == "none":
            result.ignored += 1
        result.findings.append(Finding(doc["item_id"], doc["citation"], doc["text"],
                                       hazard=hazard, why=why))

    if plan.research:
        try:
            _research(plan, client, reader, settings, result, docs)
            successes += 1
        except BudgetExhausted:
            skipped += 1
        except SearchFailed as error:
            errors.append(f"research: {error}")

    result.errors = errors
    result.credits = client.credits.used
    result.calls = client.calls
    result.status = FetchStatus(ok=successes > 0 and not errors,
                                error=errors[0][:120] if errors else "",
                                calls=client.calls, failures=client.failures,
                                credits=client.credits.used, skipped=skipped)
    result.domains = tuple(sorted({finding.citation.domain for finding in result.findings
                                   if finding.citation.domain and finding.hazard is not None
                                   and finding.hazard.kind != "none"}))
    result.summary, result.summary_by = write_summary(result, reader)
    return result


def _note_doc(docs: dict, plan: RunPlan, url, title: str, text: str, query: str, raw: dict,
              client) -> None:
    """Add one page from a search or crawl to the run's list. Each URL is read only once."""
    url = str(url or "")
    if not url and not text:
        return
    item_id = brief_id(url or title, recorded=bool(client.recorded))
    if item_id in docs:
        return
    citation = Citation(url=url, title=" ".join(str(title).split())[:200],
                        domain=domain_of(url), fetched_at=_now(), query=query,
                        recorded=bool(client.recorded), fixture=dict(raw.get("fixture") or {}))
    docs[item_id] = {"item_id": item_id, "citation": citation,
                     "text": " ".join(str(text).split())[:CONTENT_CHARS],
                     "title": citation.title, "full": bool(raw.get("raw_content"))}


def _fetch_pages(docs: dict, client, settings: BriefingSettings, errors: list,
                 known: frozenset = frozenset()) -> None:
    """Fetch official pages in full: search snippets arrive with height, radius and time window
    cut off. Pages already read (known) are not fetched, so a restart does not spend credits on
    the same page again.
    """
    wanted = [doc for doc in docs.values()
              if doc["item_id"] not in known and not doc["full"] and doc["citation"].url
              and trusted_domain(doc["citation"].url, settings.trusted_domains)]
    if not wanted:
        return
    urls = [doc["citation"].url for doc in wanted[: settings.extract_max]]
    try:
        body = client.extract(urls)
    except BudgetExhausted:
        return
    except SearchFailed as error:
        errors.append(f"extract: {error}")
        return
    by_url = {str(raw.get("url") or ""): raw for raw in body.get("results") or []}
    for doc in wanted:
        raw = by_url.get(doc["citation"].url)
        if raw is None:
            continue
        doc["text"] = " ".join(str(raw.get("raw_content") or "").split())[:CONTENT_CHARS]
        doc["full"] = True
        if raw.get("fixture"):
            doc["citation"].fixture = dict(raw["fixture"])


def _research(plan: RunPlan, client, reader: Reader, settings: BriefingSettings,
              result: RunResult, docs: dict) -> None:
    """One research call. Tavily does the finding; our grammar does the reading.

    When it points at an official domain, that page is fetched and the grammar reads it again,
    so what applies is the official text, not the model's words. If the grammar cannot read it
    or the source is not official, the structured answer counts as model-read and goes to a
    person.
    """
    body = client.research(_research_question(plan), RESEARCH_SCHEMA,
                           model=settings.research_model)
    content = body.get("content")
    hazards = content.get("hazards") if isinstance(content, dict) else None
    if not isinstance(hazards, list):
        return
    seen_urls = {doc["citation"].url for doc in docs.values()}
    for raw in hazards[:8]:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("source_url") or "")
        if url and url in seen_urls:
            continue        # our grammar has already read that page
        text = " ".join(str(raw.get("summary") or "").split())
        title = next((str(raw[key]) for key in ("place", "park", "venue", "address", "summary")
                      if raw.get(key)), "research")
        citation = Citation(url=url, title=title[:200], domain=domain_of(url),
                            fetched_at=_now(), query="tavily research",
                            recorded=bool(client.recorded), fixture=dict(body.get("fixture") or {}))
        item_id = brief_id(url or text, extra=str(raw.get("kind") or ""),
                           recorded=bool(client.recorded))
        page_id = brief_id(url, recorded=bool(client.recorded)) if url else item_id
        if {item_id, page_id} & set(plan.known) or item_id in docs or page_id in docs:
            continue
        if url and trusted_domain(url, settings.trusted_domains):
            page = _read_official(url, client, reader, citation, result)
            if page is not None:
                result.findings.append(page)
                seen_urls.add(url)
                continue
        hazard, read_by, why = reader.from_form(raw, text, "model:tavily-research")
        citation.read_by = read_by
        result.findings.append(Finding(item_id, citation, text, hazard=hazard, why=why))


def _read_official(url: str, client, reader: Reader, citation: Citation,
                   result: RunResult) -> Finding | None:
    """Read, ourselves, the official page research pointed to.

    None if we cannot, and then the model's form is used.
    """
    try:
        body = client.extract([url])
    except (SearchFailed, BudgetExhausted):
        return None
    raw = next((item for item in body.get("results") or []
                if str(item.get("url") or "") == url), None)
    if raw is None:
        return None
    text = " ".join(str(raw.get("raw_content") or "").split())[:CONTENT_CHARS]
    if raw.get("fixture"):
        citation.fixture = dict(raw["fixture"])
    hazard, read_by, why = reader.read(citation.title, text)
    citation.read_by = read_by
    if hazard is None or hazard.kind == "none":
        return None
    return Finding(brief_id(url, recorded=citation.recorded), citation, text, hazard=hazard,
                   why=why)


def _research_question(plan: RunPlan) -> str:
    places = ", ".join(plan.places) or "Manhattan and the Brooklyn and Queens waterfront"
    return (
        f"Hazards to low-altitude drone flight over {places} on {plan.day.isoformat()} "
        f"{plan.window_text}. Look for temporary flight restrictions (including VIP movements "
        "and United Nations General Assembly week), large events and street closures, park and "
        "pier closures, tower cranes, and severe weather advisories. Prefer official sources: "
        "faa.gov, weather.gov, nyc.gov, nycgovparks.org. Give the address, the park name or the "
        "centre coordinates and radius, the local start and end time, and the source url."
    )


def write_summary(result: RunResult, reader: Reader) -> tuple[str, str]:
    """Two sentences. Super writes them and code checks them; without Super, a template.

    If the model names a domain not on the list, the text is dropped: a summary that invents a
    source becomes the source.
    """
    rules = [f.hazard for f in result.findings if f.hazard is not None and f.hazard.rule_kind]
    template = _template_summary(result, rules)
    if not reader._can_compile() or not result.findings:
        return template, "template"
    lines = [f"- {f.hazard.kind}: {f.hazard.detail or f.hazard.place} "
             f"({f.citation.domain or 'unknown'})"
             for f in result.findings if f.hazard is not None and f.hazard.kind != "none"]
    if not lines:
        return template, "template"
    reply = reader.llm.ask(
        LlmTier.SUPER, SUMMARY_SYSTEM,
        "Hazards read for the briefing:\n" + "\n".join(lines[:10])
        + f"\nSource domains: {', '.join(result.domains) or 'none'}",
        max_tokens=180, timeout_s=BRIEFING_TIMEOUT_S)
    if reply is None:
        return template, "template"
    text = " ".join(reply.text.split())[:SUMMARY_CHARS]
    if _summary_problem(text, result.domains):
        reader.llm.discard(LlmTier.SUPER)
        return template, "template"
    return text, f"model:{reply.model}"


DOMAIN_TOKEN = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")
ABBREVIATIONS = re.compile(r"\b(?:St|Ave|Blvd|Dr|Mt|No|Jr|Sr|U\.S|N\.Y|a\.m|p\.m)\.", re.IGNORECASE)


def _summary_problem(text: str, domains: tuple) -> str:
    if not text or len(text) < 20:
        return "too short"
    if "http" in text or "<" in text:
        return "carries a URL or markup"
    spoken = set(DOMAIN_TOKEN.findall(text.lower()))
    known = {d.lower() for d in domains}
    unknown = [name for name in spoken
               if not any(d == name or d.endswith("." + name) for d in known)]
    if unknown:
        return f"sources not on the list {unknown}"
    plain = ABBREVIATIONS.sub("", DOMAIN_TOKEN.sub("", text))
    sentences = [part for part in re.split(r"[.!?]+(?:\s+|$)", plain) if part.strip()]
    return "" if len(sentences) <= SUMMARY_SENTENCES else f"{len(sentences)} sentences"


def _template_summary(result: RunResult, rules: list) -> str:
    kinds: dict[str, int] = {}
    for hazard in rules:
        kinds[hazard.kind] = kinds.get(hazard.kind, 0) + 1
    made = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items())) or "none"
    where = ", ".join(result.domains[:4]) or "no source"
    return (f"Briefing for {', '.join(result.plan.places[:4]) or 'the fleet'}: "
            f"{plural(len(result.findings), 'page')} read, rules {made}. "
            f"Drawn from {where} ({result.source}).")


def _now() -> float:
    return time.time()


# ---------- Grid and places ----------

def cell_of(lat: float, lon: float, cell_km: float) -> tuple[int, int]:
    """A cell of about 1 km. Each cell is asked about once per round."""
    size = max(0.2, cell_km) * 1000.0
    return (int(math.floor(lat * METRES_PER_DEG_LAT / size)),
            int(math.floor(lon * METRES_PER_DEG_LON / size)))


def cells_along(legs: list, cell_km: float, step_m: float = 250.0) -> list[tuple]:
    """Cells the corridor crosses and where it enters each, in order. [(cell, (lat, lon)), ...]

    Walks each leg: looking only at the endpoints would skip the neighbourhoods in between.
    """
    found: dict = {}
    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs or []
              if leg.get("lat") is not None and leg.get("lon") is not None]
    for here, nxt in zip(points, points[1:], strict=False):
        length = math.hypot((nxt[0] - here[0]) * METRES_PER_DEG_LAT,
                            (nxt[1] - here[1]) * METRES_PER_DEG_LON)
        steps = max(1, int(length / step_m))
        for index in range(steps + 1):
            fraction = index / steps
            at = (here[0] + (nxt[0] - here[0]) * fraction,
                  here[1] + (nxt[1] - here[1]) * fraction)
            found.setdefault(cell_of(at[0], at[1], cell_km), at)
    return list(found.items())


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def street_of(label: str) -> str:
    """Only the street name from an address. '2701 Broadway' → 'Broadway'."""
    parts = str(label or "").split()
    return " ".join(parts[1:]) if parts and parts[0][:1].isdigit() else " ".join(parts)


# ---------- The desk ----------

class BriefingDesk:
    """The runtime's pre-flight briefing.

    The world thread polls it; anything that goes outside runs on the desk's own thread.
    """

    def __init__(self, tower, config_path: str | None = None, enabled: bool = False):
        self.tower = tower
        self.settings = BriefingSettings.load(config_path)
        self.enabled = bool(enabled)
        self.run_async = True
        self.readings: dict[str, Reading] = {}
        self.round_key: object = _UNSET
        self.briefed_cells: set = set()
        self.pending_cells: dict = {}
        self.asked: set[str] = set()
        self.runs = 0
        self.last_run_tick: int | None = None
        self.last_trigger: str | None = None
        self.summary = ""
        self.summary_by = ""
        self.domains: tuple = ()
        self.status: FetchStatus | None = None
        self.source = "off"
        self.fallback: dict | None = None
        self.source_failed = False
        self.ignored = 0
        self._recorded: RecordedTavily | None = None
        self._running = False
        self._done: list[RunResult] = []
        self._manual = False
        self._recalled: set[str] = set()
        self._lock = threading.Lock()
        self._bbox: tuple | None = None
        self.load_memory()

    # ---------- What it runs on ----------

    @property
    def live(self):
        """The live Tavily client; None if there isn't one or in recorded mode."""
        forced = os.getenv("TAVILY_RECORDED", "").strip().lower()
        if forced in ("1", "true", "yes", "on"):
            return None
        return getattr(self.tower, "tavily", None)

    @property
    def mode(self) -> str:
        """live · recorded · off. No key means recorded; TAVILY_RECORDED=0 turns it off."""
        if self.live is not None:
            return "live"
        forced = os.getenv("TAVILY_RECORDED", "").strip().lower()
        if forced in ("0", "false", "no", "off"):
            return "off"
        return "recorded"

    def _serving_recorded(self) -> bool:
        """Serving recordings now? In recorded mode, or when live failed and fell back to them."""
        return self.mode == "recorded" or self.fallback is not None

    @property
    def fallback_allowed(self) -> bool:
        return os.getenv("TAVILY_RECORDED", "").strip().lower() not in ("0", "false", "no", "off")

    def recorded_client(self) -> RecordedTavily:
        if self._recorded is None:
            self._recorded = RecordedTavily()
        return self._recorded

    @property
    def day(self) -> datetime.date:
        """The briefing's day: today when live; when recorded, the day the fixture was made."""
        given = os.getenv("BRIEFING_DATE", "").strip()
        if given:
            try:
                return datetime.date.fromisoformat(given)
            except ValueError:
                pass
        if self.mode == "recorded":
            for call in self.recorded_client().fixtures:
                as_of = (call.get("fixture") or {}).get("as_of")
                if as_of:
                    try:
                        return datetime.date.fromisoformat(str(as_of))
                    except ValueError:
                        continue
        return datetime.datetime.now(datetime.UTC).date()

    # ---------- Called from the world thread ----------

    def poll(self, bbox=None) -> None:
        """Once per poll. Applies finished runs, and asks afresh if the round has changed."""
        if not self.enabled:
            return
        self._bbox = bbox if bbox is not None else self._bbox
        self._collect()
        self._sync_cards()
        self._recall_closures()
        if self.round_key is not _UNSET and self.round_key == self.tower._round:
            self._maybe_corridor_run()
            return
        self._new_round()

    def corridor_cleared(self, legs: list, asset: str = "") -> None:
        """A corridor just cleared. Queues any neighbourhood not yet asked about this round."""
        if not self.enabled or not legs:
            return
        cells = cells_along(legs, self.settings.cell_km)
        with self._lock:
            for order, (cell, entry) in enumerate(cells):
                if cell in self.briefed_cells or cell in self.pending_cells:
                    continue
                # (tick first seen, order along the corridor, entry point). Neighbourhoods
                # crossed first are asked about first.
                self.pending_cells[cell] = (self.tower.tick, order, entry)

    def request_run(self) -> tuple[int, dict]:
        """POST /briefing/run. Asks once more at the next poll."""
        if not self.enabled or self.mode == "off":
            return 503, {"error": "briefing is off"}
        if self._running:
            return 409, {"error": "briefing is already running"}
        self._manual = True
        return 200, {"ok": True, "queued": True, "source": self.mode}

    def adopt_waiting(self, items: list[dict]) -> list[dict]:
        """Rows that were waiting for a person at restart.

        The briefing's own are taken here; the rest go back to the intake inbox.
        """
        rest = []
        for item in items or []:
            reading = Reading.from_hints(str(item.get("id") or ""), item,
                                         str(item.get("text") or ""))
            if reading is None:
                rest.append(item)
                continue
            reading.status = "held"     # the card was lost: raise it again (no re-read)
            self.readings[reading.item_id] = reading
        return rest

    def load_memory(self) -> None:
        """Reload rows, finished ones included. A restart must not lift a rule: only a person or
        the window does that. Pages are not read again (the text and hints are in the store).
        """
        store = getattr(self.tower, "store", None)
        if store is None or not hasattr(store, "briefed"):
            return
        for row in store.briefed(LIVE_PREFIX):
            reading = Reading.from_hints(str(row.get("id") or ""), row.get("hints") or {},
                                         str(row.get("text") or ""))
            if reading is not None:
                self.readings.setdefault(reading.item_id, reading)

    # ---------- Rounds ----------

    def _new_round(self) -> None:
        """The round changed. Re-apply remembered rules in this round's windows, then ask once."""
        self.round_key = self.tower._round
        with self._lock:
            self.briefed_cells.clear()
            self.pending_cells.clear()
        self.asked.clear()
        self._recalled.clear()
        live = self.live
        if live is not None:
            live.credits.new_round()
        for reading in list(self.readings.values()):
            self._place(reading, remembered=True)
        self._start(self._plan("round"))

    def _maybe_corridor_run(self) -> None:
        if self._manual:
            self._manual = False
            self.asked.clear()
            self._start(self._plan("manual"))
            return
        if self._running or not self.pending_cells or self._broke():
            return
        with self._lock:
            oldest = min(first for first, _, _ in self.pending_cells.values())
            if self.tower.tick - oldest < self.settings.corridor_debounce_ticks:
                return
            if (self.last_run_tick is not None and self.tower.tick - self.last_run_tick
                    < self.settings.corridor_min_gap_ticks):
                return
            cells = sorted(self.pending_cells, key=lambda cell: self.pending_cells[cell][:2])
            chosen = [(cell, self.pending_cells[cell][2])
                      for cell in cells[: self.settings.max_cells_per_run]]
            for cell, _ in chosen:
                self.pending_cells.pop(cell, None)
                self.briefed_cells.add(cell)
        self._start(self._plan("corridor", cells=chosen))

    def _broke(self) -> bool:
        """Are this round's credits spent? Then no corridor briefing starts: calls that would
        never go out only pile empty run lines into the ledger. The cells stay queued and are
        dropped at the next round.
        """
        live = self.live
        return live is not None and live.credits.left < 1.0

    # ---------- Planning ----------

    def _plan(self, trigger: str, cells: list | None = None) -> RunPlan:
        day = self.day
        areas = tuple(self.tower.landing_areas or [])
        cells = list(cells or [])
        places, queries = [], []
        if trigger in ("round", "manual"):
            for area in self._destinations(areas):
                places.append(str(area.get("name")))
                queries += self._park_queries(area, day)
            queries += self._city_queries(day)
        for cell, entry in cells:
            for area in areas:
                if cell_of(float(area["lat"]), float(area["lon"]),
                           self.settings.cell_km) == cell:
                    places.append(str(area.get("name")))
                    queries += self._park_queries(area, day)
            streets = self._streets_near(entry)
            queries += self._street_queries(entry, streets, areas, day)
            places.append(" and ".join(streets) if streets else f"{entry[0]:.3f},{entry[1]:.3f}")
        fresh = []
        for query in queries:
            if query["text"] in self.asked:
                continue
            self.asked.add(query["text"])
            fresh.append(query)
        return RunPlan(
            trigger=trigger, round=self.tower._round, tick=self.tower.tick, day=day,
            queries=tuple(fresh[: self.settings.max_queries_per_run]),
            crawls=tuple(self.settings.crawl) if trigger in ("round", "manual") else (),
            places=tuple(dict.fromkeys(places)), cells=tuple(cells), bbox=self._bbox,
            landing_areas=areas, known=frozenset(self._known()),
            research=self.settings.research and trigger in ("round", "manual"),
            window_text=self._window_text(day))

    def _destinations(self, areas: tuple) -> list[dict]:
        """Landing sites the fleet is heading to now.

        Asking about that place at that time is what a briefing is.
        """
        found = []
        for state in (self.tower.telemetry or {}).values():
            if state.get("job_lat") is None or state.get("job_lon") is None:
                continue
            goal = (float(state["job_lat"]), float(state["job_lon"]))
            near = min(areas, key=lambda area: _distance_m(goal, (area["lat"], area["lon"])),
                       default=None)
            if near is not None and _distance_m(goal, (near["lat"], near["lon"])) < 200.0 \
                    and near not in found:
                found.append(near)
        return found

    def _park_queries(self, area: dict, day: datetime.date) -> list[dict]:
        return [{"text": template.format(park=area.get("name"), date=_spoken_date(day),
                                         year=day.year, borough=_borough(area)),
                 "topic": "news", "time_range": "week", "why": "park"}
                for template in self.settings.queries.get("park") or []]

    def _street_queries(self, at: tuple, streets: list[str], areas: tuple,
                        day: datetime.date) -> list[dict]:
        """Ask about the street corner at that point (two street names).

        Without a borough, cranes from another city come back.
        """
        if not streets:
            return []
        near = min(areas, key=lambda area: _distance_m(at, (area["lat"], area["lon"])),
                   default=None)
        return [{"text": template.format(street=" and ".join(streets), date=_spoken_date(day),
                                         year=day.year,
                                         borough=_borough(near) if near else DEFAULT_BOROUGH),
                 "topic": "general", "time_range": "month", "why": "street"}
                for template in self.settings.queries.get("street") or []]

    def _streets_near(self, at: tuple, limit: int = 2) -> list[str]:
        """One or two street names at that point, from addresses the gazetteer knows.

        We only ask about places we know.
        """
        addresses = getattr(self.tower.gazetteer, "addresses", []) or []
        close = sorted(
            (address for address in addresses
             if _distance_m(at, (address["lat"], address["lon"])) < 700.0),
            key=lambda address: _distance_m(at, (address["lat"], address["lon"])))
        streets = []
        for address in close:
            street = street_of(address.get("label", ""))
            if street and street not in streets:
                streets.append(street)
            if len(streets) >= limit:
                break
        return streets

    def _city_queries(self, day: datetime.date) -> list[dict]:
        templates = list(self.settings.queries.get("round") or [])
        seasonal = (self.settings.queries.get("seasonal") or {}).get(day.month) or []
        return [{"text": template.format(date=_spoken_date(day), year=day.year,
                                         borough=DEFAULT_BOROUGH, park="", street=""),
                 "topic": "news", "time_range": "week", "why": "city"}
                for template in templates + list(seasonal)]

    def _window_text(self, day: datetime.date) -> str:
        """The hours this round covers, in New York local time.

        A query has to be about 'that time' as much as 'that place'.
        """
        start = self._round_start()
        end = start + datetime.timedelta(
            seconds=self.settings.max_window_ticks * self.tower.performance.seconds_per_tick)
        return (f"between {utc_to_eastern(start).strftime('%H:%M')} and "
                f"{utc_to_eastern(end).strftime('%H:%M')} local time")

    def _known(self) -> set:
        """Pages already read. Anything in memory is not read again.

        After a restart, memory includes what was reloaded from the store.
        """
        return set(self.readings)

    # ---------- Running ----------

    def _start(self, plan: RunPlan) -> None:
        if self.mode == "off" or self._running:
            return
        self._running = True
        self.last_trigger = plan.trigger
        if not self.run_async:
            self._work(plan)
            self._collect()
            return
        threading.Thread(target=self._work, args=(plan,), daemon=True,
                         name=f"briefing-{plan.trigger}").start()

    def _work(self, plan: RunPlan) -> None:
        """The worker thread: the only place that goes outside or asks a model."""
        try:
            result = self._run_with_fallback(plan)
        except Exception as error:  # noqa: BLE001 — the runtime keeps going if the briefing dies
            print(f"briefing: {error!r}", flush=True)
            result = RunResult(plan=plan, source=self.mode,
                               status=FetchStatus(ok=False, error=f"{type(error).__name__}"),
                               errors=[f"{error!r}"])
        with self._lock:
            self._done.append(result)
            self._running = False

    def _run_with_fallback(self, plan: RunPlan) -> RunResult:
        """Live Tavily first. If not a single result comes back, recorded — and it says so."""
        reader = Reader(gazetteer=self.tower.gazetteer, landing_areas=plan.landing_areas,
                        day=plan.day, bbox=plan.bbox, llm=getattr(self.tower, "llm", None))
        live = self.live
        if live is not None:
            result = execute(plan, live, reader, self.settings)
            if result.status.ok or not self.fallback_allowed:
                return result
            if result.findings:
                return result       # some of it arrived; don't cover it with recordings
            recorded = execute(plan, self.recorded_client(), reader, self.settings)
            recorded.fallback = {"from": "live", "why": result.status.error or "no answer"}
            recorded.status = result.status
            recorded.source = "recorded"
            return recorded
        return execute(plan, self.recorded_client(), reader, self.settings)

    # ---------- Results into rules (world thread) ----------

    def _collect(self) -> None:
        with self._lock:
            arrived, self._done = self._done, []
        for result in arrived:
            if result.plan.round != self.tower._round:
                continue        # an answer from a past round, not a rule for this one
            self._absorb(result)

    def _absorb(self, result: RunResult) -> None:
        self.runs += 1
        self.last_run_tick = self.tower.tick
        self.source = result.source
        self.fallback = result.fallback
        self.status = result.status
        found = [f for f in result.findings if f.hazard is not None and f.hazard.kind != "none"
                 and not f.remembered]
        self.ignored += result.ignored
        self._ledger_run(result)
        self._note_source(result.status)
        for finding in result.findings:
            self._take(finding)
        if result.summary_by.startswith("model:") and result.plan.trigger != "corridor":
            self.summary, self.summary_by = result.summary, result.summary_by
        elif found or not self.summary or self.summary_by == "template":
            self.summary, self.summary_by = self._round_summary(), "template"
        self.domains = self._round_domains()

    def _take(self, finding: Finding) -> None:
        """Record one page's result, and apply it if it is a rule."""
        known = self.readings.get(finding.item_id)
        if finding.remembered:
            if known is not None:
                self._place(known, remembered=True)
            return
        if known is not None:
            return      # a page we already know; recording it again would make two cards
        reading = Reading(item_id=finding.item_id,
                          hazard=finding.hazard or Hazard(kind="none"),
                          citation=finding.citation, text=finding.text[:EVIDENCE_CHARS],
                          why=finding.why)
        reading.citation.trust = ("official"
                                  if trusted_domain(reading.citation.url,
                                                    self.settings.trusted_domains)
                                  else "unofficial")
        if reading.hazard.kind == "restriction" and reading.hazard.centre is not None:
            reading.hazard.place = self._nearest_label(reading.hazard.centre) or "TFR"
        if finding.hazard is None:
            reading.status = "unreadable" if finding.why else "invalid"
        elif finding.hazard.rule_kind is None:
            reading.status = "none" if finding.hazard.kind == "none" else "info"
        self.readings[finding.item_id] = reading
        self._store(reading)
        self._ledger_item(reading)
        if reading.hazard.rule_kind is not None:
            self._place(reading)

    def _place(self, reading: Reading, remembered: bool = False) -> None:
        """Apply a rule this round (or hold it for a person). Outside its window, do nothing."""
        if reading.hazard.rule_kind is None or reading.status in ("refused", "invalid",
                                                                  "unreadable", "none"):
            return
        if remembered and reading.round_applied == self.tower._round:
            return
        if remembered and reading.citation.recorded != self._serving_recorded():
            return      # a recorded rule must not pass for a live answer (nor the reverse)
        from_tick, until_tick, why = self._ticks(reading.hazard.window)
        if why:
            reading.why = why
            reading.from_tick, reading.until_tick = from_tick, until_tick
            return
        reading.from_tick, reading.until_tick = from_tick, until_tick
        reading.round_applied = self.tower._round
        held = reading.status in ("held", "lapsed") or not self._applies_at_once(reading)
        record = self._notice(reading, from_tick, until_tick, held)
        self.tower.notices.records[reading.item_id] = record
        self.tower.intake.notice_ids.add(reading.item_id)
        self.tower._rule_open(reading.item_id, reading.hazard.kind, from_tick, until_tick,
                              applied=not held)
        reading.status = "held" if held else ("approved" if reading.confirmed_by else "applied")
        self._store(reading)
        self._ledger_rule(reading, record, held)
        if held:
            self.tower._hold_notice({"id": reading.item_id, "text": record.text}, record,
                                    self._hold_why(reading))

    def _round_summary(self) -> str:
        """The round's briefing in two sentences: rules in force, held and info items, sources."""
        readings = list(self.readings.values())
        active = [r for r in readings if r.active_in(self.tower._round)
                  and r.hazard.rule_kind is not None]
        applied = [r for r in active if r.status in ("applied", "approved")]
        waiting = [r for r in active if r.status == "held"]
        info = [r for r in readings if r.status == "info" and r.hazard.kind == "weather"]
        rules = "; ".join(f"{r.hazard.kind} {r.hazard.place}" for r in applied) or "none"
        return (f"In force this round: {rules}. {len(waiting)} waiting for a person, "
                f"{len(info)} advisory for information, drawn from "
                f"{', '.join(self._round_domains()) or 'no source'} ({self.source}).")

    def _round_domains(self) -> tuple:
        return tuple(sorted({r.citation.domain for r in list(self.readings.values())
                             if r.citation.domain and r.status not in ("none", "invalid",
                                                                       "unreadable")}))

    def _applies_at_once(self, reading: Reading) -> bool:
        """Does it apply now? Only if the grammar read an official source: the rule only
        tightens, so it does not wait for a person.

        What a person already confirmed (approved) applies as well. Anything a model read waits
        for a person even from an official source: that a model can apply nothing is the
        backbone of this system.
        """
        if reading.confirmed_by:
            return True
        return (reading.citation.trust == "official"
                and reading.citation.read_by.startswith("grammar"))

    def _hold_why(self, reading: Reading) -> str:
        if reading.citation.read_by.startswith("model:"):
            return (f"{reading.citation.read_by} read it — it applies only once a human "
                    f"confirms it ({reading.citation.domain})")
        return (f"{reading.citation.domain or 'unknown source'} is not an official source — "
                "it applies only once a human confirms it")

    def _notice(self, reading: Reading, from_tick: int, until_tick: int | None,
                held: bool) -> BriefingNotice:
        hazard = reading.hazard
        kind = hazard.kind
        citation = reading.citation.to_dict()
        if kind == "crane":
            polygon = circle(hazard.centre, self.settings.crane_radius_m)
            ceiling, clearance = hazard.height_m, self.settings.crane_clearance_m
            name = f"CRANE · {hazard.place} · {hazard.height_m:.0f} m"
        elif kind == "closure":
            polygon = circle(hazard.centre, self.settings.closure_radius_m)
            ceiling, clearance = CLOSED_CEILING_M, 0.0
            name = f"CLOSED · {hazard.place}"
        elif kind == "restriction":
            polygon = circle(hazard.centre, hazard.radius_m)
            ceiling, clearance = hazard.ceiling_m, 0.0
            name = (f"TFR · {hazard.radius_m / 1852.0:.1f} NM · "
                    f"{self._nearest_label(hazard.centre) or hazard.place}")
        else:
            polygon = circle(hazard.centre, hazard.radius_m)
            ceiling, clearance = hazard.ceiling_m, 0.0
            name = f"EVENT · {hazard.place}"
        text = f"{reading.citation.title} — {hazard.detail}".strip(" —")
        volume = Volume(
            id=reading.item_id, name=name, polygon=polygon, floor_m=0.0, ceiling_m=ceiling,
            reference="AGL", rule="forbidden", reason=hazard.detail or name,
            source=reading.confirmed_by or reading.citation.read_by,
            clearance_m=clearance, from_tick=from_tick, until_tick=until_tick,
            tags={"kind": kind, "briefing": citation, "place": hazard.place,
                  "landing_area": hazard.landing_area, "detail": hazard.detail,
                  "centre": [round(hazard.centre[0], 6), round(hazard.centre[1], 6)],
                  "note": hazard.note},
        )
        source = "human" if reading.confirmed_by else reading.citation.read_by
        record = BriefingNotice(reading.item_id, name, kind, text, volume, from_tick, until_tick,
                                source, held=held, citation=citation)
        record.confirmed_by = reading.confirmed_by
        return record

    def _nearest_label(self, centre: tuple) -> str:
        """Nearest gazetteer address to the centre. Only a label; it never moves the position."""
        addresses = getattr(self.tower.gazetteer, "addresses", []) or []
        near = min(addresses, key=lambda a: _distance_m(centre, (a["lat"], a["lon"])),
                   default=None)
        if near is None or _distance_m(centre, (near["lat"], near["lon"])) > 800.0:
            return ""
        return f"near {near['label']}"

    def _ticks(self, window: Window | None) -> tuple[int, int | None, str]:
        """Convert the window to this round's ticks; if it has passed, say in one line why not.

        Tick 0 is the round's clock (clock_epoch_z) at that time on the briefing day. The end
        is capped at now + max_window_ticks: a notice for today must not become a rule forever.
        """
        tick = self.tower.tick
        horizon = tick + self.settings.max_window_ticks
        if window is None:
            return tick, horizon, ""
        start = self._round_start()
        spt = float(self.tower.performance.seconds_per_tick)
        from_tick = tick if window.start is None else int(
            math.ceil((window.start - start).total_seconds() / spt))
        until_tick = horizon if window.end is None else int(
            (window.end - start).total_seconds() / spt)
        if until_tick <= tick:
            return from_tick, until_tick, "the window closed before this round"
        if from_tick > horizon:
            return from_tick, until_tick, "the window opens after this round"
        return max(0, from_tick), min(until_tick, horizon), ""

    def _round_start(self) -> datetime.datetime:
        """UTC time of tick 0: the round's clock (0900Z) on the briefing day."""
        epoch = str(self.tower.performance.clock_epoch_z or "0900").zfill(4)
        day = self.day
        return datetime.datetime(day.year, day.month, day.day, int(epoch[:2]) % 24,
                                 int(epoch[2:]) % 60, tzinfo=datetime.UTC)

    # ---------- Human answers, and closure recalls ----------

    def _sync_cards(self) -> None:
        """Did a person answer the card? The notice book has the answer; we don't ask again."""
        for reading in list(self.readings.values()):
            if reading.status != "held":
                continue
            record = self.tower.notices.get(reading.item_id)
            if record is not None and not record.held:
                reading.status = "approved"
                reading.confirmed_by = record.confirmed_by
                self._store(reading)
                continue
            if record is None:
                why = self.tower.notices.unreadable.get(reading.item_id, "")
                if reading.item_id in self.tower.notices.refused:
                    reading.status = "refused"
                    self._store(reading)
                elif why:
                    reading.status = "lapsed"
                    self._store(reading)

    def _recall_closures(self) -> None:
        """Recall aircraft bound for a closed landing site: a tightening rule also applies to
        flights already airborne.

        The closure zone itself blocks nothing in the air (its ceiling is below the ground). The
        column used here only catches routes that would land on that spot; it never enters the
        airspace.
        """
        for reading in list(self.readings.values()):
            if reading.hazard.kind != "closure" or reading.item_id in self._recalled:
                continue
            record = self.tower.notices.get(reading.item_id)
            if record is None or not record.applied:
                continue
            self._recalled.add(reading.item_id)
            if not self._anyone_landing(reading.hazard.centre):
                continue
            column = Volume(id=reading.item_id, name=record.name,
                            polygon=circle(reading.hazard.centre, CLOSURE_COLUMN_M),
                            floor_m=0.0, ceiling_m=None, rule="forbidden",
                            reason=record.name, source=record.source)
            self.tower.recall_flights(column)

    def _anyone_landing(self, centre: tuple) -> bool:
        for state in (self.tower.telemetry or {}).values():
            route = state.get("route") or []
            if not route or float(state.get("alt_m") or 0.0) <= 1.0:
                continue
            last = route[-1]
            if _distance_m(centre, (float(last["lat"]), float(last["lon"]))) \
                    <= self.settings.closure_radius_m:
                return True
        return False

    # ---------- Records ----------

    def _store(self, reading: Reading) -> None:
        store = getattr(self.tower, "store", None)
        if store is None:
            return
        # Stored as source tavily, so a restart counts it as seen (store.DEDUPE_SOURCES).
        store.put_item(reading.item_id, "tavily", reading.text or reading.hazard.detail,
                       self.tower.tick, reading.citation.url, reading.hazard.kind,
                       reading.to_hints())
        store.settle_item(reading.item_id, reading.hazard.kind, reading.citation.read_by,
                          _outcome(reading.status))

    def _ledger(self, action: str, code: str, reason: str, detail: dict, outcome: str,
                verdict=Verdict.AUTO, rationale: str = "", params: dict | None = None) -> None:
        noted = Proposal(asset_id=BRIEFING_ASSET, action=action, cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=rationale[:180],
                         params=params or {})
        decision = Decision(noted.id, verdict, reason[:400], code=code, detail=detail)
        self.tower.ledger.close_entry(
            self.tower.ledger.open_entry(noted, decision,
                                         self.tower._context(None, BRIEFING_CHECKS)), outcome)

    def _ledger_run(self, result: RunResult) -> None:
        plan = result.plan
        detail = {"trigger": plan.trigger, "source": result.source, "places": list(plan.places),
                  "queries": [query["text"] for query in plan.queries],
                  "crawls": [crawl.get("url") for crawl in plan.crawls],
                  "research": plan.research, "cells": len(plan.cells),
                  "pages": len(result.findings), "credits": round(result.credits, 2),
                  "calls": result.calls, "errors": result.errors[:3],
                  "fallback": result.fallback, "summary": result.summary,
                  "summary_by": result.summary_by, "day": plan.day.isoformat()}
        ok = result.status is None or result.status.ok
        self._ledger("briefing_run", "briefing_run",
                     f"briefing ({plan.trigger}, {result.source}) · "
                     f"{plural(len(result.findings), 'page')} · "
                     f"{plural(round(result.credits), 'credit')}",
                     detail, "noted" if ok else "failed",
                     Verdict.AUTO if ok else Verdict.DENIED, result.summary)

    def _ledger_item(self, reading: Reading) -> None:
        detail = {"item": reading.item_id, "kind": reading.hazard.kind, "status": reading.status,
                  "why": reading.why, "hazard": reading.hazard.to_dict(),
                  **reading.citation.to_dict()}
        readable = reading.status not in ("unreadable", "invalid")
        self._ledger("briefing_item", "briefing_item",
                     f"{reading.hazard.detail or reading.citation.title or 'page'} · "
                     f"{reading.citation.domain or 'unknown source'}"
                     + ("" if readable else f" · could not read — {reading.why}"),
                     detail, "noted" if readable else "unreadable",
                     Verdict.AUTO if readable else Verdict.DENIED,
                     reading.citation.title or reading.text)

    def _ledger_rule(self, reading: Reading, record: BriefingNotice, held: bool) -> None:
        detail = {"item": reading.item_id, "rule": reading.item_id, "kind": reading.hazard.kind,
                  "held": held, "from_tick": reading.from_tick, "until_tick": reading.until_tick,
                  "hazard": reading.hazard.to_dict(), **reading.citation.to_dict()}
        self._ledger("briefing_rule", "briefing_rule",
                     f"{record.name} · ticks {reading.from_tick}~{reading.until_tick}"
                     + (" · waiting for a human" if held else " · applied"),
                     detail, "held" if held else "applied",
                     Verdict.HUMAN if held else Verdict.AUTO, record.name,
                     params={"rule": reading.item_id, "kind": reading.hazard.kind})

    def _note_source(self, status: FetchStatus | None) -> None:
        """One line when the source goes failed ↔ recovered.

        Only on a change: writing one every cycle would fill the ledger with failures.
        """
        if status is None or self.mode != "live":
            return
        failed_now = not status.ok
        if failed_now == self.source_failed:
            return
        self.source_failed = failed_now
        self.tower._ledger_source_change(BRIEFING_ASSET, status, failed_now)

    # ---------- Screen ----------

    def snapshot(self) -> dict:
        live = self.live
        credits = (live or self.recorded_client()).credits.to_dict()
        items = sorted((reading for reading in list(self.readings.values())
                        if reading.status != "none"),
                       key=lambda reading: reading.citation.fetched_at)[-KEPT_ITEMS:]
        return {
            "enabled": self.enabled,
            "source": "off" if not self.enabled else (self.source if self.runs else self.mode),
            "mode": self.mode,
            "fallback": self.fallback,
            "last_run_tick": self.last_run_tick,
            "last_trigger": self.last_trigger,
            "runs": self.runs,
            "running": self._running,
            "credits_used": credits["used"],
            "credits_total": credits["total"],
            "budget": credits["budget"],
            "calls": credits["calls"],
            "day": self.day.isoformat(),
            "summary": self.summary or _no_summary(self),
            "summary_by": self.summary_by,
            "domains": list(self.domains),
            "cells": {"briefed": len(self.briefed_cells), "pending": len(self.pending_cells),
                      "km": self.settings.cell_km},
            "ignored": self.ignored,
            "status": None if self.status is None else self.status.to_dict(),
            "items": [{**reading.to_dict(self.tower.tick),
                       "active": reading.active_in(self.tower._round)} for reading in items],
        }




def _outcome(status: str) -> str:
    """The outcome kept in the store (sqlite).

    Only what waits for a person may stay 'held', so that its card comes back on restart.
    """
    return {"applied": "read", "approved": "approved", "refused": "refused",
            "held": "held", "lapsed": "lapsed", "info": "read", "none": "read",
            "invalid": "unreadable", "unreadable": "unreadable"}.get(status, "read")


def _spoken_date(day: datetime.date) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def _borough(area: dict | None) -> str:
    return BOROUGH.get(str((area or {}).get("id") or ""), DEFAULT_BOROUGH)


def _no_summary(desk: BriefingDesk) -> str:
    if not desk.enabled:
        return "briefing is off."
    return "no briefing yet."
