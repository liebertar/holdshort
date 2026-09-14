# Models

One client, one contract: OpenAI-compatible `/chat/completions`, three tiers, a JSON answer or one tool call,
a timeout per call, every reply recorded to disk when `LLM_RECORD_DIR` is set. Whatever a model returns is a
proposal that code validates. If a model is missing, slow or wrong, the rule that would have been used anyway
is used.

## Who runs where

| Tier | Nebius Token Factory | Local (Ollama) | Sits | Reads | Writes |
|---|---|---|---|---|---|
| Nano | `nvidia/Nemotron-3_5-Lightning` (30B-A3B) | `nemotron-3-nano:4b`, one server per aircraft (11435–11438, `scripts/ollama_fleet.sh`) | in each aircraft's agent (a high-urgency form goes to Super) | telemetry, the concern, refusal feedback; for a route choice, the candidates with the weather, the notices in force and other aircraft's windows | the request form (action + one-line rationale); a `choose_route(id, reason)` call; a waypoint draft only after every candidate is refused |
| Super | `nvidia/nemotron-3-super-120b-a12b` (120B-A12B, 1M context) | `nemotron-3-nano:4b` as a stand-in on the runtime server (11439, 8k context) | beside the runtime | prose notices, incident reports, search snippets, briefing pages the grammar cannot read, the ledger context behind an advisory | a structured form (kind, place, window, numbers), the two-sentence briefing summary, a two-sentence advisory summary and one option id |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | not run locally | beside the runtime | the set of proposals that all passed | one choice from that set and a reason |

Why these: the Nemotron 3 cards describe agentic workflows, tool calling, long-context reasoning and
instruction following. That is the shape of the work here — forms, JSON, one tool call, reading long messy
text. None of them is trained for route geometry between buildings, and they are asked to draw it only as a
last resort. The judge is code.

## Route choice

After the straight line is refused, the operator's planner (`shared/route.py`) draws up to three
legal candidates on the agent's own copy of the airspace:

| id | Candidate |
|---|---|
| a | shortest |
| b | lowest altitude |
| c | clear of other aircraft's cleared corridors and of keep-out areas |

The drone's Nano gets a short brief — the aircraft, why it is redrawing, the weather, the notices in force,
other aircraft's windows, one line per candidate — and one tool, `choose_route(id, reason)`, whose `id` is an
enum of the candidate ids. The tool executes nothing. The agent files the chosen legs and the runtime judges
them like any other filing. If the pick is refused, the agent files the remaining candidates; the model's
waypoint draft comes only after every candidate is refused.

When the model cannot choose, the code does (`drone/agent/chooser.py`):

- A server that rejects `tools` (HTTP 400, also after a retry with `tool_choice: auto`) is not sent tools
  again; the same question goes out as a JSON form.
- An empty reply or plain text is asked again in JSON. After two in a row, that server is asked only in JSON.
- No model, no answer within `CHOICE_TIMEOUT_S` (default 10 s), or an id that is not a candidate: the rules
  pick — (c) when traffic is in the way, else (a). The count is kept.

Measured on the local fleet, one round, four `nemotron-3-nano:4b` servers:

| | Result |
|---|---|
| Multi-candidate redraws decided by the model | 18 of 18 (tool call 14, JSON 4) |
| Tool asks that returned a call | 14 of 18; the 4 empty replies were asked again in JSON and answered 4 of 4 |
| Choice latency | p50 2.3 s, p90 3.4 s |
| Model's pick differed from the shortest | 11 of 18, all "clear of traffic" |
| Model's pick approved at the first filing | 10 of 18 |

Tool calls on Ollama's 4B needed a short system prompt. With a long one that also carried the preferences,
Ollama returned empty replies (no text, no call); the preferences now sit at the end of the brief. Tool
calling on Token Factory has not been measured yet.

## Forms, drafts and labels

Request forms are one short JSON answer. If the agent's model does not answer within `LLM_TIMEOUT_S` (6 s in
each agent), the rules write the form.

Route drafts: asking a 4B model to write waypoint coordinates rarely clears the judge on a long Manhattan
crossing. That is a search problem, and A* is better at it. Drafts are therefore the last resort, and they are
not asked at all after a traffic refusal.

Every filing carries `params.model_trace`, under 1 KB: who wrote the form (the model, or the rules and why),
where the route came from (`straight`, `choice`, `draft`, `astar`), the candidates, the chosen id and its
reason, and for a draft whether it was asked, how long it took and what the agent's own check found. The
runtime never reads it; a test fails if any runtime file mentions it. The map's hover card on a guarded
aircraft or a ledger row shows it in plain words. Corridors are labelled `straight`, `model choice`, `A*`, or
`nano` for a model draft. The ledger keeps the model id.

Each agent registers every 30 s (`POST /agents/register`) with its model and `model_ok`: true if a model
answer was used for a form or a route choice since the last registration, false if every ask fell back to
rules, unchanged if nothing was asked. False puts `rules` under the aircraft instead of the model name. Route
drafts are not counted: they are the last resort and usually fail, and counting them turned aircraft whose
forms the model had written into `rules`.

## Fallback chain

`scripts/dev.sh` resolves this without flags and prints what it chose (`scripts/resolve_stack.sh` alone prints
the same table):

```
NEBIUS_API_KEY set        → Nebius: Nano per aircraft, Super and Ultra at the runtime
else Ollama fleet answers → local 4B per aircraft (11435–11438); runtime stand-in on 11439, else 11434
else Ollama answers       → one local model for everyone (11434)
else                      → rules only (forms and route choice by rules, routes by A*, prose left for a person)

TAVILY_API_KEY set        → live briefing and search; only official pages read by the grammar apply at once
else                      → recorded briefing (tests/fixtures/tavily, marked "recorded"); simulated bulletins

network                   → METAR from aviationweather.gov
else                      → simulated weather report
```

Compose does not probe for servers. `NEBIUS_API_KEY` alone reaches Token Factory. For the Mac's Ollama fleet
from containers, set the `host.docker.internal` URLs and `MODEL_NANO`/`MODEL_SUPER`, because the compose
defaults are Nebius ids, and set `MODEL_ULTRA=` empty, because there is no local Ultra. The compose block in
`.env.local.example` has the lines to uncomment.

Nothing else changes between these modes. The judge, the ledger and the scoreboard are identical.

## Running with a model

```sh
# local fleet
ollama pull nemotron-3-nano:4b
scripts/ollama_fleet.sh start 4
./scripts/dev.sh

# Nebius
NEBIUS_API_KEY=... ./scripts/dev.sh

# Nebius under compose
cp .env.local.example .env.local        # set NEBIUS_API_KEY
docker compose -f docker-compose.local.yml --env-file .env.local up --build
```

`scripts/ollama_fleet.sh start 4` starts four drone servers (11435–11438) and a runtime server (11439), all with
an 8k context, and warms the 4B on each. The resolver gives the runtime 11439 before 11434 because the Ollama
app loads a 262,144-token context, which adds over 5 GB of KV cache for the same 4B. `OLLAMA_FLEET_TOWER=0`
skips the runtime server.

Real memory, measured with `footprint`: about 7.5 GB per 8k server, because each server holds its own copy of
the weights. Ollama reports 3.0 GB; that is the model, not what the server holds. The four drone servers take
about 30 GB. With a Nebius key no local model is needed.

`LLM_REQUEST_EXTRA='{"reasoning_effort":"none"}'` keeps Nemotron's thinking mode off for the short answers;
the resolver sets it for Ollama. `CHOICE_TIMEOUT_S` (default 10) bounds one choice, tool call and JSON retry
together. `DRAFT_TIMEOUT_S` (default 60) bounds a route draft from the moment of the refusal. The display delay
runs inside both, so the screen never waits on the model.
