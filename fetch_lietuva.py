#!/usr/bin/env python3
"""Collect and group Lithuanian news."""

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
from llm import ask_json
from publishers import Allowlist, normalise
from rss_feeds import require_healthy_feeds
from rss_feeds import (
    backfill_images,
    fetch_many,
    google_news_url,
)
from textutil import shared_words, significant_tokens


OUTPUT_PATH = Path("data/lietuva.json")

LOOKBACK_HOURS = 48

MAX_STORIES = 8

MAX_FRAMES = 3

DUPLICATE_TOKENS = 3

# Bound the model request to the latest articles.
GROUP_LIMIT = 80

# Output budget, sized to the number of headlines actually sent.
GROUP_TOKENS_PER_HEADLINE = 30
GROUP_TOKENS_BASE = 200

EPOCH = utc_now().replace(year=1970)


DIRECT_FEEDS: tuple[tuple[str, str], ...] = (
    # LRT serves its RSS feed through a query route.
    ("LRT", "https://www.lrt.lt/?rss"),
    ("Delfi", "https://www.delfi.lt/rss/feeds/lithuania.xml"),
    ("15min", "https://www.15min.lt/rss"),
    ("Lrytas", "https://www.lrytas.lt/rss/"),
    ("Kauno diena", "https://kauno.diena.lt/rss.xml"),
    ("Verslo žinios", "https://www.vz.lt/rss"),
    ("Bernardinai", "https://www.bernardinai.lt/feed/"),
    ("Diena", "https://www.diena.lt/rss"),
)

GOOGLE_NEWS_URL = (
    "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
)

GOOGLE_TOPICS: tuple[tuple[str, str, str], ...] = (
    ("Lietuva", "Lietuva", "lt"),
    ("Lietuvos naujienos", "Lietuvos naujienos", "lt"),
    ("Kaunas", "Kaunas", "lt"),
    ("Vilnius", "Vilnius", "lt"),
    ("Lithuania", "Lithuania", "en"),
)


TRUSTED_PUBLISHERS: frozenset[str] = frozenset(
    {
        "LRT",
        "LRT OPUS",
        "LRT televizija",
        "Delfi",
        "Delfi Plius",
        "15min",
        "Lrytas",
        "Lietuvos rytas",
        "Kauno diena",
        "Diena",
        "Respublika",
        "Bernardinai",
        "Verslo žinios",
        "ELTA",
        "BNS",
        # the popular end of the market, which is still the news
        "Alfa",
        "TV3",
        "LNK",
        "Pasaulis",
        "Veidas",
        # regional and specialist
        "Utenos diena",
        "Šiaulių naujienos",
        "Panevėžio balsas",
        "Klaipėda",
        "Vakarų ekspresas",
        "Made in Vilnius",
        "Kaunas",
        # English-language reporting on Lithuania
        "Reuters",
        "AP News",
        "Associated Press",
        "BBC",
        "The Guardian",
        "The New York Times",
        "Politico",
        "Euronews",
        "Deutsche Welle",
        "NPR",
        "Newsweek",
        "The Independent",
        "Sky News",
        "Al Jazeera",
        "ABC News",
        "The Washington Post",
        "Financial Times",
        "Bloomberg",
    }
)

# Short publisher names require an exact match.
TRUSTED = Allowlist(
    TRUSTED_PUBLISHERS,
    exact={"LRT", "BNS", "ELTA", "TV3", "LNK", "DW", "kl.lt", "vz.lt", "ve.lt", "15min.lt"},
)

JUNK = re.compile(
    # horoscopes and the stars
    r"horoskop|zodiak|astrolog|svajonių knyg|horoscope|zodiac|astrolog"
    # weather and lotteries
    r"|orai|orų prognoz|loterij|totalizator|kazino|pokeri"
    # gossip and the celebrity beat
    r"|žvaigžd|įžymyb|šou\b|šou pasauli|serial|reality|vestuv|skyryb|roman"
    r"|meilės istorij|celebrity|gossip|red carpet|scandal"
    # consumer filler
    r"|recept|namų patarim|ar žinojote|patarimai, kaip",
    re.IGNORECASE,
)

# A local publisher does not make a foreign headline Lithuanian news.
LITHUANIA = re.compile(
    r"\b(?:lietuv|vilni|kaun|klaipėd|klaiped|šiauli|siauli|panevėž|panevez|alyt"
    r"|marijampol|taurag|telši|telsi|uten|ukmerg|mažeiki|mazeiki|jonav"
    r"|kėdaini|kedaini|plung|kreting|trak|druskinink|palang|nida|seimo|seimas"
    r"|vyriausyb|prezident|savivaldyb|ministerij|lrt|lithuania|lithuanian)",
    re.IGNORECASE,
)

# Prefer Kaunas when ranking otherwise comparable topics.
KAUNAS = re.compile(r"kaun", re.IGNORECASE)
KAUNAS_BONUS = 1.5

# The outlet of record. Whatever it leads with takes the first card.
PRIORITY_PUBLISHER = "lrt"
PRIORITY_BONUS = 100.0


def is_junk(article: dict[str, Any]) -> bool:
    """Horoscopes, forecasts, lotteries and the celebrity beat — see JUNK."""
    return bool(JUNK.search(str(article.get("title") or "")))


def is_about_lithuania(article: dict[str, Any]) -> bool:
    """True when the story names Lithuania or somewhere in it."""
    text = f"{article.get('title') or ''} {article.get('description') or ''}"
    return bool(LITHUANIA.search(text))




def collect() -> tuple[list[dict[str, Any]], list[str], int, int]:
    """Fetch every feed. Returns kept articles, rejected publishers, junk, foreign."""
    google_jobs = [
        (label, google_news_url(query, locale)) for label, query, locale in GOOGLE_TOPICS
    ]

    print(
        f"Fetching {len(DIRECT_FEEDS)} Lithuanian feeds "
        f"and {len(google_jobs)} searches..."
    )
    direct = fetch_many(list(DIRECT_FEEDS))
    google = fetch_many(google_jobs, publisher_from_source=True)

    require_healthy_feeds({**direct, **google}, "Lietuva")

    kept: list[dict[str, Any]] = []
    rejected: list[str] = []
    junk = 0
    foreign = 0

    def take(row: dict[str, Any], via: str) -> None:
        nonlocal junk, foreign
        if is_junk(row):
            junk += 1
            return
        if not is_about_lithuania(row):
            foreign += 1
            return
        row["via"] = via
        kept.append(row)

    for rows in direct.values():
        for position, row in enumerate(rows):
            row["feed_rank"] = position
            take(row, "feed")

    for rows in google.values():
        for position, row in enumerate(rows):
            publisher = str(row.get("source") or "")
            # Publisher filtering is per item because search feeds mix sources.
            if publisher not in TRUSTED:
                rejected.append(publisher or "(no publisher)")
                continue
            # Normalize publisher aliases before counting outlets.
            row["source"] = TRUSTED.display(publisher)
            row["feed_rank"] = position
            take(row, "google")

    return kept, rejected, junk, foreign




def feed_rank(article: dict[str, Any]) -> int:
    """The item's position in its own feed, or a middling default."""
    value = article.get("feed_rank")
    return value if isinstance(value, int) else 50


def topic_score(frames: list[dict[str, Any]], newest: Any) -> float:
    """How much this topic deserves the page."""
    outlets = {normalise(str(f.get("source") or "")) for f in frames}
    score = 2.0 * len(outlets)

    best_rank = min(feed_rank(f) for f in frames)
    score += max(0.0, 2.0 - best_rank / 6.0)

    if any(KAUNAS.search(str(f.get("title") or "")) for f in frames):
        score += KAUNAS_BONUS

    if newest:
        hours_old = (utc_now() - newest).total_seconds() / 3600
        score += max(0.0, 2.0 - hours_old / 12.0)

    if any(normalise(str(f.get("source") or "")) == PRIORITY_PUBLISHER for f in frames):
        score += PRIORITY_BONUS

    return score


EVENT_GROUP_SYSTEM = """\
You group one day's headlines by the EVENT they report, so a briefing shows each
event once instead of four times.

Newsrooms cover one event in their own words, and here in two languages. These are
all the same event:
  "Numu\u0161us bepilot\u012f, Lietuva ir Lenkija \u017eada sutelkti paj\u0117gas"
  "Lietuvoje numu\u0161tas dronas \u2013 kas vyko pra\u0117jusi\u0105 par\u0105?"
  "NATO jets shoot down a drone in Lithuanian airspace"
Nothing about the words connects them, so this is a reading judgement, not a
matching one.

Group by the same EVENT. A drone shot down over Lithuania is NOT the same event as
a defence budget debate, however related, and not the same as a different drone
incident on another day.

Reply with a single json object and nothing else, giving each headline's number
and the group it belongs to. Use 1 for the first group, 2 for the next, and so on.
Every headline must appear exactly once:
{"groups": [{"n": 0, "group": 1}, {"n": 1, "group": 1}, {"n": 2, "group": 2}]}"""


def group_from_model(articles: list[dict[str, Any]]) -> list[list[dict[str, Any]]] | None:
    """Ask the model to group the headlines by event. None if it cannot."""
    listing = "\n".join(
        f"{n}. {str(a.get('title') or '')[:110]}" for n, a in enumerate(articles)
    )
    reply = ask_json(
        EVENT_GROUP_SYSTEM,
        f"Headlines:\n{listing}",
        max_tokens=GROUP_TOKENS_BASE + GROUP_TOKENS_PER_HEADLINE * len(articles),
        temperature=0,
    )
    if not reply or not isinstance(reply.get("groups"), list):
        return None

    buckets: dict[int, list[dict[str, Any]]] = {}
    for entry in reply["groups"]:
        if not isinstance(entry, dict):
            continue
        number, group = entry.get("n"), entry.get("group")
        # Validate returned indices before using them to group articles.
        if not isinstance(number, int) or not 0 <= number < len(articles):
            continue
        if not isinstance(group, int):
            continue
        buckets.setdefault(group, []).append(articles[number])

    if not buckets:
        return None

    # Keep unassigned articles as their own topics.
    placed = {id(a) for rows in buckets.values() for a in rows}
    for article in articles:
        if id(article) not in placed:
            buckets[len(buckets) + 1_000] = [article]

    return list(buckets.values())


def group_by_tokens(articles: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The fallback: same event judged by shared title words."""
    topics: list[dict[str, Any]] = []
    for article in articles:
        tokens = significant_tokens(str(article.get("title") or ""))
        for topic in topics:
            if len(shared_words(tokens, topic["tokens"])) >= DUPLICATE_TOKENS:
                topic["frames"].append(article)
                break
        else:
            topics.append({"frames": [article], "tokens": set(tokens)})
    return [t["frames"] for t in topics]


def group_topics(articles: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Collect articles into topics: one event, however many newsrooms covered it."""
    if len(articles) < 2:
        return [[a] for a in articles]

    head = articles[:GROUP_LIMIT]
    grouped = group_from_model(head)
    if grouped:
        for article in articles[GROUP_LIMIT:]:
            grouped.append([article])
        return grouped

    print("  grouping by title words (model unavailable)")
    return group_by_tokens(articles)


def build_card(frames_in: list[dict[str, Any]]) -> dict[str, Any]:
    """One card: the event's own story plus a frame per outlet that carried it."""
    best_per_outlet: dict[str, dict[str, Any]] = {}
    for article in frames_in:
        key = normalise(str(article.get("source") or "?")) or "?"
        current = best_per_outlet.get(key)
        if current is None or feed_rank(article) < feed_rank(current):
            best_per_outlet[key] = article

    ordered = sorted(
        best_per_outlet.values(),
        key=lambda a: (
            normalise(str(a.get("source") or "")) == PRIORITY_PUBLISHER,
            -feed_rank(a),
        ),
        reverse=True,
    )

    frames: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for article in ordered:
        text = normalise(str(article.get("title") or ""))
        if text and text in seen_titles:
            continue
        seen_titles.add(text)
        frames.append(article)
        if len(frames) >= MAX_FRAMES:
            break

    lead = frames[0]
    card = dict(lead)
    card["frames"] = frames
    card["outlet_count"] = len(frames)
    return card


def select(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Order events by score and take the page."""
    ranked: list[tuple[float, list[dict[str, Any]]]] = []
    for frames in groups:
        stamps = [parse_timestamp(a.get("publishedAt")) for a in frames]
        newest = max((s for s in stamps if s), default=None)
        ranked.append((topic_score(frames, newest), frames))

    ranked.sort(key=lambda pair: pair[0], reverse=True)

    cards: list[dict[str, Any]] = []
    seen_sources: Counter[str] = Counter()
    for _score, frames in ranked:
        card = build_card(frames)
        key = normalise(str(card.get("source") or "?"))
        if seen_sources[key] >= 3:
            continue
        seen_sources[key] += 1
        cards.append(card)
        if len(cards) >= MAX_STORIES:
            break

    return cards


def report(cards: list[dict[str, Any]], junk: int, foreign: int) -> None:
    print()
    print("=" * 68)
    print(f"Lietuva page: {len(cards)} cards  ({junk} junk, {foreign} not Lithuanian)")
    print("=" * 68)
    for card in cards:
        observed = parse_timestamp(card.get("publishedAt"))
        when = observed.strftime("%a %H:%M") if observed else "  ?  "
        outlets = card.get("outlet_count") or 1
        lead = str(card.get("source") or "?")[:14]
        others = [
            str(f.get("source") or "?").replace(".lt", "")[:12]
            for f in card["frames"][1:]
        ]
        also = f"  +{', '.join(others)}" if others else ""
        print(f"  [{lead:<14}] {when} x{outlets}  {str(card.get('title') or '')[:34]}{also}")
    print()
    print(f"Written to {OUTPUT_PATH}")


def main() -> None:
    cutoff = utc_now() - timedelta(hours=LOOKBACK_HOURS)

    articles, rejected, junk, foreign = collect()

    if rejected:
        worst = Counter(rejected).most_common(6)
        print(
            f"  dropped {len(rejected)} items from untrusted publishers "
            f"({', '.join(f'{n}× {p}' for p, n in worst)})"
        )

    fresh: list[dict[str, Any]] = []
    for article in articles:
        stamp = parse_timestamp(article.get("publishedAt"))
        # Keep undated articles rather than treating them as expired.
        if stamp is None or stamp >= cutoff:
            fresh.append(article)

    fresh = dedupe_by_url(fresh)

    fresh = newest_first(fresh)

    cards = select(group_topics(fresh))

    # Fetch preview images only for selected cards.
    filled = backfill_images([f for card in cards for f in card["frames"]])
    if filled:
        print(f"  recovered {filled} image(s) from the article pages")

    # Try alternate outlet images when the lead feed offers none.
    borrowed = 0
    for card in cards:
        if card.get("urlToImage"):
            continue
        for frame in card.get("frames") or []:
            if frame.get("urlToImage"):
                card["urlToImage"] = frame["urlToImage"]
                borrowed += 1
                break
    if borrowed:
        print(f"  borrowed {borrowed} image(s) from another outlet on the same event")

    save_json(cards, OUTPUT_PATH)
    report(cards, junk, foreign)


if __name__ == "__main__":
    main()
