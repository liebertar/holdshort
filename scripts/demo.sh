#!/usr/bin/env bash
# One command for recording the demo. Stops any running stack, brings up a clean one (seed 7),
# restarts the round from the top and opens the staged screen (the map, with ?demo=1). Same result on
# every run — whatever holds the ports it needs is stopped first.
# Stop with Ctrl-C (as with dev.sh). dev.sh picks the model by itself: the Ollama fleet if it
# answers, else rules.
#
# Settable: RT_PORT SIM_PORT UI_PORT (a second stack), DEMO_OPEN=0 (do not open the browser).
set -euo pipefail
cd "$(dirname "$0")/.."

RT_PORT="${RT_PORT:-8000}" SIM_PORT="${SIM_PORT:-8100}" UI_PORT="${UI_PORT:-3100}"
# The seed is pinned to 7. Which aircraft each scene (refusal, weather hold, fire, lost link)
# falls on depends on the seed, and the docs and captions are written for seed 7.
export RT_PORT SIM_PORT UI_PORT SEED=7
# Non-default ports mean a second stack, with its own ledger and intake store — two runtimes
# writing one file mix their reports.
if [ "$RT_PORT" != "8000" ]; then
  export LEDGER_PATH="${LEDGER_PATH:-.run/ledger-$RT_PORT.jsonl}" INTAKE_DB="${INTAKE_DB:-.run/intake-$RT_PORT.sqlite}"
fi
# Processes call each other on 127.0.0.1 (see dev.sh: localhost tries ::1 first, where Docker
# may hold the same port).
LOOPBACK=127.0.0.1
READY_S=60

listeners() { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; }

# Stops whatever holds a port. For a process started by dev.sh, stop dev.sh so the drone agents
# go with it (dev.sh's trap cleans up all its children); for a Docker container, docker stop —
# another project's carter-agent and dynamodb-local have grabbed 8000/8100 before.
stop_port() {
  local port=$1 pid parent command ids
  for pid in $(listeners "$port"); do
    command=$(ps -o command= -p "$pid" 2>/dev/null || true)
    case "$command" in
      *com.docker*|*vpnkit*|*docker-proxy*)
        ids=$(docker ps -q --filter "publish=$port" 2>/dev/null || true)
        # shellcheck disable=SC2086 — pass several container ids unquoted
        [ -n "$ids" ] && docker stop $ids >/dev/null 2>&1 || true
        continue ;;
    esac
    parent=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    if [ -n "$parent" ] && [ "$parent" != 1 ] \
       && ps -o command= -p "$parent" 2>/dev/null | grep -q "scripts/dev.sh"; then
      kill "$parent" 2>/dev/null || true
    fi
    kill "$pid" 2>/dev/null || true
  done
}

# Drone agents left behind without dev.sh (window closed, or dev.sh killed hard). Only those
# pointed at this stack's ports are stopped — left running, every aircraft would file twice with
# the new runtime. Environment variables are read with ps -E (macOS).
stop_orphan_agents() {
  local pid
  for pid in $(ps -Eww -ax -o pid=,command= 2>/dev/null \
      | grep -E "drone[.]agent[.]loop|drone[.]direct[.]loop" \
      | grep -E "(RUNTIME_URL|SIM_URL)=http://$LOOPBACK:($RT_PORT|$SIM_PORT)( |$)" \
      | awk '{print $1}'); do
    kill "$pid" 2>/dev/null || true
  done
}

wait_free() {
  local waited=0
  while [ -n "$(listeners "$RT_PORT")$(listeners "$SIM_PORT")$(listeners "$UI_PORT")" ]; do
    [ "$waited" -ge 20 ] && { echo "  demo    ports $RT_PORT/$SIM_PORT/$UI_PORT are still held" >&2; return 1; }
    sleep 0.5; waited=$((waited + 1))
  done
}

answers() { curl -sf --max-time 2 "$1" >/dev/null 2>&1; }

# Until the simulator, runtime and screen answer and all four drone agents have registered with
# the runtime. Wait for the agents (a round that runs ahead of them misses the first scene), but
# go with however many there are if four never show up.
wait_ready() {
  local waited=0 agents=0
  until answers "http://$LOOPBACK:$SIM_PORT/health" && answers "http://$LOOPBACK:$RT_PORT/state" \
        && answers "http://$LOOPBACK:$UI_PORT/"; do
    [ "$waited" -ge $((READY_S * 2)) ] && return 1
    sleep 0.5; waited=$((waited + 1))
  done
  waited=0
  while [ "$waited" -lt 40 ]; do
    agents=$(curl -sf --max-time 2 "http://$LOOPBACK:$RT_PORT/state" \
      | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("agents") or {}))' 2>/dev/null || echo 0)
    [ "$agents" -ge 4 ] && break
    sleep 0.5; waited=$((waited + 1))
  done
  echo "  demo    $agents drone agent(s) registered"
}

for port in "$RT_PORT" "$SIM_PORT" "$UI_PORT"; do stop_port "$port"; done
stop_orphan_agents
wait_free

./scripts/dev.sh &
stack=$!
# Ctrl-C goes to dev.sh, which reaps its children (simulator, runtime, agents, screen).
trap 'kill "$stack" 2>/dev/null || true; wait "$stack" 2>/dev/null || true; exit 130' INT TERM

if ! wait_ready; then
  echo "  demo    the stack did not come up within ${READY_S} s — see the output above" >&2
  kill "$stack" 2>/dev/null || true
  exit 1
fi
# Restart the round: drop the ticks that ran before the agents attached and start at tick 0.
curl -sf --max-time 5 -X POST "http://$LOOPBACK:$SIM_PORT/reset" >/dev/null
QUERY="demo=1"
[ "$RT_PORT$SIM_PORT" = "80008100" ] || QUERY="$QUERY&rt=$RT_PORT&sim=$SIM_PORT"
URL="http://$LOOPBACK:$UI_PORT/?$QUERY"
echo "  demo    round reset to tick 0 (seed 7)"
echo "  demo    $URL"
if [ "${DEMO_OPEN:-1}" = "1" ] && [ "$(uname)" = "Darwin" ]; then open "$URL"; fi
wait "$stack"
