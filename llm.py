#!/usr/bin/env python3
"""Shared DeepSeek client for JSON responses."""

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
    """One JSON-mode call."""
    key = api_key if api_key is not None else get_api_key()
    if not key:
        return None

    payload = {
        "model": model or MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Reasoning otherwise consumes the token budget before JSON is emitted.
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
            body = response.json()
            choices = body.get("choices") if isinstance(body, dict) else None
        except ValueError:
            continue

        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        content = content.strip()
        parsed = parse_json_object(content)
        if parsed is not None:
            return parsed
        # Empty or unparseable — worth exactly one more try.

    return None
