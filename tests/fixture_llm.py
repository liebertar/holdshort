"""A model that answers from recorded replies. Offline, deterministic, and real.

`tests/fixtures/llm/*.json` holds replies the local Ollama Nemotron actually gave, promoted
from LLM_RECORD_DIR dumps with a `needle` added: a substring of the user prompt that says
which question the reply belongs to. Matching is by tier and needle, so a small change in
prompt wording does not orphan a fixture, and a fixture never answers a question it was
not recorded for — then the caller gets None and the rules take over, exactly as when the
server is down.

A recording keeps the wording of the day it was made, so the `brief` and `note` fields still
quote the Korean building names the runtime wrote back then. Only `needle` and `text` matter to
the replay, and both are English; leave the rest alone rather than edit a record after the fact.
"""

import json
import pathlib

from shared.llm.client import LlmReply, LlmTier, TieredLlm

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "llm"


def load_fixtures(folder: pathlib.Path = FIXTURE_DIR) -> list[dict]:
    records = []
    for path in sorted(folder.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if item.get("needle") and item.get("text") is not None:
                records.append({**item, "_file": path.name})
    return records


class FixtureLlm(TieredLlm):
    """Recorded replies, matched by tier and needle. No match returns None: the rules answer."""

    def __init__(self, records: list[dict] | None = None, model: str = "nemotron-3-nano",
                 tiers: tuple[str, ...] = ("nano", "super", "ultra")):
        super().__init__(base_url="http://fixture", api_key="fixture",
                         models={tier: model for tier in tiers}, timeout_s=0.0,
                         request_extra={}, record_dir="")
        self.records = load_fixtures() if records is None else list(records)
        self.asked: list[tuple[str, str]] = []     # (tier, user): what was asked, for the tests
        self.served: list[str] = []                # which fixture answered

    def ask(self, tier: LlmTier, system: str, user: str, max_tokens: int = 400,
            json_object: bool = False, timeout_s: float | None = None) -> LlmReply | None:
        self.asked.append((tier.value, user))
        haystack = system + "\n" + user
        for record in self.records:
            if record.get("tier", tier.value) != tier.value:
                continue
            if record["needle"] in haystack:
                self.served.append(record.get("_file", record["needle"]))
                reply = LlmReply(text=record["text"],
                                 model=record.get("model") or self.model_for(tier),
                                 latency_ms=int(record.get("latency_ms") or 0),
                                 via=record.get("via") or "content")
                self.account(tier, reply, reply.latency_ms)
                return reply
        self.account(tier, None, 0)
        return None
