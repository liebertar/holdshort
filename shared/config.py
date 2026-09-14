"""Config loading. The runtime knows nothing about motors or refunds; this file is the domain."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Policy:
    """A fleet-wide prohibition, checked before the limits.

    It blocks an action (a recall: no fast charging) or a resource (a no-fly zone: that pad is
    off-limits). With both empty it blocks nothing.
    """

    id: str
    reason: str
    forbid_action: str | None = None
    forbid_resource: str | None = None
    applies_to: dict = field(default_factory=dict)
    active_from_tick: int = 0
    active_until_tick: int | None = None   # ED-269 zones have validity periods too
    # A prohibition for aircraft on the ground only. A weather hold (WEATHER HOLD) stops
    # takeoffs; it doesn't stop aircraft in the air — an airborne aircraft's refiling (its way
    # down after a recall) must still be judged as usual.
    ground_only: bool = False

    def matches(self, action: str, resource: str | None, asset: dict, tick: int) -> bool:
        if tick < self.active_from_tick:
            return False
        if self.active_until_tick is not None and tick > self.active_until_tick:
            return False
        if self.ground_only and float(asset.get("alt_m") or 0.0) > 1.0:
            return False
        if self.forbid_action and action != self.forbid_action:
            return False
        if self.forbid_resource and resource != self.forbid_resource:
            return False
        if not self.forbid_action and not self.forbid_resource:
            return False
        return all(asset.get(key) == value for key, value in self.applies_to.items())


@dataclass
class Authority:
    """Limits, and the list of actions a person must see."""

    per_asset_usd: float | None      # null: money isn't judged (the operator owns the budget)
    fleet_usd: float | None
    human_required_blast: list[str] = field(default_factory=list)
    human_required_actions: list[str] = field(default_factory=list)


@dataclass
class Escalation:
    nano: str
    super: str
    ultra: str


# Model id → human-readable name, used by the screen header and aircraft labels. An id not
# listed here is shown as is; an empty id is "rules" (rules only, no model). Ollama tags (:4b,
# :latest) and Nebius ids sit side by side — the same family on either server must read as
# the same name on screen.
MODEL_DISPLAY = {
    "nemotron-3-nano:4b": "Nemotron Nano 4B",
    "nemotron-3-nano": "Nemotron Nano 30B",
    "nemotron-3-nano:latest": "Nemotron Nano 30B",
    "nvidia/nemotron-3_5-lightning": "Nemotron 3.5 Lightning",
    "nvidia/nemotron-3-super-120b-a12b": "Nemotron Super 120B",
    "nvidia/nemotron-3-ultra-550b-a55b": "Nemotron Ultra 550B",
}
RULES_DISPLAY = "rules"


def model_display(model_id: str | None) -> str:
    """Display name for a model id. An unknown id is returned as is, not made up."""
    key = (model_id or "").strip()
    if not key:
        return RULES_DISPLAY
    return MODEL_DISPLAY.get(key.lower(), key)


def plural(count: int, noun: str, many: str = "") -> str:
    """"1 page", "2 pages". These counts reach a person: the ledger, the screens, the event log."""
    return f"{count} {noun}" if count == 1 else f"{count} {many or noun + 's'}"


# Lost-link behaviour. The operator declares what the autopilot does when it loses the link.
# On approval the runtime also judges the volume that behaviour sweeps (continue_and_land:
# approved route + landing column) and keeps that volume reserved during a lost link. An
# unknown behaviour can't be judged, so its filing is refused.
CONTINUE_AND_LAND = "continue_and_land"
KNOWN_LOST_LINK_BEHAVIOURS = (CONTINUE_AND_LAND,)


@dataclass
class LostLink:
    behaviour: str = CONTINUE_AND_LAND
    timeout_ticks: int = 15          # airborne telemetry stale this long = lost link

    @property
    def known(self) -> bool:
        return self.behaviour in KNOWN_LOST_LINK_BEHAVIOURS


@dataclass
class Performance:
    """Aircraft performance declared by the operator, and this run's clock. The runtime
    computes intent (4D) time windows from these.

    The operator files only the route. Where the aircraft will be and when, the runtime
    computes from the declared performance — an operator writing its own time windows could
    write them narrow and hide a conflict. Must match the simulator's constants
    (sim/world.py); tests/test_intents.py compares the two.
    """

    cruise_mps: float = 22.0
    climb_mps: float = 2.0
    descent_mps: float = 1.75
    seconds_per_tick: float = 0.8
    clearance_ticks: int = 25        # ground approval check time; lifts off the next tick
    clock_epoch_z: str = "0900"      # Zulu time of tick 0, for converting NOTAM windows to ticks
    # Navigation tolerance: how far the operator declares the aircraft may stray from the
    # approved line. The intent (4D) corridor is the separation minimum plus this (F3548
    # expects the operator's conformance error inside the intent volume). Must exceed the
    # simulator's waypoint radius (ARRIVAL_RADIUS_M, how much it cuts corners).
    nav_tolerance_m: float = 10.0
    # Lost-link contingency, declared by the operator. The runtime uses it to detect a lost
    # link and to check the contingency volume.
    lost_link: LostLink = field(default_factory=LostLink)


@dataclass
class WeatherLimits:
    """Weather this fleet can fly in. If a report's numbers fall outside it, the runtime stops
    takeoffs (WEATHER HOLD).

    The numbers are the operator's standard — matched to manufacturer limits for small
    delivery multirotors (gusts around 12 m/s). Part 107 asks for 3 SM visibility (about
    4.8 km), but urban low-altitude BVLOS operating standards usually use a shorter distance.
    A report without a window holds for hold_default_ticks.
    """

    max_wind_mps: float = 10.0
    max_gust_mps: float = 12.0
    min_visibility_m: float = 1500.0
    hold_default_ticks: int = 500


@dataclass
class IntakeConfig:
    """Information intake: the Tavily query list (runs only with a key) and the METAR stations
    (run without one)."""

    queries: list[str] = field(default_factory=lambda: list(DEFAULT_INTAKE_QUERIES))
    metar_stations: list[str] = field(default_factory=lambda: list(DEFAULT_METAR_STATIONS))


DEFAULT_INTAKE_QUERIES = (
    "New York City wind gust forecast today",
    "NYC temporary flight restriction drones today",
    "Manhattan building fire today",
)
# Central Park (KNYC) and LaGuardia (KLGA), the service area's two stations. The
# METAR_STATIONS environment variable wins if set (split on commas and spaces).
DEFAULT_METAR_STATIONS = ("KNYC", "KLGA")


def _performance(raw: dict | None) -> Performance:
    """The performance section. lost_link is a nested table, so it's folded in separately."""
    fields = dict(raw or {})
    lost_link = fields.pop("lost_link", None) or {}
    return Performance(**fields, lost_link=LostLink(**lost_link))


def _intake(raw: dict | None) -> IntakeConfig:
    """The intake section. An empty METAR_STATIONS counts as unset — compose passes an empty
    string even when unset, and reading that as 'no stations' would quietly turn METAR off.
    METAR=off is what turns it off."""
    fields = dict(raw or {})
    stations = (os.getenv("METAR_STATIONS") or "").replace(",", " ").split()
    if stations:
        fields["metar_stations"] = stations
    return IntakeConfig(**fields)


@dataclass
class FleetConfig:
    name: str
    resources: list[str]
    authority: Authority
    escalation: Escalation
    policies: list[Policy] = field(default_factory=list)
    performance: Performance = field(default_factory=Performance)
    weather: WeatherLimits = field(default_factory=WeatherLimits)
    intake: IntakeConfig = field(default_factory=IntakeConfig)


def load(path: str | Path) -> FleetConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    models = raw.get("models", {})
    return FleetConfig(
        name=raw.get("name", "unnamed"),
        resources=list(raw.get("resources", [])),
        authority=Authority(**raw["authority"]),
        escalation=Escalation(
            nano=os.getenv("MODEL_NANO", models.get("nano", "")),
            super=os.getenv("MODEL_SUPER", models.get("super", "")),
            ultra=os.getenv("MODEL_ULTRA", models.get("ultra", "")),
        ),
        policies=[Policy(**p) for p in raw.get("policies", [])],
        performance=_performance(raw.get("performance")),
        weather=WeatherLimits(**(raw.get("weather") or {})),
        intake=_intake(raw.get("intake")),
    )
