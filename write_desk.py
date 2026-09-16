#!/usr/bin/env python3
"""
write_desk.py — the one-line desk note that opens the briefing.

PARKED. The quote is out of the page's design for now, so this is no longer part
of the daily run: run.sh does not call it and #deskBlock is commented out in
index.html. Nothing was deleted. To bring it back:

    1. add this file back to the four step arrays in run.sh
    2. restore data/desk.json to run.sh's PUBLISHED array
    3. uncomment #deskBlock in index.html

Until then it still runs correctly on its own:

    python3 write_desk.py

It writes the desk note reacting to what actually ran that day.

Reads  data/world_covered.json — specifically the stories the page displays —
       plus data/world_filtered.json for aggregate counts
Writes data/desk.json

Attribution is "Chap." The voice is dry, understated and faintly weary: someone
who has read everything and is not impressed, but is never cruel. The prompt
below spends most of its length on what NOT to write, because the default failure
mode of any model here is press-release filler.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling modules. Python puts this script's own directory on sys.path; pyright
# analyses each file standalone and cannot see them, hence the directives.
from common import load_json, save_json  # pyright: ignore[reportMissingImports]
from llm import MODEL as DEFAULT_MODEL  # pyright: ignore[reportMissingImports]
from llm import ask_json  # pyright: ignore[reportMissingImports]

# --- Configuration -----------------------------------------------------------

# Per-step override, falling back to the pipeline-wide DEEPSEEK_MODEL.
MODEL = os.environ.get("DESK_MODEL") or DEFAULT_MODEL

# What the reader actually sees. DISPLAYED_STORIES must mirror MAX_STORIES in
# render.js: when the two drift, the desk line starts commenting on stories that
# never reached the page.
INPUT_PATH = Path("data/world_covered.json")
FILTERED_PATH = Path("data/world_filtered.json")
CLUSTERED_PATH = Path("data/world_clustered.json")
OUTPUT_PATH = Path("data/desk.json")

ATTRIBUTION = "Chap."
DISPLAYED_STORIES = 5  # == MAX_STORIES in render.js

MAX_TOKENS = 220
# Not 0. This is writing, not extraction — a little variation is the point, and
# the material changes daily anyway, so runs never repeat themselves.
TEMPERATURE = 0.9

SYSTEM_PROMPT = """\
You write the opening line for a daily news briefing. The byline reads "Chap."

Voice: dry, understated, faintly weary. You have read everything and are not \
impressed, but never cruel. You notice the shape of a day — the repetition, the \
irony, the thing everyone is politely not saying. Two short beats, usually with a \
turn on the second. Understatement, not punchlines. A shrug, not a sermon.

Length: one or two sentences. Under 24 words total.

What makes a good line:
- It reacts to something SPECIFIC in the headlines. A concrete detail beats a \
grand statement.
- It sounds like a person who has been doing this job too long, not a summary.
- Baffled, tired, quietly amused: all fine. Preachy, showily clever, sentimental: \
not fine.

Never use these, which are the fingerprints of machine-written copy:
"in a world where", "remains to be seen", "serves as a reminder", "underscores", \
"highlights", "tapestry", "landscape", "amid", "as tensions rise", "only time will \
tell", "one thing is clear", "the stakes could not be higher".

Also: no rhetorical questions, no exclamation marks, no emoji, no em dashes, no \
addressing the reader, no "we" or "you", no explaining the line, no moral, and \
no stating the day or date (an earlier run signed off with a dangling "Tuesday.").

The house register, for calibration only — do not reuse these:
  "The planet keeps happening. We just sort the noise."
  "Someone won. Someone lost. Several people are very loud about it."
  "The numbers moved. Someone, somewhere, is thrilled."
  "Three ferries in one week. The sea is not making a point."

Reply with a single json object and nothing else:
{"quote": "..."}"""


# --- Helpers -----------------------------------------------------------------


def select_displayed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The stories the World page shows, in the order it shows them.

    Reads the pipeline's own ordering — see select_briefing in fetch_coverage.py
    — rather than re-deriving it. That rule used to be implemented in both
    Python and JavaScript, and when the two drifted the desk line began opening
    with an Indonesian ferry disaster that never reached the page.
    """
    ranked = [row for row in rows if isinstance(row.get("briefing_rank"), int)]
    # Already filtered to int, so no conversion — and nothing here can raise.
    ranked.sort(key=lambda row: row["briefing_rank"])
    return ranked[:DISPLAYED_STORIES]


def build_context() -> str:
    """Aggregate background: counts only, never unnamed headlines.

    A line about a story the reader cannot see reads as a mistake rather than a
    joke, so nothing specific is offered here.
    """
    try:
        filtered = json.loads(FILTERED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""

    try:
        clustered = json.loads(CLUSTERED_PATH.read_text(encoding="utf-8"))
        reviewed = len(clustered)
        discarded = reviewed - len(filtered)
    except (json.JSONDecodeError, OSError):
        reviewed, discarded = len(filtered), 0

    categories: dict[str, int] = {}
    for row in filtered:
        name = str((row.get("classification") or {}).get("category") or "?")
        categories[name] = categories.get(name, 0) + 1
    spread = ", ".join(
        f"{name} {n}" for name, n in sorted(categories.items(), key=lambda kv: -kv[1])
    )

    parts = [
        f"Background, for tone only, do not name these: {reviewed} stories were "
        "reviewed today."
    ]
    if discarded > 0:
        parts.append(f"{discarded} were thrown out as noise.")
    if spread:
        parts.append(f"The rest broke down as {spread}.")
    return " ".join(parts)


def build_material(stories: list[dict[str, Any]]) -> str:
    """The visible briefing as plain text, for the model to react to."""
    lines = ["What this morning's briefing shows, in order:"]
    for i, story in enumerate(stories, 1):
        lines.append(f"{i}. {str(story.get('title') or '').strip()}")

    context = build_context()
    if context:
        lines.extend(["", context])

    lines.extend(["", "Write today's line, about what is actually listed above."])
    return "\n".join(lines)


def ask_desk_line(material: str) -> str | None:
    """One call for the desk line. None if the model could not be reached."""
    parsed = ask_json(
        SYSTEM_PROMPT,
        material,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        model=MODEL,
    )
    if parsed is None:
        return None
    return str(parsed.get("quote", "")).strip() or None


def check_voice(quote: str) -> list[str]:
    """Flag the machine-copy fingerprints the prompt forbids.

    Cheap insurance: the model occasionally ignores the ban list, and a line that
    reads like a press release is worse than no line at all.
    """
    banned = [
        "in a world where",
        "remains to be seen",
        "serves as a reminder",
        "serves as a stark",
        "underscores",
        "highlights the",
        "tapestry",
        "landscape",
        "as tensions rise",
        "only time will tell",
        "one thing is clear",
        "stakes could not be higher",
        "amid",
    ]
    lowered = quote.lower()
    return [phrase for phrase in banned if phrase in lowered]


# --- Entry point -------------------------------------------------------------


def main() -> None:
    rows = load_json(INPUT_PATH)

    stories = select_displayed(rows)
    if not stories:
        # sys.exit directly rather than the shared fail(): pyright cannot see
        # through the suppressed sibling import, so fail()'s NoReturn is
        # invisible and flow narrowing after it breaks.
        print(
            f"ERROR: no ranked stories in {INPUT_PATH} — run fetch_coverage.py, "
            "which assigns briefing_rank.",
            file=sys.stderr,
        )
        sys.exit(1)

    material = build_material(stories)
    print(f"Reacting to the {len(stories)} stories the page displays:")
    for story in stories:
        print(f"  - {str(story.get('title') or '')[:66]}")

    quote = ask_desk_line(material)
    if not quote:
        print("ERROR: no quote produced. Nothing written.", file=sys.stderr)
        sys.exit(1)

    flags = check_voice(quote)
    if flags:
        print(
            f"WARNING: quote contains banned filler: {', '.join(flags)}",
            file=sys.stderr,
        )

    save_json(
        {
            "quote": quote,
            "attribution": ATTRIBUTION,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_PATH,
    )

    print(f"\n  {quote}")
    print(f"  — {ATTRIBUTION}")
    print(f"\nWritten to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
