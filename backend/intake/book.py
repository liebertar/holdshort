"""What the tower took in, and what code made of it.

Every item (a simulator bulletin, a search result, a line a person typed) is recorded once.
The grammar reads the regular dialects; text it cannot read goes to the Super tier, which
fills the same schema. Code validates every reading — numbers, address, radius, window —
whoever produced it. What applies at once is only a grammar reading of a bulletin from the
tower's own feed; a search snippet or a typed line is held for a person even when the grammar
read it, and so is anything a model read. A model can say "this is weather" or "this is
nothing"; it can never open a hold or close a street by itself. The weather hold this book
keeps is a policy: it tightens at once and loosens only by expiry or by a person.
"""

import hashlib
import os
from dataclasses import dataclass, field

from backend.intake.notices import NoticeRecord
from shared.config import Policy, WeatherLimits
from shared.geo import Volume
from shared.intake import (
    INTAKE_SYSTEM,
    Compiled,
    Gazetteer,
    IncidentReport,
    WeatherReport,
    from_intake_form,
    hint_number,
    incident_problems,
    parse_incident,
    parse_weather,
    weather_problems,
)
from shared.llm.client import LlmTier, TieredLlm, parse_json_object
from shared.metar import SOURCE as METAR_SOURCE
from shared.notam import Clock, parse_notice, validate

# Seconds the model gets to structure one item. Generous for the same reason as notices
# (NOTICE_TIMEOUT_S) — reading runs off the world thread, and a late answer is picked up by the
# next poll.
INTAKE_TIMEOUT_S = float(os.getenv("INTAKE_TIMEOUT_S", "60"))
# Seconds between Tavily queries. News and forecasts do not change minute to minute.
INTAKE_PERIOD_S = float(os.getenv("INTAKE_PERIOD_S", "300"))
# Actions a weather hold blocks. Only when filed on the ground (Policy.ground_only) — an
# airborne aircraft has to come down.
HELD_ACTIONS = ("fly_route", "reserve_pad", "depart")
HOLD_POLICY_PREFIX = "weather-hold"
# Length of the original text kept for the screen and the ledger.
TEXT_CHARS = 180
KEPT_ITEMS = 20
# Sources whose grammar readings apply on the tick they arrive. Simulator notices stand in for the
# runtime's own feed (the same channel as NOTAMs and recalls). A search result is a web page and
# POST /intake is text from an unknown sender, so even when the grammar reads them, they become
# rules only after a human confirms on the approval screen — one line from a 2012 typhoon article
# must not ground the fleet.
TRUSTED_SOURCES = frozenset({"sim"})
# Cap on the window. An until_tick from a report or hint beyond now + default length × this
# multiple is cut there — even if a model says 99999999, nothing is grounded for more than 27 min
# (0.8 s/tick).
MAX_WINDOW_HOLDS = 4


@dataclass
class IntakeRecord:
    id: str
    source: str                  # sim | tavily | manual
    text: str
    kind: str | None = None      # weather | incident | notice | none | None (unreadable)
    read_by: str = ""            # grammar | model:<id> | "" (unreadable)
    why: str = ""                # why it could not be read
    held: bool = False
    tick: int = 0
    url: str = ""

    @property
    def trusted(self) -> bool:
        return self.source in TRUSTED_SOURCES

    def to_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "kind": self.kind, "read_by": self.read_by,
                "why": self.why, "held": self.held, "tick": self.tick, "url": self.url,
                "trusted": self.trusted, "text": self.text[:TEXT_CHARS]}


@dataclass
class WeatherHold:
    """One takeoff stop. Three policies (HELD_ACTIONS) enforce it; this is their basis."""

    id: str
    reason: str
    until_tick: int
    since_tick: int
    source: str                  # grammar | human
    report: dict
    breaches: list[str] = field(default_factory=list)
    lift_card: str | None = None  # the "lift" card on the approval screen (filing id)
    later_report: dict | None = None   # a within-limits report that arrived during the hold

    @property
    def policy_ids(self) -> list[str]:
        return [f"{HOLD_POLICY_PREFIX}:{action}" for action in HELD_ACTIONS]

    def policies(self) -> list[Policy]:
        return [Policy(id=policy_id, reason=self.reason, forbid_action=action,
                       active_from_tick=self.since_tick, active_until_tick=self.until_tick,
                       ground_only=True)
                for policy_id, action in zip(self.policy_ids, HELD_ACTIONS, strict=True)]

    def to_dict(self) -> dict:
        return {"id": self.id, "reason": self.reason, "until_tick": self.until_tick,
                "since_tick": self.since_tick, "source": self.source, "report": self.report,
                "breaches": list(self.breaches), "lift_card": self.lift_card,
                "later_report": self.later_report}


def item_id(item: dict) -> str:
    """The item's id. Without one, a hash of the text — the same text is read only once."""
    given = str(item.get("id") or "").strip()
    if given:
        return given
    digest = hashlib.sha1(" ".join(str(item.get("text") or "").split()).encode()).hexdigest()
    return f"{item.get('source') or 'intake'}-{digest[:12]}"


class IntakeBook:
    def __init__(self, clock: Clock, llm: TieredLlm | None, limits: WeatherLimits,
                 gazetteer: Gazetteer):
        self.clock = clock
        self.llm = llm
        self.limits = limits
        self.gazetteer = gazetteer
        self.records: dict[str, IntakeRecord] = {}
        self.hold: WeatherHold | None = None
        self.last_report: dict | None = None
        # Weather a model read as over the limits. Grounds nothing until a human confirms it.
        self.held_weather: dict[str, dict] = {}
        # Ids of notices (incidents, restrictions) this book made, so NoticeBook's feed check
        # does not drop them.
        self.notice_ids: set[str] = set()
        # Search source status. last_fetch_tick counts successful cycles only — moving it on an
        # empty failed cycle reads as "just asked". source_failed survives a new round (it is
        # about the source, not the round).
        self.last_fetch_tick: int | None = None
        self.fetch: dict | None = None
        self.source_failed = False

    # ---------- what is known ----------

    def known(self, key: str) -> bool:
        return key in self.records

    def receive(self, item: dict, tick: int) -> IntakeRecord | None:
        """Records and returns a new item. None if it was seen already — it is not read again."""
        key = item_id(item)
        if key in self.records:
            return None
        record = IntakeRecord(id=key, source=str(item.get("source") or "sim"),
                              text=str(item.get("text") or ""), tick=tick,
                              url=str(item.get("url") or ""))
        self.records[key] = record
        return record

    @property
    def items_read(self) -> int:
        return sum(1 for r in self.records.values() if r.kind is not None)

    @property
    def items_unreadable(self) -> int:
        return sum(1 for r in self.records.values() if r.kind is None and r.why)

    @property
    def can_compile(self) -> bool:
        return (self.llm is not None and self.llm.enabled
                and bool(self.llm.model_for(LlmTier.SUPER)))

    # ---------- reading ----------

    def read_grammar(self, item: dict) -> Compiled | None:
        """By grammar: NOTAM phrasing → incident (with an address) → weather. If the item
        names its kind, only that grammar."""
        text = str(item.get("text") or "")
        hint = str(item.get("kind") or "")
        if hint not in ("weather", "incident"):
            notice = parse_notice(text, self.clock)
            if notice is not None:
                return Compiled("notice", notice=notice)
        if hint != "weather":
            incident = parse_incident(text, self.gazetteer, self.clock, item)
            if incident is not None:
                return Compiled("incident", incident=incident)
        if hint != "incident":
            weather = parse_weather(text, self.clock)
            if weather is not None:
                return Compiled("weather", weather=weather)
        return None

    def needs_model(self, item: dict) -> bool:
        if not self.can_compile:
            return False
        return bool(str(item.get("text") or "").strip()) and self.read_grammar(item) is None

    def compile_item(self, item: dict, bbox) -> tuple[Compiled | None, str, str]:
        """(reading, read by, why unreadable). Stores nothing — safe to call from another thread.

        Range checks apply to grammar readings too. If the grammar read it but it is out of
        range, the model is not asked again — the text was read; the values it gave make no
        sense.
        """
        text = str(item.get("text") or "")
        if not text.strip():
            return None, "", "empty text"
        compiled = self.read_grammar(item)
        if compiled is not None:
            problems = self.problems(compiled, bbox)
            if problems:
                return None, "grammar", "; ".join(problems)
            return compiled, "grammar", ""
        if not self.can_compile:
            return None, "", "the grammar could not read it and there is no model to structure it"
        reply = self.llm.ask(LlmTier.SUPER, INTAKE_SYSTEM,
                             f"Clock: tick 0 is {self.clock.epoch_z}Z, one tick is "
                             f"{self.clock.seconds_per_tick} s.\nText: {text[:2000]}",
                             max_tokens=600, json_object=True, timeout_s=INTAKE_TIMEOUT_S)
        if reply is None:
            return None, "", "no answer from the model"
        form = parse_json_object(reply.text)
        try:
            compiled = None if form is None else from_intake_form(form, self.gazetteer,
                                                                  self.clock, text, item)
        except ValueError as error:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", str(error)
        if compiled is None:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", "the model's answer is not in the form"
        problems = self.problems(compiled, bbox)
        if problems:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", "; ".join(problems)
        return compiled, f"model:{reply.model}", ""

    def problems(self, compiled: Compiled, bbox) -> list[str]:
        """All checks on what a model structured. If any one fails, it is not even held."""
        if compiled.kind == "weather":
            return weather_problems(compiled.weather)
        if compiled.kind == "incident":
            return incident_problems(compiled.incident, bbox)
        if compiled.kind == "notice":
            return validate(compiled.notice, bbox)
        return []

    def settle(self, record: IntakeRecord, compiled: Compiled | None, read_by: str,
               why: str) -> None:
        record.read_by = read_by
        if compiled is None:
            record.kind = None
            record.why = why or "not read"
            return
        record.kind = compiled.kind
        record.held = compiled.kind != "none" and self.must_hold(record, read_by)

    @staticmethod
    def must_hold(record: IntakeRecord, read_by: str) -> bool:
        """Does it need human approval to apply? Yes if a model read it or it came from outside
        the runtime's own feed."""
        return read_by.startswith("model:") or not record.trusted

    @staticmethod
    def held_why(record: IntakeRecord, read_by: str) -> str:
        if read_by.startswith("model:"):
            return "what a model read applies only once a human confirms it"
        return (f"text from {record.source} is not a runtime notice — it applies only once "
                "a human confirms it")

    # ---------- weather ----------

    def breaches(self, report: WeatherReport) -> list[str]:
        """What exceeds the limits. Equal does not — the limit is the last flyable value."""
        found = []
        if report.gust_mps is not None and report.gust_mps > self.limits.max_gust_mps:
            found.append(f"gusts {report.gust_mps:.0f} m/s > {self.limits.max_gust_mps:.0f}")
        if report.wind_mps is not None and report.wind_mps > self.limits.max_wind_mps:
            found.append(f"wind {report.wind_mps:.0f} m/s > {self.limits.max_wind_mps:.0f}")
        if (report.visibility_m is not None
                and report.visibility_m < self.limits.min_visibility_m):
            found.append(f"visibility {report.visibility_m:.0f} m < "
                         f"{self.limits.min_visibility_m:.0f}")
        return found

    def horizon(self, tick: int) -> int:
        """The farthest tick a window can reach. Reports or hints beyond it are cut here."""
        return tick + MAX_WINDOW_HOLDS * int(self.limits.hold_default_ticks)

    def hold_until(self, report: WeatherReport, item: dict, tick: int) -> int:
        """How long to ground: the text's window → the item's until_tick → the default length.
        Capped by horizon."""
        return self.window_until(report.from_tick, report.until_tick, item, tick)

    def open_hold(self, record_id: str, report: WeatherReport, breaches: list[str],
                  until_tick: int, tick: int, source: str) -> WeatherHold:
        reason = "WEATHER HOLD · " + " · ".join(breaches)
        self.hold = WeatherHold(id=record_id, reason=reason, until_tick=until_tick,
                                since_tick=tick, source=source, report=report.to_dict(),
                                breaches=list(breaches))
        self.last_report = {**report.to_dict(), "id": record_id, "source": source,
                            "breaches": list(breaches), "tick": tick}
        return self.hold

    def note_report(self, record_id: str, report: WeatherReport, breaches: list[str],
                    tick: int, source: str) -> None:
        self.last_report = {**report.to_dict(), "id": record_id, "source": source,
                            "breaches": list(breaches), "tick": tick}
        if self.hold is not None and not breaches:
            self.hold.later_report = dict(self.last_report)

    def close_hold(self) -> WeatherHold | None:
        hold, self.hold = self.hold, None
        return hold

    # ---------- incidents ----------

    def incident_record(self, record_id: str, report: IncidentReport, text: str, source: str,
                        until_tick: int, held: bool) -> NoticeRecord:
        """An incident as a notice record. From there it follows NoticeBook's path — recall,
        refusal, no landing, colouring on the screen."""
        volume = Volume(
            id=record_id, name=report.name, polygon=report.polygon(), floor_m=0.0,
            ceiling_m=None, reference="AGL", rule="forbidden", reason=report.name, source=source,
            tags={"text": text, "incident": report.kind, "place": report.place,
                  "centre": [round(report.centre[0], 6), round(report.centre[1], 6)],
                  "radius_m": round(report.radius_m, 1), "building_id": report.building_id},
            from_tick=report.from_tick, until_tick=until_tick,
        )
        self.notice_ids.add(record_id)
        return NoticeRecord(record_id, report.name, "incident", text, volume, report.from_tick,
                            until_tick, source, held=held)

    def window_until(self, from_tick: int | None, until_tick: int | None, item: dict,
                     tick: int) -> int:
        """The text's window → the item's until_tick hint (ignored unless a number) → the
        default length. Capped by horizon."""
        hinted = hint_number(item.get("until_tick"), float("nan"))
        if until_tick is not None:
            chosen = int(until_tick)
        elif hinted == hinted:
            chosen = int(hinted)
        else:
            chosen = max(tick, from_tick or tick) + int(self.limits.hold_default_ticks)
        return min(chosen, self.horizon(tick))

    # ---------- when the round changes ----------

    def clear(self) -> None:
        self.records.clear()
        self.hold = None
        self.last_report = None
        self.held_weather.clear()
        self.notice_ids.clear()

    # ---------- screen ----------

    def snapshot(self, tavily_on: bool) -> dict:
        recent = sorted(self.records.values(), key=lambda r: r.tick)[-KEPT_ITEMS:]
        tavily = "off" if not tavily_on else "failed" if self.source_failed else "enabled"
        return {
            "sources": {"tavily": tavily, "sim": True},
            "last_fetch_tick": self.last_fetch_tick,
            "fetch": self.fetch,
            "items_read": self.items_read, "items_unreadable": self.items_unreadable,
            "items": [r.to_dict() for r in recent],
        }

    def weather_snapshot(self) -> dict:
        return {
            "hold": None if self.hold is None else self.hold.to_dict(),
            "last_report": self.last_report,
            "held": list(self.held_weather.values()),
        }


# Official observations. Like the runtime's own feed (simulator notices), a grammar reading
# applies on that tick — code turned aviationweather.gov's numbers into text and the grammar
# read it back; it is not a web page. A model reading still waits for human approval,
# whatever the source.
OFFICIAL_SOURCES = frozenset({METAR_SOURCE})


class TowerIntake(IntakeBook):
    """The runtime's intake book.

    Adds one thing: official observations (METAR) are trusted like the runtime's own feed.
    """

    @staticmethod
    def must_hold(record: IntakeRecord, read_by: str) -> bool:
        if record.source in OFFICIAL_SOURCES:
            return read_by.startswith("model:")
        return IntakeBook.must_hold(record, read_by)

    def snapshot(self, tavily_on: bool) -> dict:
        out = super().snapshot(tavily_on)
        for item in out["items"]:
            if item["source"] in OFFICIAL_SOURCES:
                item["trusted"] = True
        return out


def incident_snapshot(records: list[NoticeRecord], tick: int) -> list[dict]:
    """/state.incidents. Incident notices only, with centre and radius — what the screen banner
    and the approval cards read.

    Closed windows are left out (a grammar-read notice record stays in the book after its window
    closes)."""
    out = []
    for record in records:
        if record.kind != "incident":
            continue
        if record.until_tick is not None and tick > record.until_tick:
            continue
        tags = record.volume.tags or {}
        out.append({"id": record.id, "name": record.name, "kind": tags.get("incident"),
                    "place": tags.get("place"), "centre": tags.get("centre"),
                    "radius_m": tags.get("radius_m"), "from_tick": record.from_tick,
                    "until_tick": record.until_tick, "source": record.source,
                    "applied": record.applied, "held": record.held,
                    "confirmed_by": record.confirmed_by, "text": record.text[:TEXT_CHARS]})
    return out
