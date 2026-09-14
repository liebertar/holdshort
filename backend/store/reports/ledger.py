"""A flight-by-flight account of what the ledger says, built from the ledger alone.

The ledger is append-only and one filing leaves several lines: an open line, a close line,
and — when the operator re-files the same request after a refusal — another pair. This
module folds those lines back into flights (one per filed request) with the refusals it
took, the approval it got, and what happened to the intent afterwards (conformance,
recall, withdrawal). It reads nothing but the lines: if the report cannot be built from the
ledger, the ledger is not doing its job.
"""

ROUTED = ("reserve_pad", "fly_route")
REFUSAL_KEYS = ("blocked_kind", "blocked_volume", "blocked_asset", "blocked_until_tick")
# Fleet-wide rules. They are the runtime's lines, not an aircraft's, so they fold into the
# fleet section instead of under a flight.
HOLD_CODES = ("weather_hold", "weather_hold_lifted", "weather_hold_expired", "weather_hold_closed")
# One lost link = the lost line + (the answer to the human card) + the restored line. It is
# about the link, not a flight, so it goes in the fleet section.
LINK_ACTIONS = ("link_lost", "link_restored", "lost_link_notice")


def build_report(entries: list[dict], tick: int, airspace_revision: int,
                 asset: str | None = None) -> dict:
    """Ledger lines → {generated_tick, airspace_revision,
    assets:[{asset, flights, advisories}], fleet:{weather_holds, incidents}}."""
    closed = [e for e in entries if e.get("outcome") != "pending"]
    flights: dict[str, dict] = {}
    order: list[str] = []
    followups: dict[str, dict] = {}       # intent id → {conformance: [...], recalled, withdrawn}
    advisories: dict[str, list[dict]] = {}
    holds: list[dict] = []
    incidents: list[dict] = []
    links: list[dict] = []

    for entry in closed:
        proposal = entry.get("proposal") or {}
        decision = entry.get("decision") or {}
        context = entry.get("context") or {}
        who = proposal.get("asset_id", "")
        action = proposal.get("action", "")
        code = decision.get("code")
        if code in HOLD_CODES:
            _fold_hold(holds, entry, proposal, decision, context)
            continue
        if code == "incident_keepout":
            detail = decision.get("detail") or {}
            incidents.append({"id": detail.get("incident"), "name": detail.get("name"),
                              "tick": context.get("tick"), "until_tick": detail.get("until_tick"),
                              "radius_m": detail.get("radius_m"), "source": detail.get("source"),
                              "ledger_id": entry.get("id")})
            continue
        if action in LINK_ACTIONS:
            _fold_link(links, entry, proposal, decision, context)
            continue
        if action in ROUTED:
            key = proposal.get("id") or entry.get("id")
            if key not in flights:
                order.append(key)
                flights[key] = _new_flight(who, proposal, context)
            _fold(flights[key], entry, proposal, decision, context)
        elif action == "conformance":
            params = proposal.get("params") or {}
            intent = params.get("intent") or context.get("intent_id")
            # Two kinds of conformance check: taking off before the deferred departure
            # (departure), and leaving the approved volume while the link was down (lost_link).
            followups.setdefault(intent, {}).setdefault("conformance", []).append({
                "tick": context.get("tick"), "kind": params.get("kind") or "departure",
                "planned_depart_tick": params.get("planned_depart_tick"),
                "actual_depart_tick": params.get("actual_depart_tick"),
                "ledger_id": entry.get("id")})
        elif decision.get("code") in ("recalled", "withdrawn") and context.get("intent_id"):
            followups.setdefault(context["intent_id"], {})[decision["code"]] = {
                "tick": context.get("tick"), "code": decision.get("code"),
                "policy": decision.get("policy_hit"),
                "for": (decision.get("detail") or {}).get("for"),
                "outcome": entry.get("outcome"), "ledger_id": entry.get("id")}
        elif action == "advisory":
            params = proposal.get("params") or {}
            advisories.setdefault(who, []).append({
                "tick": context.get("tick"), "trigger": params.get("trigger"),
                "chosen": params.get("chosen"), "source": params.get("source"),
                "model": params.get("model"), "summary": params.get("summary"),
                "refusals": len(params.get("refusals") or []),
                "options": [{"id": o.get("id"), "legal": o.get("legal")}
                            for o in params.get("options") or []],
                "ledger_id": entry.get("id")})

    for flight in flights.values():
        after = followups.get(flight["intent"] or "", {})
        flight["conformance"] = after.get("conformance", [])
        flight["recalled"] = after.get("recalled")
        flight["withdrawn"] = after.get("withdrawn")

    by_asset: dict[str, list[dict]] = {}
    for key in order:
        flight = flights[key]
        by_asset.setdefault(flight.pop("asset"), []).append(flight)
    names = sorted(set(by_asset) | set(advisories))
    if asset is not None:
        names = [n for n in names if n == asset]
    return {
        "generated_tick": tick, "airspace_revision": airspace_revision,
        "assets": [{"asset": name,
                    "flights": sorted(by_asset.get(name, []),
                                      key=lambda f: (f["filed_tick"] or 0, f["filed_at"] or 0)),
                    "advisories": advisories.get(name, [])}
                   for name in names],
        "fleet": {"weather_holds": holds, "incidents": incidents, "links": links},
    }


def _fold_link(links: list[dict], entry: dict, proposal: dict, decision: dict,
               context: dict) -> None:
    """One lost link = lost line + (the human's answer) + restored line. Folds into the same
    aircraft's last open case."""
    asset = proposal.get("asset_id")
    detail = decision.get("detail") or {}
    action = proposal.get("action")
    if action == "link_lost":
        links.append({"asset": asset, "since_tick": detail.get("since_tick"),
                      "declared_tick": context.get("tick"), "intent": detail.get("intent"),
                      "behaviour": detail.get("behaviour"),
                      "reserved_until_tick": detail.get("reserved_until_tick"),
                      "released": None, "kept_by": None, "restored_tick": None,
                      "conforming": None, "ledger_id": entry.get("id")})
        return
    standing = next((link for link in reversed(links) if link["asset"] == asset), None)
    if standing is None:
        return
    code = decision.get("code")
    if code == "lost_link_released":
        standing["released"] = {"tick": context.get("tick"), "by": decision.get("approved_by")}
    elif code == "lost_link_kept":
        standing["kept_by"] = decision.get("approved_by")
    elif action == "link_restored":
        standing["restored_tick"] = detail.get("restored_tick")
        standing["conforming"] = detail.get("conforming")


def _fold_hold(holds: list[dict], entry: dict, proposal: dict, decision: dict,
               context: dict) -> None:
    """One weather hold = the opening line + (lifted line | window-closed line), folded
    together under the same hold id."""
    params = proposal.get("params") or {}
    detail = decision.get("detail") or {}
    hold = params.get("hold") if isinstance(params.get("hold"), dict) else {}
    hold_id = hold.get("id") or params.get("hold") or detail.get("hold")
    code = decision.get("code")
    if code == "weather_hold":
        holds.append({"id": hold_id, "opened_tick": context.get("tick"),
                      "reason": hold.get("reason") or decision.get("reason"),
                      "until_tick": detail.get("until_tick"), "source": detail.get("source"),
                      "breaches": list(detail.get("breaches") or []), "lifted": None,
                      "expired_tick": None, "ledger_id": entry.get("id")})
        return
    standing = next((h for h in reversed(holds) if h["id"] == hold_id), None)
    if standing is None:
        return
    if code == "weather_hold_lifted":
        standing["lifted"] = {"tick": context.get("tick"), "by": decision.get("approved_by"),
                              "ledger_id": entry.get("id")}
    else:
        # The window closed or the round changed: a hold that ended without a human comes here.
        standing["expired_tick"] = context.get("tick")
        standing["closed_by"] = "round" if code == "weather_hold_closed" else "window"


def _new_flight(asset: str, proposal: dict, context: dict) -> dict:
    return {"asset": asset, "proposal": proposal.get("id"), "intent": None,
            "action": proposal.get("action"), "filed_at": proposal.get("filed_at"),
            "filed_tick": context.get("tick"), "author": proposal.get("author"),
            "drafter": None, "draft_attempts": None, "checks_run": [],
            "refusals": [], "duplicates": 0, "approved": None, "failed": None,
            "conformance": [], "recalled": None, "withdrawn": None}


def _fold(flight: dict, entry: dict, proposal: dict, decision: dict, context: dict) -> None:
    """Folds one line into its flight. The last line says who drew it (straight → rewritten)."""
    params = proposal.get("params") or {}
    if params.get("drafter") is not None:
        flight["drafter"] = params.get("drafter")
    if params.get("draft_attempts") is not None:
        flight["draft_attempts"] = params.get("draft_attempts")
    if context.get("checks_run"):
        flight["checks_run"] = list(context["checks_run"])
    if decision.get("verdict") == "denied":
        if decision.get("code") == "duplicate":
            # Not a blocked path but the same filing sent twice. Counted separately, by the
            # same standard the advisory uses for consecutive refusals.
            flight["duplicates"] += 1
            return
        flight["refusals"].append({"tick": context.get("tick"), "code": decision.get("code"),
                                   "policy_hit": decision.get("policy_hit"),
                                   **{k: params.get(k) for k in REFUSAL_KEYS},
                                   "ledger_id": entry.get("id")})
        return
    if str(entry.get("outcome") or "").startswith("failed"):
        # Approved, but the autopilot could not do it (e.g. charging while not on a pad).
        # Neither a refusal nor an approval.
        flight["failed"] = {"tick": context.get("tick"), "outcome": entry.get("outcome"),
                            "ledger_id": entry.get("id")}
        return
    if decision.get("committed") or entry.get("outcome") == "done":
        flight["approved"] = {"tick": context.get("tick"), "code": decision.get("code"),
                              "resolution": params.get("resolution"),
                              "holding_for": params.get("holding_for"),
                              "altitude_shift_m": params.get("altitude_shift_m"),
                              "approved_by": decision.get("approved_by"),
                              "outcome": entry.get("outcome"), "ledger_id": entry.get("id")}
        flight["intent"] = context.get("intent_id") or flight["intent"]


def to_markdown(report: dict) -> str:
    """A table for people to read. One flight per row."""
    lines = [f"# Ledger report — tick {report['generated_tick']}, "
             f"airspace revision {report['airspace_revision']}", "",
             "| asset | flight | filed | author | drafter | tries | checks | refusals | dup "
             "| approved | resolution | conformance | recalled | withdrawn |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for block in report["assets"]:
        for f in block["flights"]:
            refusals = "; ".join(_refusal_word(r) for r in f["refusals"]) or "—"
            approved = f["approved"]
            approved_word = f"tick {approved['tick']} ({approved['code']})" if approved else "—"
            if not approved and f.get("failed"):
                approved_word = f"tick {f['failed']['tick']} ({_cell(f['failed']['outcome'])})"
            resolution = "—"
            if approved and approved.get("resolution"):
                resolution = approved["resolution"]
                if approved.get("holding_for"):
                    resolution += f" ({approved['holding_for']})"
                if approved.get("altitude_shift_m"):
                    resolution += f" (+{approved['altitude_shift_m']:.0f} m)"
            lines.append(
                f"| {block['asset']} | {f['action']} {f['proposal']} | tick {f['filed_tick']} "
                f"| {f['author']} | {f['drafter'] or '—'} | {f['draft_attempts'] or 0} "
                f"| {','.join(f['checks_run']) or '—'} | {_cell(refusals)} "
                f"| {f['duplicates'] or '—'} | {approved_word} "
                f"| {resolution} | {len(f['conformance']) or '—'} "
                f"| {_after_word(f['recalled'])} | {_after_word(f['withdrawn'])} |")
    fleet = report.get("fleet") or {}
    if fleet.get("weather_holds") or fleet.get("incidents") or fleet.get("links"):
        lines += ["", "## Fleet", ""]
        for h in fleet.get("weather_holds") or []:
            ended = (f"lifted tick {h['lifted']['tick']} by {h['lifted']['by']}" if h.get("lifted")
                     else f"closed tick {h['expired_tick']} (round changed)"
                     if h.get("closed_by") == "round"
                     else f"expired tick {h['expired_tick']}" if h.get("expired_tick") else "open")
            lines.append(f"- weather hold · tick {h['opened_tick']} · {_cell(h['reason'])} "
                         f"· until tick {h['until_tick']} · {h['source']} · {ended}")
        for i in fleet.get("incidents") or []:
            lines.append(f"- incident · tick {i['tick']} · {_cell(i['name'])} · {i['radius_m']} m "
                         f"· until tick {i['until_tick']} · {i['source']}")
        lines += [_link_word(link) for link in fleet.get("links") or []]
    lines += _intake_lines(report.get("intake"))
    advisories = [(block["asset"], a) for block in report["assets"] for a in block["advisories"]]
    if advisories:
        lines += ["", "## Advisories", "", "| asset | tick | trigger | chosen | source | summary |",
                  "|---|---|---|---|---|---|"]
        for asset, a in advisories:
            lines.append(f"| {asset} | {a['tick']} | {a['trigger']} | {a['chosen']} "
                         f"| {a['source']} | {_cell(a['summary'])} |")
    return "\n".join(lines) + "\n"


def _link_word(link: dict) -> str:
    """One lost link on one line, including whether it conformed if the link came back."""
    if link.get("restored_tick") is None:
        ended = "still dark"
    else:
        ended = (f"restored tick {link['restored_tick']} · "
                 + ("conforming" if link.get("conforming") else "NONCONFORMING"))
    released = (f" · released tick {link['released']['tick']} by {link['released']['by']}"
                if link.get("released") else "")
    return (f"- lost link · {link['asset']} · since tick {link['since_tick']} "
            f"· {link['behaviour']} · reserved until tick {link['reserved_until_tick']}"
            f"{released} · {ended}")


def _intake_lines(intake: dict | None) -> list[str]:
    """What came in (sqlite). The ledger records decisions; this section records what was
    taken in and what became of it."""
    if not intake or not (intake.get("items") or intake.get("rules")):
        return []
    lines = ["", "## Intake", "", "| item | source | kind | tick | read by | outcome |",
             "|---|---|---|---|---|---|"]
    for item in intake.get("items") or []:
        lines.append(f"| {_cell(item['id'])} | {item['source']} | {item['kind'] or '—'} "
                     f"| {item['fetched_tick']} | {item['read_by'] or '—'} "
                     f"| {item['outcome'] or '—'} |")
    if intake.get("rules"):
        lines += ["", "| rule | item | kind | from | until | applied | lifted by |",
                  "|---|---|---|---|---|---|---|"]
        for rule in intake["rules"]:
            lines.append(f"| {rule['id']} | {_cell(rule['item_id'])} | {rule['kind']} "
                         f"| {rule['from_tick']} | {rule['until_tick']} "
                         f"| {'yes' if rule['applied'] else 'no'} | {rule['lifted_by'] or '—'} |")
    return lines


def _cell(text) -> str:
    """One table cell. A '|' in a model-written summary adds a cell and a newline breaks the
    row, so both are stripped."""
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|")


def _refusal_word(r: dict) -> str:
    what = r.get("blocked_kind") or r.get("policy_hit") or r.get("code") or "?"
    who = r.get("blocked_asset") or r.get("blocked_volume")
    return f"t{r.get('tick')} {what}" + (f":{who}" if who else "")


def _after_word(item: dict | None) -> str:
    if not item:
        return "—"
    return f"tick {item.get('tick')}" + (f" ({item['policy']})" if item.get("policy") else "")
