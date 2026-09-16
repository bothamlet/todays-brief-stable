#!/usr/bin/env python3
"""
cluster_stories.py — Step 1b of the daily news pipeline.

Merges the same story as reported by different outlets into one cluster, so the
briefing shows one card per event rather than seven cards for one ferry sinking.

Reads  data/world_raw.json
Writes data/world_clustered.json

Method: lexical, not an LLM call. Titles are lowercased, stopworded and compared
with Jaccard similarity; pairs above SIMILARITY_THRESHOLD are merged with
union-find. Verified against a live 300-article corpus: it merges the Indonesian
ferry story across seven outlets (Guardian through Fox News) with no false
merges, and costs nothing.

Why lexical rather than an LLM: classification cost is not the concern here
(300 deepseek-flash calls is fractions of a cent), but running an LLM over every
pairing is slow and nondeterministic. This is free, instant, and reproducible.

Usage:
    python cluster_stories.py            # cluster and write
    python cluster_stories.py --show 10  # also print the 10 biggest clusters
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling modules. Python puts this script's own directory on sys.path; pyright
# analyses each file standalone and cannot see them, hence the directives.
from common import load_json, parse_timestamp, save_json  # pyright: ignore[reportMissingImports]
from textutil import clean_title, significant_tokens  # pyright: ignore[reportMissingImports]

# --- Configuration -----------------------------------------------------------

INPUT_PATH = Path("data/world_raw.json")
OUTPUT_PATH = Path("data/world_clustered.json")

# 0.34 was tuned against a live corpus — but before the clean_title bug was fixed,
# so it was tuned against partly mangled tokens. Worth re-checking if clustering
# ever starts over- or under-merging.
SIMILARITY_THRESHOLD = 0.34


def jaccard(left: set[str], right: set[str]) -> float:
    """Token overlap, 0.0 – 1.0."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class UnionFind:
    """Single-linkage clustering over pairwise similarity."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, node: int) -> int:
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]  # path halving
            node = self.parent[node]
        return node

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


# --- Clustering --------------------------------------------------------------


def build_clusters(rows: list[dict[str, Any]]) -> list[list[int]]:
    """Group row indices into clusters of the same story."""
    tokens = [significant_tokens(r.get("title") or "") for r in rows]

    uf = UnionFind(len(rows))
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if jaccard(tokens[i], tokens[j]) >= SIMILARITY_THRESHOLD:
                uf.union(i, j)

    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        grouped[uf.find(index)].append(index)

    return list(grouped.values())


def pick_representative(rows: list[dict[str, Any]], members: list[int]) -> int:
    """Choose the member that best describes the story.

    Longest description wins: it gives the classifier and the card the most
    substance. Ties break toward the earliest to publish.
    """
    return max(
        members,
        key=lambda i: (
            len(rows[i].get("description") or ""),
            -i,  # earlier index wins ties (input is newest-first)
        ),
    )


def build_story(rows: list[dict[str, Any]], members: list[int]) -> dict[str, Any]:
    """Flatten one cluster into a story, keeping the original article schema."""
    lead = rows[pick_representative(rows, members)]

    # One entry per OUTLET, newest article first. An outlet that files twice on
    # the same story (France 24 did on the AfD protests) must not appear twice,
    # or outlet_count and len(outlets) disagree about what the list means.
    newest_first = sorted(
        members,
        key=lambda i: parse_timestamp(rows[i].get("publishedAt"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    by_outlet: dict[str, dict[str, Any]] = {}
    for i in newest_first:
        source = rows[i].get("source")
        if not source or source in by_outlet:
            continue
        by_outlet[source] = {
            "source": source,
            "title": rows[i].get("title"),
            "url": rows[i].get("url"),
            "publishedAt": rows[i].get("publishedAt"),
        }

    outlets = list(by_outlet.values())

    return {
        "title": lead.get("title"),
        "description": lead.get("description"),
        "url": lead.get("url"),
        "urlToImage": lead.get("urlToImage"),
        "source": lead.get("source"),
        "publishedAt": lead.get("publishedAt"),
        "via": lead.get("via", "rss"),
        # Which outlets ran this story, so downstream steps and the UI know who
        # covered it before per-lean coverage is even fetched.
        "outlets": outlets,
        "outlet_count": len(outlets),
        "article_count": len(members),
    }


# --- Entry point -------------------------------------------------------------


def main() -> None:
    show = 0
    if "--show" in sys.argv:
        try:
            show = int(sys.argv[sys.argv.index("--show") + 1])
        except (IndexError, ValueError):
            print("ERROR: --show needs a number, e.g. --show 10", file=sys.stderr)
            sys.exit(1)

    rows = load_json(INPUT_PATH)

    print(f"Clustering {len(rows)} articles...")
    clusters = build_clusters(rows)

    stories = [build_story(rows, members) for members in clusters]
    # Most-covered stories first, then most recent — a cheap prominence signal
    # before classification scores significance properly.
    stories.sort(
        key=lambda s: (
            -s["outlet_count"],
            -(parse_timestamp(s.get("publishedAt")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        )
    )

    save_json(stories, OUTPUT_PATH)

    merged = sum(len(m) for m in clusters if len(m) > 1)
    multi = [c for c in clusters if len(c) > 1]
    biggest = max((len(c) for c in clusters), default=1)

    by_outlets = Counter(s["outlet_count"] for s in stories)

    print(
        f"{len(rows)} articles -> {len(stories)} stories "
        f"({len(multi)} multi-outlet, {merged} articles absorbed, largest {biggest})"
    )
    print(
        "Outlet spread: "
        + ", ".join(
            f"{n} outlet{'s' if n > 1 else ''} x{by_outlets[n]}"
            for n in sorted(by_outlets, reverse=True)
            if n > 0
        )
    )

    if show:
        print()
        for story in stories[:show]:
            print(f"  [{story['outlet_count']}] {clean_title(story['title'] or '')[:64]}")
            for outlet in story["outlets"][:6]:
                print(f"        {outlet['source']}")

    print(f"Written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
