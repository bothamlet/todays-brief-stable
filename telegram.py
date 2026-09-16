#!/usr/bin/env python3
"""
telegram.py — send today's paper to a phone.

Two messages, because they are two different things:

    a photo   the front page, with the briefing in the caption. Readable from
              the notification itself, which is the only part of a morning paper
              most mornings need.
    a document  the paper, one self-contained file. Opens offline, pages swipe,
              the lean arrows work, the statistics render.

The Bot API is a plain HTTPS interface — there is no SDK to install and no
session to keep. Two credentials come from the environment:

    TELEGRAM_BOT_TOKEN   from @BotFather. The only real secret here.
    TELEGRAM_CHAT_ID     your own numeric id. Not a secret; it is in every URL
                         you would paste into a support thread.

Neither is ever printed. Not on success, not on failure, not in a traceback —
requests puts the full URL in its exception messages, which for this API contains
the token, so the error path below reports a status and a description instead.

A missing token is not an error. The pipeline runs fine without a phone, and a
paper that was built should not be thrown away because it could not be delivered.

Usage:
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 telegram.py
    python3 telegram.py --text-only        # skip the files, just the briefing
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Sibling module. Python puts its directory on sys.path; pyright analyses each
# file standalone and cannot see it, hence the directive.
from common import load_json  # pyright: ignore[reportMissingImports]

import requests

API = "https://api.telegram.org"

PAPER = Path("_site/paper.html")
FRONT_PAGE = Path("_site/frontpage.jpg")

# How many headlines the caption carries. Telegram shows a photo caption in full
# rather than collapsing it, so this is bounded by the caption limit rather than
# by how much fits on a lock screen.
MAX_HEADLINES = 3

# Telegram's caption limit. Written down because exceeding it is a 400 and the
# whole message is lost, not truncated.
CAPTION_LIMIT = 1024

TIMEOUT = 60.0


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def headline_lines(limit: int = MAX_HEADLINES) -> list[str]:
    """The briefing's top stories, in the pipeline's own order."""
    rows = load_json(Path("data/world_covered.json"))
    if not isinstance(rows, list):
        return []
    ranked = [r for r in rows if isinstance(r, dict) and r.get("briefing_rank")]
    ranked.sort(key=lambda r: r["briefing_rank"])
    return [str(r["title"]).strip() for r in ranked if r.get("title")][:limit]


def caption(stale: bool = False) -> str:
    """The photo's caption: how many, then the first few by name.

    A stale delivery says so first. The paper itself carries a stale badge, but
    only once it has been opened — and the entire purpose of this message is to be
    readable without opening anything. Learning that the run failed later, from a
    date under the masthead, is learning it in the wrong order.
    """
    stories = load_json(Path("data/world_covered.json"))
    total = (
        len([r for r in stories if isinstance(r, dict) and r.get("briefing_rank")])
        if isinstance(stories, list)
        else 0
    )

    lines: list[str] = []
    if stale:
        lines += ["YESTERDAY'S ISSUE — this morning's run did not finish.", ""]
    if total:
        lines.append(f"{total} in the briefing")
    lines += [f"• {title}" for title in headline_lines()]
    text = "\n".join(lines).strip() or "Today's paper is ready."

    # Truncated rather than rejected: a headline long enough to blow the caption
    # limit is a headline whose beginning is still the part that matters.
    return text if len(text) <= CAPTION_LIMIT else text[: CAPTION_LIMIT - 1] + "…"


def post(token: str, method: str, *, data: dict[str, Any], files: dict[str, Any] | None = None) -> bool:
    """One Bot API call. Returns whether it worked, and says so either way.

    Errors are reported by status and description only. requests puts the request
    URL into its own exception messages, and for this API the URL contains the bot
    token — so an unhandled traceback here would print the credential into
    whatever log this runs under.
    """
    try:
        response = requests.post(
            f"{API}/bot{token}/{method}",
            data=data,
            files=files,
            timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        print(f"  telegram: {method} could not reach the API ({type(error).__name__})")
        return False

    if response.status_code == 200:
        return True

    # Telegram explains itself in `description`; nothing else about the response
    # is worth reproducing.
    try:
        detail = json.loads(response.text).get("description", "")
    except ValueError:
        detail = ""
    print(f"  telegram: {method} failed — HTTP {response.status_code} {detail}".rstrip())
    return False


def send_briefing(token: str, chat: str, stale: bool) -> bool:
    """The briefing, as a photo caption if there is a photo and a message if not.

    Not skipped when the picture is missing. The briefing is the part that can be
    read without opening anything, so a failed screenshot should cost the picture
    and not the headlines — which is what would happen if the caption only ever
    rode along attached to an image.
    """
    text = caption(stale)

    if FRONT_PAGE.is_file():
        with FRONT_PAGE.open("rb") as handle:
            return post(
                token,
                "sendPhoto",
                data={"chat_id": chat, "caption": text},
                files={"photo": ("frontpage.jpg", handle, "image/jpeg")},
            )

    print("  telegram: no front page image — sending the briefing as text")
    return post(token, "sendMessage", data={"chat_id": chat, "text": text})


def send_paper(token: str, chat: str) -> bool:
    if not PAPER.is_file():
        print(f"  telegram: {PAPER} is missing — run bundle.py first")
        return False
    with PAPER.open("rb") as handle:
        return post(
            token,
            "sendDocument",
            data={
                "chat_id": chat,
                # The name Telegram shows and saves. Dated, because a chat fills
                # with a paper a day and "paper.html" a hundred times over is a
                # folder nobody can find anything in.
                "document": f"todays-brief-{_today()}.html",
            },
            files={
                "document": (
                    f"todays-brief-{_today()}.html",
                    handle,
                    "text/html",
                )
            },
        )


def _today() -> str:
    import datetime

    return datetime.date.today().isoformat()


def main() -> None:
    # Flags are read here rather than at import, so importing this module in a
    # test does not depend on what the test runner was called with.
    stale = "--stale" in sys.argv
    text_only = "--text-only" in sys.argv

    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")

    if not token or not chat:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", token),
                ("TELEGRAM_CHAT_ID", chat),
            )
            if not value
        ]
        print(f"{' and '.join(missing)} not set — skipping delivery.")
        return

    # Ordered, because the front page is the notification and the paper is the
    # payload: a chat should say what happened before it hands over the file.
    sent = send_briefing(token, chat, stale)

    if text_only:
        print("  telegram: text only, as asked.")
        return

    sent = send_paper(token, chat) and sent
    if sent:
        print(f"  telegram: delivered ({caption(stale).splitlines()[0]})")


if __name__ == "__main__":
    main()
