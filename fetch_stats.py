#!/usr/bin/env python3
"""
fetch_stats.py — the Sports page's dashboard: results, standings, what's next.

The sports page was a feed of articles. This gives it the panels above the feed: the
finished games, the two teams being watched, what is on next, and one feature with
the championship table beside it.

Reads, in order of how well each sport is served:

  Formula 1   Jolpica (the Ergast successor), free and keyless. Last race, drivers'
              and constructors' championships, and the next round. The best-served
              sport here by a distance.

  NBA         ESPN's web API: the scoreboard, the standings, per-team season
              averages, the roster, and per-player averages. Note that ESPN's other
              host — site.api.espn.com — answers 403 to this pipeline; the
              site.web.api host does not, which is why every ESPN URL below uses it.

  EuroLeague  Nothing, and this is a finding rather than an oversight. Eight routes
              were tried and all are gone or refuse: api-live.euroleague.net (404),
              api.euroleaguebasketball.net (no connection), the site's own api (429),
              the widget host (does not resolve), ESPN (400 — does not carry the
              competition), balldontlie (401, needs a key), FIBA (HTML), and
              TheSportsDB, which carries EuroCup, the Basketball Champions League and
              the ABA League but not EuroLeague, with only ten NBA teams and no
              badges. So no EuroLeague panel exists, and the tab shows news only.

Also worth knowing: mid-September is the quiet part of the calendar. The NBA
preseason opens on 3 October and EuroLeague later still, so the panel shows the next
scheduled game rather than a finished one, and team records read 0-0. The machinery
is season-agnostic — it reports whatever the scoreboard holds.

Writes data/stats.json as an ordered list of panels. Each panel carries a `league`
so the page's filter tabs can show or hide it, and a `kind` that tells the page which
of the four layouts to draw.

Usage:
    python fetch_stats.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json

# Sibling modules. Python puts its own directory on sys.path; pyright analyses each
# file standalone and cannot see them, hence the directives.
from common import save_json  # pyright: ignore[reportMissingImports]
from rss_feeds import USER_AGENT  # pyright: ignore[reportMissingImports]

import requests

OUTPUT_PATH = Path("data/stats.json")

TIMEOUT = 20.0

# How deep each table goes. Five is a page-sized glimpse; a championship table is
# twenty rows nobody scrolls on a phone.
MAX_ROWS = 5

# ESPN's web host. site.api.espn.com is a 403 for anything that is not a browser;
# this one is not, and serves the same shapes.
ESPN = "https://site.web.api.espn.com/apis"
JOLPICA = "https://api.jolpi.ca/ergast/f1"

# The two teams the spotlight shows. Only the NBA one can be filled — there is no
# EuroLeague source — but the shape is here so adding one later is a single entry.
# `key` is the abbreviation ESPN files the team under.
SPOTLIGHT: tuple[dict[str, str], ...] = (
    {"league": "NBA", "key": "okc", "label": "Oklahoma City Thunder"},
)

# The players this page tracks. Resolved to ESPN athlete ids at run time by name,
# because hard-coded ids rot silently: a stale id returns someone else's stats,
# which is worse than returning none.
PLAYERS: tuple[str, ...] = (
    "Domantas Sabonis",
    "Jonas Valanciunas",
    "Matas Buzelis",
    "Kasparas Jakucionis",
)

# Columns worth showing, in order, from the ESPN team and player averages.
TEAM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("avgPoints", "PPG"),
    ("avgPointsAgainst", "OPP PPG"),
    ("fieldGoalPct", "FG%"),
    ("avgRebounds", "REB"),
    ("avgAssists", "AST"),
)

PLAYER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("avgPoints", "PPG"),
    ("avgRebounds", "RPG"),
    ("avgAssists", "APG"),
)


def fetch_json(url: str) -> Any:
    """GET and parse, or None. Never raises: a dead stat source is not a dead page."""
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


# --- Formula 1 ---------------------------------------------------------------


def f1_final() -> dict[str, Any] | None:
    """The last race as a final-games card."""
    data = fetch_json(f"{JOLPICA}/current/last/results/?format=json")
    races = ((data or {}).get("MRData") or {}).get("RaceTable", {}).get("Races") or []
    if not races:
        return None

    race = races[0]
    lines = []
    for result in (race.get("Results") or [])[:3]:
        person = result.get("Driver") or {}
        name = f"{str(person.get('givenName') or '')[:1]}. {person.get('familyName') or ''}"
        lines.append(
            {
                "place": str(result.get("positionText") or result.get("position") or ""),
                "name": name.strip(),
                "value": str(result.get("Time", {}).get("time") or f"+{result.get('points')} pts")
                if isinstance(result.get("Time"), dict)
                else f"{result.get('points')} pts",
            }
        )

    return {
        "league": "F1",
        "status": "Final",
        "title": str(race.get("raceName") or "Last race"),
        "lines": lines,
    }


def f1_upcoming() -> dict[str, Any] | None:
    data = fetch_json(f"{JOLPICA}/2026/next/?format=json")
    races = ((data or {}).get("MRData") or {}).get("RaceTable", {}).get("Races") or []
    if not races:
        return None
    race = races[0]
    return {
        "league": "F1",
        "title": str(race.get("raceName") or "Next race"),
        "detail": str(race.get("date") or ""),
        "time": str(race.get("time") or ""),
    }


def standings_entries(
    lists: list[dict[str, Any]], kind: str
) -> list[Any]:
    """The entries from a Jolpica standings payload, whatever the capitalisation.

    Jolpica capitalises these keys — DriverStandings, ConstructorStandings — and a
    lookup that misses returns an empty list rather than raising, so both
    championship tables silently vanished from the first run of this file while
    the race result beside them worked perfectly. Matched case-insensitively so a
    spelling change cannot do the same thing quietly again.
    """
    wanted = f"{kind}standings".lower()
    for payload in lists:
        for key, value in payload.items():
            if key.lower() == wanted and value:
                return list(value)
    return []


def f1_standings(kind: str) -> dict[str, Any] | None:
    """Drivers' or constructors' table, as columns and rows."""
    plural = "Drivers" if kind == "driver" else "Constructors"
    data = fetch_json(f"{JOLPICA}/2026/{kind}standings/?format=json")
    lists = (
        ((data or {}).get("MRData") or {})
        .get("StandingsTable", {})
        .get("StandingsLists")
        or []
    )
    if not lists:
        return None

    entries = standings_entries(lists, kind)
    if not entries:
        return None

    rows = []
    for entry in entries[:MAX_ROWS]:
        if kind == "driver":
            person = entry.get("Driver") or {}
            name = (
                f"{str(person.get('givenName') or '')[:1]}. "
                f"{person.get('familyName') or ''}"
            )
        else:
            name = str((entry.get("Constructor") or {}).get("name") or "")
        rows.append(
            [
                str(entry.get("position") or ""),
                name.strip(),
                str(entry.get("points") or ""),
            ]
        )

    return {
        "title": f"{plural}' championship",
        "columns": ["Pos", plural[:-1], "Pts"],
        "rows": rows,
    }


# --- NBA ---------------------------------------------------------------------


def nba_scoreboard() -> list[dict[str, Any]] | None:
    """Finished games, or the next scheduled one when the season is dark.

    Returns a list of games, not one object — the page shows up to three mini-cards
    and the same call feeds both the results panel and the upcoming one.
    """
    data = fetch_json(f"{ESPN}/site/v2/sports/basketball/nba/scoreboard")
    events = (data or {}).get("events") or []
    if not events:
        return None

    games = []
    for event in events[:3]:
        competition = (event.get("competitions") or [{}])[0]
        status = ((competition.get("status") or {}).get("type") or {}).get(
            "description"
        ) or ""
        teams = []
        for team in competition.get("competitors") or []:
            info = team.get("team") or {}
            teams.append(
                {
                    "name": str(info.get("shortDisplayName") or info.get("name") or "?"),
                    "abbreviation": str(info.get("abbreviation") or ""),
                    "logo": str(info.get("logo") or ""),
                    "score": str(team.get("score") or ""),
                    "winner": bool(team.get("winner")),
                }
            )
        if len(teams) < 2:
            continue
        games.append(
            {
                "league": "NBA",
                "status": status,
                # The whole timestamp, not just the day: ESPN sends a kick-off
                # time with it and the page formats both. Trimming to the date
                # here threw away the only part of an offseason fixture that a
                # reader can use.
                "date": str(event.get("date") or ""),
                "teams": teams,
            }
        )

    return games or None


# A conference name short enough to sit under a team's record without wrapping.
# the layout shows "1st West"; "1st Western Conference" took two lines in the
# ninety-six pixel column the spotlight gives it.
SHORT_CONFERENCE: dict[str, str] = {
    "Western Conference": "West",
    "Eastern Conference": "East",
}


def nba_standings() -> dict[str, dict[str, Any]]:
    """Per-team record, standing, crest and the stats this endpoint exposes.

    Keyed by abbreviation — "OKC" — because the standings payload carries no
    `slug` at all; the team objects give a display name, an abbreviation and logos
    and nothing else. Keying on the missing field returned an empty record and the
    spotlight panel showed a bare team name.

    The stats here are not the same set the team-statistics endpoint returns, and
    the overlap is not complete: points against is only in this one.
    """
    data = fetch_json(f"{ESPN}/v2/sports/basketball/nba/standings")
    out: dict[str, dict[str, Any]] = {}
    for conference in (data or {}).get("children") or []:
        label = str(conference.get("name") or "")
        entries = (conference.get("standings") or {}).get("entries") or []
        for index, entry in enumerate(entries, 1):
            team = entry.get("team") or {}
            key = str(team.get("abbreviation") or "").lower()
            if not key:
                continue
            stats = {
                str(s.get("name")): str(s.get("displayValue") or "")
                for s in (entry.get("stats") or [])
            }
            wins, losses = stats.get("wins", "0"), stats.get("losses", "0")
            logos = team.get("logos") or []
            short = SHORT_CONFERENCE.get(label, label)
            out[key] = {
                "record": f"{wins}-{losses}",
                "standing": f"{index}{_ordinal_suffix(index)} {short}",
                "logo": str((logos[0] if logos else {}).get("href") or ""),
                "stats": stats,
            }
    return out


def _ordinal_suffix(number: int) -> str:
    if 11 <= number % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def _stat_or_dash(value: Any) -> str:
    """A statistic, or a dash when there is nothing to report.

    Preseason teams have played no games, so every average comes back as "0.0" or
    empty and the spotlight read "OPP PPG 0.0" — which looks like a real and
    alarming statistic rather than an absence.
    """
    text = str(value or "").strip()
    if not text or text in {"0", "0.0", "0.00", "—", "-"}:
        return "—"
    return text


def nba_team_stats(slug: str) -> dict[str, str]:
    """The team's season averages, keyed by the API's own field names."""
    data = fetch_json(f"{ESPN}/site/v2/sports/basketball/nba/teams/{slug}/statistics")
    out: dict[str, str] = {}
    for category in (data or {}).get("results", {}).get("stats", {}).get("categories", []):
        for stat in category.get("stats") or []:
            name = str(stat.get("name") or "")
            if name:
                out[name] = str(stat.get("displayValue") or "")
    return out


def athlete_id(name: str) -> str | None:
    data = fetch_json(f"{ESPN}/common/v3/search?query={name}&limit=5&type=player")
    for item in (data or {}).get("items") or []:
        if str(item.get("displayName") or "").lower() == name.lower():
            return str(item.get("id") or "")
    return None


def athlete_averages(player_id: str) -> dict[str, str]:
    """The player's most recent season averages, keyed by the API's field names."""
    data = fetch_json(
        f"{ESPN}/common/v3/sports/basketball/nba/athletes/{player_id}/stats"
    )
    for category in (data or {}).get("categories") or []:
        if category.get("name") != "averages":
            continue
        names = category.get("names") or []
        seasons = category.get("statistics") or []
        if not names or not seasons:
            continue
        # The list runs oldest to newest; the last entry is the current season.
        latest = seasons[-1]
        averages = {
            str(n): str(v) for n, v in zip(names, latest.get("stats") or [])
        }
        averages["_season"] = str((latest.get("season") or {}).get("displayName") or "")
        return averages
    return {}


def nba_roster(slug: str) -> list[str]:
    data = fetch_json(f"{ESPN}/site/v2/sports/basketball/nba/teams/{slug}/roster")
    return [
        str(a.get("displayName") or "")
        for a in (data or {}).get("athletes") or []
        if a.get("displayName")
    ]


def nba_leader(slug: str) -> dict[str, Any] | None:
    """The team's most-used player by minutes, with their averages.

    "Most minutes" rather than "most points" because a bench scorer is not the
    player a team is built around, and minutes is the closest thing to that in a
    box score.
    """
    best: tuple[float, str, dict[str, str]] | None = None
    for name in nba_roster(slug):
        player_id = athlete_id(name)
        if not player_id:
            continue
        averages = athlete_averages(player_id)
        try:
            minutes = float(averages.get("avgMinutes") or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        if minutes and (best is None or minutes > best[0]):
            best = (minutes, name, averages)

    if not best:
        return None
    _minutes, name, averages = best
    return {
        "name": name,
        "stats": [
            [_stat_or_dash(averages.get(field)), short] for field, short in PLAYER_COLUMNS
        ],
    }


def nba_players() -> dict[str, Any] | None:
    """Season averages for the tracked Lithuanian players."""
    rows: list[list[str]] = []
    season = ""
    for name in PLAYERS:
        player_id = athlete_id(name)
        if not player_id:
            continue
        averages = athlete_averages(player_id)
        if not averages:
            continue
        season = season or str(averages.get("_season") or "")
        rows.append(
            [name] + [_stat_or_dash(averages.get(field)) for field, _ in PLAYER_COLUMNS]
        )

    if not rows:
        return None
    return {
        "league": "NBA",
        "title": "Lietuva · NBA",
        "note": season or "latest season",
        "columns": ["Player"] + [short for _, short in PLAYER_COLUMNS],
        "rows": rows,
    }


# --- Panels ------------------------------------------------------------------


# Competitions this page follows and has no results for. Listed rather than
# hard-coded to one, because the absence is a property of the competition, not of
# the page: EuroLeague publishes nothing reachable, so its tab would otherwise be
# an empty frame above the news it does have.
#
# (league tag, display name, the note explaining the missing half)
NEWS_PANELS: tuple[tuple[str, str, str], ...] = (
    ("EUROLEAGUE", "EuroLeague", "No results feed"),
)


def sports_rows() -> list[dict[str, Any]]:
    """What fetch_sports.py chose for this page, or nothing.

    Read rather than searched: the feed already decided which stories matter and
    the panels take from that decision rather than looking for the same news a
    second time. fetch_sports.py is step 7 of run.sh and this is step 8, so the
    file is there.
    """
    path = Path("data/sports.json")
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("url")]


def sports_feature(table: dict[str, Any] | None) -> dict[str, Any] | None:
    """The F1 story from the sports page, with the drivers' table beside it.

    the layout's feature is not a statistic: it is an article — a picture, a
    headline, a paragraph and a link — with the championship table filling the
    right-hand column. So this reads what fetch_sports.py already chose rather
    than searching for F1 news a second time, and the page and the panel cannot
    disagree about which race is in the news.

    That ordering is why this is here and not in the browser: fetch_sports.py is
    step 7 of run.sh and this is step 8, so the file exists by the time this runs.
    """
    for row in sports_rows():
        if row.get("league") != "F1":
            continue
        # No picture is not a reason to drop it. Several F1 feeds are reached
        # through Google, whose links resolve to a consent screen and so never
        # yield one, and the card is built to carry on without it.
        if not row.get("title") or not row.get("url"):
            continue
        return {
            "kind": "feature",
            "league": "F1",
            "title": str(row["title"]),
            "summary": str(row.get("description") or ""),
            "url": str(row["url"]),
            "image": str(row.get("urlToImage") or ""),
            "source": str(row.get("source") or ""),
            "table": table,
        }
    return None


def build_panels() -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = []

    # 1. Final games — one mini-card per league that has a result.
    finals = [card for card in (nba_scoreboard() or []) if "scheduled" not in card["status"].lower()]
    f1 = f1_final()
    if f1:
        finals.append(
            {
                "league": "F1",
                "status": "Final",
                "title": f1["title"],
                "lines": f1["lines"],
            }
        )
    if finals:
        panels.append(
            {"kind": "games", "league": "ALL", "title": "Final games", "cards": finals}
        )

    # 2. Team spotlight — one panel per team, side by side.
    standings = nba_standings()
    spotlight: list[dict[str, Any]] = []
    for team in SPOTLIGHT:
        key = team["key"]
        record = standings.get(key, {})
        # Two endpoints, one table: the team-statistics call has shooting and
        # rebounding, the standings call has points against, and neither has both.
        stats = dict(record.get("stats") or {})
        stats.update(nba_team_stats(key))
        leader = nba_leader(key)
        if not stats and not leader:
            continue
        spotlight.append(
            {
                "league": team["league"],
                "team": team["label"],
                "record": record.get("record", ""),
                "standing": record.get("standing", ""),
                "logo": record.get("logo", ""),
                "stats": [
                    [short, _stat_or_dash(stats.get(field))] for field, short in TEAM_COLUMNS
                ],
                "player": leader,
            }
        )
    if spotlight:
        panels.append(
            {
                "kind": "spotlight",
                "league": "NBA",
                "title": "Team spotlight",
                "cards": spotlight,
            }
        )

    # 3. Upcoming games — NBA from the scoreboard, F1 from the calendar.
    upcoming: list[dict[str, Any]] = []
    for card in nba_scoreboard() or []:
        if "scheduled" not in card["status"].lower():
            continue
        upcoming.append(
            {
                "league": "NBA",
                # Names and crests, the same shape the results panel uses, so a
                # fixture reads as two clubs rather than two words.
                "teams": [
                    {"name": t["name"], "logo": t["logo"]} for t in card["teams"]
                ],
                "time": card["date"],
            }
        )
    race = f1_upcoming()
    if race:
        # The date and the time arrive separately here, and the page wants one
        # value it can format either way.
        when = race["detail"]
        if race.get("time"):
            when = f"{when}T{race['time']}"
        # A race has a name where a game has teams, and no crest either way.
        upcoming.append(
            {
                "league": "F1",
                "teams": [{"name": race["title"], "logo": ""}],
                "time": when,
            }
        )
    if upcoming:
        panels.append(
            {
                "kind": "upcoming",
                "league": "ALL",
                "title": "Upcoming games",
                "cards": upcoming,
            }
        )

    # 4. The feature, and whichever championship table did not go into it.
    #
    # The drivers' table belongs to the feature card — it is the column beside the
    # story in the layout — so it is built first, handed over, and not repeated as a
    # panel of its own. The constructors' table has no such story and stays a
    # panel. When there is no F1 story the drivers' table falls back to being one
    # too, so a quiet news day costs nothing.
    driver_table = f1_standings("driver")
    if driver_table:
        driver_table.update({"league": "F1", "kind": "table"})

    feature = sports_feature(driver_table)

    standings_panels: list[dict[str, Any]] = []
    if feature:
        standings_panels.append(feature)
    elif driver_table:
        standings_panels.append(driver_table)

    constructor_table = f1_standings("constructor")
    if constructor_table:
        constructor_table.update({"league": "F1", "kind": "table"})
        standings_panels.append(constructor_table)

    players = nba_players()
    if players:
        players["kind"] = "table"
        standings_panels.append(players)

    # 5. The competitions with news but no results, as a panel of headlines.
    #
    # `tabs` restricts this to its own tab. It exists because that tab would
    # otherwise be an empty frame, and under ALL the same two headlines are
    # already in the feed below it — a panel that only repeats what is on screen
    # under its feet is furniture.
    rows = sports_rows()
    for league, label, note in NEWS_PANELS:
        cards = [
            {
                "title": str(row.get("title") or ""),
                "url": str(row["url"]),
                "source": str(row.get("source") or ""),
            }
            for row in rows
            if row.get("league") == league and row.get("title")
        ]
        if not cards:
            continue
        standings_panels.append(
            {
                "kind": "stories",
                "league": league,
                "tabs": [league],
                "title": f"{label} · in the news",
                "note": note,
                "cards": cards,
            }
        )

    return panels + standings_panels


def report(panels: list[dict[str, Any]]) -> None:
    print()
    print("=" * 68)
    print(f"Stats: {len(panels)} panel(s)")
    print("=" * 68)
    for panel in panels:
        size = len(panel.get("cards") or panel.get("rows") or [])
        print(f"  [{panel.get('league'):<10}] {panel['kind']:<10} "
              f"{str(panel.get('title'))[:34]:<34} ({size})")
    print()
    print(f"Written to {OUTPUT_PATH}")


def main() -> None:
    panels = build_panels()

    if not panels:
        # Not a failure: no sport is in season, or every source is down. The page
        # renders its news without a dashboard, which beats an empty dashboard.
        print("No statistics available — writing an empty file.")
        save_json([], OUTPUT_PATH)
        return

    save_json(panels, OUTPUT_PATH)
    report(panels)


if __name__ == "__main__":
    main()
