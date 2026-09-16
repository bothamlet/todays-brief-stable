#!/usr/bin/env python3
"""
common.py — shared plumbing for the news pipeline.

Error exits, JSON input/output and timestamp parsing. Every step previously
carried its own copy: `fail` was defined six times, `save` five, and
`parse_timestamp` twice, so a fix in one never reached the others.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, NoReturn


def fail(message: str) -> NoReturn:
    """Print a clear error and exit non-zero, without writing an output file."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def load_json(path: Path) -> list[dict[str, Any]]:
    """Load a list-shaped pipeline file, or exit explaining what is missing."""
    if not path.exists():
        fail(f"{path} not found. Run the earlier pipeline steps first.")
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        fail(f"could not read {path}: {exc}")
    if not isinstance(rows, list) or not rows:
        fail(f"{path} contains no items.")
    return rows


def save_json(payload: Any, path: Path) -> None:
    """Write JSON, creating the parent folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def first_existing(paths: Sequence[Path]) -> Path:
    """The first path that exists, or exit listing what was looked for."""
    for path in paths:
        if path.exists():
            return path
    wanted = " or ".join(str(p) for p in paths)
    fail(f"no input found. Expected {wanted}. Run the earlier pipeline steps first.")


def utc_now() -> datetime:
    """Timezone-aware now, so nothing downstream has to guess."""
    return datetime.now(timezone.utc)


# Weekday and month names as Lithuanian feeds spell them in RFC 822. Kauno diena
# sends "Ant, 15 Rug 2026 23:44:00 EEST", which email.utils reads as English and
# rejects, so every one of its dates silently became None — the items were then
# kept as "undated", sorted arbitrarily and could displace a newer story.
#
# "Rug" is mapped to September, not August, on the evidence: the items carrying it
# were a day old in mid-September. Lithuanian abbreviates Rugpjutis (August) and
# Rugsejis (September) alike, so the full "Rugp" is mapped separately to August.
_LT_DAYS = {
    "pir": "Mon",
    "ant": "Tue",
    "tre": "Wed",
    "ket": "Thu",
    "pen": "Fri",
    "ses": "Sat",
    "sek": "Sun",
}
_LT_MONTHS = {
    "sau": "Jan",
    "vas": "Feb",
    "kov": "Mar",
    "bal": "Apr",
    "geg": "May",
    "bir": "Jun",
    "lie": "Jul",
    "rugp": "Aug",
    "rug": "Sep",
    "spa": "Oct",
    "lap": "Nov",
    "gru": "Dec",
}
_LT_DIACRITICS = str.maketrans({"ė": "e", "š": "s", "ž": "z", "ū": "u", "ų": "u"})

_LT_TIMESTAMP = re.compile(r"^([A-Za-z]{3}),\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")


def _englishify_lithuanian(text: str) -> str:
    """Rewrite a Lithuanian RFC 822 date into one email.utils will accept."""
    match = _LT_TIMESTAMP.match(text)
    if not match:
        return text
    day, date, month, year = match.groups()
    key = month.lower().translate(_LT_DIACRITICS)
    if key[:4] in ("rugp",):
        english = _LT_MONTHS["rugp"]
    else:
        english = _LT_MONTHS.get(key[:3])
    if not english:
        return text
    return text.replace(
        f"{day}, {date} {month} {year}",
        f"{_LT_DAYS.get(day.lower(), 'Mon')}, {date} {english} {year}",
        1,
    )


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an RSS (RFC 822) or Atom (ISO 8601) timestamp.

    Feeds mix the two freely, and some carry neither, so this returns None rather
    than raising. Lithuanian-language RFC 822 is handled too, because Kauno diena
    and Diena publish it and would otherwise come through permanently undated.
    """
    if not value:
        return None

    text = value.strip()

    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        pass  # not ISO 8601 — fall through and try RFC 822 below

    try:
        stamp = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        stamp = None

    if stamp is None:
        try:
            stamp = parsedate_to_datetime(_englishify_lithuanian(text))
        except (TypeError, ValueError):
            return None

    if stamp is None:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def newest_first(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by publishedAt descending, undated last."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        stories,
        key=lambda s: parse_timestamp(s.get("publishedAt")) or floor,
        reverse=True,
    )


def dedupe_by_url(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact repeats by URL, falling back to the title, preserving order.

    Story-level merging — the same event reported by six outlets — is
    deliberately not attempted here; that is cluster_stories' job.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("url") or item.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
