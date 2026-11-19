#!/usr/bin/env python3
"""Record daily story sightings in SQLite and annotate the current edition."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import (
    fail,
    load_json,
    save_json,
)
from rss_feeds import backfill_images
from textutil import ordinal, significant_tokens

INPUT_PATH = Path("data/world_filtered.json")
OUTPUT_PATH = Path("data/world_tracked.json")
DB_PATH = Path("data/briefing.db")
SIGNIFICANCE_RANK = {"high": 0, "medium": 1, "low": 2}
IMAGE_BACKFILL_LIMIT = 30
LOOKBACK_DAYS = 21

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    day         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stories (
    id           INTEGER PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    best_title   TEXT NOT NULL,
    category     TEXT,
    region       TEXT,
    topic        TEXT,
    urls         TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS sightings (
    story_id    INTEGER NOT NULL,
    seen_day    TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    title       TEXT,
    significance TEXT,
    outlet_count INTEGER,
    PRIMARY KEY (story_id, seen_day)
);

CREATE INDEX IF NOT EXISTS idx_sightings_story ON sightings(story_id);
CREATE INDEX IF NOT EXISTS idx_stories_last_seen ON stories(last_seen);
"""


def story_urls(story: dict[str, Any]) -> set[str]:
    """Every URL associated with a story: its lead article and its coverage."""
    urls: set[str] = set()
    lead = str(story.get("url") or "").strip()
    if lead:
        urls.add(lead)
    for outlet in story.get("outlets") or []:
        url = str((outlet or {}).get("url") or "").strip()
        if url:
            urls.add(url)
    return urls


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def connect() -> sqlite3.Connection:
    """Open the store, creating the schema and parent folder if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def recent_stories(conn: sqlite3.Connection, cutoff: str) -> list[sqlite3.Row]:
    """Stories seen at or after `cutoff`, newest first."""
    return list(
        conn.execute(
            "SELECT * FROM stories WHERE last_seen >= ? ORDER BY last_seen DESC",
            (cutoff,),
        )
    )


def saved_urls(row: sqlite3.Row) -> set[str]:
    try:
        return set(json.loads(row["urls"] or "[]"))
    except (TypeError, ValueError):
        return set()


def unambiguous_matches(candidates: Iterable[tuple[float, int, int]]) -> dict[int, int]:
    """Keep only mutual best matches; a tie on either side is inconclusive."""
    by_current: dict[int, tuple[float, int | None]] = {}
    by_previous: dict[int, tuple[float, int | None]] = {}
    for score, current, previous in sorted(candidates, reverse=True):
        for choices, key, match in (
            (by_current, current, previous), (by_previous, previous, current)
        ):
            if key not in choices:
                choices[key] = (score, match)
            elif choices[key][0] == score:
                choices[key] = (score, None)
    return {
        current: previous for current, (_, previous) in by_current.items()
        if previous is not None and by_previous[previous][1] == current
    }


def match_edition(
    titles: list[str], urls: list[set[str]], stored: list[sqlite3.Row]
) -> dict[int, int]:
    current_tokens = [significant_tokens(title) for title in titles]
    previous_tokens = [significant_tokens(row["best_title"]) for row in stored]
    previous_urls = [saved_urls(row) for row in stored]
    exact: list[tuple[int, int, int]] = []
    for current, (tokens, links) in enumerate(zip(current_tokens, urls)):
        if not links:
            continue
        for previous, known in enumerate(previous_urls):
            if shared := links & known:
                # An unchanged headline only breaks ties between equal link counts.
                same_title = len(tokens) >= 3 and tokens == previous_tokens[previous]
                exact.append((2 * len(shared) + same_title, current, previous))
    matches = unambiguous_matches(exact)

    # Even an ambiguous URL match outweighs a similar headline.
    linked_current = {current for _, current, _ in exact}
    linked_previous = {previous for _, _, previous in exact}
    frequencies = Counter(word for tokens in previous_tokens for word in tokens)
    rare_ceiling = max(1, len(stored) // 20)

    # shortcut: lexical evidence misses rewritten headlines; revisit if misses dominate.
    fuzzy: list[tuple[float, int, int]] = []
    for current, tokens in enumerate(current_tokens):
        if current in linked_current:
            continue
        for previous, known in enumerate(previous_tokens):
            if previous in linked_previous:
                continue
            shared = tokens & known
            if len(shared) < 3:
                continue
            similarity = len(shared) / len(tokens | known)
            if similarity >= 0.6 and any(
                len(word) >= 5 and frequencies[word] <= rare_ceiling for word in shared
            ):
                fuzzy.append((similarity, current, previous))
    matches.update(unambiguous_matches(fuzzy))
    return matches


def open_run(conn: sqlite3.Connection, now: datetime, day: str) -> int:
    """Record this run and return its id."""
    cursor = conn.execute(
        "INSERT INTO runs (started_at, day) VALUES (?, ?)", (now.isoformat(), day)
    )
    return as_int(cursor.lastrowid)


def track(
    conn: sqlite3.Connection, stories: list[dict[str, Any]], now: datetime, day: str
) -> dict[str, Any]:
    """Match against prior runs, then record each distinct daily sighting."""
    cutoff = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    stored = recent_stories(conn, cutoff)
    titles = [str(story.get("title") or "").strip() for story in stories]
    links = [story_urls(story) for story in stories]
    matches = match_edition(titles, links, stored)
    run_id = open_run(conn, now, day)

    for current, (story, title, urls) in enumerate(zip(stories, titles, links)):
        classification = story.get("classification") or {}
        previous = matches.get(current)
        row = stored[previous] if previous is not None else None

        if row is None:
            cursor = conn.execute(
                """
                INSERT INTO stories
                    (first_seen, last_seen, best_title, category, region, topic, urls)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    title,
                    classification.get("category"),
                    classification.get("region"),
                    classification.get("topic"),
                    json.dumps(sorted(urls)),
                ),
            )
            story_id = as_int(cursor.lastrowid)
        else:
            story_id = as_int(row["id"])
            known = saved_urls(row)
            conn.execute(
                """
                UPDATE stories
                   SET last_seen = ?, best_title = ?, urls = ?,
                       category = ?, region = ?, topic = ?
                 WHERE id = ?
                """,
                (
                    now.isoformat(),
                    title,
                    json.dumps(sorted(known | urls)[-40:]),
                    classification.get("category"),
                    classification.get("region"),
                    classification.get("topic"),
                    story_id,
                ),
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO sightings
                (story_id, seen_day, seen_at, title, significance, outlet_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                day,
                now.isoformat(),
                title,
                classification.get("significance"),
                story.get("outlet_count"),
            ),
        )

        days = as_int(
            conn.execute(
                "SELECT COUNT(DISTINCT seen_day) AS n FROM sightings WHERE story_id = ?",
                (story_id,),
            ).fetchone()["n"]
        )
        first_seen = str(
            conn.execute(
                "SELECT first_seen FROM stories WHERE id = ?", (story_id,)
            ).fetchone()["first_seen"]
        )
        story["history"] = {
            "days_running": days,
            "first_seen": first_seen,
            "is_developing": days > 1,
            "updates": days,
            "update_ordinal": ordinal(days),
        }

    conn.commit()
    return {"fresh": len(stories) - len(matches), "continuing": len(matches), "run_id": run_id}


def print_history(conn: sqlite3.Connection) -> None:
    """Show what the store remembers, most persistent first."""
    rows = list(
        conn.execute(
            """
            SELECT s.best_title, s.first_seen, s.last_seen,
                   COUNT(DISTINCT v.seen_day) AS days
              FROM stories s
              LEFT JOIN sightings v ON v.story_id = s.id
             GROUP BY s.id
             ORDER BY days DESC, s.last_seen DESC
             LIMIT 20
            """
        )
    )
    if not rows:
        print("\n  (the store is empty)")
        return

    print(f"\n  {DB_PATH} remembers {len(rows)} story(ies):")
    for row in rows:
        print(
            f"    {row['days']}d  {row['first_seen'][:10]} -> {row['last_seen'][:10]}  "
            f"{str(row['best_title'])[:48]}"
        )


def main() -> None:
    stories = load_json(INPUT_PATH)

    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    existed = DB_PATH.exists()

    conn = connect()
    try:
        result = track(conn, stories, now, day)

        ranked = sorted(
            stories,
            key=lambda s: SIGNIFICANCE_RANK.get(
                str((s.get("classification") or {}).get("significance") or ""), 3
            ),
        )
        filled = backfill_images(ranked[:IMAGE_BACKFILL_LIMIT])
        if filled:
            print(f"Recovered {filled} image(s) from the article pages")

        save_json(stories, OUTPUT_PATH)
        if "--history" in sys.argv:
            print_history(conn)

        developing = [s for s in stories if (s.get("history") or {}).get("is_developing")]
    finally:
        conn.close()

    print(
        f"{'Opened' if not existed else 'Using'} {DB_PATH} "
        f"({result['fresh']} new, {result['continuing']} continuing)"
    )
    print(f"Annotated {len(stories)} stories -> {OUTPUT_PATH}")
    if developing:
        print(f"\nStill developing ({len(developing)}):")
        for story in developing[:8]:
            history = story["history"]
            print(
                f"  {history['update_ordinal']:>4} update, day {history['days_running']:>2}  "
                f"{str(story.get('title') or '')[:52]}"
            )


if __name__ == "__main__":
    main()
