"""OpenAI-compatible client with three Nemotron tiers.

The model never decides anything. It fills in a form (nano/super), drafts a route that a
judge will read (nano), chooses one of the operator planner's candidate routes by calling a
tool (nano), or returns one index from a list the runtime built (ultra).
Anything else is discarded and the caller falls back to rules. That is what keeps this
side of the system non-authoritative.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from shared.http import post_json_status


class LlmTier(str, Enum):
    NANO = "nano"      # something is off → writes the filing · a route is needed → drafts it
    SUPER = "super"    # the cause must be found → attaches the evidence
    ULTRA = "ultra"    # filings overlap → picks one of those that passed


# Without an answer the rules take over, so there's no reason to wait long. The runtime waits
# 20 s, the drone agent 6 s (loop.build_llm) — the refusal display lasts 5.6 s, and waiting
# longer than that makes the screen look frozen.
DEFAULT_TIMEOUT_S = 20.0

# Thinking models sometimes put <think>…</think> before the answer. JSON inside it is thought,
# not the answer, so it's dropped. With no closing tag the model was cut off mid-thought, and
# all of it is thought.
THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


@dataclass
class LlmReply:
    text: str
    model: str
    latency_ms: int = 0
    # Source field: content | think-stripped | reasoning_content | reasoning | tool_calls
    via: str = "content"
    # Answer as tool calls: [{"name", "arguments" (dict or None), "raw"}]; empty for text only.
    tool_calls: list = field(default_factory=list)


@dataclass
class TierStats:
    ok: int = 0            # answers received and used as is
    fallback: int = 0      # no answer, or discarded, so the rules took over
    last_ms: int | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "fallback": self.fallback, "last_ms": self.last_ms}


def strip_think(text: str) -> str:
    """Strip a leading <think>…</think> and keep only the answer. Opened but never closed
    means it's all thought."""
    if not text:
        return ""
    stripped = THINK_BLOCK.sub("", text, count=1)
    if stripped == text and text.lstrip().startswith("<think>"):
        return ""
    return stripped


def host_of(base_url: str) -> str:
    """Which server we're asking, for the one-line screen header and the logs."""
    url = (base_url or "").lower()
    if not url:
        return "none"
    # 11434 is the default server; 11435.. is the fleet, one per aircraft (scripts/ollama_fleet.sh).
    if re.search(r":1143\d(?!\d)", url) or "ollama" in url:
        return "ollama"
    if "nebius" in url:
        return "nebius"
    return "other"


def _extra_from_env() -> dict:
    """Per-server extra arguments (e.g. Ollama stops thinking only with reasoning_effort=none).

    Branching on server names in code means editing code whenever the server changes. One
    line of JSON comes from the environment and is merged into the request as is. Bad JSON is
    reported, not silently ignored.
    """
    raw = os.getenv("LLM_REQUEST_EXTRA", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"LLM_REQUEST_EXTRA is not JSON: {raw!r}", flush=True)
        return {}
    return parsed if isinstance(parsed, dict) else {}


class TieredLlm:
    def __init__(self, base_url: str = "", api_key: str = "", models: dict | None = None,
                 timeout_s: float | None = None, request_extra: dict | None = None,
                 record_dir: str | None = None):
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("NEBIUS_API_KEY", "")
        self.models = models or {}
        # compose passes an empty string even when unset. Empty means absent.
        self.timeout_s = float(os.getenv("LLM_TIMEOUT_S") or str(DEFAULT_TIMEOUT_S)
                               if timeout_s is None else timeout_s)
        self.request_extra = _extra_from_env() if request_extra is None else dict(request_extra)
        self.record_dir = os.getenv("LLM_RECORD_DIR", "") if record_dir is None else record_dir
        self.stats: dict[str, TierStats] = {tier.value: TierStats() for tier in LlmTier}
        # Servers that don't know response_format answer 400; once seen, it's never sent again.
        self._json_mode_ok = True
        # Likewise tools: once a server answers them with 400, they aren't sent again.
        # The caller asks with the JSON form instead.
        self._tools_ok = True
        self._recorded = 0
        # When the server was last unreachable (timeout, connection failure), monotonic;
        # cleared on an answer. Callers use it to decide whether to put another long question
        # to a server that just failed to answer.
        self.unreachable_at: float | None = None
        # Locks only the books (stats, record numbers), never the HTTP call — the drone agent's
        # main thread may ask for a filing while a worker thread asks for a route draft, both
        # on the same client. Unlocked, `ok += 1` updates overwrite each other and record file
        # numbers collide, so one call's record erases another's.
        self._books = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.models)

    @property
    def host(self) -> str:
        return host_of(self.base_url)

    def model_for(self, tier: LlmTier) -> str:
        return self.models.get(tier.value, "")

    @property
    def tools_ok(self) -> bool:
        """May tools be sent to this server? Stays False after one 400."""
        return self._tools_ok

    def ask(self, tier: LlmTier, system: str, user: str, max_tokens: int = 400,
            json_object: bool = False, timeout_s: float | None = None) -> LlmReply | None:
        """Ask once. timeout_s is this question's own budget (drafts take longer than filings)."""
        model = self.model_for(tier)
        if not self.enabled or not model:
            return None
        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        wants_json = json_object and self._json_mode_ok
        if wants_json:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.request_extra)

        started = time.monotonic()
        url = f"{self.base_url}/chat/completions"
        status, response = post_json_status(url, payload, timeout=budget, headers=headers)
        if wants_json and status == 400:
            # The server doesn't support forced JSON: retry once without the argument. A
            # timeout (0) isn't retried — it would only wait again.
            self._json_mode_ok = False
            retry = {k: v for k, v in payload.items() if k != "response_format"}
            status, response = post_json_status(url, retry, timeout=budget, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        reply = reply_from(response, model, latency_ms)
        self.unreachable_at = time.monotonic() if status == 0 else None
        self.account(tier, reply, latency_ms)
        self.record(tier, model, system, user, reply, latency_ms)
        return reply

    def ask_tools(self, tier: LlmTier, system: str, user: str, tools: list[dict],
                  tool_choice="required", max_tokens: int = 200,
                  timeout_s: float | None = None) -> LlmReply | None:
        """Ask once via tool calling (OpenAI-compatible tools/tool_choice). Called tools land
        in reply.tool_calls.

        This is the format the Nemotron card was trained on. The model can only call tools;
        what a tool does is up to the calling code — nothing is executed here.
        Some servers don't know 'required', so a 400 gets one retry with 'auto'. Another 400
        means the server doesn't know tools at all: tools_ok goes off and this returns None
        (not counted — the model didn't fail to answer, the server doesn't know the format).
        The caller asks again with JSON. A timeout (0) isn't retried.
        """
        model = self.model_for(tier)
        if not self.enabled or not model or not self._tools_ok:
            return None
        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        payload.update(self.request_extra)
        names = [str((tool.get("function") or {}).get("name")) for tool in tools]
        started = time.monotonic()
        url = f"{self.base_url}/chat/completions"
        status, response = post_json_status(url, payload, timeout=budget, headers=headers)
        if status == 400 and payload.get("tool_choice") != "auto":
            status, response = post_json_status(url, {**payload, "tool_choice": "auto"},
                                                timeout=budget, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        if status == 400:
            self._tools_ok = False
            self.record(tier, model, system, user, None, latency_ms,
                        extra={"tools": names, "status": 400})
            return None
        reply = tool_reply_from(response, model, latency_ms)
        self.unreachable_at = time.monotonic() if status == 0 else None
        self.account(tier, reply, latency_ms)
        self.record(tier, model, system, user, reply, latency_ms,
                    extra={"tools": names, "tool_choice": tool_choice})
        return reply

    def unreachable_within(self, seconds: float) -> bool:
        """Was the server unreachable in the last seconds? False if an answer came since."""
        return (self.unreachable_at is not None
                and time.monotonic() - self.unreachable_at < seconds)

    def discard(self, tier: LlmTier) -> None:
        """Received but not a valid form, so dropped. Counted as the rules taking over."""
        with self._books:
            stats = self.stats[tier.value]
            stats.ok = max(0, stats.ok - 1)
            stats.fallback += 1

    def account(self, tier: LlmTier, reply: LlmReply | None, latency_ms: int) -> None:
        with self._books:
            stats = self.stats[tier.value]
            stats.last_ms = latency_ms
            if reply is None:
                stats.fallback += 1
            else:
                stats.ok += 1

    def record(self, tier: LlmTier, model: str, system: str, user: str,
               reply: LlmReply | None, latency_ms: int, extra: dict | None = None) -> None:
        """With LLM_RECORD_DIR set, save each call to a file — raw material for test fixtures."""
        if not self.record_dir:
            return
        with self._books:
            self._recorded += 1
            serial = self._recorded
        payload = {
            "tier": tier.value, "model": model, "system": system, "user": user,
            "text": reply.text if reply else None, "via": reply.via if reply else None,
            "latency_ms": latency_ms, "at": time.time(),
            "tool_calls": reply.tool_calls if reply else None, **(extra or {}),
        }
        try:
            folder = Path(self.record_dir)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}-{os.getpid()}-{serial:04d}-{tier.value}.json"
            (folder / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except OSError as error:
            print(f"cannot write to LLM_RECORD_DIR: {error}", flush=True)

    def stats_dict(self) -> dict:
        return {tier: stats.to_dict() for tier, stats in self.stats.items()}


def reply_from(response: dict | None, model: str, latency_ms: int = 0) -> LlmReply | None:
    """Extract the answer: content first, then reasoning_content if empty, then reasoning.

    A thinking model's thoughts can eat all of max_tokens and leave content empty. If the
    thinking field holds the answer, use that — it goes through the form check and judgement
    again anyway.
    """
    if not response:
        return None
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    content = content if isinstance(content, str) else ""
    text, via = strip_think(content).strip(), "content"
    if text and text != content.strip():
        via = "think-stripped"
    if not text:
        for name in ("reasoning_content", "reasoning"):
            candidate = message.get(name)
            if isinstance(candidate, str) and candidate.strip():
                text, via = candidate.strip(), name
                break
    if not text:
        return None
    return LlmReply(text=text, model=model, latency_ms=latency_ms, via=via)


def tool_calls_from(response: dict | None) -> list[dict]:
    """message.tool_calls as [{"name", "arguments", "raw"}]; an empty list if malformed.

    Per the OpenAI spec arguments is a JSON string (a string in Ollama too), but some servers
    send a dict.
    """
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return []
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(calls, list):
        return []
    found = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        raw = function.get("arguments")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        found.append({"name": function["name"], "arguments": parse_tool_arguments(raw),
                      "raw": text[:500]})
    return found


def parse_tool_arguments(raw) -> dict | None:
    """One tool's arguments: a dict as is, a string parsed as JSON, otherwise None."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return parse_json_object(raw)
    return parsed if isinstance(parsed, dict) else None


def tool_reply_from(response: dict | None, model: str, latency_ms: int = 0) -> LlmReply | None:
    """Tool calls if any, else the text answer (reply_from). None if neither."""
    calls = tool_calls_from(response)
    plain = reply_from(response, model, latency_ms)
    if not calls:
        return plain
    return LlmReply(text=plain.text if plain else "", model=model, latency_ms=latency_ms,
                    via="tool_calls", tool_calls=calls)


def parse_json_object(text: str) -> dict | None:
    """Fish out a single object even when the model mixes in prose; drop it if none is found.

    JSON inside <think> is not the answer. Models have written {"legs": …} while thinking and
    then no answer at all, so thoughts are stripped first and only the rest is searched.
    """
    text = strip_think(text or "")
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        candidate = text[start : end + 1] if 0 <= start < end else None
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_choice(text: str, option_count: int) -> int | None:
    """Accept only a single number from the list. It must be the number alone; out of range is
    dropped.

    Back when numbers were fished out of sentences, an answer like "not 7, go with 2" picked
    7. Only a lone number (at most a leading # or a trailing period) counts as an answer. For
    a sentence, the rules decide instead.
    """
    if not text:
        return None
    match = re.fullmatch(r"\s*#?\s*(\d+)\s*[.)]?\s*", strip_think(text))
    if not match:
        return None
    index = int(match.group(1)) - 1
    return index if 0 <= index < option_count else None
