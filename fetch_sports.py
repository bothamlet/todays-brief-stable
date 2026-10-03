#!/usr/bin/env python3
"""
fetch_sports.py — the Sports page.

Sports is not a category of world news; it is its own beat with its own sources
and its own interests:

    NBA        Oklahoma City Thunder, and any Lithuanian in the league
    F1
    Basketball EuroLeague (Žalgiris above all), and the international game —
               Olympics, FIBA World Cup, EuroBasket

Two kinds of source, and the difference matters:

  Direct feeds   A newsroom's own RSS. Every outlet here is an established
                 sports desk, so anything it publishes passes straight through:
                 ESPN, CBS, Motorsport, Autosport, RaceFans, TalkBasket,
                 Eurohoops, Sportando, 15min.

  Google News    Topic searches, for interests no working feed covers directly.
                 The Žalgiris club feed, BasketNews, FIBA and OKC Thunder's own
                 site all return nothing, so these interests would otherwise be
                 unreachable. Google is an aggregator, not a newsroom: it will
                 return a betting site as readily as a newspaper, so everything
                 from these feeds is filtered through TRUSTED_PUBLISHERS first.

The publisher for a Google item comes from RSS's <source> element, not the
search query — see publisher_from_source in rss_feeds.fetch_feed.

Writes data/sports.json, already ranked and capped, so the page renders what it
is given rather than re-deciding.

Usage:
    python fetch_sports.py
"""

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

# --- Configuration -----------------------------------------------------------

OUTPUT_PATH = Path("data/sports.json")

# Longer than the world pipeline's 24h. Sports feeds are chatty and a quiet
# Tuesday would leave the page bare; recency still wins inside each interest, so
# a two-day-old item only shows when nothing fresher exists.
LOOKBACK_HOURS = 48

MAX_STORIES = 10
MAX_PER_INTEREST = 2

# What the page represents, as opposed to what its cards are tagged with.
#
# The tags are for the reader and are specific: NBA · Lietuva, EuroLeague ·
# Žalgiris, Formula 1. The walk is for the page and is coarse, because a page is
# five stories long and four competitions fit in five. The empty string is the
# general-basketball bucket — the international and national-team beats, whose
# stories belong to no competition on this page and which the round-robin still
# has to reach.
COMPETITION_ORDER: tuple[str, ...] = ("NBA", "EUROLEAGUE", "F1", "")

# --- What the page is about --------------------------------------------------
# Order is priority: seen first gets the top slot, and the round-robin in
# select() walks this order. Keywords are matched case-insensitively against
# headline and summary, and a story is tagged with the first interest that hits,
# so the specific beats the general.

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
        # Just the club. "Kaunas" used to be here and matched a Kaunas FOOTBALL
        # club's coaching change off a general sport feed — a city is not a team.
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
        # General NBA news — what the five NBA feeds below are actually for. Sits
        # after every specific basketball interest so those win the tag first, and
        # its keyword is deliberately just "nba": the direct NBA feeds carry the
        # rest through their fallback, and a longer team list here would start
        # stealing headlines from okc and lt_player above.
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

# --- Direct feeds ------------------------------------------------------------
# Every URL below was fetched live and confirmed to return items. The publisher
# is the feed itself, so these need no allowlist: a sports desk's own feed is
# already the newsroom.
#
# The third field is the interest to fall back on when no keyword matches, or
# None when the feed is too broad to assume anything. 15min is the Lithuanian
# sport feed, not a basketball feed: left to fall back, it labelled a Kaunas
# football club's coaching change as EuroLeague · Žalgiris. A general feed has to
# earn its tag, so None means an item with no keyword match is dropped.

DIRECT_FEEDS: tuple[tuple[str, str, str | None], ...] = (
    # NBA — these carry the Thunder and the Lithuanians among everything else
    # NBA — these carry the Thunder and the Lithuanians among everything else, and
    # fall back to plain NBA: a story from an NBA feed is NBA news even when no
    # keyword fires.
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
    # EuroLeague — the /en/ path matters: eurohoops.net/feed/ serves Greek and
    # Turkish on the same feed, and put a Turkish game report on the page.
    ("TalkBasket", "https://www.talkbasket.net/euroleague/feed", "euroleague"),
    ("Eurohoops", "https://www.eurohoops.net/en/feed/", "euroleague"),
    ("Sportando", "https://sportando.basketball/en/feed/", "euroleague"),
    # Lithuanian sport in Lithuanian — by far the best Žalgiris coverage, but it
    # covers football and everything else too, hence the None.
    ("15min", "https://www.15min.lt/rss/sportas", None),
)

# --- Google News topic feeds -------------------------------------------------
# (query, locale key, interest). Google News is the only way to reach some of
# these: searching a phrase beats waiting for a general NBA feed to notice.
# The URL builder and locale table live in rss_feeds.py, shared with the Lietuva
# beat — the two copies were byte-identical.

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

# --- Reputability ------------------------------------------------------------
# Applied to Google News items only. Lower-cased, matched against the publisher
# name from <source>, so substrings work ("bbc" covers "BBC Sport").
#
# Google returns whatever ranks, and on the OKC search that included
# sportsbook.fanduel.com — a betting site is not a sports desk. Dropping it is
# the same call the world roster makes on advocacy outlets and tabloids.

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

# The gate lives in publishers.py — see the note there on why it is forgiving
# about the form of a name and strict about the identity behind it. "si.com" is
# listed in `exact` because it is an abbreviation that shares no substring with
# "Sports Illustrated", and Google credits that outlet both ways.
TRUSTED = Allowlist(TRUSTED_PUBLISHERS, exact={"si.com", "apnews.com"})


def publisher_is_trusted(name: str) -> bool:
    """Is this publisher one we are willing to put on the page?"""
    return name in TRUSTED


# --- Matching ----------------------------------------------------------------


def interest_for(
    text: str, fallback: str | None
) -> tuple[str | None, str | None, bool]:
    """Name the first interest this text matches, or the feed's own fallback.

    First match wins, and INTERESTS is ordered specific-to-general, so a story
    about Sabonis is tagged NBA · Lietuva rather than Basketball.

    The third value says whether a keyword actually matched. Callers need to know
    the difference between "this is about the Thunder" and "this came from a feed
    that is usually about the Thunder".

    `fallback` of None means the feed is too broad to assume anything: nothing
    matches, nothing is returned, and the caller drops the item.
    """
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


# --- The page's filter tabs ---------------------------------------------------
#
# Each interest belongs to a competition, with two deliberate gaps. The general
# basketball beats — "Basketball" and "Krepšinis · Lietuva", the international and
# national-team coverage — belong to no competition on this page at all, and
# filing them under NBA or EuroLeague would stamp a competition label on a story
# that is not about that competition. They carry an empty league and appear under
# ALL only, which is honest: the tab that shows everything shows them.
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


# --- Not sport ---------------------------------------------------------------
# Gambling copy reaches the page on legitimate feeds: Sportando published "In-Play
# Betting and Basketball: Why Live Markets…" and it was picked like any other
# basketball story. Same call the world roster makes on advocacy outlets, applied
# to betting tips.
#
# Word boundaries throughout, and the list kept short on purpose — "bet", "odds"
# and "value" are ordinary English words that turn up in real reporting, so only
# unmistakable gambling vocabulary is matched.
JUNK = re.compile(
    r"\b(?:betting|bets?|bookmakers?|bookies|odds|wagers?|wagering"
    r"|gambl(?:e|ing)|casino|parlay|accumulator|tipsters?|punters?"
    r"|free spins|promo code)\b",
    re.IGNORECASE,
)


def is_junk(article: dict[str, Any]) -> bool:
    """True for gambling copy — see JUNK."""
    return bool(JUNK.search(str(article.get("title") or "")))


# --- Gathering ---------------------------------------------------------------


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

    # Both result maps are keyed by URL, so the interest a feed was fetched for is
    # looked up by URL. It cannot come from the item's own `source`: on a Google
    # feed that field holds the publisher ("Yahoo Sports"), not the search query,
    # so every Google item would fall through to the same default.
    for url, rows in direct.items():
        fallback = direct_interest.get(url, "basketball")
        for row in rows:
            if is_junk(row):
                continue
            text = f"{row.get('title') or ''} {row.get('description') or ''}"
            key, label, matched = interest_for(text, fallback)
            # A general feed (15min covers all Lithuanian sport) contributes only
            # what actually names one of the interests.
            if not matched and fallback is None:
                continue
            row["interest"], row["interest_label"] = key, label
            # Written here rather than after selection. select() walks competitions
            # and reads this field, so assigning it afterwards — which is where it
            # lived first — gave every article an empty league, collapsed the
            # round-robin to a single bucket, and cut the page from ten stories to
            # two without raising anything.
            row["league"] = league_for(key)
            row["via"] = "feed"
            kept.append(row)

    for url, rows in google.items():
        fallback = google_interest.get(url, "basketball")
        for row in rows:
            if is_junk(row):
                continue
            source = str(row.get("source") or "")
            # Rejection is per item, not per feed: a topic search mixes a wire
            # service and a betting slip in the same hundred results.
            if not publisher_is_trusted(source):
                rejected.append(source or "(no publisher)")
                continue

            # Google searches the whole article but hands us only the headline, so
            # a "Domantas Sabonis" search returns pieces that name him in the body
            # and nowhere else. Tagging those NBA · Lietuva puts a label on the card
            # that the card does not support — and a Žalgiris search surfaced a
            # Lithuanian FOOTBALL club's coaching change the same way. A topic feed
            # other than the catch-all has to earn its tag with a visible keyword.
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


# --- Selection ---------------------------------------------------------------


def select(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick and order the page: round-robin across competitions, freshest first.

    Round-robin rather than a flat sort, so one busy competition cannot take the
    whole page. A flat sort on recency would fill it with NBA news on the days the
    NBA is loud and show no F1 at all — the page is meant to cover the interests,
    not to reflect which one had the busiest afternoon.

    The walk is over *competitions*, not over the eight interests the cards are
    tagged with. Those two things are not the same job: the tag tells the reader
    what a story is about (NBA · Lietuva, EuroLeague · Žalgiris), while the walk
    decides what the page represents, and a page is only five stories long. Four
    competitions fit in five. Eight interests do not, and pretending otherwise is
    how a cap quietly deleted EuroLeague.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        # The competition, falling back to the general-basketball bucket for the
        # interests that belong to none — the international and national-team
        # beats. That bucket is a real part of the page and gets its own slot.
        key = str(article.get("league") or "")
        buckets.setdefault(key, []).append(article)

    for rows in buckets.values():
        # Newest first, except that a story carrying a picture outranks one that is
        # merely newer. On Sports most stories have no picture at all — the Google
        # topic feeds cannot supply one — so ordering purely by recency put all the
        # text-only cards first and the photographs in a clump in the middle. This
        # only ever reorders within one competition, so it cannot promote a story
        # into a page it does not belong on.
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


# --- Entry point -------------------------------------------------------------


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
        # An undated item is kept: Google occasionally omits pubDate, and a
        # story missing its clock is not evidence it is old.
        if stamp is None or stamp >= cutoff:
            fresh.append(article)

    fresh = dedupe_by_url(fresh)
    # newest_first RETURNS a sorted list; it does not sort in place. Called as a
    # bare statement it does nothing, leaving the list in collection order — every
    # direct feed before every Google item — so "freshest first" within an interest
    # was really "whichever feed was fetched first".
    fresh = newest_first(fresh)

    chosen = select(fresh)

    # No league tagging here any more: it happens in collect(), because select()
    # reads it. See the note there.

    # Only the items actually going on the page: this costs one page fetch each,
    # and a Google-sourced story is skipped because its link is a redirect that
    # lands on Google's EU consent screen rather than the article.
    filled = backfill_images(chosen)
    if filled:
        print(f"  recovered {filled} image(s) from the article pages")

    save_json(chosen, OUTPUT_PATH)
    report(chosen, len(fresh))


if __name__ == "__main__":
    main()
