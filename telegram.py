#!/usr/bin/env python3
"""Send the briefing and HTML issue through Telegram."""

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

# Keep credentials outside the checkout.
ENV_FILE = Path.home() / ".config" / "todays-brief" / "env"

MAX_HEADLINES = 3

# Telegram rejects captions beyond its character limit.
CAPTION_LIMIT = 1024

TIMEOUT = 60.0


def env(name: str) -> str:
    """A setting, from the environment or from the credentials file."""
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
    """Print every chat that has messaged this bot, and return how many."""
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
    """The photo's caption: how many, then the first few by name."""
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

    return text if len(text) <= CAPTION_LIMIT else text[: CAPTION_LIMIT - 1] + "…"


def post(token: str, method: str, *, data: dict[str, Any], files: dict[str, Any] | None = None) -> bool:
    """Call the Bot API without exposing the token in errors."""
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

    # Do not log raw responses or request URLs.
    try:
        detail = json.loads(response.text).get("description", "")
    except ValueError:
        detail = ""
    print(f"  telegram: {method} failed — HTTP {response.status_code} {detail}".rstrip())
    return False


def send_briefing(token: str, chat: str, stale: bool) -> bool:
    """The briefing, as a photo caption if there is a photo and a message if not."""
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
    """Build form fields separately from the uploaded document."""
    return {"chat_id": chat}


def send_paper(token: str, chat: str) -> bool:
    """Send the paper as a document."""
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
    stale = "--stale" in sys.argv or is_stale()
    text_only = "--text-only" in sys.argv

    token = env("TELEGRAM_BOT_TOKEN")
    chat = env("TELEGRAM_CHAT_ID")

    # Allow chat discovery before a chat ID is configured.
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
