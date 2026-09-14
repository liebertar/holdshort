#!/usr/bin/env bash
# One Ollama server per drone — N of them on 11435, 11436, … (default 4).
#
# Why: Ollama forces a single concurrent slot on the nemotron-3-nano (Mamba hybrid) family
# (OLLAMA_NUM_PARALLEL is ignored). Four aircraft asking one server for drafts queue up and the
# last three time out (live run: 7 of 9 drafts cut off, 0 nano approvals). One server per
# aircraft gives one slot per aircraft. The model folder (~/.ollama/models, OLLAMA_MODELS) is
# shared as is, and each server loads its own small 4B (nemotron-3-nano:4b, 2.8 GB).
#
# One more server is started: the runtime's model server (11439), used by the runtime's Super
# stand-in (reading notices, advisories). It used to be the default server (11434, the Ollama
# app), but the app sets a 256k context window, which put over 5 GB more KV cache on the same 4B
# (Ollama shows 8.4 GB; 3.0 GB at 8k). A notice is around 2,000 characters, so 8k is plenty.
# Real memory is higher than Ollama shows: each server loads the weights into its own heap
# (MALLOC_LARGE in footprint), so one 8k server is about 7.5 GB and four are 30 GB (measured
# with footprint, 2026-09-11).
# To skip the runtime's model server, set OLLAMA_FLEET_TOWER=0 — the runtime then uses 11434 as
# before.
#
#   scripts/ollama_fleet.sh start  [N]   # start the servers and warm each 4B once
#                                        # (so the first draft does not wait for the load)
#   scripts/ollama_fleet.sh stop   [N]
#   scripts/ollama_fleet.sh status [N]
#
# Logs go to .run/ollama-<port>.log, pids to .run/ollama-<port>.pid. Export the
# LLM_PER_ASSET_URLS printed after start and run scripts/dev.sh: aircraft i uses server i
# (the local fleet block in .env.local.example).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .run

COMMAND="${1:-status}"
SIZE="${2:-${OLLAMA_FLEET_SIZE:-4}}"
MODEL="${OLLAMA_FLEET_MODEL:-nemotron-3-nano:4b}"
BASE_PORT="${OLLAMA_FLEET_BASE_PORT:-11434}"     # servers start at the port after this
# Context window. Ollama's default sizes it from VRAM, up to 256k, and one 4B then takes 8.4 GB.
# A draft is about 1.5k tokens of map reading + a 700-token answer, so 8k is plenty, and four
# servers have to fit in memory together.
CONTEXT="${OLLAMA_FLEET_CONTEXT:-8192}"
# How long a warmed model stays loaded. Comfortably longer than one demo round (13 min).
KEEP_ALIVE="${OLLAMA_FLEET_KEEP_ALIVE:-2h}"
# The runtime's model server. scripts/resolve_stack.sh tries this port first for the runtime.
TOWER="${OLLAMA_FLEET_TOWER:-1}"
TOWER_PORT="${OLLAMA_TOWER_PORT:-11439}"
if [ "$TOWER" = 1 ] && [ "$((BASE_PORT + SIZE))" -ge "$TOWER_PORT" ]; then
  echo "the fleet (:$((BASE_PORT + 1))..:$((BASE_PORT + SIZE))) overlaps the runtime server :$TOWER_PORT — move OLLAMA_TOWER_PORT" >&2
  exit 2
fi

port_of() { echo $((BASE_PORT + $1)); }
url_of() { echo "http://127.0.0.1:$(port_of "$1")/v1"; }
is_up() { curl -sf --max-time 1 "http://127.0.0.1:$1/api/version" >/dev/null 2>&1; }

# Every port this script manages: N drone servers, plus the runtime's model server.
ports() {
  for i in $(seq 1 "$SIZE"); do port_of "$i"; done
  if [ "$TOWER" = 1 ]; then echo "$TOWER_PORT"; fi
}

tower_note() {
  if [ "$TOWER" = 1 ]; then echo " + runtime server :$TOWER_PORT"; fi
}

urls_line() {
  local urls=""
  for i in $(seq 1 "$SIZE"); do urls="${urls:+$urls }$(url_of "$i")"; done
  echo "$urls"
}

start_one() {
  local port=$1
  if is_up "$port"; then
    echo "  :$port already up"
    return
  fi
  OLLAMA_HOST="127.0.0.1:$port" OLLAMA_KEEP_ALIVE="$KEEP_ALIVE" OLLAMA_CONTEXT_LENGTH="$CONTEXT" \
    nohup ollama serve >".run/ollama-$port.log" 2>&1 &
  echo $! >".run/ollama-$port.pid"
  echo "  :$port started (pid $!, log .run/ollama-$port.log)"
}

wait_up() {
  local port=$1
  for _ in $(seq 1 100); do
    is_up "$port" && return 0
    sleep 0.2
  done
  echo "  :$port did not come up within 20 s — see .run/ollama-$port.log" >&2
  return 1
}

warm_one() {
  # Load the model with one tiny question, so the first real draft does not wait seconds for it.
  local port=$1 started ended
  started=$(date +%s)
  curl -s --max-time 180 "http://127.0.0.1:$port/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word ok.\"}],\"max_tokens\":4,\"reasoning_effort\":\"none\"}" \
    >".run/ollama-$port.warm.json" 2>&1 || true
  ended=$(date +%s)
  if grep -q '"choices"' ".run/ollama-$port.warm.json"; then
    echo "  :$port $MODEL warmed ($((ended - started)) s)"
  else
    echo "  :$port warm-up failed — $(head -c 200 ".run/ollama-$port.warm.json")" >&2
  fi
}

stop_one() {
  local port=$1 pid=""
  [ -f ".run/ollama-$port.pid" ] && pid=$(cat ".run/ollama-$port.pid")
  # No pid file, or a stale one: find the process holding the port. 11434 (the default server)
  # never gets here.
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    pid=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  fi
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    echo "  :$port stopped (pid $pid)"
  else
    echo "  :$port not running"
  fi
  rm -f ".run/ollama-$port.pid"
}

# The /api/ps answer on one line: model name (memory, context window)
PS_SUMMARY=$(cat <<'PY'
import json, sys
loaded = json.load(sys.stdin).get("models") or []
print(", ".join("%s (%.1f GB, ctx %s)" % (m["name"], m.get("size_vram", 0) / 1e9, m.get("context_length"))
               for m in loaded) or "(no model loaded)")
PY
)

status_one() {
  local port=$1 loaded role=""
  if [ "$port" = "$TOWER_PORT" ]; then role=" (runtime)"; fi
  if is_up "$port"; then
    loaded=$(curl -s --max-time 2 "http://127.0.0.1:$port/api/ps" | python3 -c "$PS_SUMMARY" 2>/dev/null || echo "?")
    echo "  :$port$role up — $loaded"
  else
    echo "  :$port$role down"
  fi
}

case "$COMMAND" in
  start)
    echo "Ollama fleet: $SIZE servers$(tower_note) ($MODEL, ctx $CONTEXT, keep-alive $KEEP_ALIVE)"
    for port in $(ports); do start_one "$port"; done
    for port in $(ports); do wait_up "$port"; done
    # Warm them together: they mmap the same blob, so it beats one at a time. The servers are
    # children of this shell too, so wait only on the warm-up pids — otherwise wait would not
    # return until the servers go down.
    warmers=""
    for port in $(ports); do
      warm_one "$port" &
      warmers="$warmers $!"
    done
    # shellcheck disable=SC2086
    wait $warmers
    echo
    echo "export LLM_PER_ASSET_URLS=\"$(urls_line)\""
    if [ "$TOWER" = 1 ]; then
      echo "# runtime server (Super stand-in) http://127.0.0.1:$TOWER_PORT/v1 — scripts/dev.sh uses it"
    fi
    ;;
  stop)
    for port in $(ports); do stop_one "$port"; done
    ;;
  status)
    for port in $(ports); do status_one "$port"; done
    echo "LLM_PER_ASSET_URLS=\"$(urls_line)\""
    ;;
  *)
    echo "usage: $0 start|stop|status [N]" >&2
    exit 2
    ;;
esac
