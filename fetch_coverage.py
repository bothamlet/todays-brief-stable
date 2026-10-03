#!/usr/bin/env python3
"""
fetch_coverage.py — Step 5 of the daily news pipeline.

For every high-significance story, gather multi-outlet coverage per political
lean and attach it as `coverage: {left: [...], center: [...], right: [...]}`.

The source is RSS: a fixed roster of two left, two centre and two right outlets,
each read through several desk feeds and matched locally on keywords. No key, no
quota, no API quota, no domain guessing, and no third-party index deciding
what exists — which is also what makes the Guardian and the New York Times
reachable at all.

There used to be a second source. NewsAPI queried by keyword against
domain-restricted searches, off by default, and it is gone: RSS found strictly
more frames for every story tested, needs no key, and needs no API quota.
What NewsAPI left behind was two hundred lines of machinery for a source nobody
had switched on — request builders, a domain probe, a source index, three
diagnostic subcommands, fifteen constants, and a degraded-run guard that existed
only because a metered API can throttle. The instruction to enable it sat in this
docstring, in the constant beside it, and in four separate comments, none of
which had ever been acted on.

Feeds are fetched once per run, not once per story, so adding stories is free.
Nothing is fabricated — a lean with no hits gets an empty list.

Input:  data/world_tracked.json, falling back through the earlier stages
Output: data/world_covered.json  (input articles, high-significance ones enriched)

Keyword extraction uses DEEPSEEK_API_KEY, optionally — it falls back to a
stopword heuristic if the key is unset or unavailable.

Usage:
    python fetch_coverage.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    sys.exit(
        "ERROR: missing dependency 'requests'. Install it with:\n"
        "    pip install -r requirements.txt"
    )

from briefing import (
    assign_briefing_topics,
    briefing_candidates,
    dedupe_briefing_coverage,
    report_briefing,
    select_briefing,
)
from common import (
    dedupe_by_url,
    fail,
    first_existing,
    load_json,
    save_json,
)
from llm import MODEL, ask_json, get_api_key
from rss_feeds import require_healthy_feeds
from rss_feeds import fetch_many
from textutil import (
    STOPWORDS,
    significant_tokens,
    title_overlap,
)


# --- Configuration -----------------------------------------------------------
#
# Was a table of forty constants for two sources. Fifteen of them configured
# NewsAPI — its URLs, its key, its page size, its polite delay between calls, the
# three thresholds its domain probe used, and the fraction of failed queries that
# meant a degraded run. All of it went with the source.

DEEPSEEK_MODEL = os.environ.get("COVERAGE_MODEL") or MODEL

# Pipeline order is: fetch_world -> cluster_stories -> classify_articles ->
# track_stories -> here. So the TRACKED file is preferred: it is the classified
# set plus each story's `history` block, which is what the developing cue needs.
INPUT_CANDIDATES = (
    Path("data/world_tracked.json"),  # preferred: classified AND tracked
    Path("data/world_filtered.json"),  # fallback: no history, cue degrades to hidden
    Path("data/world_clustered.json"),  # fallback: unclassified
    Path("data/world_raw.json"),  # fallback: unclustered
)
OUTPUT_PATH = Path("data/world_covered.json")

LOOKBACK_HOURS = 48  # how far back to search for coverage
REQUEST_TIMEOUT = 30.0  # seconds per call
KEYWORD_MAX_TOKENS = 100  # the reply is a tiny JSON object
MAX_QUERY_TERMS = 2  # distinctive entities for local coverage matching

LEANS = ("left", "center", "right")

RSS_WORKERS = 8  # parallel feed fetches; feeds are independent hosts
MAX_RSS_HITS_PER_LEAN = 2  # frames kept per lean; the card shows at most this

# How many content words a candidate headline must share with the story headline.
#
# Matching on the primary term alone was far too loose: a story about Israel's
# destruction of south Lebanon pulled in "UK bans goods from Israeli settlements"
# and "Israel's E1 plan explained", because every one of them contains "Israel".
# Requiring ALL terms instead was far too strict, dropping 75% of frames and
# emptying six stories completely — a legitimate Ukraine drone story lost six of
# its seven frames to "drone strikes" not being verbatim in the headline.
#
# Sharing two content words with the story headline is the calibrated middle:
# it drops the West Bank stories while keeping every story covered at least once.
MIN_TITLE_OVERLAP = 2
# How many high-significance stories to gather coverage for. Classification now
# yields ~66 high-significance stories a day, and fetching coverage for every one
# produces ~2000 items — far more than a briefing can use. Raised this if you
# want the full set.
MAX_STORIES_FOR_COVERAGE = 20

# ---------------------------------------------------------------------------
# Briefing selection — which covered stories are worth showing, in what order.
#
# Ranking by importance alone filled four of the five slots with one Iran-Saudi
# escalation: a base attack, a pipeline attack, a ship strike and a summit. Each
# was genuinely distinct and genuinely significant; together they were one event
# monopolising the page. Selection still favours importance, but caps how many
# slots any single topic may take.
#
# Recomputed here rather than in the UI so render.js and write_desk.py read the
# same answer instead of each re-implementing the rule and drifting apart.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RSS OUTLET ROSTER — the fixed set we read every day.
#
# A constant roster keeps the left/centre/right balance predictable day to day.
# RSS also removes the API key, the 100/day quota, the 429s and the domain-string
# guessing in one move.
#
# REPUTABLE SOURCES ONLY: established newsrooms with published editorial
# standards and a corrections practice. Aggregators, tabloids, advocacy outlets
# and opinion magazines are excluded on purpose even where their feeds work:
# Breitbart, the Daily Caller and the Daily Signal (advocacy), the New York Post
# (tabloid), HuffPost (aggregator), National Review and Reason (opinion).
#
# The right bucket is thin as a result, and that is a finding rather than a gap
# in the config — see the note on it below.
#
# Each outlet lists several desk feeds, so a story is covered regardless of which
# section it lands in (world, sport, business). Every URL below was fetched live
# and confirmed to return items.
#
# One exception: Reuters has no public feed, so it goes through Google News RSS.
# See the note on that entry — its links are Google redirects, not reuters.com.
#
# Lean ratings are approximate, drawn from commonly published bias references.
# Move outlets between buckets freely.
# ---------------------------------------------------------------------------
RSS_OUTLETS: dict[str, dict[str, list[str]]] = {
    "left": {
        "The New York Times": [
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        ],
        "The Washington Post": [
            "https://feeds.washingtonpost.com/rss/world",
            "https://feeds.washingtonpost.com/rss/national",
        ],
        "The Guardian": [
            "https://www.theguardian.com/world/rss",
            "https://www.theguardian.com/us-news/rss",
        ],
        "NPR": [
            "https://feeds.npr.org/1001/rss.xml",
        ],
        "ProPublica": [
            "https://www.propublica.org/feeds/propublica/main",
        ],
        "The Atlantic": [
            "https://www.theatlantic.com/feed/all/",
        ],
    },
    "center": {
        # Reuters publishes no usable RSS: feeds.reuters.com no longer resolves,
        # and reuters.com answers automated requests with 401/404. Google News RSS
        # bridges it and returns real Reuters headlines attributed to Reuters, but
        # note the trade-off: article links are news.google.com redirects rather
        # than canonical reuters.com URLs. They do resolve when followed.
        "Reuters": [
            "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US:en",
        ],
        # AP publishes no public feed either (apnews.com answers 403). Same Google
        # News bridge, same redirect-link caveat as Reuters.
        "AP News": [
            "https://news.google.com/rss/search?q=site%3Aapnews.com&hl=en-US&gl=US&ceid=US:en",
        ],
        "BBC News": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.bbci.co.uk/sport/rss.xml",
            "https://feeds.bbci.co.uk/news/business/rss.xml",
        ],
        "The Economist": [
            "https://www.economist.com/the-world-this-week/rss.xml",
        ],
        "Financial Times": [
            "https://www.ft.com/world?format=rss",
        ],
        "Al Jazeera": [
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
        "Le Monde": [
            "https://www.lemonde.fr/en/rss/une.xml",
        ],
        "The Straits Times": [
            "https://www.straitstimes.com/news/world/rss.xml",
        ],
    },
    # The thin bucket, and structurally so rather than by choice: the mainstream
    # right-leaning newsrooms publish no usable free feed. The Telegraph returns
    # 402 (paywall), The Times 404, The Australian and the Washington Examiner 403,
    # and the Wall Street Journal has no public RSS at all. What is left is Fox
    # News' news operation plus two smaller but properly edited newsrooms.
    "right": {
        "Fox News": [
            "https://moxie.foxnews.com/google-publisher/latest.xml",
            "https://moxie.foxnews.com/google-publisher/world.xml",
            "https://moxie.foxnews.com/google-publisher/sports.xml",
        ],
        "The Dispatch": [
            "https://thedispatch.com/feed/",
        ],
        "National Post": [
            "https://nationalpost.com/feed",
        ],
    },
}

# ---------------------------------------------------------------------------
# OUTLET DOMAINS — edit freely; these are a starting point, not an endorsement.
#
# Grouped per commonly published media-bias references (AllSides / Ad Fontes
# style ratings). Swap in whichever outlets you actually want to hear from.
#
# Bare domains only — no scheme, no "www.", no path. Run --list-sources to see
# every outlet NewsAPI exposes, and --check-domains to confirm yours work.
# ---------------------------------------------------------------------------
OUTLETS_BY_LEAN: dict[str, list[str]] = {
    # Every domain below was probed live and returns >=50 articles/week, which is
    # roughly the floor for ever matching a specific story in a 48h window.
    #
    # Adding domains here is FREE: they are OR'd into a single `domains` param,
    # so a lean costs one request no matter how many outlets it lists.
    #
    # NewsAPI's index is not the obvious one. Confirmed dead (0 results/week):
    # nytimes.com, theguardian.com, reuters.com, msnbc.com, us.cnn.com,
    # nationalreview.com, washingtontimes.com, pjmedia.com, bloomberg.com.
    # Never trust /sources' domain strings — always re-check with --check-domains.
    #
    # Trailing comment = measured articles/week and approximate bias rating.
    "left": [
        "huffpost.com",  # 293   Left
        "latimes.com",  # 291   Lean Left
        "npr.org",  # 269   Lean Left
        "nbcnews.com",  # 145   Lean Left
        "cnn.com",  # 107   Lean Left
    ],
    "center": [
        "usatoday.com",  # 3196  Center
        "bbc.co.uk",  # 1149  Center
        "time.com",  # 70    Center
        "apnews.com",  # 50    Center
    ],
    # Note: NewsAPI's high-volume right-leaning options skew further right than
    # the left/centre buckets, because the mainstream-right outlets (National
    # Review, Washington Times, WSJ) are all absent from the index. Trim to taste.
    "right": [
        "nypost.com",  # 1674  Lean Right
        "foxnews.com",  # 712   Right
        "breitbart.com",  # 374   Right, further
        "dailycaller.com",  # 151   Right, further
        "reason.com",  # 122   Libertarian / Lean Right
        "dailysignal.com",  # 103   Right
    ],
}


# --- Keyword extraction ------------------------------------------------------


def fallback_terms(title: str) -> list[str]:
    """Crude offline keyword extraction: drop stopwords, prefer proper nouns."""
    cleaned = re.sub(r"\s+[-–—|]\s+[^-–—|]+$", "", title)  # drop " - Outlet" tail
    words = re.findall(r"[A-Za-z][A-Za-z'\-]+", cleaned)
    keep = [w for w in words if w.lower() not in STOPWORDS and len(w) > 2]
    proper = [w for w in keep if w[0].isupper()]
    return (proper or keep)[:MAX_QUERY_TERMS]


def deepseek_terms(
    session: requests.Session, api_key: str, title: str, description: str | None
) -> list[str] | None:
    """Ask DeepSeek for the most distinctive terms. Returns None on any failure."""
    system = (
        "You pick search terms for finding news coverage of a story.\n\n"
        "Given a headline, reply with a single json object containing one key "
        '"terms" whose value is a list of at most 2 of the most distinctive '
        "search terms — proper nouns, named entities, places.\n\n"
        "Fewer terms means more matches, so choose only the two that best "
        "identify this specific story. Prefer named entities over generic "
        "words. No boolean operators, no quotes.\n\n"
        'Example reply: {"terms": ["Houthis", "Bab el-Mandeb"]}'
    )
    user = f"Headline: {title}\nSummary: {(description or '').strip() or '(none)'}"

    parsed = ask_json(system, user, max_tokens=KEYWORD_MAX_TOKENS,
                      model=DEEPSEEK_MODEL, api_key=api_key, session=session)
    raw = parsed.get("terms", []) if parsed else []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    terms = [str(term).strip() for term in raw if isinstance(term, str) and term.strip()]
    return terms[:MAX_QUERY_TERMS] or None


def format_term(term: str) -> str:
    """Quote multi-word or hyphenated terms so NewsAPI treats them as a phrase."""
    term = term.strip()
    if " " in term or "-" in term:
        return f'"{term}"'
    return term


def build_query(terms: list[str]) -> str:
    """OR-join the terms so any one of them can match.

    NewsAPI ANDs bare space-separated terms, so `Houthis Yemen pipeline`
    requires all three and matches almost nothing. `Houthis OR Yemen` matches
    either, which is what we want when hunting for coverage of one story.
    """
    return " OR ".join(format_term(t) for t in terms[:MAX_QUERY_TERMS])


def extract_terms(
    session: requests.Session,
    deepseek_key: str | None,
    article: dict[str, Any],
) -> list[str]:
    """Best-effort distinctive search terms for a story, preferring the model.

    Returns raw terms, not a query string: NewsAPI needs them OR-joined into its
    own syntax, while RSS matching just wants the plain strings.
    """
    title = (article.get("title") or "").strip()

    terms: list[str] | None = None
    if deepseek_key:
        terms = deepseek_terms(
            session, deepseek_key, title, article.get("description")
        )

    if not terms:
        terms = fallback_terms(title)

    if not terms:
        terms = [title[:60]] if title else []

    return terms


# --- RSS coverage ------------------------------------------------------------

# Feed fetching and parsing live in rss_feeds.py, shared with fetch_world.py.
# This file used to carry its own copy, which silently drifted: it served 140px
# Guardian thumbnails long after the shared module had been fixed to request
# full-size CDN crops.


def fetch_rss_by_lean() -> dict[str, list[dict[str, Any]]]:
    """Fetch every configured feed once, grouped by lean. Never raises.

    Done once per run rather than once per story, so the cost is fixed no matter
    how many stories need coverage.
    """
    out: dict[str, list[dict[str, Any]]] = {lean: [] for lean in LEANS}
    jobs = [
        (lean, outlet, url)
        for lean, outlets in RSS_OUTLETS.items()
        for outlet, urls in outlets.items()
        for url in urls
    ]
    if not jobs:
        return out

    # fetch_many keys its results by URL, so each maps back to its lean.
    by_url = fetch_many([(outlet, url) for _, outlet, url in jobs], workers=RSS_WORKERS)
    require_healthy_feeds(by_url, "World coverage")
    for lean, _outlet, url in jobs:
        out[lean].extend(by_url.get(url, []))

    return out


def match_score(item: dict[str, Any], terms: list[str]) -> int:
    """How many of `terms` appear in the item. 0 means no match."""
    haystack = f"{item.get('title') or ''} {item.get('description') or ''}".lower()
    return sum(1 for term in terms if term and term.lower() in haystack)



def rss_coverage(
    items: list[dict[str, Any]], terms: list[str], story_title: str
) -> list[dict[str, Any]]:
    """Feed items that are actually about this story, best-ranked first.

    Three guards, each added because its absence produced visible nonsense:

    1. The title must overlap the story headline by MIN_TITLE_OVERLAP content
       words. Without it, a single broad term like "Israel" matched every Israel
       headline on the wire, so a Lebanon story showed West Bank settlements.
    2. There is no any-term fallback. It once caught a too-narrow primary, but
       it did more harm than good: a Philippines ferry story fell back to
       "Philippines" and pulled in an unrelated naval story. An empty lean is
       honest; a lean padded with another story is misleading.
    3. One frame per outlet, because an outlet filing three times filled the
       whole group with a single perspective.
    """
    if not terms:
        return []

    story_tokens = significant_tokens(story_title)
    hits: list[tuple[int, dict[str, Any]]] = []

    for item in items:
        title = str(item.get("title") or "")
        if title_overlap(title, story_tokens) < MIN_TITLE_OVERLAP:
            continue
        hits.append((match_score(item, terms), item))

    hits.sort(key=lambda pair: -pair[0])

    picked: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for _, item in hits:
        source = str(item.get("source") or "")
        if source in seen_sources:
            continue
        seen_sources.add(source)
        picked.append(item)
        if len(picked) >= MAX_RSS_HITS_PER_LEAN:
            break

    return picked


COVERAGE_SAME_STORY_PROMPT = """\
You check whether candidate articles cover the SAME story as a given headline.

Candidates were matched by shared words, which is not sufficient. A story about
"Ukraine drone strikes kill four" pulls in "Moscow claims Ukraine planned to
start a war"; a story about Israel's destruction of south Lebanon pulls in a
West Bank settlements story. Each pair is about the same war and the same
country. Neither pair is about the same story.

Same story means the same specific event, decision or development: one news item,
not one subject area. Sharing a country, a conflict or a theme is NOT enough.
Differing only in how it is framed or headlined is fine; those ARE the same story.

Reply with a single json object and nothing else:
{"same_story": [2, 5]}
List only the candidate numbers covering the same specific story, in any order.
If none do, return an empty list."""


def parse_indices(text: str, upper_bound: int) -> list[int] | None:
    """Pull a list of 1-based candidate numbers out of a model reply."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    raw = parsed.get("same_story")
    if not isinstance(raw, list):
        return None

    out: list[int] = []
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= upper_bound and number not in out:
            out.append(number)
    return out


def verify_same_story(
    session: requests.Session,
    api_key: str | None,
    story_title: str,
    frames: list[tuple[str, dict[str, Any]]],
) -> list[int]:
    """Which candidate frames cover the same specific story. Never raises.

    Returns 1-based indices into `frames`. On any failure it keeps everything,
    because silently dropping coverage is worse than keeping a loose match.
    """
    everything = list(range(1, len(frames) + 1))
    if not api_key or len(frames) < 2:
        return everything

    listing = [
        f"{number}. [{lean}] {item.get('source')}: {str(item.get('title') or '')[:150]}"
        for number, (lean, item) in enumerate(frames, 1)
    ]
    user = (
        f"Story: {story_title[:200]}\n\n"
        "Candidates:\n" + "\n".join(listing) + "\n\n"
        "Which candidates cover that same specific story?"
    )

    parsed = ask_json(COVERAGE_SAME_STORY_PROMPT, user, max_tokens=200,
                      model=DEEPSEEK_MODEL, api_key=api_key, session=session)
    if parsed is None:
        return everything
    indices = parse_indices(json.dumps(parsed), len(frames))
    return everything if indices is None else indices


# --- Output ------------------------------------------------------------------


def report(stories: list[dict[str, Any]]) -> None:
    """Print per-story coverage counts per lean, split by source."""
    print()
    print("=" * 74)
    print("Coverage found per story")
    print("=" * 74)

    grand_total = 0
    for story in stories:
        coverage = story.get("coverage", {})
        counts = ", ".join(f"{lean}={len(coverage.get(lean, []))}" for lean in LEANS)

        items = [i for lean in LEANS for i in coverage.get(lean, [])]
        grand_total += len(items)

        title = (story.get("title") or "(no headline)").strip()
        print(f"  {title[:66]}")
        print(f"      {counts}")
        print(f"      sources: {len(items)} articles")
        print(f"      terms: {story.get('search_terms', [])}")

    print()
    print(
        f"{len(stories)} high-significance stories, "
        f"{grand_total} coverage articles found"
    )
    print(f"Written to {OUTPUT_PATH}")


# --- Entry point -------------------------------------------------------------


def run_pipeline() -> None:
    input_path = first_existing(INPUT_CANDIDATES)
    articles = load_json(input_path)
    print(f"Input: {input_path} ({len(articles)} stories)")

    session = requests.Session()

    deepseek_key = get_api_key()
    print(f"Keywords: {DEEPSEEK_MODEL if deepseek_key else 'stopword fallback'}")

    n_outlets = sum(len(o) for o in RSS_OUTLETS.values())
    n_feeds = sum(len(u) for o in RSS_OUTLETS.values() for u in o.values())
    print(f"Sources: RSS ({n_outlets} outlets, {n_feeds} feeds)")

    high_significance = [
        a
        for a in articles
        if (a.get("classification") or {}).get("significance") == "high"
    ]
    stories = high_significance[:MAX_STORIES_FOR_COVERAGE]

    if not stories:
        print("No high-significance stories — writing input through unchanged.")
        for rank, story in enumerate(select_briefing(articles)):
            story["briefing_rank"] = rank
        save_json(articles, OUTPUT_PATH)
        print(f"Written to {OUTPUT_PATH}")
        return

    if len(high_significance) > len(stories):
        print(
            f"Covering the top {len(stories)} of {len(high_significance)} "
            f"high-significance stories (MAX_STORIES_FOR_COVERAGE)."
        )

    # Feeds do not depend on the story, so fetch them once for the whole run.
    print("Fetching feeds...")
    rss_items = fetch_rss_by_lean()
    summary = "  ".join(f"{lean}={len(rss_items[lean])}" for lean in LEANS)
    print(f"  {summary} items cached")

    print(f"\nGathering coverage for {len(stories)} high-significance stories...")

    for position, story in enumerate(stories, start=1):
        title = (story.get("title") or "(no headline)").strip()
        print(f"\n  [{position}/{len(stories)}] {title[:66]}")

        terms = extract_terms(session, deepseek_key, story)
        query = build_query(terms)
        story["search_terms"] = terms
        story["search_query"] = query
        print(f"      terms: {terms}")

        coverage: dict[str, list[dict[str, Any]]] = {}
        for lean in LEANS:
            hits = rss_coverage(
                rss_items.get(lean, []), terms, str(story.get("title") or "")
            )
            coverage[lean] = dedupe_by_url(hits)
            print(f"      {lean}: {len(coverage[lean])}")

        # Semantic check. Word overlap cannot separate "same war" from "same
        # story": "Ukraine drone strikes kill four" and "Moscow claims Ukraine
        # planned to start a war" share ukraine and russia and nothing else. One
        # judgement covers every candidate for this story.
        frames: list[tuple[str, dict[str, Any]]] = [
            (lean, item) for lean in LEANS for item in coverage.get(lean, [])
        ]
        if frames:
            keep = verify_same_story(
                session, deepseek_key, str(story.get("title") or ""), frames
            )
            if len(keep) < len(frames):
                print(
                    f"      same-story check: kept {len(keep)} of "
                    f"{len(frames)} frames"
                )
            coverage = {lean: [] for lean in LEANS}
            for number in keep:
                lean, item = frames[number - 1]
                coverage[lean].append(item)

        story["coverage"] = coverage

    # Which stories make the page, and in what order. Done once, here, so
    # render.js and write_desk.py read the same answer instead of each
    # re-implementing the rule in their own language.
    #
    # Topic labels come from one call that sees every candidate at once: the
    # per-story classifier cannot be consistent with itself, since it never sees
    # its own previous labels.
    candidates = briefing_candidates(articles)
    assign_briefing_topics(candidates)

    briefing = select_briefing(articles)
    dedupe_briefing_coverage(briefing)
    for rank, story in enumerate(briefing):
        story["briefing_rank"] = rank

    # No degraded-run guard any more. It existed because NewsAPI rate-limits and
    # a throttled run would have overwritten a good file with a thin one; the
    # constraint went with the source. RSS has no meter, and a run that finds
    # nothing now is a run that found nothing.
    save_json(articles, OUTPUT_PATH)
    report(stories)
    report_briefing(briefing)


def main() -> None:
    # No argument handling: the four diagnostic subcommands (--check-domains,
    # --list-sources, --probe, --find-domain) all existed to interrogate
    # NewsAPI's source index, and they went with it. This script now does one
    # thing, which is the thing run.sh calls.
    run_pipeline()


if __name__ == "__main__":
    main()
