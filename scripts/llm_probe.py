"""Is the model reachable, and does each tier answer in the shape we need?

    LLM_BASE_URL=http://localhost:11434/v1 NEBIUS_API_KEY=ollama \\
    MODEL_NANO=nemotron-3-nano MODEL_SUPER=nemotron-3-nano MODEL_ULTRA=nemotron-3-nano \\
    LLM_REQUEST_EXTRA='{"reasoning_effort":"none"}' PYTHONPATH=. python3 scripts/llm_probe.py

Lists the nemotron models the server knows, then asks one form per tier and prints the
model id, which field the answer came from, the latency, and the parsed JSON.
Exits 1 if any tier returns nothing usable — that is the tier the runtime would be
running on rules for.
"""

import json
import os
import sys
import urllib.error
import urllib.request

from backend.runtime.arbiter import SYSTEM as ARBITER_SYSTEM
from backend.runtime.arbiter import parse_verdict
from drone.agent.detect import Concern
from drone.agent.propose import Proposer, system_for
from shared.llm.client import LlmTier, TieredLlm, parse_json_object


def list_models(base_url: str, api_key: str) -> list[str]:
    request = urllib.request.Request(f"{base_url.rstrip('/')}/models")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        print(f"GET /models failed: {error}")
        return []
    ids = [item.get("id", "") for item in payload.get("data", [])]
    return [name for name in ids if "nemotron" in name.lower()]


def main() -> int:
    llm = TieredLlm(models={
        "nano": os.getenv("MODEL_NANO", ""),
        "super": os.getenv("MODEL_SUPER", ""),
        "ultra": os.getenv("MODEL_ULTRA", ""),
    })
    if not llm.enabled:
        print("LLM_BASE_URL and MODEL_* are required (see .env.local.example)")
        return 1
    print(f"server {llm.base_url} ({llm.host}), timeout {llm.timeout_s:.0f}s, "
          f"extra {json.dumps(llm.request_extra)}")
    print("nemotron models listed:", ", ".join(list_models(llm.base_url, llm.api_key)) or "(none)")

    concern = Concern(kind="needs_route", urgency="normal",
                      detail="delivering to Union Square, battery 88%")
    telemetry = {"id": "drone-01", "model": "dv-x500", "state": "ready", "battery": 88.0,
                 "vibration": 0.1, "autonomy_health": 1.0, "passengers": 0, "cargo": 6}
    form_user = Proposer._brief(concern, telemetry, "pad:launch")
    arbiter_user = ("Resource: pad:launch\n"
                    "1. asset=drone-01 action=reserve_pad impact=schedule battery=40% passengers=0 "
                    "why=maintenance check\n"
                    "2. asset=drone-02 action=reserve_pad impact=cargo battery=9% passengers=0 "
                    "why=battery 9%")

    failed = []
    for tier in LlmTier:
        model = llm.model_for(tier)
        if not model:
            print(f"[{tier.value}] no model")
            failed.append(tier.value)
            continue
        if tier is LlmTier.ULTRA:
            reply = llm.ask(tier, ARBITER_SYSTEM, arbiter_user, max_tokens=160, json_object=True)
            parsed = parse_verdict(reply.text, 2) if reply else None
        else:
            reply = llm.ask(tier, system_for(("pad:launch",)), form_user, max_tokens=160,
                            json_object=True)
            parsed = parse_json_object(reply.text) if reply else None
        if reply is None:
            print(f"[{tier.value}] {model}: no answer (timeout or error)")
            failed.append(tier.value)
            continue
        print(f"[{tier.value}] {model} via={reply.via} {reply.latency_ms}ms "
              f"parsed={json.dumps(parsed, ensure_ascii=False)}")
        if parsed is None:
            print(f"    raw: {reply.text[:200]!r}")
            failed.append(tier.value)
    if failed:
        print(f"tiers with no usable answer: {', '.join(failed)} — rules stand in for them")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
