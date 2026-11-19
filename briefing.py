#!/usr/bin/env python3
"""Rank stories for a varied briefing."""

from __future__ import annotations

from typing import Any

from llm import ask_json
from textutil import (
    normalise,
    topic_key,
)

LEANS = ("left", "center", "right")

BRIEFING_SIZE = 10  # stories that receive a briefing_rank
BRIEFING_CATEGORY = "World"  # what the World page draws from
MAX_PER_TOPIC = 1  # slots one ongoing event may occupy
MAX_PER_REGION = 2


def lean_coverage_count(story: dict[str, Any]) -> int:
    """How many of the three leans this story has any coverage from."""
    coverage = story.get("coverage") or {}
    return sum(1 for lean in LEANS if coverage.get(lean))


def significance_rank(story: dict[str, Any]) -> int:
    """Sort key: high first, anything unrecognised last."""
    level = str((story.get("classification") or {}).get("significance") or "")
    return {"high": 0, "medium": 1, "low": 2}.get(level, 3)


def briefing_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stories eligible for the briefing, including single-outlet quiet-day items."""

    def worth_showing(story: dict[str, Any]) -> bool:
        if not story.get("title") or not story.get("url"):
            return False
        coverage = story.get("coverage") or {}
        if not any(coverage.get(lean) for lean in LEANS):
            return False
        category = str((story.get("classification") or {}).get("category") or "")
        return category == BRIEFING_CATEGORY

    return [row for row in rows if worth_showing(row)]


def select_briefing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order the stories worth showing, keeping topics and regions varied."""
    candidates = briefing_candidates(rows)
    if not candidates:
        candidates = [row for row in rows if row.get("title") and row.get("url")
                      and (row.get("classification") or {}).get("category") == BRIEFING_CATEGORY]

    # Stable sorts apply the highest-priority key last.
    candidates.sort(key=lambda r: str(r.get("publishedAt") or ""), reverse=True)
    candidates.sort(key=lambda r: r.get("outlet_count") or 0, reverse=True)
    candidates.sort(key=lean_coverage_count, reverse=True)
    candidates.sort(key=significance_rank)

    picked: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    topic_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}

    for story in candidates:
        topic = topic_key(
            str(story.get("briefing_topic") or (story.get("classification") or {}).get("topic") or "")
        )

        if topic_counts.get(topic, 0) >= MAX_PER_TOPIC:
            deferred.append(story)
            continue
        region = str((story.get("classification") or {}).get("region") or "")
        if region_counts.get(region, 0) >= MAX_PER_REGION:
            deferred.append(story)
            continue

        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        region_counts[region] = region_counts.get(region, 0) + 1
        picked.append(story)
        if len(picked) >= BRIEFING_SIZE:
            break

    # Deferred stories still have to satisfy the caps.
    if len(picked) < BRIEFING_SIZE:
        for story in deferred:
            if len(picked) >= BRIEFING_SIZE:
                break
            classification = story.get("classification") or {}
            topic = topic_key(
                str(story.get("briefing_topic") or classification.get("topic") or "")
            )
            region = str(classification.get("region") or "")
            if topic_counts.get(topic, 0) >= MAX_PER_TOPIC:
                continue
            if region_counts.get(region, 0) >= MAX_PER_REGION:
                continue
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
            region_counts[region] = region_counts.get(region, 0) + 1
            picked.append(story)

    return picked


def report_briefing(briefing: list[dict[str, Any]]) -> None:
    """Print the selected order and its topic spread."""
    print()
    print("=" * 74)
    print("Briefing selection")
    print("=" * 74)
    for rank, story in enumerate(briefing):
        classification = story.get("classification") or {}
        leans = [
            f"{lean[0].upper()}{len((story.get('coverage') or {}).get(lean) or [])}"
            for lean in LEANS
        ]
        print(
            f"  {rank + 1}. [{str(classification.get('region') or '?')[:12]:12}] "
            f"[{lean_coverage_count(story)}/3 {' '.join(leans):13}] "
            f"{str(story.get('title') or '')[:32]}"
        )

    topics: dict[str, int] = {}
    for story in briefing:
        name = str(story.get("briefing_topic") or "?")
        topics[name] = topics.get(name, 0) + 1
    over = [t for t, n in topics.items() if n > MAX_PER_TOPIC]
    regions: dict[str, int] = {}
    for story in briefing:
        name = str((story.get("classification") or {}).get("region") or "?")
        regions[name] = regions.get(name, 0) + 1
    region_note = ", ".join(f"{r} {n}" for r, n in sorted(regions.items(), key=lambda kv: -kv[1]))
    over_region = [r for r, n in regions.items() if n > MAX_PER_REGION]
    print(
        f"\n  {len(topics)} distinct topics; none over the cap of "
        f"{MAX_PER_TOPIC}: {not over}"
    )
    print(
        f"  regions: {region_note}; none over {MAX_PER_REGION}: {not over_region}"
    )


def dedupe_briefing_coverage(briefing: list[dict[str, Any]]) -> None:
    """Stop one article opening two different cards."""
    used: set[str] = set()

    for story in briefing:
        coverage = story.get("coverage") or {}
        keys = {
            lean: [str(i.get("url") or i.get("title") or "").strip().lower() for i in coverage.get(lean, [])]
            for lean in LEANS
        }
        total = sum(len(v) for v in keys.values())
        repeats = sum(1 for v in keys.values() for k in v if k and k in used)

        dropped = 0
        for lean in LEANS:
            kept = []
            for item, key in zip(coverage.get(lean, []), keys[lean]):
                is_repeat = bool(key) and key in used
                # Keep one repeat if removing them all would empty the story.
                if is_repeat and total - repeats + dropped + 1 < 1:
                    is_repeat = False
                if is_repeat:
                    dropped += 1
                    continue
                kept.append(item)
            coverage[lean] = kept

        for lean in LEANS:
            for item in coverage.get(lean, []):
                key = str(item.get("url") or item.get("title") or "").strip().lower()
                if key:
                    used.add(key)


TOPIC_GROUP_PROMPT = """\
You group one day's news stories into topics, for a briefing that must not show
the same kind of event over and over.

Each story was labelled individually earlier, which produced inconsistent
results: three separate ferry disasters came back as "indonesian ship sinking",
"vanuatu ferry sinking" and "philippines ferry fire", so a cap meant to stop
repetition saw three different topics.

You see them all at once. Group by the KIND of event, not by country or by
specific incident:
- Three unrelated ferry disasters are one topic.
- Several stories about one war are one topic.
- An election in Sweden and an election in Chile are one topic.

Topic labels: 1-3 lowercase words, reused verbatim across every story in a group.
Do not invent a distinct label per story just to be specific.

For every story, echo the first few words of its headline exactly as given, so the
label can be matched back to the story without relying on position.

Reply with a single json object and nothing else:
{"topics": [{"title": "Swedes vote in close general", "topic": "elections"}, {"title": "Six dead and search under way", "topic": "ferry disasters"}]}"""


def assign_briefing_topics(stories: list[dict[str, Any]]) -> None:
    """Give every candidate a consistent topic, using one call that sees them all."""
    for story in stories:
        story["briefing_topic"] = topic_key(
            str((story.get("classification") or {}).get("topic") or "")
        )

    if len(stories) < 2:
        return

    listing = [
        f"{number}. {str(story.get('title') or '')[:160]}"
        for number, story in enumerate(stories, 1)
    ]
    parsed = ask_json(
        TOPIC_GROUP_PROMPT,
        "Stories:\n" + "\n".join(listing),
        max_tokens=1200,
    )
    returned = parsed.get("topics") if parsed else None
    if not isinstance(returned, list):
        return

    # The model may reorder responses, so match on the echoed headline.
    by_headline: list[tuple[str, str]] = []
    for entry in returned:
        if not isinstance(entry, dict):
            continue
        headline = normalise(str(entry.get("title") or ""))
        label = topic_key(str(entry.get("topic") or ""))
        if headline and label:
            by_headline.append((headline, label))

    for story in stories:
        title = normalise(str(story.get("title") or ""))
        if not title:
            continue
        for headline, label in by_headline:
            # The echo may be a truncation of the title or vice versa.
            if title.startswith(headline) or headline.startswith(title[: len(headline)]):
                story["briefing_topic"] = label
                break
