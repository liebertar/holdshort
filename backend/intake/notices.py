"""Notices the runtime holds: what each one closes, from when, on whose word.

A notice comes in as text. The grammar reads the FAA dialect and the result applies the
tick it lands — a restriction never waits for anyone. Text the grammar cannot read goes to
the Super tier, which compiles it into the same schema; that result is validated hard and
then held until a person confirms it on the approval screen. A model can propose a closure
and can never impose one. The banner on the screen is drawn from this book, not from the
simulator: what is enforced is what is shown.
"""

import os
from dataclasses import dataclass, field

from shared.geo import Volume
from shared.llm.client import LlmTier, TieredLlm, parse_json_object
from shared.notam import (
    COMPILE_SYSTEM,
    Clock,
    Notice,
    from_model_form,
    parse_notice,
    shape_problems,
    validate,
)

# Seconds the model gets to structure one notice. With the runtime's default of 20 s, the local
# 30B stand-in could not read it in three rounds out of four (it took 5–27 s). Reading runs off
# the world thread (service._read_later), so a long read does not stop the tick, and a late
# answer is picked up by the next poll.
NOTICE_TIMEOUT_S = float(os.getenv("NOTICE_TIMEOUT_S", "60"))


@dataclass
class NoticeRecord:
    id: str
    name: str
    kind: str                       # notam | zone (the old structured form)
    text: str
    volume: Volume
    from_tick: int | None
    until_tick: int | None
    source: str                     # grammar | structured | model:<id> | human
    held: bool = False              # awaiting human approval; judgement ignores it meanwhile
    applied: bool = False           # is it in the airspace right now
    problems: list[str] = field(default_factory=list)
    confirmed_by: str | None = None

    def due(self, tick: int) -> bool:
        """Should it be in force now? Not while held; inside its window if it has one."""
        if self.held:
            return False
        if self.from_tick is not None and tick < self.from_tick:
            return False
        return self.until_tick is None or tick <= self.until_tick

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "from_tick": self.from_tick, "until_tick": self.until_tick,
            "source": self.source, "polygon": [[lat, lon] for lat, lon in self.volume.polygon],
            "floor_m": self.volume.floor_m, "ceiling_m": self.volume.ceiling_m,
            "text": self.text, "held": self.held, "applied": self.applied,
            "confirmed_by": self.confirmed_by,
        }


class NoticeBook:
    def __init__(self, clock: Clock, llm: TieredLlm | None = None):
        self.clock = clock
        self.llm = llm
        self.records: dict[str, NoticeRecord] = {}
        self.unreadable: dict[str, str] = {}     # id → why unreadable, so polls do not ask again
        # Refused by a person, kept apart from the sentence: the briefing asks this set, not the
        # words. A reason that merely contains "refused" must never read as a person's answer.
        self.refused: set[str] = set()

    def get(self, notice_id: str) -> NoticeRecord | None:
        return self.records.get(notice_id)

    def known(self, notice_id: str) -> bool:
        return notice_id in self.records or notice_id in self.unreadable

    @property
    def can_compile(self) -> bool:
        return (self.llm is not None and self.llm.enabled
                and bool(self.llm.model_for(LlmTier.SUPER)))

    def needs_model(self, item: dict) -> bool:
        """Is this a notice neither the grammar nor the structured form can read, so only a
        model can? (False without a model — then it is simply unreadable, and recorded so at
        once.)"""
        if item.get("polygon") or not self.can_compile:
            return False
        text = str(item.get("text") or "")
        return bool(text.strip()) and parse_notice(text, self.clock) is None

    def read(self, item: dict,
             bbox: tuple[float, float, float, float] | None) -> NoticeRecord | None:
        """One notice into a record. Grammar → applies at once, model → held, neither → None
        (unreadable)."""
        notice_id = str(item.get("id") or "")
        if not notice_id or self.known(notice_id):
            return self.records.get(notice_id)
        return self.settle(item, self.compile_item(item, bbox))

    def compile_item(self, item: dict, bbox: tuple[float, float, float, float] | None
                     ) -> tuple[NoticeRecord | None, str]:
        """Builds the record without storing it — safe to call from another thread. Returns
        (record, why unreadable).

        The model call lives here. The world thread only takes the result through settle().
        """
        notice_id = str(item.get("id") or "")
        name = str(item.get("name") or notice_id)
        if item.get("polygon"):
            # The old structured form (carries a polygon). Trusted like the grammar — it came
            # as data.
            problems = shape_problems(item["polygon"])
            if problems:
                return None, "; ".join(problems)
            # The posting tick is information, not a window — if it is on the list, it applies now.
            volume = Volume.from_dict({**item, "name": name, "from_tick": None})
            return NoticeRecord(notice_id, name, str(item.get("kind") or "zone"),
                                str(item.get("text") or ""), volume,
                                None, item.get("until_tick"), "structured"), ""

        text = str(item.get("text") or "")
        parsed = parse_notice(text, self.clock)
        if parsed is not None:
            return self._record(notice_id, name, item, parsed, "grammar"), ""

        compiled, model, problems = self.compile(text)
        if compiled is None:
            return None, "; ".join(problems) or "no answer from the model"
        problems = validate(compiled, bbox)
        if problems:
            return None, "; ".join(problems)
        return self._record(notice_id, name, item, compiled, f"model:{model}", held=True), ""

    def adopt(self, notice_id: str, name: str, item: dict, notice: Notice, source: str,
              held: bool = False) -> NoticeRecord:
        """Puts a notice another book (information intake) read into this one. From there it
        takes the same path — due/lapsed/held, recall and refusal, colouring on the screen."""
        record = self._record(notice_id, name, item, notice, source, held=held)
        self.records[notice_id] = record
        return record

    def settle(self, item: dict, result: tuple[NoticeRecord | None, str]) -> NoticeRecord | None:
        """Records compile_item's result; an unreadable one with its reason (so polls do not
        ask again)."""
        notice_id = str(item.get("id") or "")
        record, why = result
        if record is None:
            self.unreadable[notice_id] = why or "no answer from the model"
            return None
        self.records[notice_id] = record
        return record

    def _record(self, notice_id: str, name: str, item: dict, notice: Notice, source: str,
                held: bool = False) -> NoticeRecord:
        # If the text has a window, that window is the rule. Otherwise from now, when it
        # arrived, until the list's until_tick.
        from_tick = notice.from_tick
        until_tick = notice.until_tick if notice.until_tick is not None else item.get("until_tick")
        volume = Volume(
            id=notice_id, name=notice.name or name, polygon=list(notice.polygon),
            floor_m=notice.floor_m, ceiling_m=notice.ceiling_m, reference=notice.reference,
            rule="forbidden", reason=str(item.get("reason") or notice.text), source=source,
            tags={"text": notice.text}, from_tick=from_tick, until_tick=until_tick,
        )
        return NoticeRecord(notice_id, volume.name, str(item.get("kind") or "notam"), notice.text,
                            volume, from_tick, until_tick, source, held=held)

    def compile(self, text: str) -> tuple[Notice | None, str, list[str]]:
        """Text the grammar could not read goes to the model. Returns (notice, model id,
        problems). Without a model, it is unreadable."""
        if not text.strip():
            return None, "", ["empty text"]
        if not self.can_compile:
            return None, "", ["the grammar could not read it and there is no model to structure it"]
        reply = self.llm.ask(LlmTier.SUPER, COMPILE_SYSTEM,
                             f"Clock: tick 0 is {self.clock.epoch_z}Z, one tick is "
                             f"{self.clock.seconds_per_tick} s.\nNotice: {text}",
                             max_tokens=600, json_object=True, timeout_s=NOTICE_TIMEOUT_S)
        if reply is None:
            return None, "", ["no answer from the model"]
        form = parse_json_object(reply.text)
        notice = None if form is None else from_model_form(form, self.clock, text)
        if notice is None:
            self.llm.discard(LlmTier.SUPER)
            return None, reply.model, ["the model's answer is not in the form"]
        return notice, reply.model, []

    def confirm(self, notice_id: str, actor: str, allow: bool) -> NoticeRecord | None:
        """A human has looked. Approved: it applies from now on the human's word. Refused: only
        the record remains, and it never applies."""
        record = self.records.get(notice_id)
        if record is None or not record.held:
            return None
        record.confirmed_by = actor
        if allow:
            record.held = False
            record.source = "human"
            record.volume.source = "human"
        else:
            self.records.pop(notice_id)
            self.unreadable[notice_id] = f"{actor} refused"
            self.refused.add(notice_id)
        return record

    # The lists below iterate over a copy of records. If the world thread walks the same dict
    # while an approval (HTTP thread) deletes a record, the poll dies with "dictionary changed
    # size during iteration".

    def due(self, tick: int) -> list[NoticeRecord]:
        return [r for r in list(self.records.values()) if not r.applied and r.due(tick)]

    def lapsed(self, tick: int, feed_ids: set[str]) -> list[NoticeRecord]:
        """Those that must no longer apply: the window closed or they left the notice list."""
        return [r for r in list(self.records.values())
                if r.applied and (not r.due(tick) or r.id not in feed_ids)]

    def stale_held(self, tick: int, feed_ids: set[str]) -> list[NoticeRecord]:
        """Held, with nothing left to ask: the window closed or they left the notice list.

        Left alone, the approval card and the "waiting for a person" banner stay until the round
        ends, and a late confirmation applies nothing (due checks the window).
        """
        return [r for r in list(self.records.values())
                if r.held and self._over(r, tick, feed_ids)]

    def stale_confirmed(self, tick: int, feed_ids: set[str]) -> list[NoticeRecord]:
        """Confirmed by a human but passed without ever applying: the window ended before it came
        into force, or it dropped off the list.

        Left alone, it stays in /state.notices until the round ends, and with no grammar to ask
        again it has to be forgotten (grammar-read records are kept after the window closes —
        reading them again costs nothing).
        """
        return [r for r in list(self.records.values())
                if r.source == "human" and not r.held and not r.applied
                and self._over(r, tick, feed_ids)]

    @staticmethod
    def _over(record: NoticeRecord, tick: int, feed_ids: set[str]) -> bool:
        return ((record.until_tick is not None and tick > record.until_tick)
                or record.id not in feed_ids)

    def forget(self, notice_id: str, why: str | None = None) -> None:
        """Drops the record. Given a why, the same id arriving again is not sent to the model."""
        self.records.pop(notice_id, None)
        if why:
            self.unreadable[notice_id] = why

    def clear(self) -> None:
        self.records.clear()
        self.unreadable.clear()
        self.refused.clear()

    def snapshot(self) -> list[dict]:
        """Source of the screen banner: what applies or is scheduled, and what waits for a
        human.

        Held records are included too (held=True, applied=False). On held, the banner says
        "waiting for a person before it applies" — without them, a notice a model read would
        exist only on the approval screen and stay "not yet read by the runtime" on the map.
        applied says what is enforced; a held record blocks nothing.
        """
        return [r.to_dict() for r in list(self.records.values())]

    def pending(self) -> list[dict]:
        """Only those awaiting human approval — the same list as the approval screen."""
        return [r.to_dict() for r in list(self.records.values()) if r.held]
