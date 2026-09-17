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
import hashlib
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

# The closest zoom RainViewer serves. Beyond it the service returns an error card
# rather than a tile — a valid PNG with a 200 — so ZOOM must not exceed this, and
# it is written down separately from ZOOM precisely so that a future change to one
# does not quietly pass the other's limit. `test_the_zoom_is_one_rainviewer_
# actually_serves` holds the two together.
MAX_RADAR_ZOOM = 7

# Zoom 7, which is the closest RainViewer actually serves. Eight is not a smaller
# square of the same map: it is a card reading "Zoom Level Not Supported", served
# as a valid PNG with a 200, which is why the first attempt at this shipped a grid
# of nine error messages and looked like a working map in the JSON.
#
# It is still a zoomed-in view — closer to a street atlas than the country-wide
# picture that came before — because the window below shows one tile's width of
# this grid rather than all of it.
ZOOM = MAX_RADAR_ZOOM

# A zoom RainViewer certainly refuses, used once per run to fingerprint its error
# card. Derived rather than written out, so that raising the ceiling above moves
# this with it instead of leaving the fingerprint asking for a zoom that now works
# — which would make every tile compare unequal to it and pass. See
# error_card_hash.
UNSUPPORTED_ZOOM = MAX_RADAR_ZOOM + 4

# Tiles per axis in the downloaded grid, and therefore how many window-widths the
# layer is. Two, not three: the window is one tile wide and the centring shift
# moves it at most half a tile either way, so a two-by-two grid covers everything
# that can be seen. Three would download nine tiles per frame and draw four of
# them.
GRID = 2

# How many radar frames the loop animates. Six covers the hour leading up to now
# at RainViewer's ten-minute steps, and six frames of four tiles is about 500KB —
# the animation is not what makes this file large.
LOOP_FRAMES = 6

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


# Code to drawing, grouped the way a reader groups them rather than the way the
# codes are numbered. 51 and 55 are different drizzle intensities and the same
# picture; 80 to 82 are showers, which the code list files under rain. Two
# drawings for light drizzle and heavy drizzle would be two drawings nobody could
# tell apart at 22 pixels.
#
# On this side of the wire rather than in render.js because the pipeline already
# decides what the weather *is* — see describe() — and the renderer should no more
# choose the shape than it chooses the words. It also makes it testable.
WEATHER_KINDS: tuple[tuple[frozenset[int], str], ...] = (
    (frozenset({0}), "clear"),
    (frozenset({1, 2}), "partly"),
    (frozenset({3}), "cloudy"),
    (frozenset({45, 48}), "fog"),
    (frozenset({51, 53, 55, 56, 57}), "drizzle"),
    (frozenset({61, 63, 80, 81}), "rain"),
    (frozenset({65, 66, 67, 82}), "heavy"),
    (frozenset({71, 73, 75, 77, 85, 86}), "snow"),
    (frozenset({95, 96, 99}), "storm"),
)

# What an unrecognised code gets. Not an empty string: a missing drawing is a
# blank square at the top of the panel, which reads as a rendering fault rather
# than as weather nobody classified.
DEFAULT_KIND = "cloudy"


def icon_kind(code: Any) -> str:
    """Which drawing a WMO code gets. Never empty."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return DEFAULT_KIND
    for codes, kind in WEATHER_KINDS:
        if value in codes:
            return kind
    return DEFAULT_KIND


def _rounded(value: Any) -> int | None:
    """A whole number, or None.

    Surface pressure arrives as 1017.9 and humidity as 85.0, and nobody has ever
    wanted the decimal. None rather than a dash: the renderer decides how to show
    an absence, and a dash here would be a string pretending to be a number.
    """
    try:
        return int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


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


def tile_frac(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Where a point falls in the tile grid, as a fraction rather than an index.

    The integer version throws away the part that decides whether a map is
    centred on a city or has it jammed against an edge — which is the whole
    difference between "zoomed in on Kaunas" and "somewhere near Kaunas".
    """
    count = 2**zoom
    return (
        (lon + 180.0) / 360.0 * count,
        (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * count,
    )


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


def fetch_tile(
    session: requests.Session, url: str, error_hash: str | None = None
) -> str | None:
    """One tile as a data URI, or None.

    None rather than an exception: a tile server being down should cost the map a
    corner, not the morning its weather.

    `error_hash` is what stops a tile service answering with a picture instead of
    a refusal. RainViewer serves zoom levels up to seven and returns a 1.3KB card
    reading "Zoom Level Not Supported" for anything beyond — a perfectly valid
    PNG that passes a status check, a content-type check and a magic-byte check,
    and that drew a tidy nine-tile map of error messages before anyone noticed.
    """
    try:
        response = session.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        if response.status_code != 200 or response.content[:4] != b"\x89PNG":
            return None
        if error_hash and fingerprint(response.content) == error_hash:
            return None
        return "data:image/png;base64," + base64.b64encode(response.content).decode(
            "ascii"
        )
    except requests.RequestException:
        return None


def fingerprint(payload: bytes) -> str:
    """A stable digest of a tile, for comparing one against another.

    Nothing to do with integrity — both sides of every comparison are bytes this
    process just downloaded — but sha256 rather than md5 because the linters flag
    md5 on sight and arguing with a linter about a fingerprint costs more than
    hashing 1.3KB twice.
    """
    return hashlib.sha256(payload).hexdigest()


def error_card_hash(session: requests.Session, host: str, path: str) -> str | None:
    """The fingerprint of the tile RainViewer serves for a zoom it does not have.

    Asked once per run, at a zoom it certainly refuses, and compared against
    everything else. The card is byte-identical every time, which makes this
    exact — where guessing from file size cannot separate an error card from a
    legitimately empty radar tile with no rain in it, since both are about a
    kilobyte and both are valid PNGs.
    """
    x, y = tile_number(LATITUDE, LONGITUDE, UNSUPPORTED_ZOOM)
    try:
        response = session.get(
            f"{host}{path}/256/{UNSUPPORTED_ZOOM}/{x}/{y}/2/1_1.png",
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        if response.status_code != 200 or response.content[:4] != b"\x89PNG":
            return None
        return fingerprint(response.content)
    except requests.RequestException:
        return None


def radar_frames(session: requests.Session) -> tuple[str, list[tuple[str, str]]] | None:
    """The recent radar frames: the tile host, and a list of (path, local time).

    A list rather than the newest one, because the panel animates them. RainViewer
    keeps around thirteen frames of the last two hours at roughly ten-minute
    steps; the most recent `LOOP_FRAMES` are taken, so the loop covers the hour
    leading up to now, which is the hour that says where the rain is going.

    `nowcast` is empty more often than not, so the loop is built from `past`
    alone rather than mixing forecast frames into animation. A loop that jumps
    from observation to prediction looks like a glitch.
    """
    try:
        response = session.get(
            RAINVIEWER_INDEX, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None

    past = (payload.get("radar") or {}).get("past") or []
    host = str(payload.get("host") or "").rstrip("/")
    if not past or not host:
        return None

    frames: list[tuple[str, str]] = []
    for entry in past[-LOOP_FRAMES:]:
        path = str(entry.get("path") or "")
        if not path:
            continue
        try:
            stamp = datetime.fromtimestamp(int(entry["time"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        # Local, because the label beside the map is local and a radar stamped
        # in UTC is two or three hours out for most of the year.
        frames.append((path, stamp.astimezone().strftime("%H:%M")))

    return (host, frames) if frames else None


def view_window(frac: float, grid: int) -> tuple[int, float]:
    """Where the downloaded grid starts, and how far to slide it.

    `frac` is an absolute tile coordinate with a fraction on it — at zoom 7
    Kaunas is at 72.49 across, not at 1.49. Getting that wrong is not a subtle
    error: mixing a grid offset into it and then using the result as an index
    fetched tiles 0 and 1, which are open ocean, and the map came back as four
    near-identical blank images and a 16KB file that every check passed.

    The window is one tile wide and centred on the point, so the grid starts half
    a tile before it and the point lands somewhere inside. The second value is
    how far that somewhere is from the middle, as a fraction of the grid.
    """
    try:
        first = math.floor(frac - 0.5)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"cannot place tile coordinate {frac!r}") from error
    return first, 0.5 - (frac - first) / grid


def build_radar(session: requests.Session) -> dict[str, Any] | None:
    """Base map, and a stack of radar frames over it, centred on Kaunas."""
    index = radar_frames(session)
    if not index:
        return None
    host, frames = index

    # What RainViewer answers with when asked for a zoom it does not serve. The
    # panel below draws at zoom seven because that is as close as it goes, and
    # this is what says so rather than trusting the constant to stay true.
    error_hash = error_card_hash(session, host, frames[-1][0])

    # Fractional, not integer: the city is almost never in the middle of its own
    # tile — Kaunas sits at 0.99 across and 0.10 down, which is the top-right
    # corner — so a grid built from whole tiles puts it hard against an edge with
    # most of the map showing somewhere nobody asked about.
    frac_x, frac_y = tile_frac(LATITUDE, LONGITUDE, ZOOM)

    # Which tiles the window can actually reach, and the shift that puts the city
    # in the middle of it.
    #
    # The window is one tile wide — it shows a single tile's worth of the grid —
    # and the shift moves it at most half a tile either way, so two tiles an axis
    # is everything that can be seen. Three would download nine per frame and draw
    # four of them, which at six frames is most of a megabyte spent on tiles that
    # never reach a pixel.
    corners: list[int] = []
    shift: list[float] = []
    for frac in (frac_x, frac_y):
        first, offset = view_window(frac, GRID)
        corners.append(first)
        shift.append(offset)

    start_x, start_y = corners
    offset_x, offset_y = shift

    def axis(start: int) -> list[int]:
        return [start + i for i in range(GRID)]

    xs, ys = axis(start_x), axis(start_y)

    base: list[str | None] = []
    for y in ys:
        for x in xs:
            base.append(
                fetch_tile(session, BASEMAP.format(z=ZOOM, x=x, y=y), error_hash)
            )

    if any(tile is None for tile in base):
        # A map with a hole in it is worse than no map: the hole reads as "no
        # weather there" rather than "that tile did not download".
        return None

    # One grid per frame. Radar tiles can individually fail and the map still
    # reads — a missing precipitation tile is indistinguishable from no
    # precipitation, which is what it usually is.
    stacks: list[dict[str, Any]] = []
    for path, when in frames:
        tiles: list[str] = []
        for y in ys:
            for x in xs:
                tiles.append(
                    fetch_tile(
                        session,
                        f"{host}{path}/256/{ZOOM}/{x}/{y}/2/1_1.png",
                        error_hash,
                    )
                    or ""
                )
        stacks.append({"time": when, "tiles": tiles})

    if not stacks:
        return None

    return {
        "zoom": ZOOM,
        "grid": GRID,
        "centre": [LATITUDE, LONGITUDE],
        "shift": [offset_x, offset_y],
        "origin": [start_x, start_y],
        "base": base,
        "frames": stacks,
        # The label and the credit belong to the newest frame, which is what the
        # still image shows and what the loop settles on.
        "time": stacks[-1]["time"],
        "from": stacks[0]["time"],
        "attribution": ATTRIBUTION,
    }


# --- Weather -----------------------------------------------------------------


def fetch_forecast(session: requests.Session) -> dict[str, Any] | None:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "precipitation,weather_code,wind_speed_10m,wind_direction_10m,is_day,"
            "surface_pressure"
        ),
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_sum,precipitation_probability_max,sunrise,sunset"
        ),
        # Hourly, for the row of the next few hours the layout has under the big
        # temperature. WMO code and chance of rain rather than millimetres: an
        # hour's rainfall in mm is a number nobody has a feel for.
        "hourly": "weather_code,temperature_2m,precipitation_probability",
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


def build_hours(payload: dict[str, Any], count: int = 6) -> list[dict[str, Any]]:
    """The next few hours, starting from now rather than from midnight.

    Open-Meteo returns hourly from 00:00 of the first day, so the row has to be
    sliced from the current hour or it draws tonight's weather as a forecast for
    the morning. `current.time` is what places the slice — it is the only field in
    the response that says where now is.
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return []

    current = str((payload.get("current") or {}).get("time") or "")
    # "2026-09-17T12:15" and "2026-09-17T12:00" — the hour is the first twelve
    # characters, which is why this compares prefixes rather than parsing.
    hour_prefix = current[:13]
    start = next(
        (i for i, t in enumerate(times) if str(t)[:13] >= hour_prefix), 0
    ) if hour_prefix else 0

    hours: list[dict[str, Any]] = []
    for index in range(start, min(start + count, len(times))):

        def at(field: str) -> Any:
            values = hourly.get(field) or []
            return values[index] if index < len(values) else None

        codes = at("weather_code")
        hours.append(
            {
                # "07:00" out of "2026-09-17T07:00" — the API pads the hour, and
                # slicing the clock is cheaper than parsing and re-formatting it.
                "time": str(times[index])[-5:],
                "code": codes,
                "kind": icon_kind(codes),
                "description": describe(codes),
                "temperature": at("temperature_2m"),
                "probability": at("precipitation_probability"),
            }
        )
    return hours


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
                "kind": icon_kind(at("weather_code")),
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
            "kind": icon_kind(current.get("weather_code")),
            "description": describe(current.get("weather_code")),
            "temperature": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation"),
            "wind": current.get("wind_speed_10m"),
            "wind_direction": compass(current.get("wind_direction_10m")),
            # Surface pressure, which is the one a barometer reads and the one the
            # mock shows. Rounded here rather than in the renderer: it arrives as
            # 1017.9 and nobody has ever wanted the decimal.
            "pressure": _rounded(current.get("surface_pressure")),
            "is_day": bool(current.get("is_day")),
        },
        "days": days,
        # The hourly row, and the radar. `days` is today first, then the three
        # drawn as cards underneath.
        "hours": build_hours(payload),
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
