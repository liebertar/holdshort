"""METAR from aviationweather.gov: an official observation, read by code, no key needed.

The tower's own weather feed in the simulator is a METAR-like line; this module brings the
real thing. aviationweather.gov answers with JSON per station (wind, gust, visibility as
numbers plus the raw observation). The numbers are folded into the same spelled-out dialect
the intake grammar already reads deterministically ("KLGA WIND 220 AT 13 GUST 22 KT VIS
10SM"), so a real observation and a scripted bulletin take exactly the same path — grammar,
limits, hold — and nothing here decides anything. When the network is not there the source
is off, once in the ledger, and the poller keeps trying quietly.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from shared.tavily import ERROR_CHARS, FetchStatus

METAR_URL = "https://aviationweather.gov/api/data/metar"
SOURCE = "metar"
# Seconds to wait for a station's JSON. This runs off the world thread, so a long wait never
# stalls a tick.
DEFAULT_TIMEOUT_S = 10.0
# Poll interval (s). METARs come hourly (specials in between), so 5 minutes is plenty.
DEFAULT_PERIOD_S = 300.0


class MetarFailed(Exception):
    """One cycle failed — unreachable, refused, too slow, or a broken reply."""


@dataclass
class Observation:
    """Only what's needed to turn one observation into intake text. Units as given (kt, SM)."""

    station: str
    obs_time: int                    # epoch s; also the item id, so an observation counts once
    wind_kt: float | None
    gust_kt: float | None
    visibility_sm: float | None
    wind_dir: str                    # "220" | "VRB" | ""
    weather: str = ""                # weather codes (RA, BR …); the grammar reads precipitation
    raw: str = ""

    def text(self) -> str:
        """In the phrasing the grammar reads. No time — the run's clock (tick 0 = 0900Z) and
        real time differ."""
        parts = [self.station]
        if self.wind_kt is not None:
            direction = self.wind_dir if self.wind_dir in ("VRB",) or self.wind_dir.isdigit() \
                else "000"
            parts.append(f"WIND {direction.zfill(3) if direction.isdigit() else direction} "
                         f"AT {self.wind_kt:.0f}")
            if self.gust_kt is not None:
                parts.append(f"GUST {self.gust_kt:.0f}")
            parts.append("KT")
        if self.visibility_sm is not None:
            parts.append(f"VIS {_fraction(self.visibility_sm)}SM")
        if self.weather:
            parts.append(self.weather)
        return " ".join(parts)

    def item(self) -> dict:
        return {"id": f"metar-{self.station}-{self.obs_time}", "source": SOURCE,
                "kind": "weather", "text": self.text(), "station": self.station,
                "title": self.raw[:120], "url": f"{METAR_URL}?ids={self.station}",
                "obs_time": self.obs_time, "fetched_at": time.time()}


def _fraction(value: float) -> str:
    """Visibility in the form the grammar reads: whole numbers as is, 1/2, 1/4 and 3/4 as
    fractions, anything else to one decimal."""
    if float(value).is_integer():
        return f"{int(value)}"
    for numerator, denominator in ((1, 4), (1, 2), (3, 4), (1, 8), (3, 8), (5, 8), (7, 8)):
        if abs(value - numerator / denominator) < 1e-6:
            return f"{numerator}/{denominator}"
    return f"{value:.1f}"


def _number(value) -> float | None:
    """None if not a number. "10+" (10 SM or more) is 10, "1/2" is 0.5, "M1/4" (under 1/4)
    is 0.25."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().rstrip("+").lstrip("M")
    if "/" in text:
        head, _, tail = text.partition("/")
        whole, _, top = head.rpartition(" ")
        try:
            return (float(whole) if whole else 0.0) + float(top or head) / float(tail)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_observation(raw: dict) -> Observation | None:
    """One aviationweather.gov observation → Observation. None with neither wind nor
    visibility (nothing to read)."""
    if not isinstance(raw, dict):
        return None
    station = str(raw.get("icaoId") or "").strip().upper()
    if not station:
        return None
    wind = _number(raw.get("wspd"))
    gust = _number(raw.get("wgst"))
    visibility = _number(raw.get("visib"))
    if wind is None and gust is None and visibility is None:
        return None
    direction = raw.get("wdir")
    direction = "VRB" if str(direction).upper() == "VRB" else (
        f"{int(direction):03d}" if isinstance(direction, (int, float)) else "")
    obs_time = raw.get("obsTime")
    try:
        obs_time = int(obs_time)
    except (TypeError, ValueError):
        obs_time = int(time.time())
    weather = " ".join(str(raw.get("wxString") or "").split())
    return Observation(station=station, obs_time=obs_time, wind_kt=wind, gust_kt=gust,
                       visibility_sm=visibility, wind_dir=direction, weather=weather,
                       raw=str(raw.get("rawOb") or ""))


class MetarClient:
    def __init__(self, stations: list[str], base_url: str = METAR_URL,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self.stations = [s.strip().upper() for s in stations if s.strip()]
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.calls = 0
        self.failures = 0
        self.last_error = ""

    @classmethod
    def from_env(cls, stations: list[str]) -> "MetarClient | None":
        """None with no stations or METAR=off — the source is off and asks nothing."""
        if os.getenv("METAR", "on").strip().lower() in ("off", "0", "false", "no"):
            return None
        if not [s for s in stations if s.strip()]:
            return None
        return cls(stations, os.getenv("METAR_URL", METAR_URL),
                   float(os.getenv("METAR_TIMEOUT_S") or DEFAULT_TIMEOUT_S))

    def fetch(self) -> list[dict]:
        """All stations at once. MetarFailed if unreachable or the reply is broken."""
        query = urllib.parse.urlencode({"ids": ",".join(self.stations), "format": "json"})
        self.calls += 1
        try:
            request = urllib.request.Request(f"{self.base_url}?{query}", method="GET")
            request.add_header("Accept", "application/json")
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            self._fail(f"HTTP {error.code}")
        except urllib.error.URLError as error:
            self._fail(f"unreachable: {error.reason}")
        except (TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
            self._fail(f"{type(error).__name__}: {error}")
        if not isinstance(body, list):
            self._fail("reply is not a list of observations")
        items = []
        for raw in body:
            observation = parse_observation(raw)
            if observation is not None:
                items.append(observation.item())
        return items

    def _fail(self, why: str) -> None:
        self.failures += 1
        self.last_error = why[:ERROR_CHARS]
        raise MetarFailed(self.last_error)


class MetarPoller:
    """Each cycle, fetches observations and hands them over — on its own thread, so the world
    thread never waits on the network.

    deliver(items, status) — the status is handed over even with no items (the same promise
    as tavily.IntakePoller).
    """

    def __init__(self, client: MetarClient, period_s: float, deliver):
        self.client = client
        self.period_s = period_s
        self.deliver = deliver
        self.fetches = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, daemon=True, name="intake-metar")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            self._stop.wait(self.period_s)

    def fetch_once(self) -> list[dict]:
        found, error = [], ""
        try:
            found = self.client.fetch()
        except MetarFailed as failed:
            error = str(failed)
        self.fetches += 1
        status = FetchStatus(ok=not error, error=error, calls=self.client.calls,
                             failures=self.client.failures)
        try:
            self.deliver(found, status)
        except Exception as problem:  # noqa: BLE001 — a failed handover doesn't stop the next cycle
            print(f"metar deliver: {problem!r}", flush=True)
        return found
