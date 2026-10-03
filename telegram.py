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
    python3 telegram.py --find-chat        # discover the chat id
    python3 telegram.py --text-only        # skip the files, just the briefing
"""

from __future__ import annotations

import json
import os
import sys
import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from common import load_json, parse_timestamp, save_json

import requests

API = "https://api.telegram.org"

PAPER = Path("_site/paper.html")
FRONT_PAGE = Path("_site/frontpage.jpg")

# Where the credentials live when they are not in the environment: outside the
# repository, mode 600, and deliberately not in the systemd unit — unit files are
# world-readable on many systems, and a bot token in one is a bot token you end up
# revoking. A token committed to git is the same mistake with a longer memory.
#
# One file, read in one place. deliver.sh does not source it and the timer does
# not set it, so there is no second copy to fall out of date.
ENV_FILE = Path.home() / ".config" / "todays-brief" / "env"

# How many headlines the caption carries. Telegram shows a photo caption in full
# rather than collapsing it, so this is bounded by the caption limit rather than
# by how much fits on a lock screen.
MAX_HEADLINES = 3

# Telegram's caption limit. Written down because exceeding it is a 400 and the
# whole message is lost, not truncated.
CAPTION_LIMIT = 1024

TIMEOUT = 60.0


def env(name: str) -> str:
    """A setting, from the environment or from the credentials file.

    The file wins nothing — an exported variable takes precedence, so a one-off
    run with `TELEGRAM_CHAT_ID=... python3 telegram.py` does what it looks like it
    does. Read here rather than by whichever shell happens to invoke this, because
    deliver.sh, the systemd timer and a human at a prompt are three callers and
    exactly one of them remembers to source anything.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value

    return next(
        (
            line.split("=", 1)[1].strip().strip("'\"")
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(f"{name}=")
        ),
        "",
    ) if ENV_FILE.is_file() else ""


def find_chat(token: str) -> int:
    """Print every chat that has messaged this bot, and return how many.

    This exists because the obvious ways to find a chat id do not work. The
    number in a Telegram Web URL is version-dependent and has broken more than
    once, and the clients do not agree on whether a private chat's id appears
    there at all. The API, by contrast, simply says what it saw.

    It only says what it saw, though — Telegram retains updates for about a day
    and a bot that has never been messaged has nothing to report. Hence the empty
    case being spelled out rather than reported as a failure.
    """
    try:
        response = requests.get(f"{API}/bot{token}/getUpdates", timeout=TIMEOUT)
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Could not reach the API ({type(error).__name__}).")
        return 0

    if not payload.get("ok"):
        # The description says what is wrong with the token without the token.
        print(f"Telegram refused the request: {payload.get('description', 'no reason given')}")
        return 0

    seen: dict[int, str] = {}
    for update in payload.get("result") or []:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if not chat.get("id"):
            continue
        raw_id = chat.get("id")
        if not raw_id:
            continue
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            # Telegram sends a number. Anything else is not a chat this can print.
            continue
        name = (
            chat.get("title")
            or " ".join(
                part
                for part in (chat.get("first_name"), chat.get("last_name"))
                if part
            )
            or chat.get("username")
            or "(no name)"
        )
        username = f" (@{chat['username']})" if chat.get("username") else ""
        seen[chat_id] = f"{name}{username} · {chat.get('type', '?')}"

    if not seen:
        print("No chats are visible to this bot yet.")
        print()
        print("  Open a chat with your bot and send it anything — /start is fine.")
        print("  Then run this again. Telegram keeps updates for about a day, so")
        print("  the message has to be recent; it does not have to be new.")
        print()
        print("  This works from Telegram Web. The id comes from the API rather")
        print("  than from the browser, so the client does not matter.")
        return 0

    print("Chats that have messaged this bot:\n")
    for chat_id, description in sorted(seen.items()):
        print(f"  {chat_id:<18} {description}")
    print()
    print("  Put the one you want in ~/.config/todays-brief/env:")
    print(f"      TELEGRAM_CHAT_ID={sorted(seen)[0]}")
    return len(seen)


def headline_lines(limit: int = MAX_HEADLINES) -> list[str]:
    """The briefing's top stories, in the pipeline's own order."""
    rows = load_json(Path("data/world_covered.json"))
    if not isinstance(rows, list):
        return []
    ranked = [r for r in rows if isinstance(r, dict) and type(r.get("briefing_rank")) is int]
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
        len([r for r in stories if isinstance(r, dict) and type(r.get("briefing_rank")) is int])
        if isinstance(stories, list)
        else 0
    )

    lines: list[str] = []
    if stale:
        lines += [f"PREVIOUS ISSUE ({issue_date()}) — a fresh issue is unavailable.", ""]
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


def document_fields(chat: str) -> dict[str, str]:
    """The form fields for sendDocument — everything except the file itself.

    `document` must not be among them. A file field that is also a plain string
    field is not a harmless duplicate: Telegram reads the string as a file_id or a
    URL, ignores the upload in the same request, and answers "failed to get HTTP
    URL content" — an error about a URL nobody sent, which is a riddle to debug
    from the message alone.

    Split out so that constraint can be tested without talking to Telegram.
    """
    return {"chat_id": chat}


def send_paper(token: str, chat: str) -> bool:
    """Send the paper as a document.

    Dated, because a chat fills with a paper a day and "paper.html" a hundred
    times over is a folder nobody can find anything in.
    """
    if not PAPER.is_file():
        print(f"  telegram: {PAPER} is missing — run bundle.py first")
        return False

    name = f"todays-brief-{_today()}.html"
    with PAPER.open("rb") as handle:
        return post(
            token,
            "sendDocument",
            data=document_fields(chat),
            files={"document": (name, handle, "text/html")},
        )


def issue_date() -> str:
    try:
        payload = json.loads(Path("data/built.json").read_text(encoding="utf-8"))
        stamp = parse_timestamp(payload.get("built_at"))
        return stamp.astimezone().date().isoformat() if stamp else "date unknown"
    except (OSError, ValueError, AttributeError):
        return "date unknown"


def _today() -> str:
    return issue_date() if issue_date() != "date unknown" else date.today().isoformat()


def is_stale() -> bool:
    return issue_date() != date.today().isoformat()


def deliver(token: str, chat: str, stale: bool) -> bool:
    """Remember accepted parts so a failed document does not resend the photo."""
    if not PAPER.is_file():
        print("  telegram: no paper to send")
        return False
    identity = hashlib.sha256(PAPER.read_bytes() + chat.encode() + str(stale).encode() + date.today().isoformat().encode()).hexdigest()
    receipt_path = PAPER.parent / "delivery.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = {}
    if not isinstance(receipt, dict) or receipt.get("issue") != identity:
        receipt = {"issue": identity, "briefing": False, "paper": False}
    for part, send in (("briefing", lambda: send_briefing(token, chat, stale)),
                       ("paper", lambda: send_paper(token, chat))):
        if receipt.get(part):
            continue
        if not send():
            return False
        receipt[part] = True
        save_json(receipt, receipt_path)
    return True


def main() -> int:
    # Flags are read here rather than at import, so importing this module in a
    # test does not depend on what the test runner was called with.
    stale = "--stale" in sys.argv or is_stale()
    text_only = "--text-only" in sys.argv

    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")

    # Setting up, rather than delivering. Handled before the credential check
    # because the whole point is to find the chat id that is missing.
    if "--find-chat" in sys.argv:
        if not token:
            print("TELEGRAM_BOT_TOKEN is not set, so there is nothing to ask.")
            print(f"Put it in {ENV_FILE} or export it.")
            return 0
        find_chat(token)
        return 0

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
        # Only worth suggesting when the chat id is the missing half; a missing
        # token is not something this command can help with.
        if not chat and token:
            print("Find it with:  python3 telegram.py --find-chat")
        return 3

    if "--check-config" in sys.argv:
        return 0
    if text_only:
        return 0 if send_briefing(token, chat, stale) else 1
    if not deliver(token, chat, stale):
        return 1
    print(f"  telegram: delivered ({caption(stale).splitlines()[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
