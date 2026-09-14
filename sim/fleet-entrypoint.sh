#!/bin/sh
# Starts one PX4 autopilot and sends its offboard MAVLink to the runtime container.
#
# Uses SIH (simulation-in-hardware): the airframe physics runs inside the autopilot, so there is
# no Gazebo — half a CPU core and 10 MiB of memory. The old version ran six aircraft in Gazebo
# and the rendering alone ate the whole Mac; what we have to prove is the command path, not the
# picture.
#
# Why not the image's own entrypoint: that script points all MAVLink at host.docker.internal.
# That is right when the runtime runs on the host (scripts/sitl.sh), but inside compose the
# runtime is a container on the same network, not the host. PX4's mavlink -t only takes an IPv4
# address, so the name (runtime) is resolved to an address here.
#
# Why -d: without stdin the interactive shell (pxh>) reads empty lines forever, burning a CPU
# core and writing tens of MB of log (44 MB in 35 s, measured).
set -eu

PX4_PREFIX=/opt/px4-gazebo
[ -d "$PX4_PREFIX" ] || PX4_PREFIX=/opt/px4
RC="$PX4_PREFIX/etc/init.d-posix/px4-rc.mavlink"
PARTNER="${MAVLINK_PARTNER:-runtime}"

resolve() {
  getent ahostsv4 "$1" 2>/dev/null | awk '/STREAM/ {print $1; exit}'
}

# The name does not resolve until the runtime container is up. Wait a little.
partner_ip=""
attempt=0
while [ "$attempt" -lt "${PARTNER_WAIT_S:-60}" ]; do
  partner_ip="$(resolve "$PARTNER")"
  [ -n "$partner_ip" ] && break
  attempt=$((attempt + 1))
  sleep 1
done
if [ -z "$partner_ip" ]; then
  echo "fleet: could not resolve $PARTNER — check MAVLINK_PARTNER"
  exit 1
fi
echo "fleet: sending offboard MAVLink to $PARTNER($partner_ip):14540"

# Point only the offboard link (remote 14540) at the runtime.
sed -i '/-o \$udp_offboard_port_remote/ s/mavlink start -x -u/mavlink start -x -t '"$partner_ip"' -u/' "$RC"

# The ground-station link (remote 14550) stays on the host, so QGroundControl still sees
# this aircraft.
host_ip="$(resolve host.docker.internal)"
if [ -n "$host_ip" ]; then
  sed -i '/^mavlink start -x -u \$udp_gcs_port_local/ s/mavlink start -x -u/mavlink start -x -t '"$host_ip"' -u/' "$RC"
  echo "fleet: ground-station link to host($host_ip):14550 — for QGroundControl"
fi

exec "$PX4_PREFIX/bin/px4" -d "$@"
