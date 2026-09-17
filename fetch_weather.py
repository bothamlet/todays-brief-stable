#!/usr/bin/env python3
"""
fetch_weather.py — the weather, for Kaunas.

A morning paper that tells you what happened but not what to wear is missing the
one thing every reader checks first.

Two sources, both free and neither needing a key:

  Open-Meteo    The forecast. Current conditions, today's high and low, rain in
                millimetres, the chance of it, and sunrise and sunset, all in
                Europe/Vilnius rather than UTC — an 05:00 sunrise is an hour wrong
                in the summer otherwise.

  RainViewer    The radar. Thirteen frames of the last two hours, the newest of
                which is the picture of where the rain is now. The tiles are a
                transparent overlay, so they are drawn over a base map rather
                than on their own — rain over nothing tells you it is raining
                somewhere, not whether it is coming here.

The tiles are downloaded and inlined as data URIs, which is the part worth
explaining. The paper is assembled into one file that opens with no network, and
a radar that needs five tile servers to be up in order to show anything would be
the one panel that breaks on a train. Nine base tiles and nine radar tiles come
to about 280KB, which is a fair price for a map that is always there.

Zoom 7 and a three-by-three grid: roughly 550km across, which is wide enough to
show a front arriving from the Baltic and close enough that Kaunas is not a dot
in the corner.

Writes data/weather.json as a list of panels, the same shape fetch_stats.py uses,
so the page renders it through the machinery that already exists.

Usage:
    python fetch_weather.py
"""

from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling modules. Python puts this script's own directory on sys.path; pyright
# analyses each file standalone and cannot see them, hence the directives.
from common import save_json, utc_now  # pyright: ignore[reportMissingImports]

import requests

OUTPUT_PATH = Path("data/weather.json")

# Kaunas. The one Lithuanian city the rest of the pipeline already treats as home
# — fetch_lietuva.py gives Kaunas stories a bonus for the same reason.
PLACE = "Kaunas"
LATITUDE, LONGITUDE = 54.8972, 23.8861

# Where the paper is read, not where the coordinates are. Open-Meteo takes this
# and returns every timestamp already local, which saves doing arithmetic on
# sunrise and getting it wrong twice a year.
TIMEZONE = "Europe/Vilnius"

# Zoom and grid. See the module docstring: seven and three.
ZOOM = 7
GRID = 3

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
RAINVIEWER_INDEX = "https://api.rainviewer.com/public/weather-maps.json"

# OpenStreetMap's own tiles, not CARTO's. CARTO was the first choice on size —
# 187KB against 396KB for the same nine — and it stamps "API KEY REQUIRED" across
# every tile on the keyless endpoint. Nine watermarks per morning, and no way to
# see it except by looking at the rendered page: the fetch returns a valid PNG
# with a 200, so every check short of the picture passes.
#
# Nine tiles a day for one reader is well inside OSM's tile policy, which is
# about bulk scraping rather than a personal morning paper — and the user agent
# below says what this is, which is what that policy actually asks for.
BASEMAP = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = "© OpenStreetMap contributors"

TIMEOUT = 25.0

# A user agent that says what it is, which is what the tile services ask for and
# what keeps this from looking like a scraper. The contact is deliberately vague:
# this is one reader's morning paper, not a service.
USER_AGENT = "today-s-brief/1.0 (personal daily digest)"

# WMO weather codes to words. The API returns a number and a number is not a
# forecast as far as a reader is concerned.
WMO: dict[int, str] = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with hail",
}

# Sixteen points is more than a wind direction needs and about what a reader wants.
COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def describe(code: Any) -> str:
    """The words for a WMO code, or an honest shrug."""
    try:
        return WMO.get(int(code), "—")
    except (TypeError, ValueError):
        return "—"


def compass(degrees: Any) -> str:
    """A wind direction as a point, for a reader rather than a forecast model.

    Guarded rather than trusted: a calm hour reports its direction as null, and a
    missing reading arithmetic'd into an index is a crash for no reason. Infinity
    and NaN reach int() as errors of their own, so they are caught too.
    """
    try:
        value = float(degrees) % 360
        return COMPASS[int((value + 11.25) % 360 // 22.5) % len(COMPASS)]
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return ""


# --- Tiles -------------------------------------------------------------------


def tile_number(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """The slippy-map tile containing a point.

    The standard web-mercator formula, written out because the alternative is a
    dependency for six lines of arithmetic.

    The int() calls are the reason for the try. A latitude beyond the
    projection's own limit of ±85.0511 divides the world into more tiles than
    there are, and NaN or infinity — which a coordinate read out of a JSON
    payload can be — raises on the conversion rather than on the arithmetic. The
    point is to fail with something that names the coordinates, since the raw
    error is "cannot convert float NaN to integer" and that is all it says.
    """
    count = 2**zoom
    try:
        x = int((lon + 180.0) / 360.0 * count)
        y = int(
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * count
        )
    except (ValueError, OverflowError) as error:
        raise ValueError(
            f"no tile for latitude={lat!r} longitude={lon!r} at zoom {zoom}"
        ) from error
    return max(0, min(count - 1, x)), max(0, min(count - 1, y))


def fetch_tile(session: requests.Session, url: str) -> str | None:
    """One tile as a data URI, or None.

    None rather than an exception: a tile server being down should cost the map a
    corner, not the morning its weather.
    """
    try:
        response = session.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        if response.status_code != 200 or response.content[:4] != b"\x89PNG":
            return None
        return "data:image/png;base64," + base64.b64encode(response.content).decode(
            "ascii"
        )
    except requests.RequestException:
        return None


def radar_frame(session: requests.Session) -> tuple[str, str] | None:
    """The newest radar frame: its tile path template and the time it is from."""
    try:
        response = session.get(
            RAINVIEWER_INDEX, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None

    frames = ((payload.get("radar") or {}).get("past") or [])
    if not frames:
        return None

    # The last of `past` is the most recent, not the first. `nowcast` is empty
    # more often than not, so it is not worth preferring when it exists.
    newest = frames[-1]
    host = str(payload.get("host") or "").rstrip("/")
    path = str(newest.get("path") or "")
    if not host or not path:
        return None

    try:
        stamp = datetime.fromtimestamp(int(newest["time"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return None

    # The panel is local time, so the frame label is too.
    local = stamp.astimezone()
    return f"{host}{path}", local.strftime("%H:%M")


def build_radar(session: requests.Session) -> dict[str, Any] | None:
    """A three-by-three grid of base and radar tiles around Kaunas."""
    frame = radar_frame(session)
    if not frame:
        return None
    tile_path, frame_time = frame

    centre_x, centre_y = tile_number(LATITUDE, LONGITUDE, ZOOM)
    offset = GRID // 2

    base: list[str | None] = []
    rain: list[str | None] = []
    for dy in range(-offset, offset + 1):
        for dx in range(-offset, offset + 1):
            x, y = centre_x + dx, centre_y + dy
            base.append(
                fetch_tile(session, BASEMAP.format(z=ZOOM, x=x, y=y))
            )
            # Colour scheme 2, smoothed, with snow shown. See RainViewer's docs.
            rain.append(
                fetch_tile(session, f"{tile_path}/256/{ZOOM}/{x}/{y}/2/1_1.png")
            )

    if any(tile is None for tile in base):
        # A map with a hole in it is worse than no map: the hole is read as "no
        # weather there" rather than "that tile did not download".
        return None

    return {
        "zoom": ZOOM,
        "grid": GRID,
        "centre": [LATITUDE, LONGITUDE],
        "base": base,
        # Radar tiles can individually fail and the map still reads: a missing
        # precipitation tile is indistinguishable from no precipitation, which is
        # what it usually is.
        "rain": [tile or "" for tile in rain],
        "time": frame_time,
        "attribution": ATTRIBUTION,
    }


# --- Weather -----------------------------------------------------------------


def fetch_forecast(session: requests.Session) -> dict[str, Any] | None:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "precipitation,weather_code,wind_speed_10m,wind_direction_10m,is_day"
        ),
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,precipitation_probability_max,sunrise,sunset"
        ),
        "timezone": TIMEZONE,
        "forecast_days": 4,
    }
    try:
        response = session.get(
            OPEN_METEO,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def build_panel(payload: dict[str, Any], radar: dict[str, Any] | None) -> dict[str, Any]:
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}

    dates = daily.get("time") or []
    days: list[dict[str, Any]] = []
    for index, date in enumerate(dates[:4]):
        try:
            weekday = datetime.strptime(date, "%Y-%m-%d").strftime("%a")
        except (TypeError, ValueError):
            weekday = str(date)

        def at(field: str, fallback: Any = None) -> Any:
            values = daily.get(field) or []
            return values[index] if index < len(values) else fallback

        days.append(
            {
                "date": date,
                "weekday": weekday,
                "code": at("weather_code"),
                "description": describe(at("weather_code")),
                "min": at("temperature_2m_min"),
                "max": at("temperature_2m_max"),
                "rain": at("precipitation_sum"),
                "probability": at("precipitation_probability_max"),
                "sunrise": str(at("sunrise") or "")[-5:],
                "sunset": str(at("sunset") or "")[-5:],
            }
        )

    return {
        "kind": "weather",
        "title": PLACE,
        "updated": str(current.get("time") or "")[-5:] or utc_now().strftime("%H:%M"),
        "current": {
            "code": current.get("weather_code"),
            "description": describe(current.get("weather_code")),
            "temperature": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation"),
            "wind": current.get("wind_speed_10m"),
            "wind_direction": compass(current.get("wind_direction_10m")),
            "is_day": bool(current.get("is_day")),
        },
        "days": days,
        "radar": radar,
        # Whatever the API says it answered in, so a timezone that silently
        # reverted to UTC would be visible on the page rather than in the sunrise.
        "timezone": str(payload.get("timezone") or TIMEZONE),
    }


def report(panels: list[dict[str, Any]]) -> None:
    print()
    print("=" * 70)
    print("Weather")
    print("=" * 70)
    for panel in panels:
        now = panel["current"]
        print(
            f"  {panel['title']} {panel['updated']} — {now['temperature']}°C "
            f"{now['description']}, feels {now['feels_like']}°, "
            f"{now['humidity']}% humidity, wind {now['wind']} km/h "
            f"{now['wind_direction']}"
        )
        for day in panel["days"]:
            print(
                f"    {day['weekday']} {day['date']}  "
                f"{day['min']}–{day['max']}°  {day['description']:<18} "
                f"{day['rain']}mm {day['probability']}%  "
                f"{day['sunrise']}→{day['sunset']}"
            )
        radar = panel.get("radar")
        if radar:
            print(
                f"    radar {radar['time']} · zoom {radar['zoom']} · "
                f"{radar['grid']}×{radar['grid']} tiles · {radar['attribution']}"
            )
        else:
            print("    radar: unavailable this run")
    print()
    print(f"Written to {OUTPUT_PATH}")


def main() -> None:
    session = requests.Session()

    forecast = fetch_forecast(session)
    if not forecast:
        # Not a failure. A page with no weather is the page it was yesterday, and
        # an empty file is what tells renderSection to draw nothing.
        print("No forecast available — writing an empty file.")
        save_json([], OUTPUT_PATH)
        return

    panel = build_panel(forecast, build_radar(session))
    panels = [panel]

    save_json(panels, OUTPUT_PATH)
    report(panels)


if __name__ == "__main__":
    main()
