#!/usr/bin/env python3
"""Collect and compare alternate coverage of world stories."""

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



DEEPSEEK_MODEL = os.environ.get("COVERAGE_MODEL") or MODEL

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

# Require enough common headline terms before semantic verification.
MIN_TITLE_OVERLAP = 2
MAX_STORIES_FOR_COVERAGE = 20

# Outlet lean is editorial metadata, not article-level scoring.

# Separate direct feeds from publisher searches so shared feeds are fetched once.
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
        # Use a publisher search where no direct Reuters feed is available.
        "Reuters": [
            "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US:en",
        ],
        # Use a publisher search where no direct AP feed is available.
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
    # Direct feeds and publisher searches feed the same coverage pool.
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

OUTLETS_BY_LEAN: dict[str, list[str]] = {
    # Publisher-specific searches supplement the direct feeds.
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
    "right": [
        "nypost.com",  # 1674  Lean Right
        "foxnews.com",  # 712   Right
        "breitbart.com",  # 374   Right, further
        "dailycaller.com",  # 151   Right, further
        "reason.com",  # 122   Libertarian / Lean Right
        "dailysignal.com",  # 103   Right
    ],
}




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
    """OR-join the terms so any one of them can match."""
    return " OR ".join(format_term(t) for t in terms[:MAX_QUERY_TERMS])


def extract_terms(
    session: requests.Session,
    deepseek_key: str | None,
    article: dict[str, Any],
) -> list[str]:
    """Best-effort distinctive search terms for a story, preferring the model."""
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





def fetch_rss_by_lean() -> dict[str, list[dict[str, Any]]]:
    """Fetch every configured feed once, grouped by lean."""
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
    """Rank candidates that share enough headline terms for verification."""
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
    """Which candidate frames cover the same specific story."""
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

        # Title overlap alone cannot distinguish episodes within an ongoing conflict.
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

    # Rank once so browser and notification use the same briefing.
    candidates = briefing_candidates(articles)
    assign_briefing_topics(candidates)

    briefing = select_briefing(articles)
    dedupe_briefing_coverage(briefing)
    for rank, story in enumerate(briefing):
        story["briefing_rank"] = rank

    save_json(articles, OUTPUT_PATH)
    report(stories)
    report_briefing(briefing)


def main() -> None:
    run_pipeline()


if __name__ == "__main__":
    main()
