#!/usr/bin/env python3
"""Discover world news from RSS."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from common import (
    dedupe_by_url,
    fail,
    newest_first,
    parse_timestamp,
    save_json,
    utc_now,
)
from rss_feeds import require_healthy_feeds
from rss_feeds import fetch_many


OUTPUT_PATH = Path("data/world_raw.json")

LOOKBACK_HOURS = 24  # drop items older than this
MAX_ARTICLES = 300  # safety valve only; clustering does the real reduction
RSS_WORKERS = 12  # parallel feed fetches

# Publisher-specific searches cover outlets without usable feeds.
DISCOVERY_FEEDS: dict[str, str] = {
    # left
    "The New York Times": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "The Washington Post": "https://feeds.washingtonpost.com/rss/world",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "NPR": "https://feeds.npr.org/1001/rss.xml",
    "ProPublica": "https://www.propublica.org/feeds/propublica/main",
    "The Atlantic": "https://www.theatlantic.com/feed/all/",
    # centre
    "BBC News": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "The Economist": "https://www.economist.com/the-world-this-week/rss.xml",
    "Financial Times": "https://www.ft.com/world?format=rss",
    "France 24": "https://www.france24.com/en/rss",
    "Le Monde": "https://www.lemonde.fr/en/rss/une.xml",
    "The Straits Times": "https://www.straitstimes.com/news/world/rss.xml",
    "Sky News": "https://feeds.skynews.com/feeds/rss/world.xml",
    # right
    "Fox News": "https://moxie.foxnews.com/google-publisher/world.xml",
    "The Dispatch": "https://thedispatch.com/feed/",
    "National Post": "https://nationalpost.com/feed",
}




def gather(feeds: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch every feed once. Returns (items, names that came back empty)."""
    results = fetch_many(list(feeds.items()), workers=RSS_WORKERS)

    require_healthy_feeds(results, "World discovery")
    items: list[dict[str, Any]] = []
    empty: list[str] = []
    for name, url in feeds.items():
        fetched = results.get(url, [])
        if not fetched:
            empty.append(name)
        items.extend(fetched)

    return items, empty


def recent_only(
    items: list[dict[str, Any]], cutoff: datetime
) -> tuple[list[dict[str, Any]], int]:
    """Keep items published on or after the cutoff. Returns (kept, dropped)."""
    dated = [(parse_timestamp(i.get("publishedAt")), i) for i in items]
    kept = [item for stamp, item in dated if stamp and stamp >= cutoff]
    return newest_first(kept), len(items) - len(kept)


def summarise(rows: list[dict[str, Any]]) -> str:
    """One line naming every outlet and how many articles it contributed."""
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get("source") or "?")
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{name} {n}" for name, n in sorted(counts.items()))




def main() -> None:
    cutoff = utc_now() - timedelta(hours=LOOKBACK_HOURS)

    print(f"Fetching {len(DISCOVERY_FEEDS)} feeds...")
    items, empty = gather(DISCOVERY_FEEDS)

    if not items:
        fail("no feed returned anything — check connectivity. Nothing written.")

    if empty:
        print(
            f"WARNING: {len(empty)} feed(s) returned nothing: {', '.join(empty)}",
            file=sys.stderr,
        )

    rows, dropped = recent_only(items, cutoff)
    if not rows:
        # Undated or lagging feeds should not empty the briefing.
        print(
            f"WARNING: nothing within the last {LOOKBACK_HOURS}h; "
            "keeping undated items too.",
            file=sys.stderr,
        )
        rows = newest_first(items)

    rows = dedupe_by_url(rows)[:MAX_ARTICLES]
    if not rows:
        fail("no usable articles after filtering. Nothing written.")

    save_json(rows, OUTPUT_PATH)

    print(
        f"Fetched {len(items)} items, dropped {dropped} older than "
        f"{LOOKBACK_HOURS}h -> {len(rows)} articles"
    )
    print(f"Sources: {summarise(rows)}")
    print(f"Written to {OUTPUT_PATH}")
    print(f"Newest: {rows[0]['title'][:78]}")


if __name__ == "__main__":
    main()
