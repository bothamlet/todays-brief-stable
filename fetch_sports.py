#!/usr/bin/env python3
"""Collect sports news by competition."""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from common import (
    dedupe_by_url,
    newest_first,
    parse_timestamp,
    save_json,
    utc_now,
)
from publishers import Allowlist
from rss_feeds import require_healthy_feeds
from rss_feeds import (
    backfill_images,
    fetch_many,
    google_news_url,
)


OUTPUT_PATH = Path("data/sports.json")

LOOKBACK_HOURS = 48

MAX_STORIES = 10
MAX_PER_INTEREST = 2

# General basketball spans competitions and has no filter tab of its own.
COMPETITION_ORDER: tuple[str, ...] = ("NBA", "EUROLEAGUE", "F1", "")


INTERESTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "okc",
        "NBA · Thunder",
        (
            "oklahoma city",
            "okc",
            "thunder",
            "gilgeous-alexander",
            "chet holmgren",
            "jalen williams",
            "sam presti",
            "daigneault",
            "paycom center",
        ),
    ),
    (
        "lt_player",
        "NBA · Lietuva",
        (
            "sabonis",
            "valanciunas",
            "valančiūnas",
            "buzelis",
            "jakucionis",
            "jakučionis",
        ),
    ),
    (
        "zalgiris",
        "EuroLeague · Žalgiris",
        # Match the club name, not the city shared by other teams.
        ("žalgiris", "zalgiris"),
    ),
    (
        "lt_basket",
        "Krepšinis · Lietuva",
        (
            "lithuania",
            "lithuanian",
            "lietuvos",
            "lietuva",
            "rinktinė",
            "krepšinis",
            "lkl",
        ),
    ),
    (
        "f1",
        "Formula 1",
        (
            "formula 1",
            "formula one",
            "f1",
            "grand prix",
            "verstappen",
            "norris",
            "piastri",
            "hamilton",
            "ferrari",
            "mclaren",
            "red bull racing",
            "mercedes-amg",
            "aston martin",
            "alpine",
            "williams racing",
            "haas",
            "sprint race",
            "pole position",
            "safety car",
        ),
    ),
    (
        "euroleague",
        "EuroLeague",
        (
            "euroleague",
            "euro league",
            "final four",
            "olympiacos",
            "panathinaikos",
            "fenerbahce",
            "fenerbahçe",
            "baskonia",
            "anadolu efes",
            "virtus",
            "monaco basket",
        ),
    ),
    (
        "nba",
        "NBA",
        ("nba",),
    ),
    (
        "basketball",
        "Basketball",
        (
            "basketball",
            "eurobasket",
            "fiba",
            "world cup",
            "olympic",
            "final four",
        ),
    ),
)


DIRECT_FEEDS: tuple[tuple[str, str, str | None], ...] = (
    ("ESPN NBA", "https://www.espn.com/espn/rss/nba/news", "nba"),
    ("CBS Sports NBA", "https://www.cbssports.com/rss/headlines/nba/", "nba"),
    ("Yahoo Sports NBA", "https://sports.yahoo.com/nba/rss/", "nba"),
    ("SB Nation NBA", "https://www.sbnation.com/rss/nba/index.xml", "nba"),
    ("Hoops Rumors", "https://www.hoopsrumors.com/feed", "nba"),
    # F1
    ("ESPN F1", "https://www.espn.com/espn/rss/f1/news", "f1"),
    ("Motorsport.com", "https://www.motorsport.com/rss/f1/news/", "f1"),
    ("Autosport", "https://www.autosport.com/rss/f1/news/", "f1"),
    ("RaceFans", "https://www.racefans.net/feed/", "f1"),
    ("The Race", "https://the-race.com/feed/", "f1"),
    ("Formula1.com", "https://www.formula1.com/en/latest/all.xml", "f1"),
    # The English feed needs the /en/ path.
    ("TalkBasket", "https://www.talkbasket.net/euroleague/feed", "euroleague"),
    ("Eurohoops", "https://www.eurohoops.net/en/feed/", "euroleague"),
    ("Sportando", "https://sportando.basketball/en/feed/", "euroleague"),
    # Local Lithuanian sources broaden club coverage.
    ("15min", "https://www.15min.lt/rss/sportas", None),
)


GOOGLE_TOPICS: tuple[tuple[str, str, str, str], ...] = (
    ("NBA · Thunder", '"Oklahoma City Thunder"', "en", "okc"),
    ("NBA · Sabonis", '"Domantas Sabonis"', "en", "lt_player"),
    (
        "NBA · Valančiūnas",
        '"Jonas Valanciunas" OR "Jonas Valančiūnas"',
        "en",
        "lt_player",
    ),
    ("NBA · Buzelis", '"Matas Buzelis"', "en", "lt_player"),
    ("NBA · Jakučionis", '"Kasparas Jakucionis"', "en", "lt_player"),
    ("EuroLeague · Žalgiris", '"Žalgiris" basketbolas', "lt", "zalgiris"),
    ("Krepšinis · Lietuva", "Lietuvos krepšinis rinktinė", "lt", "lt_basket"),
    (
        "Basketball · World",
        'EuroBasket OR "FIBA World Cup" OR "Olympic basketball"',
        "en",
        "basketball",
    ),
)


TRUSTED_PUBLISHERS: frozenset[str] = frozenset(
    {
        # international wires and broadsheets
        "reuters",
        "ap news",
        "associated press",
        "abc news",
        "bbc",
        "the guardian",
        "the new york times",
        "the washington post",
        "the athletic",
        "the independent",
        "sky sports",
        "espn",
        # basketball
        "nba.com",
        "cbs sports",
        "nbc sports",
        "yahoo sports",
        "sports illustrated",
        "hoops rumors",
        "hoops rummage",
        "hoopshype",
        "the oklahoman",
        "okc thunder wire",
        "thunderous intentions",
        "eurohoops",
        "talkbasket",
        "sportando",
        "basketnews",
        "basketnews.lt",
        "krepsinis.net",
        # motorsport
        "motorsport.com",
        "autosport",
        "the race",
        "racefans",
        "formula1.com",
        "fia",
        # lithuanian
        "15min",
        "lrt",
        "delfi",
        "lrytas",
        "kauno diena",
        "basketnews",
    }
)

TRUSTED = Allowlist(TRUSTED_PUBLISHERS, exact={"si.com", "apnews.com"})


def publisher_is_trusted(name: str) -> bool:
    """Is this publisher one we are willing to put on the page?"""
    return name in TRUSTED




def interest_for(
    text: str, fallback: str | None
) -> tuple[str | None, str | None, bool]:
    """Name the first interest this text matches, or the feed's own fallback."""
    lowered = text.lower()
    for key, label, keywords in INTERESTS:
        if any(word in lowered for word in keywords):
            return key, label, True
    if fallback is None:
        return None, None, False
    for key, label, _keywords in INTERESTS:
        if key == fallback:
            return key, label, False
    return fallback, fallback, False


# Filter labels identify competitions, not teams.
LEAGUES: dict[str, str] = {
    "okc": "NBA",
    "lt_player": "NBA",
    "nba": "NBA",
    "f1": "F1",
    "zalgiris": "EUROLEAGUE",
    "euroleague": "EUROLEAGUE",
}


def league_for(interest: Any) -> str:
    """The competition a story belongs to, or "" when it belongs to none."""
    return LEAGUES.get(str(interest or ""), "")


JUNK = re.compile(
    r"\b(?:betting|bets?|bookmakers?|bookies|odds|wagers?|wagering"
    r"|gambl(?:e|ing)|casino|parlay|accumulator|tipsters?|punters?"
    r"|free spins|promo code)\b",
    re.IGNORECASE,
)


def is_junk(article: dict[str, Any]) -> bool:
    """True for gambling copy — see JUNK."""
    return bool(JUNK.search(str(article.get("title") or "")))




def collect() -> list[dict[str, Any]]:
    """Fetch every feed, tag interests, and drop what fails the bar."""
    direct_jobs = [(name, url) for name, url, _interest in DIRECT_FEEDS]
    google_jobs = [
        (label, google_news_url(query, locale))
        for label, query, locale, _interest in GOOGLE_TOPICS
    ]

    print(f"Fetching {len(direct_jobs)} direct feeds and {len(google_jobs)} searches...")
    direct = fetch_many(direct_jobs)
    google = fetch_many(
        google_jobs,
        publisher_from_source=True,
    )

    require_healthy_feeds({**direct, **google}, "Sports")

    direct_interest = {url: interest for _name, url, interest in DIRECT_FEEDS}
    google_interest = {
        google_news_url(query, locale): interest
        for _label, query, locale, interest in GOOGLE_TOPICS
    }

    kept: list[dict[str, Any]] = []
    rejected: list[str] = []

    # Keep the first matching feed's context for each URL.
    for url, rows in direct.items():
        fallback = direct_interest.get(url, "basketball")
        for row in rows:
            if is_junk(row):
                continue
            text = f"{row.get('title') or ''} {row.get('description') or ''}"
            key, label, matched = interest_for(text, fallback)
            if not matched and fallback is None:
                continue
            row["interest"], row["interest_label"] = key, label
            # Assign leagues before page selection groups stories by competition.
            row["league"] = league_for(key)
            row["via"] = "feed"
            kept.append(row)

    for url, rows in google.items():
        fallback = google_interest.get(url, "basketball")
        for row in rows:
            if is_junk(row):
                continue
            source = str(row.get("source") or "")
            # Filter publishers per item because search feeds mix sources.
            if not publisher_is_trusted(source):
                rejected.append(source or "(no publisher)")
                continue

            # Validate from the headline because search covers full article text.
            text = f"{row.get('title') or ''} {row.get('description') or ''}"
            key, label, matched = interest_for(text, fallback)
            if not matched and fallback != "basketball":
                continue

            row["interest"], row["interest_label"] = key, label
            row["league"] = league_for(key)  # see the note on the direct loop above
            row["via"] = "google"
            kept.append(row)

    if rejected:
        worst = Counter(rejected).most_common(6)
        print(
            f"  dropped {len(rejected)} items from untrusted publishers "
            f"({', '.join(f'{n}× {p}' for p, n in worst)})"
        )

    return kept




def select(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick and order the page: round-robin across competitions, freshest first."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        key = str(article.get("league") or "")
        buckets.setdefault(key, []).append(article)

    for rows in buckets.values():
        # Prefer imagery within each competition bucket.
        rows.sort(
            key=lambda a: (
                bool(a.get("urlToImage")),
                parse_timestamp(a.get("publishedAt")) or utc_now().replace(year=1970),
            ),
            reverse=True,
        )
        del rows[MAX_PER_INTEREST * 4 :]  # keep the sort cheap, keep some depth

    chosen: list[dict[str, Any]] = []
    taken: dict[str, int] = {}

    depth = 0
    while len(chosen) < MAX_STORIES and depth < MAX_PER_INTEREST:
        for key in COMPETITION_ORDER:
            if len(chosen) >= MAX_STORIES:
                break
            if taken.get(key, 0) > depth:
                continue
            pool = buckets.get(key) or []
            if len(pool) <= depth:
                continue
            chosen.append(pool[depth])
            taken[key] = taken.get(key, 0) + 1
        depth += 1

    return chosen




def report(chosen: list[dict[str, Any]], total: int) -> None:
    print()
    print("=" * 68)
    print(f"Sports page: {len(chosen)} of {total} candidates")
    print("=" * 68)
    for article in chosen:
        stamp = article.get("publishedAt") or "?"
        observed = parse_timestamp(stamp)
        when = observed.strftime("%a %H:%M") if observed else str(stamp)[:16]
        label = str(article.get("interest_label") or "")
        print(f"  [{label:<20}] {when}  {str(article.get('title') or '')[:44]}")
    print()
    print(f"Written to {OUTPUT_PATH}")


def main() -> None:
    cutoff = utc_now() - timedelta(hours=LOOKBACK_HOURS)

    articles = collect()

    fresh: list[dict[str, Any]] = []
    for article in articles:
        stamp = parse_timestamp(article.get("publishedAt"))
        # Keep undated articles rather than treating them as expired.
        if stamp is None or stamp >= cutoff:
            fresh.append(article)

    fresh = dedupe_by_url(fresh)
    fresh = newest_first(fresh)

    chosen = select(fresh)


    # Fetch preview images only for selected cards.
    filled = backfill_images(chosen)
    if filled:
        print(f"  recovered {filled} image(s) from the article pages")

    save_json(chosen, OUTPUT_PATH)
    report(chosen, len(fresh))


if __name__ == "__main__":
    main()
