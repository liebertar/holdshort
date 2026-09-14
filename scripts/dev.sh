#!/usr/bin/env bash
# Runs the same stack as local processes, without Docker. Stop with Ctrl-C.
# No flags — scripts/resolve_stack.sh picks the model and intake paths from what is running
# (Nebius key → Ollama fleet → a single Ollama → rules) and prints the choice as one table.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=.
mkdir -p .run

# Reads .env.local if present (else the old name .env; the example is .env.local.example).
# Values already in the environment win, so LLM_BASE_URL=… ./scripts/dev.sh overrides one run.
ENV_FILE=.env.local
[ -f "$ENV_FILE" ] || ENV_FILE=.env
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    [ -n "${!key:-}" ] && continue
    # Strip one pair of quotes around the value. .env is not a shell, so the quotes would stay:
    # LLM_PER_ASSET_URLS="a b" would give the first aircraft "a and the last b", and both would
    # run without a model.
    value="${line#*=}"
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    export "$key=$value"
  done < "$ENV_FILE"
fi

# Pick the model and intake paths. Sets STACK_MODEL, LLM_BASE_URL (runtime), PER_ASSET_URLS
# (per aircraft), RUNTIME_SUPER, MODEL_*, STACK_METAR/STACK_TAVILY, INTAKE_DB, DIRECT_*.
# shellcheck source=scripts/resolve_stack.sh
. scripts/resolve_stack.sh
resolve_stack

# Model settings go unchanged to both the drone agents and the runtime. Exporting them here
# makes the background processes below see the same values (empty means rules only).
export LLM_BASE_URL NEBIUS_API_KEY MODEL_NANO MODEL_SUPER MODEL_ULTRA
export LLM_REQUEST_EXTRA="${LLM_REQUEST_EXTRA:-}" LLM_RECORD_DIR="${LLM_RECORD_DIR:-}"
[ -n "${LLM_TIMEOUT_S:-}" ] && export LLM_TIMEOUT_S
# Intake. Tavily runs only with a key; the runtime polls METAR periodically (one ledger line when
# unreachable).
export TAVILY_API_KEY="${TAVILY_API_KEY:-}" INTAKE_DB
[ -n "${METAR:-}" ] && export METAR
[ -n "${METAR_STATIONS:-}" ] && export METAR_STATIONS
[ -n "${METAR_PERIOD_S:-}" ] && export METAR_PERIOD_S

cleanup() { pkill -P $$ || true; }
trap cleanup EXIT INT TERM

# Processes call each other on 127.0.0.1. On this Mac localhost tries ::1 first, and when a Docker
# container opens the same port on [::1] (dynamodb-local 8100, carter-agent 8000) the runtime
# polls it instead of the simulator and the ticks stop — it did freeze at tick 2618.
LOOPBACK=127.0.0.1
# Ports can be changed (a second stack, or when something else holds the defaults):
# RT_PORT SIM_PORT UI_PORT.
RT_PORT="${RT_PORT:-8000}" SIM_PORT="${SIM_PORT:-8100}" UI_PORT="${UI_PORT:-3100}"
# The simulator stamps the direct wiring's model on each aircraft (DIRECT_MODEL) — those agents
# have no path to register with the runtime, so the launcher passes it here.
PORT=$SIM_PORT TICK_SECONDS="${TICK_SECONDS:-0.2}" FLEET_LIMIT_USD=720 DIRECT_MODEL="$DIRECT_MODEL" \
  python3 -m sim.service & sleep 1
# A second stack keeps its own ledger too (LEDGER_PATH, INTAKE_DB) — two runtimes writing one
# file mix their reports.
PORT=$RT_PORT CONFIG=configs/fleet.yaml SIM_URL=http://$LOOPBACK:$SIM_PORT MODEL_SUPER="$RUNTIME_SUPER" \
  LEDGER_PATH="${LEDGER_PATH:-.run/ledger.jsonl}" python3 -m backend.api.server & sleep 1

# Aircraft i uses server i (PER_ASSET_URLS, the Ollama fleet), else LLM_BASE_URL. Ollama forces
# one concurrent request on this model family, so four sharing one server queue up and drafts get
# cut off. The stand-in (Super) goes on the runtime's line only — given to an aircraft, it would
# ask its own 4B server for a 30B model on urgent filings.
# The direct wiring uses the same filing writer, but eight processes sharing one local Ollama
# slot starve the runtime side's route drafts (0 drafts live). By default only the direct side
# runs on rules; DIRECT_LLM=1 turns the model on there too.
index=0
for asset in drone-01 drone-02 drone-03 drone-04; do
  agent_url="${PER_ASSET_URLS[$index]:-$LLM_BASE_URL}"
  agent_nano="$(agent_nano_for "$agent_url")"
  ASSET_ID=$asset RUNTIME_URL=http://$LOOPBACK:$RT_PORT LLM_BASE_URL="$agent_url" MODEL_NANO="$agent_nano" \
    MODEL_SUPER="$MODEL_SUPER" python3 -m drone.agent.loop &
  ASSET_ID=$asset TRANSPORT=http SIM_URL=http://$LOOPBACK:$SIM_PORT LLM_BASE_URL="$DIRECT_LLM_URL" \
    MODEL_NANO="$DIRECT_NANO" python3 -m drone.direct.loop &
  index=$((index + 1))
done

python3 frontend/serve.py "$UI_PORT" frontend >/dev/null 2>&1 &
echo
print_stack_table
UI_QUERY=""
[ "$RT_PORT$SIM_PORT" = "80008100" ] || UI_QUERY="?rt=$RT_PORT&sim=$SIM_PORT"
echo "  screen  http://$LOOPBACK:$UI_PORT$UI_QUERY   (not localhost: it tries ::1 first, where Docker may hold the same port)"
echo "  state   http://$LOOPBACK:$RT_PORT/state   world http://$LOOPBACK:$SIM_PORT/compare"
echo
wait
