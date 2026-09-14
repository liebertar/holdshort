"""Plain number comparisons. No model runs here, which is why 8,000 of these are affordable."""

from dataclasses import dataclass

VIBRATION_ALERT = 0.55
AUTONOMY_ALERT = 0.35
@dataclass
class Concern:
    kind: str
    urgency: str
    detail: str


def detect(telemetry: dict) -> Concern | None:
    state = telemetry.get("state", "")
    if state in ("grounded", "stranded", "diverted"):
        return None

    # Faults surface the same whether charging or cruising
    if telemetry.get("autonomy_health", 1.0) <= AUTONOMY_ALERT:
        return Concern(
            "autonomy_fault", "high",
            f"autonomy health {telemetry.get('autonomy_health'):.2f}",
        )

    if telemetry.get("vibration", 0.0) >= VIBRATION_ALERT and not telemetry.get("assigned_pad"):
        return Concern(
            "motor_fault", "high", f"motor vibration {telemetry.get('vibration'):.2f}"
        )

    # A flat pack reads as a small negative number, which rounds to the nonsense "-0%" on screen.
    battery = max(telemetry.get("battery", 100.0), 0.0)
    idle = not telemetry.get("assigned_pad") and not telemetry.get("route")

    # A delivery order (or a return to the depot) with no approved route yet: file to be
    # allowed to go. Filed even while loading or unloading — so the aircraft doesn't stand
    # waiting for approval where the job ended. Battery isn't checked here: an aircraft that
    # went out has to come back regardless (otherwise it sat at the landing site forever), and
    # the charge before leaving is checked in the yard (needs_pad below).
    if (
        telemetry.get("job")
        and state in ("loading", "dropping", "picking", "ready", "cruising", "landed")
        and idle
    ):
        return Concern("needs_route", "normal",
                       f"delivering to {telemetry['job']}, battery {battery:.0f}%")

    # In its own spot in the depot yard (nowhere to go, on the ground): load the next job right
    # there. The charger cycle was removed — battery management is the operator's business,
    # and the short hop from the yard to the pad read on screen as "what is that weird route?".
    if not telemetry.get("job") and state == "ready" and idle:
        return Concern("needs_reload", "normal", f"battery {battery:.0f}%, loading the next job")

    # An aircraft down at a landing site (landed) unloads and files for its next route
    # (needs_route above). There is no charging filing.
    # No rule turns a delivery back for battery. The operator plans each loop within range and
    # tops up back in the yard (above). An aircraft turning around mid-route isn't what this
    # demo is about.
    return None
