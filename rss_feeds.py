#!/usr/bin/env python3
"""
rss_feeds.py — shared RSS/Atom fetching for the news pipeline.

Used by both fetch_world.py (discovery) and fetch_coverage.py (per-lean
coverage). Kept in one place so the two never drift apart.

RSS is the pipeline's backbone because it needs no API key, has no quota, cannot
be rate limited, and — unlike an aggregator's index — cannot silently decide that
an outlet does not exist.
"""

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


def _unescape(text: str) -> str:
    """Resolve HTML entities, including double-escaped ones.

    ElementTree decodes XML entities, but feeds escape HTML-only ones on top of
    that: `&nbsp;` arrives as `&amp;nbsp;` and therefore lands here as the literal
    string "&nbsp;". Loop until stable so both layers resolve.
    """
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
    """The first paragraph of an HTML fragment, or the whole thing if untagged.

    Feeds put a standfirst in one <p> and the article body in following ones.
    Stripping every tag flattened them into a single run-on with no punctuation
    between them — the Guardian's read "...in southern Saudi Arabia Houthi
    fighters have said they used drones...", 400 characters with not one full
    stop. Keeping only the opening paragraph gives a usable summary.
    """
    match = re.search(r"<p\b[^>]*>(.*?)</p>", text or "", flags=re.I | re.S)
    return match.group(1) if match else (text or "")


def clip(text: str, max_chars: int) -> str:
    """Trim to a whole word, marking the cut only when one actually happened."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def strip_markup(text: str) -> str:
    """Strip tags and collapse whitespace from a feed description."""
    # Google News appends the publisher inside a <font> tag. Drop that wholesale
    # first, or it survives the generic tag strip as a trailing "  Reuters".
    text = re.sub(r"<font\b[^>]*>.*?</font>", " ", text or "", flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", text)
    return _unescape(re.sub(r"\s+", " ", without_tags).strip())


# ---------------------------------------------------------------------------
# Image upgrades
#
# Two separate problems, needing two different fixes.
#
# 1. SIZE PARAMETERS. The BBC hands back /standard/240/ (240x135). That CDN is
#    unsigned, so rewriting the path to /standard/1024/ yields 1024x576 —
#    verified 200 OK.
#
# 2. SIGNED URLS. The Guardian's i.guim.co.uk signs its query string: editing
#    width=140 to width=1200 returns "401 invalid signature", and stripping the
#    signature returns "401 missing signature". It cannot be rewritten at all.
#    Instead the feed is asked for its other candidates — it ships the same photo
#    at 140, 460 and 700px, each separately signed — and the widest is taken.
#    See _image_candidates below.
#
# The NYT one is a shape problem as much as a size one: it defaults to a SQUARE
# crop, which a 16:9 card axes the top and bottom off. Its variants measure:
#     threeByTwoLargeAt2X  14028x9352   9.4 MB   <- would wreck page load
#     superJumbo            2048x1365   521 KB
#     threeByTwoSmallAt2X    600x400     55 KB   <- chosen
#     mediumSquareAt3X      1800x1800   653 KB   <- square, the default
# ---------------------------------------------------------------------------
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
    for pattern, replacement in IMAGE_UPGRADES:
        url = pattern.sub(replacement, url)

    # NY Post runs on WordPress and hands back the untouched original: 2000x1333
    # at 2.2 MB, or 661 KB once its percent-encoded query is decoded — the
    # encoding also defeats the CDN's own optimisation. Its CDN honours ?w=,
    # which lands at 800x533 / 130 KB. Measured, not assumed.
    if "nypost.com" in url:
        url = unquote(url)
        if "w=" not in url:
            url = f"{url}{'&' if '?' in url else '?'}w=800"

    return url


def _image_candidates(node: ET.Element) -> list[tuple[int, str]]:
    """Every image the item offers as (declared_width, url).

    Feeds frequently list one photo at several widths with a separate signature
    per width, so the only way to get a large one is to pick the right candidate
    rather than rewrite a URL.
    """
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
    """Drop a trailing " - Publisher" from a headline.

    The Google News bridge appends it, so Reuters and AP titles arrived as
    "...shipping - Reuters" and rendered that way on the card. Matched against
    the feed's own name so a legitimate dash clause is never eaten.
    """
    if not title or not name:
        return title
    suffix = re.compile(r"\s+[-–—]\s+" + re.escape(name) + r"\.?\s*$", re.IGNORECASE)
    return suffix.sub("", title).strip()


def _is_restatement(description: str | None, title: str) -> bool:
    """True when a feed offers no real summary, only the headline again.

    Google News descriptions are literally the headline followed by the
    publisher, so once the publisher is stripped the "summary" is the headline
    verbatim and the card prints the same sentence twice. Treating that as no
    description lets the card fall back to the story-level summary instead.
    """

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
    """Fetch one RSS or Atom feed and normalise its items. Never raises.

    Returns [] on any network or parse failure so one dead feed cannot take down
    a run.

    `publisher_from_source` reads RSS's <source> element for the outlet's real
    name instead of using `name` (the label this feed was fetched under). Only
    pass it for a Google News bridge, where `name` is a search query and <source>
    is the publisher. It is off by default because <source> is not reliably a
    publisher: turning it on globally put photo credits on the cards, as
    "© Janis Laizans, Reuters" turned up where "Reuters" belonged.
    """
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )
        if response.status_code != 200:
            return []
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError):
        return []

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

    return out


def fetch_many(
    jobs: list[tuple[str, str]],
    *,
    workers: int = DEFAULT_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
    publisher_from_source: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch many (name, url) feeds concurrently. Returns {url: items}.

    Keyed by URL so callers can map results back to their own structures — the
    discovery roster and the per-lean coverage roster have different shapes.
    """
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


# --- The Google News bridge ---------------------------------------------------

GOOGLE_NEWS_URL = (
    "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
)

# Locale tuples for the editions the beats search: (hl, gl, ceid).
GOOGLE_LOCALES: dict[str, tuple[str, str, str]] = {
    "en": ("en-US", "US", "US:en"),
    "lt": ("lt", "LT", "LT:lt"),
}


def google_news_url(query: str, locale: str = "en") -> str:
    """Build a Google News topic-search RSS URL.

    Shared because both beats had their own byte-identical copy of this function
    and of the two constants under it. Anything that builds a Google URL needs
    publisher_from_source=True on the fetch as well — see fetch_feed.
    """
    hl, gl, ceid = GOOGLE_LOCALES[locale]
    return GOOGLE_NEWS_URL.format(query=quote_plus(query), hl=hl, gl=gl, ceid=ceid)


# --- Recovering images the feed did not carry --------------------------------

# og:image is the standard, twitter:image is the fallback. Both orders occur:
# some pages put property first, some put content first.
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
    """The article page's own preview image, or None. Never raises.

    Measured against this pipeline's own articles, this recovers about two in
    three. It cannot do better: the Financial Times answers 403 to anything that
    is not a browser, so its articles will never have a picture here.
    """
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
    """Give items their article's own preview image when the feed carried none.

    Call this on the items you are about to show, never on everything: it costs
    one page fetch per item, and most articles the pipeline collects are never
    displayed. Returns how many were filled in.

    Two kinds of item are skipped because a fetch cannot help them: those without
    a usable URL, and Google News links. Google answers a request from here with
    its EU consent page and the link is a search-result redirect regardless, so a
    Google-sourced story simply has no image to be had.
    """
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
