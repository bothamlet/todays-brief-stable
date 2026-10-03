#!/usr/bin/env python3
"""
fetch_lietuva.py — the Lietuva page.

The page used to be a filter over the world news roster: stories about Lithuania,
as reported by the Guardian and the Straits Times. That is Lithuania seen from
outside — useful, but not what a Lithuanian news page is for. This gives the page
its own beat and its own sources.

Two kinds of source, as with the sports beat:

  Direct feeds   A Lithuanian newsroom's own RSS: Delfi, 15min, Lrytas, Kauno
                 diena, Verslo žinios, Bernardinai, Diena. LRT's own feeds are
                 dead — every path tried returns nothing — so LRT arrives through
                 the Google bridge below instead, which is the only way to get it.

  Google News    Topic searches in the Lithuanian edition, which is also how the
                 smaller outlets surface: LRT, Respublika, Alfa, TV3, LNK and the
                 regional papers. Google is an aggregator, so these pass through an
                 allowlist, but a wide one — a Lithuanian page without Respublika
                 would be pretending the country has four newspapers. The bar
                 excludes gambling, content farms and nobody else.

Three editorial rules decide what actually reaches the page:

  1. LRT leads. It is the outlet of record, so its best story takes the first
     card rather than competing on score with everything else.

  2. A story is a topic, not an article. When several newsrooms cover one event
     the card carries all of them and the arrows step between them — the same
     mechanism as the World page's per-lean frames, minus the lean labels, since
     these are not politically opposed outlets reading the same event differently.
     Outlets agreeing on a story is also the honest importance signal: four
     newsrooms covering something is news, one newsroom covering it may be a press
     release.

  3. Gossip, horoscopes and weather are not news. Delfi, Lrytas, Kauno diena and
     Diena all lead their feeds with horoscopes, so untouched they take the page.

Writes data/lietuva.json, ranked and capped, so the page renders what it is given.

Usage:
    python fetch_lietuva.py
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
from llm import ask_json
from publishers import Allowlist, normalise
from rss_feeds import require_healthy_feeds
from rss_feeds import (
    backfill_images,
    fetch_many,
    google_news_url,
)
from textutil import shared_words, significant_tokens

# --- Configuration -----------------------------------------------------------

OUTPUT_PATH = Path("data/lietuva.json")

# Two days: a week is too long for a front page and a day is too short for a
# country this size to have enough in English-language coverage.
LOOKBACK_HOURS = 48

MAX_STORIES = 8

# How many outlets one card may step through. Two keeps the arrows meaningful;
# the World page uses the same number for the same reason.
MAX_FRAMES = 3

# Content words two headlines must share to count as the same topic. Set at 3
# because the duplicate that prompted this — the OpenAI/Anthropic/Google item on
# both Kauno diena and Verslo žinios — shares far more, while two unrelated
# stories from one outlet rarely share three.
DUPLICATE_TOKENS = 3

# How many headlines the grouping call may see. The page shows eight cards, so a
# story outside the newest few dozen cannot reach it anyway — and the call has to
# return one entry per headline it is given. At 600 candidates it was asked for
# roughly 15,000 output tokens while allowed 1,500, so the reply truncated, the
# json never parsed, and every run silently fell back to word matching. That
# fallback is why one drone incident appeared as four separate cards.
GROUP_LIMIT = 80

# Output budget, sized to the number of headlines actually sent.
GROUP_TOKENS_PER_HEADLINE = 30
GROUP_TOKENS_BASE = 200

EPOCH = utc_now().replace(year=1970)

# --- Sources -----------------------------------------------------------------

DIRECT_FEEDS: tuple[tuple[str, str], ...] = (
    # LRT's feed is not at a /rss path — every one of those 404s. It is a query
    # parameter on the root, declared in the site's own <link rel="alternate">,
    # which is where it was finally found. Worth the hunt: as a direct feed it
    # supplies real article URLs instead of Google redirects, and 8 of 8 sampled
    # articles yield their og:image, where a Google-sourced LRT item could have
    # neither.
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

# (label, query, locale). The English one carries Lithuania as the outside world
# sees it — NATO, the drone incidents, the Belarus border — which is half of what
# a front page is for. LRT is deliberately absent: it is a direct feed above now,
# and asking Google for it too would only add redirect links to the same articles.
GOOGLE_TOPICS: tuple[tuple[str, str, str], ...] = (
    ("Lietuva", "Lietuva", "lt"),
    ("Lietuvos naujienos", "Lietuvos naujienos", "lt"),
    ("Kaunas", "Kaunas", "lt"),
    ("Vilnius", "Vilnius", "lt"),
    ("Lithuania", "Lithuania", "en"),
)

# --- Reputability ------------------------------------------------------------
# Wide on purpose: this is a Lithuanian page. Excluded are the categories rather
# than the tone — betting and casino sites, content farms, and aggregators that
# rewrite other people's reporting.

TRUSTED_PUBLISHERS: frozenset[str] = frozenset(
    {
        # Written as each outlet writes itself, not lowercased: normalise() folds
        # case anyway, and these strings are also what the card displays. The
        # previous all-lowercase list is why a page showed "lrt" beside "LRT".
        #
        # Domain forms are deliberately absent. "lrt.lt" reduces to the same
        # canonical form as "LRT" and is matched by it, so listing both only
        # created a second spelling to display.
        # national broadcasters and papers of record
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

# Short names are matched exactly, so the abbreviations need listing alongside the
# spellings above — and so do the domains that are abbreviations of nothing.
# "lrt.lt" reduces to the same canonical form as "LRT" and needs no entry; "kl.lt"
# reduces to "kl" and "vz.lt" to "vz", which share no substring with "Klaipėda" or
# "Verslo žinios". Leaving them out dropped 16 items a run.
TRUSTED = Allowlist(
    TRUSTED_PUBLISHERS,
    exact={"LRT", "BNS", "ELTA", "TV3", "LNK", "DW", "kl.lt", "vz.lt", "ve.lt", "15min.lt"},
)

# --- Not news -----------------------------------------------------------------
# A factual list, not a taste judgement: horoscopes, forecasts, lotteries and the
# celebrity beat. Every Lithuanian feed sampled leads with at least one of these,
# and untouched they dominate a recency-ordered page.
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

# The topic is not Lithuanian news unless it says so. Lithuanian outlets carry the
# world too: Kauno diena's main feed led with an amusement-park injury in
# Maharashtra.
#
# Every alternative is anchored to a word start, which matters more than it looks.
# Unanchored, "trak" (for Trakai) matches inside "a-TRAK-cionas" — so a story about
# an amusement ride in India counted as Lithuanian news. The names are stems, not
# whole words, because Lithuanian inflects: "kaun" has to catch Kaunas, Kaune and
# Kauno alike.
LITHUANIA = re.compile(
    r"\b(?:lietuv|vilni|kaun|klaipėd|klaiped|šiauli|siauli|panevėž|panevez|alyt"
    r"|marijampol|taurag|telši|telsi|uten|ukmerg|mažeiki|mazeiki|jonav"
    r"|kėdaini|kedaini|plung|kreting|trak|druskinink|palang|nida|seimo|seimas"
    r"|vyriausyb|prezident|savivaldyb|ministerij|lrt|lithuania|lithuanian)",
    re.IGNORECASE,
)

# Kaunas is worth a second look — the user asked for it — but as a nudge inside
# the score, not a category of its own.
KAUNAS = re.compile(r"kaun", re.IGNORECASE)
KAUNAS_BONUS = 1.5

# The outlet of record. Whatever it leads with takes the first card.
PRIORITY_PUBLISHER = "lrt"
PRIORITY_BONUS = 100.0


def is_junk(article: dict[str, Any]) -> bool:
    """Horoscopes, forecasts, lotteries and the celebrity beat — see JUNK."""
    return bool(JUNK.search(str(article.get("title") or "")))


def is_about_lithuania(article: dict[str, Any]) -> bool:
    """True when the story names Lithuania or somewhere in it.

    Crude by construction — it will drop a domestic story that happens not to name
    a place, and it cannot see a foreign story that mentions Vilnius in passing.
    The alternative is the classifier in classify_articles.py, which answers this
    properly; that costs a model call per headline, and this costs nothing.
    """
    text = f"{article.get('title') or ''} {article.get('description') or ''}"
    return bool(LITHUANIA.search(text))


# --- Gathering ---------------------------------------------------------------


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

    # Direct feeds are newsrooms we chose, so they pass the publisher gate.
    # Each item carries its position in its own feed, which is the outlet's own
    # statement of what matters: item 0 is that newsroom's lead.
    for rows in direct.values():
        for position, row in enumerate(rows):
            row["feed_rank"] = position
            take(row, "feed")

    for rows in google.values():
        for position, row in enumerate(rows):
            publisher = str(row.get("source") or "")
            # Per item, not per feed: one topic search returns LRT, Respublika and
            # a betting slip in the same hundred results.
            if publisher not in TRUSTED:
                rejected.append(publisher or "(no publisher)")
                continue
            # Google credits LRT as "lrt.lt"; LRT's own feed says "LRT". One page
            # showing both spellings of one newsroom is what this fixes.
            row["source"] = TRUSTED.display(publisher)
            row["feed_rank"] = position
            take(row, "google")

    return kept, rejected, junk, foreign


# --- Topics ------------------------------------------------------------------


def feed_rank(article: dict[str, Any]) -> int:
    """The item's position in its own feed, or a middling default.

    collect() sets this from enumerate(), so it is already an int; the check is
    there because this feeds a sort key, and a malformed record should not take
    down a run over a tie-break.
    """
    value = article.get("feed_rank")
    return value if isinstance(value, int) else 50


def topic_score(frames: list[dict[str, Any]], newest: Any) -> float:
    """How much this topic deserves the page.

    Three signals, and the first is the honest one: how many separate newsrooms
    thought it was worth covering. The others are the outlet's own ranking of it
    (feed position) and recency, which breaks ties.
    """
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
        # Indices are checked because a hallucinated one would silently drop, or
        # worse duplicate, a story that the page then never shows.
        if not isinstance(number, int) or not 0 <= number < len(articles):
            continue
        if not isinstance(group, int):
            continue
        buckets.setdefault(group, []).append(articles[number])

    if not buckets:
        return None

    # Anything the model left out becomes its own single-article topic, so a bad
    # reply can lose a grouping but never a story.
    placed = {id(a) for rows in buckets.values() for a in rows}
    for article in articles:
        if id(article) not in placed:
            buckets[len(buckets) + 1_000] = [article]

    return list(buckets.values())


def group_by_tokens(articles: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The fallback: same event judged by shared title words.

    Keeps each topic anchored to its FIRST article's words, never a growing union.
    Unioning looked natural and was wrong — every article admitted widened the
    topic, so it loosened as it filled, and the first card became LRT and Delfi on
    a drone incident plus lrytas.lt on a broken flagpole.
    """
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
    """Collect articles into topics: one event, however many newsrooms covered it.

    Token matching cannot do this, and the reason is worth recording. The page's
    duplicates were one drone incident appearing as four cards:

        Numu\u0161us bepilot\u012f, Lietuva ir Lenkija \u017eada sutelkti paj\u0117gas
        LRT trumpai. Lietuvoje numu\u0161tas dronas \u2013 kas vyko pra\u0117jusi\u0105 par\u0105?
        NATO jets shoot down a drone in Lithuanian airspace
        Po naktinio drono incidento Lietuvoje \u2013 naujausi...

    The Lithuanian and English headlines share no words at all, and the two
    Lithuanian ones share one \u2014 below any threshold that would not also merge
    unrelated stories. Same-event judgement across languages is a reading task, so
    it goes to the model, in one call that sees every headline at once. That is
    also the only way to be consistent: a per-article call cannot know what it
    concluded about the previous one.
    """
    if len(articles) < 2:
        return [[a] for a in articles]

    # Only the newest GROUP_LIMIT are worth grouping: the page shows eight cards,
    # and the rest cannot reach it. Whatever is left over becomes its own topic
    # below, so nothing is lost by the cap.
    head = articles[:GROUP_LIMIT]
    grouped = group_from_model(head)
    if grouped:
        # Everything past the cap is grouped with itself. These are the oldest
        # candidates, so they sort last and are cut by MAX_STORIES regardless.
        for article in articles[GROUP_LIMIT:]:
            grouped.append([article])
        return grouped

    print("  grouping by title words (model unavailable)")
    return group_by_tokens(articles)


def build_card(frames_in: list[dict[str, Any]]) -> dict[str, Any]:
    """One card: the event's own story plus a frame per outlet that carried it.

    Two passes, because a card can repeat itself in two different ways.

    First, one frame per outlet: a newsroom that ran two pieces on the same event
    would otherwise put its own name on the card twice and inflate the count that
    says how many newsrooms covered it.

    Then, one frame per article. Deduping by outlet does not catch syndication, and
    Diena republishes Kauno diena word for word — so every card read
    "Kauno diena / Diena" with the identical headline under both. They genuinely
    are different outlets, so the outlet key cannot see it; identical text is
    dropped as well. This runs after the ordering, so the preferred outlet is the
    one that survives.
    """
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
        # No outlet takes more than three cards. Without this the page is whichever
        # four newsrooms publish fastest, which is not the same as the day's news.
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
        # An undated item is kept: a story missing its clock is not evidence it is
        # old, and Google occasionally omits the field.
        if stamp is None or stamp >= cutoff:
            fresh.append(article)

    fresh = dedupe_by_url(fresh)

    # newest_first RETURNS a sorted list; it does not sort in place. Called as a
    # bare statement it does nothing at all, which left the list in collection
    # order — every direct feed before every Google item — and quietly undid the
    # "freshest outlet first" ordering select() depends on.
    fresh = newest_first(fresh)

    cards = select(group_topics(fresh))

    # Only what is going on the page: this costs one page fetch per item, and the
    # frames are what the reader sees when they press the arrows. Runs before the
    # card-level fallback below so that fallback can use the pictures this finds.
    filled = backfill_images([f for card in cards for f in card["frames"]])
    if filled:
        print(f"  recovered {filled} image(s) from the article pages")

    # A card leads with LRT's headline, and LRT's own feed carries no images — so
    # the picture comes from whichever frame has one. The headline and the link
    # stay the lead outlet's; the photograph is of the event, which by definition
    # every frame is about. This is why it runs after the backfill.
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
