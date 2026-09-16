#!/usr/bin/env python3
"""
llm.py — one DeepSeek client for the whole pipeline.

Four steps call the model and each had grown its own POST, its own key lookup
and its own response parsing — five copies in total. That meant every hard-won
fix had to be applied five times, and the two that mattered most were:

* `thinking` must be disabled. deepseek-flash reasons by default, and reasoning
  tokens are billed against `max_tokens`, so a long chain of thought silently
  consumed the budget and returned empty content on roughly 40% of calls. The
  first production run mis-classified 7 of 12 articles before this was found.
* JSON mode occasionally returns empty content anyway, so one retry is cheap
  insurance.

Both now live here, once.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

API_URL = "https://api.deepseek.com/chat/completions"
API_KEY_ENV = "DEEPSEEK_API_KEY"

# Cheap and fast. Override with DEEPSEEK_MODEL to try another.
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

REQUEST_TIMEOUT = 45.0
MAX_ATTEMPTS = 2


def get_api_key() -> str | None:
    """Read the key configured for this process."""
    return os.environ.get(API_KEY_ENV, "").strip() or None


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a reply, tolerating prose or a code fence."""
    if not text:
        return None

    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]

    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1:
        return None

    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def ask_json(
    system: str,
    user: str,
    *,
    max_tokens: int = 300,
    temperature: float = 0.0,
    model: str | None = None,
    api_key: str | None = None,
    session: requests.Session | None = None,
    attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any] | None:
    """One JSON-mode call. Returns the parsed object, or None on any failure.

    Never raises: every caller treats a missing verdict as "carry on without it",
    which beats dropping a story or an entire run. Pass `model` to override the
    default for one step.
    """
    key = api_key if api_key is not None else get_api_key()
    if not key:
        return None

    payload = {
        "model": model or MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # See the module docstring: without this, long reasoning silently empties
        # the response.
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {key}"}
    poster = session.post if session is not None else requests.post

    for attempt in range(1, attempts + 1):
        try:
            response = poster(API_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue

        if response.status_code != 200:
            continue

        try:
            choices = response.json().get("choices") or []
        except ValueError:
            continue

        if not choices:
            continue

        content = ((choices[0].get("message") or {}).get("content") or "").strip()
        parsed = parse_json_object(content)
        if parsed is not None:
            return parsed
        # Empty or unparseable — worth exactly one more try.

    return None
