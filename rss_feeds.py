#!/usr/bin/env python3
"""Fetch and normalize RSS and Atom feeds."""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote_plus, unquote

import requests
from xml.etree import ElementTree as ET

MEDIA_NS = "{http://search.yahoo.com/mrss/}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

DEFAULT_TIMEOUT = 20.0
DEFAULT_WORKERS = 8
USER_AGENT = "Mozilla/5.0 (compatible; news-pipeline/1.0)"
MAX_DESCRIPTION_CHARS = 220


class FeedItems(list[dict[str, Any]]):
    """Items plus fetch status: a valid empty feed is different from an outage."""

    def __init__(self, items=(), *, ok: bool = True):
        super().__init__(items)
        self.ok = ok


def require_healthy_feeds(results: dict[str, list[dict[str, Any]]], label: str) -> None:
    """At least half the requested feeds must return valid RSS/Atom documents."""
    from common import fail

    healthy = sum(getattr(items, "ok", True) for items in results.values())
    if not results or healthy == 0 or healthy / len(results) < 0.5:
        fail(f"{label}: only {healthy}/{len(results)} feeds succeeded; keeping the previous issue.")
    if healthy < len(results):
        print(f"WARNING: {label}: {len(results) - healthy}/{len(results)} feeds failed.")


def _unescape(text: str) -> str:
    """Resolve HTML entities, including double-escaped ones."""
    for _ in range(3):
        resolved = html.unescape(text)
        if resolved == text:
            break
        text = resolved
    return text


def _first_text(node: ET.Element, *paths: str) -> str:
    """First non-empty text among the given child paths, entities resolved."""
    for path in paths:
        found = node.find(path)
        if found is not None:
            text = _unescape("".join(found.itertext()).strip())
            if text:
                return text
    return ""


def first_paragraph(text: str) -> str:
    """The first paragraph of an HTML fragment, or the whole thing if untagged."""
    match = re.search(r"<p\b[^>]*>(.*?)</p>", text or "", flags=re.I | re.S)
    return match.group(1) if match else (text or "")


def clip(text: str, max_chars: int) -> str:
    """Trim to a whole word, marking the cut only when one actually happened."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def strip_markup(text: str) -> str:
    """Strip tags and collapse whitespace from a feed description."""
    # Google News appends its publisher as a font tag.
    text = re.sub(r"<font\b[^>]*>.*?</font>", " ", text or "", flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", text)
    return _unescape(re.sub(r"\s+", " ", without_tags).strip())


# Keep signed image queries intact.
MAX_IMAGE_WIDTH = 1600  # widest feed-declared candidate we will accept

IMAGE_UPGRADES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(ichef\.bbci\.co\.uk/ace/standard/)\d+"), r"\g<1>1024"),
    (
        re.compile(
            r"-(?:thumbStandard|mediumSquareAt3X|threeByTwoSmallAt2X|articleLarge|"
            r"superJumbo|threeByTwoLargeAt2X)"
            r"(\.(?:jpe?g|png|webp))"
        ),
        r"-threeByTwoSmallAt2X\g<1>",
    ),
)


def upgrade_image(url: str) -> str:
    """Ask the outlet's CDN for a card-sized crop instead of a thumbnail."""
    # Decode HTML entities so image query separators reach the CDN intact.
    url = _unescape(url)
    for pattern, replacement in IMAGE_UPGRADES:
        url = pattern.sub(replacement, url)

    # WordPress may expose a higher-resolution source.
    if "nypost.com" in url:
        url = unquote(url)
        if "w=" not in url:
            url = f"{url}{'&' if '?' in url else '?'}w=800"

    return url


def _image_candidates(node: ET.Element) -> list[tuple[int, str]]:
    """Every image the item offers as (declared_width, url)."""
    out: list[tuple[int, str]] = []

    def add(element: ET.Element) -> None:
        url = (element.get("url") or "").strip()
        if not url.startswith("http"):
            return
        try:
            width = int(element.get("width") or 0)
        except ValueError:
            width = 0
        out.append((width, url))

    enclosure = node.find("enclosure")
    if enclosure is not None:
        add(enclosure)

    for tag in (f"{MEDIA_NS}content", f"{MEDIA_NS}thumbnail"):
        for found in node.findall(tag):
            add(found)

    return out


def _first_image(node: ET.Element) -> str | None:
    """Widest usable image on the item, at card size."""
    candidates = _image_candidates(node)
    if not candidates:
        return None

    sized = [(w, u) for w, u in candidates if 0 < w <= MAX_IMAGE_WIDTH]
    if sized:
        return upgrade_image(max(sized, key=lambda pair: pair[0])[1])

    # No usable width metadata — take the first and let the CDN decide.
    return upgrade_image(candidates[0][1])


def _strip_publisher(title: str, name: str) -> str:
    """Drop a trailing " - Publisher" from a headline."""
    if not title or not name:
        return title
    suffix = re.compile(r"\s+[-–—]\s+" + re.escape(name) + r"\.?\s*$", re.IGNORECASE)
    return suffix.sub("", title).strip()


def _is_restatement(description: str | None, title: str) -> bool:
    """True when a feed offers no real summary, only the headline again."""

    def norm(text: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    body, headline = norm(description), norm(title)
    if not body or not headline:
        return False
    return body == headline or body in headline or headline in body


def fetch_feed(
    name: str,
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    publisher_from_source: bool = False,
) -> list[dict[str, Any]]:
    """Fetch one RSS or Atom feed and normalise its items."""
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )
        if response.status_code != 200:
            return FeedItems(ok=False)
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError):
        return FeedItems(ok=False)

    if root.tag not in ("rss", "RDF", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF", f"{ATOM_NS}feed"):
        return FeedItems(ok=False)
    nodes = root.findall(".//item") or root.findall(f".//{ATOM_NS}entry")
    out: list[dict[str, Any]] = []

    for node in nodes:
        publisher = name
        if publisher_from_source:
            publisher = (node.findtext("source") or "").strip() or name
        title = _strip_publisher(
            _first_text(node, "title", f"{ATOM_NS}title"), publisher
        )
        if not title:
            continue

        link = _first_text(node, "link")
        if not link:
            link_node = node.find(f"{ATOM_NS}link")
            if link_node is not None:
                link = (link_node.get("href") or "").strip()

        description = (
            clip(
                strip_markup(
                    first_paragraph(
                        _first_text(
                            node,
                            "description",
                            f"{ATOM_NS}summary",
                            f"{ATOM_NS}content",
                        )
                    )
                ),
                MAX_DESCRIPTION_CHARS,
            )
            or None
        )
        if _is_restatement(description, title):
            description = None

        out.append(
            {
                "title": title,
                "description": description,
                "url": link or None,
                "urlToImage": _first_image(node),
                "source": publisher,
                "publishedAt": _first_text(
                    node, "pubDate", f"{ATOM_NS}updated", f"{ATOM_NS}published"
                )
                or None,
                "via": "rss",
            }
        )

    return FeedItems(out)


def fetch_many(
    jobs: list[tuple[str, str]],
    *,
    workers: int = DEFAULT_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
    publisher_from_source: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch many (name, url) feeds concurrently."""
    if not jobs:
        return {}

    def work(job: tuple[str, str]) -> tuple[str, list[dict[str, Any]]]:
        name, url = job
        return url, fetch_feed(
            name, url, timeout, publisher_from_source=publisher_from_source
        )

    results: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, items in pool.map(work, jobs):
            results[url] = items

    return results



GOOGLE_NEWS_URL = (
    "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
)

# Locale tuples for the editions the beats search: (hl, gl, ceid).
GOOGLE_LOCALES: dict[str, tuple[str, str, str]] = {
    "en": ("en-US", "US", "US:en"),
    "lt": ("lt", "LT", "LT:lt"),
}


def google_news_url(query: str, locale: str = "en") -> str:
    """Build a Google News topic-search RSS URL."""
    hl, gl, ceid = GOOGLE_LOCALES[locale]
    return GOOGLE_NEWS_URL.format(query=quote_plus(query), hl=hl, gl=gl, ceid=ceid)



# Accept either attribute order in image metadata.
_OG_PATTERNS = (
    re.compile(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I
    ),
    re.compile(
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', re.I
    ),
)


def fetch_og_image(url: str, timeout: float = 12.0) -> str | None:
    """The article page's own preview image, or None."""
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )
        if response.status_code != 200:
            return None
    except requests.RequestException:
        return None

    head = response.text[:200_000]
    for pattern in _OG_PATTERNS:
        match = pattern.search(head)
        if match:
            return upgrade_image(match.group(1).strip()) or match.group(1).strip()
    return None


def backfill_images(
    items: list[dict[str, Any]],
    *,
    workers: int = 6,
    timeout: float = 12.0,
) -> int:
    """Give items their article's own preview image when the feed carried none."""
    needs = [
        item
        for item in items
        if not item.get("urlToImage")
        and str(item.get("url") or "").startswith("http")
        and "news.google.com" not in str(item.get("url") or "")
    ]
    if not needs:
        return 0

    def work(item: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        return item, fetch_og_image(str(item["url"]), timeout=timeout)

    filled = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item, image in pool.map(work, needs):
            if image:
                item["urlToImage"] = image
                filled += 1
    return filled
