#!/usr/bin/env python3
"""
track_stories.py — Step 4 of the daily news pipeline: memory.

Keeps a SQLite record of every story the pipeline has seen, so the briefing can
say whether something is new or has been running for days.

Every earlier step is stateless. The clusterer merges one day's articles; the
classifier judges one day's stories. Neither can tell a fresh event from the
fourth day of an ongoing one, because neither has ever seen a previous run. That
is the whole reason a "DEVELOPING — 3RD UPDATE" line could not be built before.

Reads  data/world_filtered.json
Writes data/world_tracked.json   (input stories, each with a `history` block)
       data/briefing.db          (created on first run)

Ordinal counting is by DISTINCT DAY, not by run: re-running the pipeline three
times in one afternoon says "1st day", not "3rd update". Sightings are keyed on
(story_id, seen_day) so repeats are simply ignored.

Usage:
    python track_stories.py            # record and annotate
    python track_stories.py --history  # also print what the store remembers
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import (
    fail,
    load_json,
    save_json,
)
from rss_feeds import backfill_images
from textutil import (
    ordinal,
    shared_words,
    significant_tokens,
    word_frequencies,
)


# --- Configuration -----------------------------------------------------------

INPUT_PATH = Path("data/world_filtered.json")
OUTPUT_PATH = Path("data/world_tracked.json")
DB_PATH = Path("data/briefing.db")

# Sort key for picking which stories are worth a page fetch for their image.
SIGNIFICANCE_RANK = {"high": 0, "medium": 1, "low": 2}

# How many stories to spend that fetch on. The wired pages show five per category
# across four categories, so this covers them with room to spare.
IMAGE_BACKFILL_LIMIT = 30

LOOKBACK_DAYS = 21  # how far back to hunt for the same story
# The overlap test itself lives in match_existing: three shared words of any kind,
# or two including one that is rare across the store. A flat threshold is not
# enough, because "dead" and "attack" appear in half the store while "ferry" and
# "indonesia" identify one story.

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


# --- Helpers -----------------------------------------------------------------


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
    """Coerce a sqlite value to int without raising.

    lastrowid is None on a no-op insert and sqlite column values are untyped, so
    a bare int() here is a conversion that can fail on data we do not control.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- Store -------------------------------------------------------------------


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


def match_existing(
    conn: sqlite3.Connection,
    stored: list[sqlite3.Row],
    title: str,
    tokens: set[str],
    urls: set[str],
    frequencies: dict[str, int],
) -> sqlite3.Row | None:
    """The stored story this one continues, if any.

    A shared article URL is decisive — the same link resurfacing means the same
    running story. Otherwise title overlap decides, weighted by rarity: "ferry"
    and "indonesia" are worth far more than "dead" or "attack", which appear in
    half the store and would otherwise merge unrelated conflicts.

    Deliberately conservative. Missing that a story is developing is a cosmetic
    loss; claiming a fresh story is on its third update is a false statement.
    """
    for row in stored:
        try:
            known = set(json.loads(row["urls"] or "[]"))
        except json.JSONDecodeError:
            known = set()
        if urls & known:
            return row

    if not stored:
        return None

    # A word in more than a quarter of stored stories carries little identity.
    rare_ceiling = max(1, len(stored) // 4)

    best: sqlite3.Row | None = None
    best_score = 0
    for row in stored:
        common = shared_words(tokens, significant_tokens(str(row["best_title"])))
        if not common:
            continue
        rare = {w for w in common if frequencies.get(w, 0) <= rare_ceiling}
        # Three shared words of any kind, or two including a distinctive one.
        if not (len(common) >= 3 or (len(common) >= 2 and rare)):
            continue
        score = len(common) + len(rare)
        if score > best_score:
            best, best_score = row, score
    return best


def open_run(conn: sqlite3.Connection, now: datetime, day: str) -> int:
    """Record this run and return its id."""
    cursor = conn.execute(
        "INSERT INTO runs (started_at, day) VALUES (?, ?)", (now.isoformat(), day)
    )
    return as_int(cursor.lastrowid)


# --- Tracking ----------------------------------------------------------------


def track(
    conn: sqlite3.Connection, stories: list[dict[str, Any]], now: datetime, day: str
) -> dict[str, Any]:
    """Match every story against the store, record the sighting, annotate it.

    Matching compares against PREVIOUS runs only. Stories created earlier in this
    same run are already distinct — separating them is the clusterer's job — and
    matching them here merged 62 of 180 stories into each other on the very first
    run, against an empty database.
    """
    cutoff = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    stored = recent_stories(conn, cutoff)
    frequencies = word_frequencies(str(r["best_title"]) for r in stored)
    run_id = open_run(conn, now, day)

    created_this_run: set[int] = set()
    fresh = 0
    continuing = 0

    for story in stories:
        title = str(story.get("title") or "").strip()
        tokens = significant_tokens(title)
        urls = story_urls(story)
        classification = story.get("classification") or {}

        history_rows = [r for r in stored if as_int(r["id"]) not in created_this_run]
        row = match_existing(conn, history_rows, title, tokens, urls, frequencies)

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
            created_this_run.add(story_id)
            stored.append(
                conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
            )
            fresh += 1
        else:
            story_id = as_int(row["id"])
            # Keep the newest headline and a union of URLs, so tomorrow's match
            # can still find this story once today's links age out.
            try:
                known = set(json.loads(row["urls"] or "[]"))
            except json.JSONDecodeError:
                known = set()
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
            continuing += 1

        # Keyed on the day, so a second run today is ignored rather than counted.
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
        first_day = first_seen[:10]
        story["history"] = {
            "days_running": days,
            "first_seen": first_seen,
            "is_developing": days > 1,
            "updates": days,
            "update_ordinal": ordinal(days),
        }

    conn.commit()
    return {"fresh": fresh, "continuing": continuing, "run_id": run_id}


# --- Output ------------------------------------------------------------------


def save(stories: list[dict[str, Any]]) -> None:
    """Write the annotated story list, creating data/ if needed."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(stories, indent=2, ensure_ascii=False), encoding="utf-8"
    )


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


# --- Entry point -------------------------------------------------------------


def main() -> None:
    stories = load_json(INPUT_PATH)

    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    existed = DB_PATH.exists()

    conn = connect()
    try:
        result = track(conn, stories, now, day)

        # The category pages show the most significant stories in each category
        # first, so those are the ones worth spending a page fetch on. The
        # pipeline collects far more articles than any page will ever display, and
        # this is one request per story. Best-effort: see backfill_images.
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
