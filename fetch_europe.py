#!/usr/bin/env python3
"""Select stories for the Europe section."""

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


TRACKED_PATH = Path("data/world_tracked.json")
COVERED_PATH = Path("data/world_covered.json")
OUTPUT_PATH = Path("data/europe.json")

REGION = "europe"

MAX_STORIES = 8

# Cap stories per outlet to preserve source variety.
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
    """Most significant first, newest inside a tier — then spread across outlets."""
    # Apply date before significance so stable sorting keeps chronology within each tier.
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
