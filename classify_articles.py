#!/usr/bin/env python3
"""
classify_articles.py — Step 2 of the daily news pipeline.

Reads data/world_raw.json, classifies each headline with DeepSeek, drops the
noise, then writes a significance-sorted list to data/world_filtered.json.

Auth: set DEEPSEEK_API_KEY in the environment.

Override the model with CLASSIFY_MODEL (default: deepseek-flash).

Usage:
    python classify_articles.py

Cost notes:
    * Only the headline + summary are sent — never the full article body.
    * max_tokens is capped low because the reply is a tiny JSON object.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Sibling modules. Python puts this script's own directory on sys.path; pyright
# analyses each file standalone and cannot see them, hence the directives.
from common import first_existing, load_json, save_json  # pyright: ignore[reportMissingImports]
from llm import MODEL as DEFAULT_MODEL  # pyright: ignore[reportMissingImports]
from llm import ask_json  # pyright: ignore[reportMissingImports]

# --- Configuration -----------------------------------------------------------

INPUT_CANDIDATES = (
    Path("data/world_clustered.json"),  # preferred: one story per event
    Path("data/world_raw.json"),  # fallback if clustering hasn't run
)
OUTPUT_PATH = Path("data/world_filtered.json")

# Per-step override, falling back to the pipeline-wide DEEPSEEK_MODEL.
MODEL = os.environ.get("CLASSIFY_MODEL") or DEFAULT_MODEL

MAX_TOKENS = 200  # generous enough that the JSON can't be truncated mid-object

CATEGORIES = ("World", "Lithuania", "Tech", "Sports", "Other", "Discard")
SIGNIFICANCE = ("high", "medium", "low")
SIGNIFICANCE_RANK = {"high": 0, "medium": 1, "low": 2}

# Used when the model errors or returns something unparseable. We keep the
# article (never silently drop it) but park it at the bottom of the pile.
FALLBACK: dict[str, str] = {
    "category": "Other",
    "significance": "low",
    "topic": "uncategorised",
    "region": "Global",
    "reason": "classification unavailable",
}

# classify() never raises — a failed call degrades to FALLBACK so one bad headline
# cannot abort the run. That is right for one headline and catastrophic for all of
# them: if the model is rate-limited or out of quota, every article falls back and
# a full set of FALLBACK verdicts overwrites the previous run's real ones. The
# result looks like a successful run in every log line except the significance
# counts. Past this share, the run is not written over good data.
MAX_FALLBACK_FRACTION = 0.5

# DeepSeek's JSON output mode requires the literal word "json" in the prompt
# plus an example of the shape we want.
SYSTEM_PROMPT = """\
You triage headlines for a daily news briefing.

Category (pick exactly one):
- World: genuine international news — geopolitics, conflict, major policy, global economic shifts.
- Lithuania: news about Lithuania.
- Tech: technology news.
- Sports: actual sports results or events. Not predictions, not fantasy content.
- Other: economy or business news that doesn't fit World.
- Discard: horoscopes, celebrity gossip, tabloid content, fantasy sports predictions, listicles, or anything with no real informational value.

Significance (pick exactly one):
- high: major, consequential story.
- medium: notable but not urgent.
- low: technically on-topic but minor or routine.

Topic (always):
- topic: a 2-4 word label naming what the story is about, at the level an editor
  would use to group related coverage.
- Stories about the same ongoing event MUST get the same label, even when their
  headlines share no words. "Houthis attack Saudi base", "Saudi pipeline shut"
  and "ship struck near Hormuz" are one event, not three: label them all the
  same thing, e.g. "Iran-Saudi escalation".
- Use a plain, stable label. Do not invent a new phrasing each time.

Region (always):
- region: the part of the world the story is mainly about, as exactly one of:
  Middle East, Europe, Asia, Africa, Americas, Oceania, Global.
- Use Global only when the story is genuinely worldwide — markets, climate, a UN
  vote — rather than sited somewhere. A story about Iran and Saudi Arabia is
  Middle East, not Global.

Reply with a single json object and nothing else, in exactly this shape:
{"category": "World", "significance": "high", "topic": "Iran-Saudi escalation", "region": "Middle East", "reason": "major geopolitical shift"}"""


# --- Helpers -----------------------------------------------------------------


def coerce_verdict(parsed: dict[str, Any]) -> dict[str, str]:
    """Coerce a model reply to our exact schema, falling back where needed."""
    category = str(parsed.get("category", "")).strip().title()
    if category not in CATEGORIES:
        category = FALLBACK["category"]

    significance = str(parsed.get("significance", "")).strip().lower()
    if significance not in SIGNIFICANCE:
        significance = FALLBACK["significance"]

    reason = str(parsed.get("reason", "")).strip()[:120] or "no reason given"

    # Carried so the briefing can avoid filling every slot with one ongoing
    # event. Headlines about the same crisis often share no vocabulary at all,
    # so this cannot be derived lexically downstream.
    topic = str(parsed.get("topic", "")).strip()[:48].lower() or FALLBACK["topic"]

    # The topic label is deliberately granular — "yemen conflict" and "us-iran
    # conflict" are separate topics — so it cannot stop one REGION taking the
    # whole page. The briefing caps per region as well as per topic.
    regions = {
        "middle east",
        "europe",
        "asia",
        "africa",
        "americas",
        "oceania",
        "global",
    }
    region = str(parsed.get("region", "")).strip().lower()
    if region not in regions:
        region = FALLBACK["region"].lower()

    return {
        "category": category,
        "significance": significance,
        "topic": topic,
        "region": region,
        "reason": reason,
    }


# --- Classification ----------------------------------------------------------


def build_prompt(article: dict[str, Any]) -> str:
    """Headline + summary only — keeps the per-article token cost tiny."""
    title = (article.get("title") or "").strip() or "(no headline)"
    description = (article.get("description") or "").strip() or "(no summary available)"
    return f"Headline: {title}\nSummary: {description}"


def classify(article: dict[str, Any]) -> dict[str, str]:
    """Ask the model to categorise one story. Never raises."""
    parsed = ask_json(
        SYSTEM_PROMPT,
        build_prompt(article),
        max_tokens=MAX_TOKENS,
        model=MODEL,
    )
    return coerce_verdict(parsed) if parsed else dict(FALLBACK)


def report(kept: list[dict[str, Any]], discarded: list[dict[str, Any]]) -> None:
    """Print the kept/discarded summary with reasons."""
    total = len(kept) + len(discarded)

    print()
    print("=" * 68)
    print(f"Kept {len(kept)} of {total}  |  discarded {len(discarded)}")
    print("=" * 68)

    by_significance = {level: 0 for level in SIGNIFICANCE}
    by_category: dict[str, int] = {}
    for article in kept:
        verdict = article["classification"]
        by_significance[verdict["significance"]] += 1
        by_category[verdict["category"]] = by_category.get(verdict["category"], 0) + 1

    print(
        "Significance: "
        + "  ".join(f"{level}={by_significance[level]}" for level in SIGNIFICANCE)
    )
    print(
        "Categories:   "
        + "  ".join(f"{name}={count}" for name, count in sorted(by_category.items()))
    )

    if discarded:
        print()
        print("Discarded:")
        for article in discarded:
            verdict = article["classification"]
            title = (article.get("title") or "(no headline)").strip()
            print(f"  - {title}")
            print(f"      [{verdict['category']}] {verdict['reason']}")

    print()
    print(f"Written to {OUTPUT_PATH}")


# --- Entry point -------------------------------------------------------------


def main() -> None:
    input_path = first_existing(INPUT_CANDIDATES)
    articles = load_json(input_path)
    print(f"Input: {input_path} ({len(articles)} stories)")

    print(f"Classifying {len(articles)} articles with {MODEL}...")

    kept: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    fallbacks = 0

    for position, article in enumerate(articles, start=1):
        title = (article.get("title") or "(no headline)").strip()
        print(f"  [{position}/{len(articles)}] {title[:62]}")

        article["classification"] = classify(article)
        verdict = article["classification"]
        if verdict["reason"] == FALLBACK["reason"]:
            fallbacks += 1
        print(f"        -> {verdict['category']} / {verdict['significance']}")

        (discarded if verdict["category"] == "Discard" else kept).append(article)

    # Stable sort: high -> medium -> low, original order preserved within a tier.
    kept.sort(key=lambda a: SIGNIFICANCE_RANK[a["classification"]["significance"]])

    if articles and fallbacks / len(articles) > MAX_FALLBACK_FRACTION:
        partial = OUTPUT_PATH.with_suffix(".partial.json")
        save_json(articles, partial)
        print()
        print(
            f"WARNING: {fallbacks}/{len(articles)} classifications fell back to "
            "defaults — the model is probably rate-limited or out of quota.\n"
            f"Left the existing {OUTPUT_PATH} untouched and wrote this run to\n"
            f"{partial} instead."
        )
        sys.exit(1)

    if fallbacks:
        print(f"Note: {fallbacks}/{len(articles)} classifications fell back to defaults.")

    save_json(kept, OUTPUT_PATH)
    report(kept, discarded)


if __name__ == "__main__":
    main()
