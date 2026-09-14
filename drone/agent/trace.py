"""What the map's hover card says about who wrote a filing, and how.

Every proposal an agent files carries params.model_trace: which model wrote the form (or that
the rules did, and why), and where the route came from — the straight line, a candidate the
model chose, a candidate the rules chose, or a model draft. It is a label for people. The
runtime never reads it (the judge sees the same legs whoever drew them), and it stays under
1 KB so a ledger line does not grow with it.

    {"form": {"model", "concern", "action", "rationale", "latency_ms", "used", "fallback_reason"},
     "route": {"source": "straight"|"choice"|"draft"|"astar",
               "choice": {"candidates", "chosen", "reason"} | null,
               "draft": {"asked", "latency_ms", "breach", "used"} | null} | null}
"""

import json

TRACE_BYTES = 1024
# Over 1 KB, text is shrunk to each of these lengths in turn. Keys stay as they are — the
# screen relies on them.
SHRINK_STEPS = (160, 100, 60, 30, 0)
ROUTE_SOURCES = ("straight", "choice", "draft", "astar")


def concern_words(concern, telemetry: dict) -> str:
    """The concern in human words, e.g. "has a delivery to Morningside Park, no cleared route"."""
    kind = getattr(concern, "kind", "")
    job = str(telemetry.get("job") or "")
    if kind == "needs_route":
        if job == "Warehouse":
            return "heading back to the warehouse, no cleared route"
        return f"has a delivery to {job or 'its next stop'}, no cleared route"
    if kind == "needs_reload":
        return "at its warehouse seat, ready to load the next parcels"
    if kind == "motor_fault":
        return f"motor vibration {float(telemetry.get('vibration') or 0.0):.2f}, needs a pad"
    if kind == "needs_pad":
        return "needs a pad to set down"
    if kind == "autonomy_fault":
        return f"autonomy health {float(telemetry.get('autonomy_health') or 0.0):.2f}"
    return str(getattr(concern, "detail", "") or kind)[:SHRINK_STEPS[0]]


def form_part(model: str, concern: str, action: str, rationale: str, latency_ms: int,
              used: bool, fallback_reason: str | None) -> dict:
    """Who wrote the filing. If the rules did, model is "" and fallback_reason says why."""
    return {"model": model if used else "", "concern": str(concern or ""),
            "action": str(action or ""), "rationale": str(rationale or ""),
            "latency_ms": int(latency_ms or 0), "used": bool(used),
            "fallback_reason": None if used else (fallback_reason or "rules")}


def choice_part(candidates: list[dict], chosen: str, reason: str) -> dict:
    """Which candidate was picked and why. Candidates are summarised (no coordinates) — the
    coordinates are in the filing's legs."""
    return {"candidates": [{"id": c.get("id"), "label": c.get("label"),
                            "length_m": c.get("length_m"), "max_alt_m": c.get("max_alt_m")}
                           for c in candidates],
            "chosen": chosen, "reason": str(reason or "")}


def draft_part(asked: bool, latency_ms: int, breach: str | None, used: bool) -> dict:
    """Whether a model draft (the last resort) was asked for, how long it took, what our
    pre-check caught, and whether it was used."""
    return {"asked": bool(asked), "latency_ms": int(latency_ms or 0),
            "breach": breach or None, "used": bool(used)}


def route_part(source: str, choice: dict | None = None, draft: dict | None = None) -> dict:
    if source not in ROUTE_SOURCES:
        raise ValueError(f"unknown route source {source!r}")
    return {"source": source, "choice": choice, "draft": draft}


def model_trace(form: dict | None, route: dict | None) -> dict:
    """The trace of one filing. Over 1 KB, the longest text shrinks first (bounded)."""
    return bounded({"form": dict(form) if form else None,
                    "route": json.loads(json.dumps(route)) if route else None})


def size_of(trace: dict) -> int:
    return len(json.dumps(trace, ensure_ascii=False).encode("utf-8"))


def bounded(trace: dict, limit: int = TRACE_BYTES) -> dict:
    """Fit within limit bytes. Text (reason, concern, breach) shrinks first; if still over,
    the candidate summaries are cut down to ids."""
    for cap in SHRINK_STEPS:
        if size_of(trace) <= limit:
            return trace
        trace = _shortened(trace, cap)
    choice = ((trace.get("route") or {}).get("choice") or {})
    if choice.get("candidates"):
        choice["candidates"] = [{"id": c.get("id")} for c in choice["candidates"]]
    return trace


def _shortened(trace: dict, cap: int) -> dict:
    form = trace.get("form")
    if form:
        for key in ("rationale", "concern", "fallback_reason"):
            if isinstance(form.get(key), str):
                form[key] = form[key][:cap]
    route = trace.get("route") or {}
    if route.get("choice") and isinstance(route["choice"].get("reason"), str):
        route["choice"]["reason"] = route["choice"]["reason"][:cap]
    if route.get("draft") and isinstance(route["draft"].get("breach"), str):
        route["draft"]["breach"] = route["draft"]["breach"][:cap] or None
    return trace
