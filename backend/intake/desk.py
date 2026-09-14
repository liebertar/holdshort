"""The world thread's one call into intake, and the path one item takes.

submit_intake keeps its validator rather than being half-moved to the api layer: it is
nineteen lines, it calls take_in, and moving it would point the package arrow backwards.
"""

import threading

from backend.intake.book import item_id


class IntakeDeskMixin:
    def absorb(self, bulletins: list[dict]) -> None:
        """Take notices in as rules. A zone notice is prose: read it, add it to the airspace.

        Blocking only the resource and leaving the airspace as is keeps clearing routes through
        the closed zone; that is why routes into a zone closed over the base kept passing.
        Notices the grammar reads apply on that tick. Notices the model reads are held until a
        human approves them. Once a notice expires or drops off the feed, it leaves the
        judgement too.
        """
        items = [i for i in bulletins if i.get("kind") in ("recall", "zone", "notam")]
        self._collect_read_notices()
        known = {p.id for p in self.policies.all()}
        bbox = self.service_bbox()
        for item in items:
            if item["id"] in known:
                continue
            if item.get("kind") == "recall":
                self._enforce_policy(item)
                continue
            if self.notices.known(item["id"]) or item["id"] in self._reading:
                continue
            if self.notice_async and self.notices.needs_model(item):
                self._read_later(item, bbox)
                continue
            self._settle_notice(item, self.notices.read(item, bbox))
        # Intake. Weather and incident bulletins take the same path as search results and manual
        # input. An incident becomes a notice record that _apply_notices below applies in the
        # same poll.
        self._collect_read_intake()
        self._note_fetch()
        self._note_metar_fetch()
        self._take_intake([i for i in bulletins if _is_intake(i)] + self._drain_intake_inbox(),
                          bbox)
        # Pre-flight briefing. Turn finished runs into rules (into the notice book; _apply_notices
        # below applies them in the same poll), and have the worker thread ask again if the
        # round changed or new neighbourhoods have piled up.
        self.briefing.poll(bbox)
        self._apply_notices({i["id"] for i in items} | self.intake.notice_ids)
        self._tick_intake()

    def submit_intake(self, body: dict):
        """POST /intake. One sentence typed by a person; queue it in the inbox, return its id.

        The id starts with manual-. If the caller reused a simulator notice's id, that notice
        would count as 'already seen' and the real gust report would go unread. Hints
        (radius_m, until_tick) are checked for being numbers here: blowing up on the world
        thread would lose the rest of that poll's items too."""
        text = " ".join(str(body.get("text") or "").split())[:2000]
        if not text:
            return 400, {"error": "text is empty"}
        hints, problem = _intake_hints(body)
        if problem:
            return 400, {"error": problem}
        kind = body.get("kind") if body.get("kind") in ("weather", "incident", "notam") else None
        given = "".join(str(body.get("id") or "").split())[:80]
        item = {"id": f"manual-{given}" if given else item_id({"text": text, "source": "manual"}),
                "kind": kind, "text": text, "source": "manual", **hints}
        self.take_in([item])
        return 200, {"ok": True, "id": item["id"], "queued": True}

    def _drain_intake_inbox(self) -> list[dict]:
        with self._guard:
            arrived, self._intake_inbox = self._intake_inbox, []
        return arrived

    def _take_intake(self, items: list[dict], bbox) -> None:
        """Once per item: record it (intake_received), then read it. The grammar reads now,
        the model on another thread."""
        for item in items:
            key = item_id(item)
            if not self.intake.known(key) and self.store.seen(key, str(item.get("source") or "")):
                # Search result or manual input already read before the restart; don't raise
                # its human card again.
                continue
            record = self.intake.receive(item, self.tick)
            if record is None:
                continue        # already seen: neither read nor recorded again
            received = ("was waiting for a human before the restart — the card goes back up"
                        if item.get("reopened") else f"received from {record.source}")
            self._ledger_intake(record, item, "intake_received", "noted", received,
                                {"kind_hint": item.get("kind"),
                                 **({"reopened": True} if item.get("reopened") else {})})
            if self.intake_async and self.intake.needs_model(item):
                self._read_intake_later(item, record, bbox)
                continue
            try:
                result = self.intake.compile_item(item, bbox)
            except Exception as error:  # noqa: BLE001 — one item must not stop the poll
                result = (None, "", f"read failed {error!r}")
            self._settle_intake(item, record, result)

    def _read_intake_later(self, item: dict, record, bbox) -> None:
        """The model reads prose outside the grammar, off the world thread (for the same reason
        as _read_later)."""
        self._reading_intake.add(record.id)
        round_at = self._round

        def work() -> None:
            try:
                result = self.intake.compile_item(item, bbox)
            except Exception as error:  # noqa: BLE001 — recorded as unreadable
                result = (None, "", f"model read failed {error!r}")
            with self._guard:
                self._read_intake.append((round_at, item, record, result))

        threading.Thread(target=work, daemon=True, name=f"intake-{record.id}").start()

    def _collect_read_intake(self) -> None:
        """Record reader-thread results on the world thread; drop answers from a past round."""
        with self._guard:
            arrived, self._read_intake = self._read_intake, []
        for round_at, item, record, result in arrived:
            self._reading_intake.discard(record.id)
            if round_at != self._round or self.intake.records.get(record.id) is not record:
                continue
            self._settle_intake(item, record, result)

    def _settle_intake(self, item: dict, record, result: tuple) -> None:
        """Record and apply what was read. If one item blows up, the poll carries on and the
        item is left as unreadable."""
        compiled, read_by, why = result
        self.intake.settle(record, compiled, read_by, why)
        if compiled is None:
            self._ledger_intake(record, item, "intake_unreadable", "unreadable",
                                f"could not read it — {record.why}", {"why": record.why,
                                                                "read_by": read_by})
            return
        try:
            self._apply_intake(item, record, compiled, read_by)
        except Exception as error:  # noqa: BLE001 — the item broke, not the runtime
            self.intake.settle(record, None, read_by, f"apply failed {error!r}")
            self._ledger_intake(record, item, "intake_unreadable", "unreadable",
                                f"read but could not apply — {record.why}",
                                {"why": record.why, "read_by": read_by})

    def _apply_intake(self, item: dict, record, compiled, read_by: str) -> None:
        if compiled.kind == "none":
            self._ledger_intake(record, item, "intake_read", "noted",
                                "nothing to do with the fleet",
                                {"kind": "none", "read_by": read_by})
        elif compiled.kind == "weather":
            self._take_weather(item, record, compiled.weather, read_by)
        elif compiled.kind == "incident":
            self._take_incident(item, record, compiled.incident, read_by)
        elif compiled.kind == "notice":
            self._take_notice(item, record, compiled.notice, read_by)


def _is_intake(item: dict) -> bool:
    """What intake reads from the notice list: weather, incidents, and bare prose with no kind."""
    kind = item.get("kind")
    if kind in ("weather", "incident"):
        return True
    return kind not in ("recall", "zone", "notam") and bool(str(item.get("text") or "").strip())


def _intake_hints(body: dict) -> tuple[dict, str]:
    """Structured values of POST /intake. A non-number where a number is due gets a 400; free
    text is not taken as is."""
    hints = {}
    for key in ("name", "address", "building_id"):
        if body.get(key) is not None:
            hints[key] = " ".join(str(body[key]).split())[:120]
    try:
        if body.get("radius_m") is not None:
            hints["radius_m"] = float(body["radius_m"])
        if body.get("until_tick") is not None:
            hints["until_tick"] = int(body["until_tick"])
    except (TypeError, ValueError):
        return {}, "radius_m and until_tick must be numbers"
    return hints, ""
