#!/usr/bin/env python3
"""Group same-day RSS reports into stories."""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import load_json, parse_timestamp, save_json
from textutil import clean_title, significant_tokens


INPUT_PATH = Path("data/world_raw.json")
OUTPUT_PATH = Path("data/world_clustered.json")

# The threshold is tuned for shared headline tokens.
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
    """Choose the member that best describes the story."""
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

    # Use the newest article from each outlet.
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
        "outlets": outlets,
        "outlet_count": len(outlets),
        "article_count": len(members),
    }




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
