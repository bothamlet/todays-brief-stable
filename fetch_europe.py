#!/usr/bin/env python3
"""
fetch_europe.py — the Europe page.

This one is not a beat. The sports and Lietuva pages fetch their own sources
because the world roster does not cover their subjects well; Europe needs nothing
new. Every story the world pipeline collects is already classified with a region,
and 63 of today's 234 are Europe — data that was being computed and read by no
page at all.

It reads two files and writes one, so it takes about a second:

    data/world_tracked.json   the classified, history-annotated stories
    data/world_covered.json   for briefing_rank — what the World page leads with
    data/europe.json          Europe, without the front page's own stories

Two decisions worth stating.

Europe is what the front page is NOT. The World page shows the briefing, and the
briefing is two-thirds European on a European news day, so a naive region filter
would open the Europe tab with the same two cards the reader just swiped past. The
stories the briefing already carries are held back, which makes this page additive
rather than a re-run. It is why the page starts lower down the day's news.

Sourcing is the world roster — Le Monde, France 24, Sky News, the Financial Times,
The Economist, the BBC — rather than European-native outlets. That is the same
trade the region data already made, and it keeps this page free. A Europe desk with
Euronews, Politico Europe, Der Spiegel and El Pais would cover the continent better
than six international desks do; that is a beat like the other two, and it is the
obvious upgrade if this page reads thin.

Usage:
    python fetch_europe.py
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from common import (
    load_json,
    newest_first,
    parse_timestamp,
    save_json,
)
from publishers import normalise

# --- Configuration -----------------------------------------------------------

TRACKED_PATH = Path("data/world_tracked.json")
COVERED_PATH = Path("data/world_covered.json")
OUTPUT_PATH = Path("data/europe.json")

REGION = "europe"

MAX_STORIES = 8

# Delfi and Lrytas publish fifty items a day each on the Lietuva page for the same
# reason this exists: without it the page is whichever two desks file fastest.
MAX_PER_SOURCE = 2

SIGNIFICANCE_RANK = {"high": 0, "medium": 1, "low": 2}


def already_on_the_front_page() -> set[str]:
    """Headlines the World page displays, so this page can leave them alone."""
    covered = load_json(COVERED_PATH)
    return {
        str(story.get("title") or "").strip().lower()
        for story in covered
        if story.get("briefing_rank") is not None
    }


def european(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stories the classifier put in Europe, minus the front page's own."""
    briefed = already_on_the_front_page()
    kept: list[dict[str, Any]] = []

    for story in stories:
        classification = story.get("classification") or {}
        if classification.get("region") != REGION:
            continue
        if str(story.get("title") or "").strip().lower() in briefed:
            continue
        kept.append(story)

    return kept


def rank(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most significant first, newest inside a tier — then spread across outlets.

    A flat two-key sort is enough here, unlike the World page's briefing: there are
    no topic caps and no lean balance to keep, so there is nothing for two
    implementations to disagree about.
    """
    # Two passes, not two sorts. Python's sort is stable, so sorting by date first
    # and by tier second keeps the date order intact inside each tier. Writing it
    # as one order then another — sorted() by tier, then newest_first() over the
    # result — discards the tier entirely, and the first run of this page showed
    # eight medium stories while nine high-significance ones sat unused.
    ordered = newest_first(stories)
    ordered.sort(
        key=lambda s: SIGNIFICANCE_RANK.get(
            str((s.get("classification") or {}).get("significance") or ""), 3
        )
    )

    chosen: list[dict[str, Any]] = []
    used: Counter[str] = Counter()
    for story in ordered:
        source = normalise(str(story.get("source") or "?")) or "?"
        if used[source] >= MAX_PER_SOURCE:
            continue
        used[source] += 1
        chosen.append(story)
        if len(chosen) >= MAX_STORIES:
            break
    return chosen


def report(chosen: list[dict[str, Any]], total: int) -> None:
    print()
    print("=" * 68)
    print(f"Europe page: {len(chosen)} of {total} candidates")
    print("=" * 68)
    for story in chosen:
        observed = parse_timestamp(story.get("publishedAt"))
        when = observed.strftime("%a %H:%M") if observed else "  ?  "
        verdict = story.get("classification") or {}
        source = str(story.get("source") or "?")[:16]
        print(
            f"  [{source:<16}] {when} {str(verdict.get('significance') or '')[:6]:<6} "
            f"{str(story.get('title') or '')[:38]}"
        )
    print()
    print(f"Written to {OUTPUT_PATH}")


def main() -> None:
    tracked = load_json(TRACKED_PATH)
    candidates = european(tracked)
    chosen = rank(candidates)

    if not chosen:
        print("No European stories outside the briefing — writing an empty page.")
    save_json(chosen, OUTPUT_PATH)
    report(chosen, len(candidates))


if __name__ == "__main__":
    main()
