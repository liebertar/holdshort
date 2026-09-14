#!/usr/bin/env bash
# Starts one real PX4 autopilot (SIH) in a container and puts the host's stack behind it.
# This is the path the demo video uses — the screen, simulator and drone agents are all host
# processes; the container is only the autopilot.
#
#   ./scripts/sitl.sh            PX4 + stack (Ctrl-C stops both)
#   ./scripts/sitl.sh px4        start PX4 only and print how to attach the stack
#   ./scripts/sitl.sh check      check the link and the mission protocol (scripts/px4_check.py)
#   ./scripts/sitl.sh fly        check + one short hop up and down
#                                (check and fly use a running PX4, or start one and stop it
#                                when done)
#   ./scripts/sitl.sh stop       stop the PX4 container
#
# All four aircraft stay simulated; only MAVLINK_MIRROR (default drone-01) is also flown by PX4.
# Judgements and the score still belong to the simulator — PX4 is a mirror that proves the
# command path.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${PX4_IMAGE:-px4io/px4-sitl-gazebo:v1.18.0-beta2}"
NAME="${PX4_CONTAINER:-sky-net-px4}"
MIRROR="${MAVLINK_MIRROR:-drone-01}"
PORT="${MAVLINK_PORT:-14540}"
# PX4 (SIH) runs at 1x: that is all the SIH lockstep sustains in Docker on this Mac (measured:
# 1x → 0.94x, 2x → erratic, 4x → 0.26x, i.e. slower). At the default tick (0.2 s) the sim runs
# at 4x, so PX4 lags the aircraft on the map — the command path still shows; for positions to
# line up too, run the sim in real time: TICK_SECONDS=0.8 ./scripts/sitl.sh
SPEED="${PX4_SIM_SPEED_FACTOR:-1}"

seat_of() {
  # The mirror aircraft's seat on the depot roof, in the world's own coordinates — starting
  # here makes PX4's track overlay that aircraft on the map.
  PYTHONPATH=. python3 - "$1" <<'PY'
import sys
from sim.world import SEAT_ROOF_M, seat_of, to_latlon
asset = sys.argv[1]
index = int(asset.rsplit("-", 1)[-1]) - 1 if asset.rsplit("-", 1)[-1].isdigit() else 0
lat, lon = to_latlon(*seat_of(index))
print(f"{lat:.6f} {lon:.6f} {SEAT_ROOF_M:.0f}")
PY
}

start_px4() {
  read -r home_lat home_lon home_alt <<<"$(seat_of "$MIRROR")"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  # On Docker Desktop the image's entrypoint points MAVLink at host.docker.internal. Linux has
  # no such name, so join the host network and let it arrive on 127.0.0.1.
  local network=()
  docker info --format '{{.OperatingSystem}}' 2>/dev/null | grep -qi "docker desktop" || \
    network=(--network host)
  docker run -d --name "$NAME" "${network[@]}" \
    -e PX4_SIM_MODEL=sihsim_quadx \
    -e PX4_HOME_LAT="$home_lat" -e PX4_HOME_LON="$home_lon" -e PX4_HOME_ALT="$home_alt" \
    -e PX4_SIM_SPEED_FACTOR="$SPEED" \
    -e PX4_PARAM_MPC_XY_CRUISE=20 -e PX4_PARAM_MPC_XY_VEL_MAX=20 \
    -e PX4_PARAM_MPC_TKO_SPEED=2 -e PX4_PARAM_MPC_Z_V_AUTO_UP=2 \
    -e PX4_PARAM_MPC_Z_V_AUTO_DN=1.75 -e PX4_PARAM_COM_RCL_EXCEPT=1 \
    "$IMAGE" -d >/dev/null
  echo "  px4     $NAME · SIH · $MIRROR seat $home_lat,$home_lon (roof ${home_alt}m) · ${SPEED}x speed"
  echo "  link    MAVLink → host UDP $PORT"
}

# The runtime needs pymavlink to speak MAVLink. Without it, make a small venv in .run and put
# its python first on PATH — nothing is installed into the system python.
ensure_pymavlink() {
  if python3 -c 'import pymavlink' >/dev/null 2>&1; then
    return
  fi
  local venv=".run/sitl-venv"
  if [ ! -x "$venv/bin/python3" ]; then
    echo "  deps    no pymavlink; creating $venv (once)"
    python3 -m venv "$venv"
  fi
  "$venv/bin/python3" -c 'import pymavlink, yaml' >/dev/null 2>&1 || \
    "$venv/bin/pip" install --quiet pymavlink==2.4.49 pyyaml==6.0.2
  PATH="$PWD/$venv/bin:$PATH"
  export PATH
}

case "${1:-stack}" in
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "px4 stopped."
    ;;
  px4)
    start_px4
    echo
    echo "  attach the stack like this:"
    echo "    ADAPTER=composite MAVLINK_MIRROR=$MIRROR \\"
    echo "      MAVLINK_ENDPOINT=udpin:0.0.0.0:$PORT ./scripts/dev.sh"
    ;;
  check|fly)
    ensure_pymavlink
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      start_px4
      # A PX4 started here is stopped here. One already running (in use by the stack) is
      # left alone.
      trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
    fi
    extra=""
    [ "${1}" = "fly" ] && extra="--fly"
    PYTHONPATH=. python3 scripts/px4_check.py "udpin:0.0.0.0:$PORT" $extra
    ;;
  stack)
    ensure_pymavlink
    start_px4
    trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
    echo
    ADAPTER=composite MAVLINK_MIRROR="$MIRROR" MAVLINK_ENDPOINT="udpin:0.0.0.0:$PORT" \
      ./scripts/dev.sh
    ;;
  *)
    echo "usage: scripts/sitl.sh [stack|px4|check|fly|stop]"
    exit 2
    ;;
esac
