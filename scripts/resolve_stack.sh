#!/usr/bin/env bash
# Picks the stack settings by itself — no flags. scripts/dev.sh sources it and calls
# resolve_stack; run on its own it prints the choice as a table (--env: KEY=VALUE lines).
#
# Model path (the first that matches):
#   1) LLM_BASE_URL, if given. A Nebius URL without a key counts as not given — every call would
#      be a 401, so rules would write the filings while the screen showed a model name (which is
#      what copying .env.local.example verbatim does).
#   2) NEBIUS_API_KEY set: Nebius Token Factory + nvidia/... model ids.
#   3) The Ollama fleet (11435..11438, scripts/ollama_fleet.sh) answers: a 4B per aircraft. The
#      runtime's stand-in goes to the runtime's model server (11439, 8k context) if it answers,
#      else to 11434 if that answers (the Ollama app sets a 256k context, putting over 5 GB
#      more KV cache on the same 4B).
#   4) Only 11434 answers: everyone shares that one Ollama.
#   5) Nothing answers: rules only.
# Intake: Tavily shows on only with TAVILY_API_KEY, METAR only when the network answers — but
# METAR is not switched off here: the runtime re-polls it periodically anyway and writes a
# ledger line when unreachable (it comes back by itself with the network). Simulator notices
# are always on.
#
# The probe addresses can be changed (the tests point them at fake servers): OLLAMA_HOST_PROBE,
# OLLAMA_BASE_PORT, OLLAMA_FLEET_SIZE, OLLAMA_TOWER_PORT, METAR_PROBE_URL.

NEBIUS_URL=https://api.tokenfactory.nebius.com/v1
NEBIUS_NANO=nvidia/Nemotron-3_5-Lightning
NEBIUS_SUPER=nvidia/nemotron-3-super-120b-a12b
NEBIUS_ULTRA=nvidia/Nemotron-3-Ultra-550b-a55b
OLLAMA_HOST_PROBE="${OLLAMA_HOST_PROBE:-127.0.0.1}"
OLLAMA_BASE_PORT="${OLLAMA_BASE_PORT:-11434}"
OLLAMA_FLEET_SIZE="${OLLAMA_FLEET_SIZE:-4}"
OLLAMA_TOWER_PORT="${OLLAMA_TOWER_PORT:-11439}"
METAR_PROBE_URL="${METAR_PROBE_URL:-https://aviationweather.gov/api/data/metar?ids=KNYC&format=json}"
# Local default model. The 30B takes 26 GB at its default context and cannot be loaded alongside
# the 4B fleet (see the dev.sh comments).
LOCAL_NANO_DEFAULT="${LOCAL_NANO_DEFAULT:-nemotron-3-nano:4b}"
LOCAL_SUPER_STANDIN="${LOCAL_SUPER_STANDIN:-nemotron-3-nano:4b}"
# The argument that turns thinking off on Ollama /v1 (confirmed 2026-09-09). Left on, answers
# arrive 5-27 s later.
OLLAMA_REQUEST_EXTRA='{"reasoning_effort":"none"}'

ollama_up() { curl -sf --max-time 1 "http://$OLLAMA_HOST_PROBE:$1/api/version" >/dev/null 2>&1; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
is_nebius_url() { case "$(lower "$1")" in *nebius*) return 0 ;; esac; return 1; }
is_ollama_url() { case "$(lower "$1")" in *:1143[0-9]*|*ollama*) return 0 ;; esac; return 1; }

# Sets: STACK_MODEL (nebius|ollama-fleet|ollama|other|rules), LLM_BASE_URL (the runtime, and
# aircraft outside the fleet), PER_ASSET_URLS (array, in aircraft order), RUNTIME_SUPER,
# MODEL_NANO/SUPER/ULTRA, NEBIUS_API_KEY, LLM_REQUEST_EXTRA.
resolve_models() {
  local key="${NEBIUS_API_KEY:-}" given="${LLM_BASE_URL:-}" fleet=() index port
  # "ollama" is a placeholder from the old Ollama setup, not a key.
  [ "$key" = "ollama" ] && key=""
  if [ -n "$given" ] && is_nebius_url "$given" && [ -z "$key" ]; then
    given=""
  fi
  read -r -a PER_ASSET_URLS <<< "${LLM_PER_ASSET_URLS:-}"
  STACK_MODEL=rules
  if [ -n "$given" ]; then
    LLM_BASE_URL="$given"
    if is_nebius_url "$given"; then
      STACK_MODEL=nebius
    elif is_ollama_url "$given"; then
      STACK_MODEL=ollama
      [ "${#PER_ASSET_URLS[@]}" -gt 0 ] && STACK_MODEL=ollama-fleet
    else
      STACK_MODEL=other
    fi
  elif [ -n "$key" ]; then
    STACK_MODEL=nebius
    LLM_BASE_URL="$NEBIUS_URL"
  else
    for index in $(seq 1 "$OLLAMA_FLEET_SIZE"); do
      port=$((OLLAMA_BASE_PORT + index))
      if ollama_up "$port"; then fleet+=("http://$OLLAMA_HOST_PROBE:$port/v1"); fi
    done
    if [ "${#PER_ASSET_URLS[@]}" -eq 0 ] && [ "${#fleet[@]}" -gt 0 ]; then
      PER_ASSET_URLS=("${fleet[@]}")
    fi
    # The runtime's server. The runtime's model server comes first — 11434 is the Ollama app,
    # which sets a 256k context.
    LLM_BASE_URL=""
    if ollama_up "$OLLAMA_TOWER_PORT"; then
      LLM_BASE_URL="http://$OLLAMA_HOST_PROBE:$OLLAMA_TOWER_PORT/v1"
    elif ollama_up "$OLLAMA_BASE_PORT"; then
      LLM_BASE_URL="http://$OLLAMA_HOST_PROBE:$OLLAMA_BASE_PORT/v1"
    fi
    if [ "${#PER_ASSET_URLS[@]}" -gt 0 ]; then
      STACK_MODEL=ollama-fleet
    elif [ -n "$LLM_BASE_URL" ]; then
      STACK_MODEL=ollama
    fi
  fi
  NEBIUS_API_KEY="$key"
  RUNTIME_SUPER="${MODEL_SUPER:-}"
  case "$STACK_MODEL" in
    nebius)
      MODEL_NANO="${MODEL_NANO:-$NEBIUS_NANO}"
      MODEL_SUPER="${MODEL_SUPER:-$NEBIUS_SUPER}"
      MODEL_ULTRA="${MODEL_ULTRA:-$NEBIUS_ULTRA}"
      RUNTIME_SUPER="$MODEL_SUPER" ;;
    ollama|ollama-fleet)
      # The stand-in goes on the runtime's line only. Given to an aircraft, it would ask its own
      # 4B server for a 30B model on urgent filings.
      [ -n "${LLM_BASE_URL:-}" ] && RUNTIME_SUPER="${MODEL_SUPER:-$LOCAL_SUPER_STANDIN}"
      LLM_REQUEST_EXTRA="${LLM_REQUEST_EXTRA:-$OLLAMA_REQUEST_EXTRA}" ;;
  esac
  MODEL_NANO="${MODEL_NANO:-}" MODEL_SUPER="${MODEL_SUPER:-}" MODEL_ULTRA="${MODEL_ULTRA:-}"
}

# One aircraft's nano. Unless given, an Ollama server defaults to the 4B. An Ollama found by
# probing is Ollama even off 1143x (OLLAMA_BASE_PORT) — the chosen path decides, not the shape
# of the URL.
agent_nano_for() {
  if [ -n "${MODEL_NANO:-}" ]; then
    printf '%s' "$MODEL_NANO"
  elif [ -n "$1" ]; then
    case "${STACK_MODEL:-}" in
      ollama|ollama-fleet) printf '%s' "$LOCAL_NANO_DEFAULT" ;;
      *) if is_ollama_url "$1"; then printf '%s' "$LOCAL_NANO_DEFAULT"; fi ;;
    esac
  fi
}

# Sets: STACK_TAVILY, STACK_METAR (on|off), STACK_METAR_WHY, INTAKE_DB.
resolve_intake() {
  STACK_TAVILY=off
  [ -n "${TAVILY_API_KEY:-}" ] && STACK_TAVILY=on
  STACK_METAR=off STACK_METAR_WHY=""
  if [ "$(lower "${METAR:-on}")" = off ]; then
    STACK_METAR_WHY="METAR=off"
  elif curl -sf --max-time 3 "$METAR_PROBE_URL" >/dev/null 2>&1; then
    STACK_METAR=on
  else
    STACK_METAR_WHY="aviationweather.gov did not answer — the runtime retries every ${METAR_PERIOD_S:-300} s"
  fi
  INTAKE_DB="${INTAKE_DB:-.run/intake.sqlite}"
}

# Sets: DIRECT_LLM_URL, DIRECT_NANO, DIRECT_MODEL (the model id the simulator stamps on each
# direct-wiring aircraft). The direct side defaults to rules — eight processes sharing one local
# slot starve the runtime side's drafts.
resolve_direct() {
  DIRECT_LLM_URL="" DIRECT_NANO=""
  if [ "${DIRECT_LLM:-0}" = "1" ] && [ -n "${LLM_BASE_URL:-}" ]; then
    DIRECT_LLM_URL="$LLM_BASE_URL"
    DIRECT_NANO="$(agent_nano_for "$LLM_BASE_URL")"
  fi
  DIRECT_MODEL="$DIRECT_NANO"
}

resolve_stack() {
  resolve_models
  resolve_intake
  resolve_direct
}

print_stack_env() {
  printf '%s\n' "STACK_MODEL=$STACK_MODEL" "LLM_BASE_URL=${LLM_BASE_URL:-}" \
    "PER_ASSET_URLS=${PER_ASSET_URLS[*]:-}" "MODEL_NANO=${MODEL_NANO:-}" \
    "MODEL_SUPER=${MODEL_SUPER:-}" "MODEL_ULTRA=${MODEL_ULTRA:-}" "RUNTIME_SUPER=${RUNTIME_SUPER:-}" \
    "LLM_REQUEST_EXTRA=${LLM_REQUEST_EXTRA:-}" "STACK_TAVILY=$STACK_TAVILY" \
    "STACK_METAR=$STACK_METAR" "INTAKE_DB=$INTAKE_DB" "DIRECT_MODEL=${DIRECT_MODEL:-}"
}

print_stack_table() {
  local drones="" index url
  for index in "${!PER_ASSET_URLS[@]}"; do
    url="${PER_ASSET_URLS[$index]##*//}"
    drones="${drones} ${url%%/*}"
  done
  echo "  ── stack ─────────────────────────────────────────────────────────────"
  case "$STACK_MODEL" in
    rules) echo "  model   rules — no model server answered; rules write every filing" ;;
    ollama-fleet)
      echo "  model   ollama-fleet · drones $(agent_nano_for "${PER_ASSET_URLS[0]}") @${drones}"
      if [ -n "${LLM_BASE_URL:-}" ]; then
        url="${LLM_BASE_URL##*//}"
        echo "          runtime ${RUNTIME_SUPER:-no super} @ ${url%%/*} (Super stand-in)"
      else
        echo "          runtime rules (neither the tower server nor 11434 answered)"
      fi ;;
    *) echo "  model   $STACK_MODEL · nano ${MODEL_NANO:-$(agent_nano_for "$LLM_BASE_URL")}" \
            "· super ${RUNTIME_SUPER:-none} @ ${LLM_BASE_URL##*//}" ;;
  esac
  echo "  intake  metar $STACK_METAR${STACK_METAR_WHY:+ ($STACK_METAR_WHY)}" \
       "· tavily $STACK_TAVILY · sim on"
  echo "  store   $INTAKE_DB"
  echo "  direct  ${DIRECT_MODEL:-rules}${DIRECT_MODEL:+ (DIRECT_LLM=1)}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  resolve_stack
  if [ "${1:-}" = "--env" ]; then print_stack_env; else print_stack_table; fi
fi
