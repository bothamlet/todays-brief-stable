#!/usr/bin/env python3
"""Collect and rank technology news."""

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


OUTPUT_PATH = Path("data/tech.json")
TRACKED_PATH = Path("data/world_tracked.json")

# Use separate windows for collection and publication.
LOOKBACK_HOURS = 24
PRIMARY_LOOKBACK_HOURS = 72

MAX_STORIES = 8
MAX_PER_INTEREST = 2

# Cap archive feeds before ranking.
MAX_PER_FEED = 40


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

# Interest labels and column quotas represent different choices.
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

# Leave the trailing edge open to match variants of each keyword.
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


PRIMARY: frozenset[str] = frozenset(
    {
        "openai",
        "google deepmind",
        "google research",
        "hugging face",
        "nvidia",
    }
)


MARKETING = re.compile(
    r"^(how|why|what|when|helping|reimagining|building|supporting|"
    r"the case for|a guide to|inside|meet|understanding)\b",
    re.IGNORECASE,
)


def source_tier(article: dict[str, Any]) -> int:
    """0 for a lab's own announcement, 1 for reporting, 2 for a lab's marketing."""
    if normalise(str(article.get("source") or "")) not in PRIMARY:
        return 1
    title = str(article.get("title") or "").strip()
    return 2 if MARKETING.match(title) else 0




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
    """The world roster's Tech stories, so the good policy coverage survives."""
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




def select(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin across interests, so no one angle takes the page."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        theme = BUCKETS.get(str(article.get("interest")), "industry")
        buckets.setdefault(theme, []).append(article)

    for rows in buckets.values():
        ordered = newest_first(rows)
        # Stable sorts keep chronology within each priority tier.
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
        # Keep undated articles rather than treating them as expired.
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
