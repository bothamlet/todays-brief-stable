#!/usr/bin/env python3
"""
fetch_tech.py — the Tech page.

Tech was the weakest page in the paper and the reason was visible in what landed
on it. It read the world roster filtered by category, and the world roster is
seventeen general news desks: Reuters World, the BBC, the FT. Those newsrooms
cover technology the way they cover a foreign ministry — through its politics.
A typical morning's haul was "Congress is under pressure to act on AI",
"Sanders and Bannon warn of AI dangers", "EU pitches social media ban for
under-16s". Twenty-four stories about AI, and two of them about a thing anybody
had made.

That is not a sourcing problem you fix by adding one more feed. It is a different
question, so this is a different beat, with sources chosen for what they are for:

  Primary         The lab, the platform, the maker: OpenAI, Google DeepMind,
                  Google Research, Hugging Face, NVIDIA. For a model release the
                  announcement is deeper than any write-up of it and arrives
                  first — and these are the only sources that carry the thing
                  itself rather than a reaction to it.

  AI-native       Newsrooms whose subject is the technology: The Decoder,
                  TechCrunch's AI desk, Simon Willison, IEEE Spectrum, MIT
                  Technology Review, TLDR AI.

  Tech press      Fast, and technical by default: Ars Technica, Wired, The
                  Register, 404 Media, Platformer.

Anthropic, Meta AI, Mistral, Cohere and Stability AI have no working feed — five
of the six lab blogs tried — so a Claude or Llama release reaches this page
through the AI-native desks above rather than from the source. Worth knowing:
it is a real gap, not an oversight.

The world roster still contributes. Its Tech stories are gathered too, and land
in the Policy interest. That coverage is good — an EU ruling is worth knowing
about — it simply must not be the whole page, which is what the round-robin
below guarantees: seven interests, at most two stories each, walking the
technical ones first. Policy gets a slot. It does not get the page.

Writes data/tech.json, already ranked and capped, so the page renders what it is
given rather than re-deciding.

Usage:
    python fetch_tech.py
"""

from __future__ import annotations

import json
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
from publishers import normalise
from rss_feeds import require_healthy_feeds
from rss_feeds import backfill_images, fetch_many

# --- Configuration -----------------------------------------------------------

OUTPUT_PATH = Path("data/tech.json")
TRACKED_PATH = Path("data/world_tracked.json")

# Two windows, because they answer two different questions.
#
# Reported news goes stale in a day: a write-up of a launch is worth reading the
# morning it lands and worth very little three days later. A release does not work
# that way — "Introducing ChatGPT for Financial Services" is the definitive
# account of something that now exists, and it is still the best thing to read
# about it a week on.
#
# Holding primary sources to a news cycle produced the opposite of what this page
# is for. OpenAI publishes marketing and customer stories most days and a release
# every week or two, so a 24-hour window kept "Reimagining advertising with AI"
# and discarded "Introducing ChatGPT for Financial Services" — the shallow thing
# daily, the deep thing rarely.
LOOKBACK_HOURS = 24
PRIMARY_LOOKBACK_HOURS = 72

# Deliberately larger than the category pages' five. This is the page that is
# supposed to go deeper, and a beat file that caps at five throws away the
# narrow interests — a chips story never beats a model release for one of five
# slots, but it wins the slot it is alone in.
MAX_STORIES = 8
MAX_PER_INTEREST = 2

# Per feed, before anything else. OpenAI's archive is 1,198 items and Hugging
# Face's 862; sorting that many to show two is work for nothing.
MAX_PER_FEED = 40

# --- Sources -----------------------------------------------------------------
# The third field is the interest to fall back on when no keyword matches. It
# says what the feed is usually about, which is not the same as what any given
# item is about — a DeepMind post about safety policy is a Policy story that
# happens to live on a models blog.

DIRECT_FEEDS: tuple[tuple[str, str, str], ...] = (
    # Primary.
    ("OpenAI", "https://openai.com/news/rss.xml", "models"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "models"),
    ("Google Research", "https://research.google/blog/rss/", "research"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "models"),
    ("NVIDIA", "https://blogs.nvidia.com/feed/", "chips"),
    # AI-native.
    ("The Decoder", "https://the-decoder.com/feed/", "models"),
    (
        "TechCrunch AI",
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "products",
    ),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", "models"),
    ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss", "research"),
    (
        "MIT Technology Review",
        "https://www.technologyreview.com/feed/",
        "research",
    ),
    ("TLDR AI", "https://tldr.tech/api/rss/ai", "models"),
    # Tech press.
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "products"),
    ("Wired", "https://www.wired.com/feed/rss", "safety"),
    ("The Register", "https://www.theregister.com/headlines.atom", "products"),
    ("404 Media", "https://www.404media.co/rss/", "safety"),
    ("Platformer", "https://www.platformer.news/rss/", "policy"),
)

# --- What the page is about --------------------------------------------------
# Order is priority: the round-robin in select() walks this, one from each pass,
# so the technical interests fill first and Policy takes what is left rather than
# what it can grab.
#
# Keyword matching is a plain substring test, which makes short words dangerous:
# "law" matches "flaw", "ban" matches "urban", "eu" matches "museum". Everything
# below is a form that cannot land inside an unrelated word.

INTERESTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "models",
        "Models",
        (
            "introducing",
            "now available",
            "open weights",
            "open-source model",
            "model card",
            "checkpoint",
            "gpt-",
            "claude",
            "gemini",
            "llama",
            "qwen",
            "deepseek",
            "mistral",
            "grok",
            "phi-",
            "granite",
            "command r",
            "foundation model",
            "frontier model",
        ),
    ),
    (
        "chips",
        "Chips & compute",
        (
            "nvidia",
            "gpu",
            "tpu",
            "silicon",
            " chip",
            "chips",
            "fabrication",
            "tsmc",
            "data centre",
            "data center",
            "datacentre",
            "compute cluster",
            "hbm",
            "wafer",
            "inference cost",
            "megawatt",
        ),
    ),
    (
        "research",
        "Research",
        (
            "researchers",
            "arxiv",
            "benchmark",
            "state of the art",
            "breakthrough",
            "scaling law",
            "interpretability",
            "training run",
            "preprint",
            "protein structure",
            "theorem",
            "we present",
        ),
    ),
    (
        "safety",
        "Safety & security",
        (
            "alignment",
            "jailbreak",
            "guardrail",
            "deepfake",
            "disinformation",
            "model theft",
            "prompt injection",
            "exploit",
            "vulnerabilit",
            "cyberattack",
            "malicious",
            "misuse",
            "safeguard",
            "red team",
        ),
    ),
    (
        "products",
        "Products",
        (
            "browser",
            "smartphone",
            "wearable",
            "rolls out",
            "roll out",
            "update",
            "app store",
            "assistant",
            "android",
            "windows",
            "feature",
            "device",
            "laptop",
        ),
    ),
    (
        "business",
        "Business",
        (
            "funding round",
            "raises $",
            "valuation",
            "acquires",
            "acquisition",
            "initial public offering",
            "lays off",
            "layoffs",
            "revenue",
            "earnings",
            "partnership",
            "merger",
        ),
    ),
    (
        "policy",
        "Policy & law",
        (
            "regulat",
            "legislation",
            "court",
            "lawsuit",
            "senate",
            "congress",
            "parliament",
            "commission",
            "antitrust",
            "copyright",
            "liability",
            "privacy",
            "banned",
            "banning",
            "to ban",
            "ruling",
            "ministry",
        ),
    ),
)

# What the page represents, as opposed to what its cards are tagged with.
#
# The tags are for the reader and are specific — seven of them, because "Chips &
# compute" and "Policy & law" are different things to be told. The walk is for
# the page and is coarser, because a page is five stories long and seven
# interests cannot fit in five. Five themes can.
#
# Merging is not a compromise here. A model release and a paper are both AI, and
# ranking inside the merged bucket still puts the release first — that is what
# source_tier is for. Business and policy are both the industry around the
# technology, and whichever of the two is stronger that morning takes the slot.
BUCKETS: dict[str, str] = {
    "models": "ai",
    "research": "ai",
    "chips": "infrastructure",
    "safety": "safety",
    "products": "products",
    "business": "industry",
    "policy": "industry",
}

BUCKET_ORDER: tuple[str, ...] = (
    "ai",
    "infrastructure",
    "safety",
    "products",
    "industry",
)

# Note the trailing boundary: there is none, and that is deliberate.
#
# It was `\b(...)\b` at first, which silently disabled the `save 40%`
# alternative: `%` is not a word character, and neither is the space after it, so
# there is no word boundary there and the pattern could never match. A junk
# filter that quietly drops one of its own rules is invisible — the headline just
# reaches the page.
#
# Dropping the trailing boundary also lets the other alternatives catch plurals
# ("coupons", "webinars"), which the leading one already prevented from matching
# mid-word.
JUNK = re.compile(
    r"\b("
    r"best\s+(vpn|antivirus|phone|laptop|router|ai\s+tools?)|"
    r"deals?\s+of\s+the\s+day|prime\s+day|black\s+friday|cyber\s+monday|"
    r"save\s+\d+\s*%|"
    r"sponsored|advertorial|partner\s+content|paid\s+post|webinar|"
    r"price\s+prediction|to\s+the\s+moon|how\s+to\s+buy|"
    r"coupon|promo\s+code|giveaway|"
    r"shares?\s+(surge|jump|plunge|soar|tumble)|stock\s+(surge|jump|plunge)|"
    r"box\s+office|new\s+trailer|movie\s+trailer|season\s+\d+\s+premiere"
    r")",
    re.IGNORECASE,
)


def is_junk(article: dict[str, Any]) -> bool:
    """Deal roundups, listicles and sponsored copy — see JUNK."""
    return bool(JUNK.search(str(article.get("title") or "")))


# Sources carrying the thing itself rather than a reaction to it.
#
# Within an interest these come first, and that ordering is the reason this page
# exists. A model release announced by the lab that made it is deeper and earlier
# than any write-up of it — and on recency alone the write-up always wins,
# because a newsroom publishes four pieces about a release in the time the lab
# publishes one. The first run of this file had no primary source on the page at
# all for exactly that reason.
PRIMARY: frozenset[str] = frozenset(
    {
        "openai",
        "google deepmind",
        "google research",
        "hugging face",
        "nvidia",
    }
)


# The recognisable shape of a post that is not news.
#
# Titles opening this way are customer stories, thought leadership and brand copy:
# "How Fyxer built an AI executive assistant people trust", "Helping older adults
# use AI in everyday life", "Reimagining advertising with AI", "How to connect AI
# usage to business value". Four of OpenAI's most recent fourteen are shapes like
# this, and none of them announces anything.
#
# Recognising marketing is more reliable than recognising a release, which was the
# first attempt. Announcements share no shape — "Introducing X", "X is now
# available", "NVIDIA Vera Rubin NVL72 Delivers Leading Performance" — so a regex
# for them caught the first two and demoted a real launch, and its failure mode
# was to hide the exact thing this page exists to show. Almost everything that is
# not news, by contrast, opens the same way.
#
# Only applied to primary sources. These openings are ordinary in a newsroom's
# hands — "Why the EU banned..." is reporting — and there they are left alone.
MARKETING = re.compile(
    r"^(how|why|what|when|helping|reimagining|building|supporting|"
    r"the case for|a guide to|inside|meet|understanding)\b",
    re.IGNORECASE,
)


def source_tier(article: dict[str, Any]) -> int:
    """0 for a lab's own announcement, 1 for reporting, 2 for a lab's marketing.

    The order is this page's argument. A lab's announcement is the thing itself:
    definitive, and earlier than anyone's account of it. Reporting is somebody
    else's account — second-hand, but independent and written to be read. A
    lab's marketing is neither news about something that now exists nor
    independent, and it publishes more often than either.

    That last point is the one that took two runs to see. Ordered above
    reporting, "Helping older adults use AI in everyday life" beat Ars Technica
    for a slot on the page — because it came from the lab, which looked like
    primary sourcing and was really a publishing cadence.
    """
    if normalise(str(article.get("source") or "")) not in PRIMARY:
        return 1
    title = str(article.get("title") or "").strip()
    return 2 if MARKETING.match(title) else 0


# --- Matching ----------------------------------------------------------------


def interest_for(
    text: str, fallback: str | None
) -> tuple[str | None, str | None, bool]:
    """Name the first interest this text matches, or the feed's own fallback.

    First match wins, and INTERESTS is ordered technical-to-political, so a story
    about a model release is tagged Models even when it also mentions a regulator.

    The third value says whether a keyword actually matched, which is the
    difference between "this is about a chip" and "this came from a feed that is
    usually about chips".
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


# --- Gathering ---------------------------------------------------------------


def from_feeds() -> list[dict[str, Any]]:
    """Everything the direct feeds returned, tagged and truncated per feed."""
    results = fetch_many(
        [(name, url) for name, url, _interest in DIRECT_FEEDS], workers=8
    )
    require_healthy_feeds(results, "Tech")
    fallback_by_url = {url: interest for _name, url, interest in DIRECT_FEEDS}

    articles: list[dict[str, Any]] = []
    for url, rows in results.items():
        fallback = fallback_by_url.get(url, "products")
        # The feeds are sorted newest first by fetch_feed, so the slice is the
        # newest forty rather than an arbitrary forty.
        for row in rows[:MAX_PER_FEED]:
            if is_junk(row):
                continue
            text = f"{row.get('title') or ''} {row.get('description') or ''}"
            key, label, _matched = interest_for(text, fallback)
            if key is None:
                continue
            row["interest"], row["interest_label"] = key, label
            row["via"] = "feed"
            articles.append(row)

    return articles


def from_world() -> list[dict[str, Any]]:
    """The world roster's Tech stories, so the good policy coverage survives.

    These arrive classified rather than fed, so they are reduced to the same card
    shape as everything else and tagged by the same rules — which lands nearly
    all of them in Policy, because that is what a world desk writes about
    technology.
    """
    if not TRACKED_PATH.is_file():
        return []
    try:
        rows = json.loads(TRACKED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []

    articles: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (row.get("classification") or {}).get("category") != "Tech":
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url or is_junk(row):
            continue

        text = f"{title} {row.get('description') or ''}"
        key, label, _matched = interest_for(text, "policy")
        articles.append(
            {
                "title": title,
                "description": str(row.get("description") or ""),
                "url": url,
                "urlToImage": str(row.get("urlToImage") or ""),
                "source": str(row.get("source") or ""),
                "publishedAt": str(row.get("publishedAt") or ""),
                "interest": key,
                "interest_label": label,
                "via": "world",
            }
        )

    return articles


# --- Selection ---------------------------------------------------------------


def select(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin across interests, so no one angle takes the page.

    Depth passes, taking one story per interest per pass. That is what makes the
    page balanced rather than merely long: a busy interest cannot fill every slot
    by publishing more, because it gets one per pass like everything else.

    This is the fix for the actual complaint. Flat ranking by recency gave a page
    of whatever the wire services had written most about, and what wire services
    write most about is legislation.

    The walk is over themes rather than the seven interests the cards are tagged
    with — see BUCKETS. The tag tells the reader what a story is; the walk decides
    what the page represents, and a page is five stories long.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        theme = BUCKETS.get(str(article.get("interest")), "industry")
        buckets.setdefault(theme, []).append(article)

    for rows in buckets.values():
        # newest_first RETURNS a sorted list; calling it as a statement does
        # nothing at all. This bug has been made twice in this codebase.
        ordered = newest_first(rows)
        # Two sorts, not one. Python's sort is stable, so date-then-tier gives
        # newest-first inside each tier. Sorting by tier first and date second
        # would discard the date entirely, which is the trap this codebase has
        # already fallen into once.
        ordered.sort(key=source_tier)
        rows[:] = ordered[: MAX_PER_INTEREST * 5]

    order = list(BUCKET_ORDER)
    chosen: list[dict[str, Any]] = []
    taken: Counter[str] = Counter()
    depth = 0

    while len(chosen) < MAX_STORIES and depth < MAX_PER_INTEREST:
        for key in order:
            if len(chosen) >= MAX_STORIES:
                break
            if taken[key] > depth:
                continue
            pool = buckets.get(key) or []
            if len(pool) <= taken[key]:
                continue
            chosen.append(pool[taken[key]])
            taken[key] += 1
        depth += 1

    return chosen


# --- Entry point -------------------------------------------------------------


def report(chosen: list[dict[str, Any]], total: int) -> None:
    print()
    print("=" * 70)
    print(f"Tech page: {len(chosen)} of {total} candidates")
    print("=" * 70)
    for article in chosen:
        stamp = parse_timestamp(article.get("publishedAt"))
        when = stamp.strftime("%a %H:%M") if stamp else "  ?  "
        label = str(article.get("interest_label") or "")
        print(f"  [{label:<18}] {when}  {str(article.get('title') or '')[:44]}")

    if chosen:
        print()
        print("  by interest:", dict(Counter(str(a.get("interest")) for a in chosen)))
        print(
            "  by source  :",
            dict(Counter(str(a.get("source")) for a in chosen).most_common(6)),
        )
        tiers = Counter(source_tier(a) for a in chosen)
        print(
            "  by tier    :",
            {
                "lab announcement": tiers[0],
                "reporting": tiers[1],
                "lab marketing": tiers[2],
            },
        )
    print()
    print(f"Written to {OUTPUT_PATH}")


def main() -> None:
    now = utc_now()
    news_cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    primary_cutoff = now - timedelta(hours=PRIMARY_LOOKBACK_HOURS)

    gathered = from_feeds() + from_world()

    fresh: list[dict[str, Any]] = []
    for article in gathered:
        stamp = parse_timestamp(article.get("publishedAt"))
        # An undated item is kept: Atom feeds sometimes omit the date, and a
        # story missing its clock is not evidence that it is old.
        #
        # A lab's own announcement keeps its value for a few days; everything
        # else is judged by a news cycle. See PRIMARY_LOOKBACK_HOURS.
        cutoff = primary_cutoff if source_tier(article) == 0 else news_cutoff
        if stamp is None or stamp >= cutoff:
            fresh.append(article)

    fresh = dedupe_by_url(fresh)

    chosen = select(fresh)

    # One page fetch each, and only for the items actually going on the page.
    filled = backfill_images(chosen)
    if filled:
        print(f"  recovered {filled} image(s) from the article pages")

    save_json(chosen, OUTPUT_PATH)
    report(chosen, len(fresh))


if __name__ == "__main__":
    main()
