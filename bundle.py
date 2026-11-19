#!/usr/bin/env python3
"""Package an issue as standalone HTML."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, parse_qs
from common import atomic_write, newest_first
from rss_feeds import upgrade_image, fetch_og_image
from pathlib import Path

import requests

INDEX = Path("index.html")
DEFAULT_OUTPUT = Path("_site/paper.html")

# Embed the Latin subsets used by the briefing.
KEEP_SUBSETS = ("latin", "latin-ext")

# Allowlist font hosts because stylesheet links are remote input.
FONT_HOSTS = ("fonts.gstatic.com", "fonts.googleapis.com")

# Request the browser stylesheet to receive WOFF2 instead of TrueType.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

TIMEOUT = 20.0


def local_asset(name: str) -> Path | None:
    """Resolve only project-relative assets, rejecting paths outside the checkout."""
    if not name or name.startswith(("/", "http:", "https:", "//")):
        return None
    path = Path(name)
    if ".." in path.parts:
        return None
    return path if path.is_file() else None


def font_url(url: str) -> bool:
    """Whether a URL found in the Google stylesheet is one to download."""
    return url.startswith("https://") and url.split("/")[2] in FONT_HOSTS




def escape_script(text: str) -> str:
    """Prevent script text from closing the enclosing HTML element."""
    return text.replace("</script", "<\\/script")


def escape_json(payload: object) -> str:
    """Escape JSON before embedding it in an HTML script element."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c")


def data_urls() -> list[str]:
    """Every data file render.js declares, read from render.js itself."""
    source = Path("render.js").read_text(encoding="utf-8")
    names = re.findall(r'\w+_URL = "([^"]+)"', source)
    # Reject asset paths outside the project root.
    return [name for name in names if name.startswith("data/") and ".." not in Path(name).parts]


def inline_scripts(html: str) -> tuple[str, int]:
    """Replace <script src="..."> with the script's own contents."""
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        src = match.group(1)
        path = local_asset(src)
        if path is None:
            raise SystemExit(f"bundle: missing script {src}")
        count += 1
        body = escape_script(path.read_text(encoding="utf-8"))
        return f"<script>\n{body}\n</script>"

    html = re.sub(r'<script src="([^"]+)"></script>', replace, html)
    return html, count


def inline_stylesheet(html: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        href = match.group(1)
        path = local_asset(href)
        if path is None:
            print(f"  MISSING stylesheet: {href}", file=sys.stderr)
            return match.group(0)
        count += 1
        return f"<style>\n{path.read_text(encoding='utf-8')}\n</style>"

    html = re.sub(r'<link rel="stylesheet" href="([^"]+)">', replace, html)
    return html, count


IMAGE_LIMIT = 8 * 1024 * 1024
# Budget unique embedded photos, keeping normal phone attachments manageable.
IMAGE_BUDGET = 12 * 1024 * 1024


def preferred_image_url(url: str, article_url: str, *, offline: bool = False) -> str:
    """Fix escaped CDN parameters and use Guardian's published larger preview."""
    url = upgrade_image(url)
    if offline:
        return url
    try:
        image = urlsplit(url)
        article = urlsplit(article_url)
        width = int(parse_qs(image.query).get("width", ["0"])[0])
    except (ValueError, TypeError):
        return url
    if (image.hostname == "i.guim.co.uk" and 0 < width < 1000
            and article.scheme in ("http", "https")
            and article.hostname in ("theguardian.com", "www.theguardian.com")
            and not article.username):
        preview = fetch_og_image(article_url)
        if preview:
            try:
                parsed = urlsplit(preview)
                preview_width = int(parse_qs(parsed.query).get("width", ["0"])[0])
                if parsed.hostname == image.hostname and preview_width > width:
                    return preview
            except ValueError:
                pass
    return url


def download_image(url: str) -> bytes | None:
    """Bounded, best-effort raster image download for an offline issue."""
    if url.startswith("data:image/") and ";base64," in url:
        try:
            data = base64.b64decode(url.split(",", 1)[1], validate=True)
            return data if len(data) <= IMAGE_LIMIT else None
        except ValueError:
            return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            return None
    except ValueError:
        return None
    try:
        with requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20,
                          stream=True) as response:
            if response.status_code != 200:
                return None
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data.extend(chunk)
                if len(data) > IMAGE_LIMIT:
                    return None
            return bytes(data)
    except requests.RequestException:
        return None


def image_uri(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def inline_images(payload: dict[str, object], *, offline: bool = False) -> dict[str, str]:
    """Store each unique photo once and replace repeated copies with asset keys."""
    references = []
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("urlToImage"), str) and value["urlToImage"]:
                references.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload)
    # Resolve once per image/article pair, including images in cached issue data.
    pairs = list(dict.fromkeys((row["urlToImage"], str(row.get("url") or "")) for row in references))
    with ThreadPoolExecutor(max_workers=6) as pool:
        preferred = dict(zip(pairs, pool.map(
            lambda pair: preferred_image_url(*pair, offline=offline), pairs)))
    for row in references:
        row["urlToImage"] = preferred[(row["urlToImage"], str(row.get("url") or ""))]
    urls = list(dict.fromkeys(row["urlToImage"] for row in references))
    replacements = {}
    assets: dict[str, str] = {}
    spent = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        downloader = lambda url: download_image(url) if not offline or url.startswith("data:") else None
        for url, data in zip(urls, pool.map(downloader, urls)):
            uri = image_uri(data) if data else None
            key = "brief-image:" + hashlib.sha256(data).hexdigest() if data and uri else ""
            cost = 0 if key in assets else len(uri or "")
            if uri and spent + cost <= IMAGE_BUDGET:
                replacements[url] = key
                assets[key] = uri
                spent += cost
            else:
                try:
                    parsed = urlsplit(url)
                    remote = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
                except ValueError:
                    remote = False
                replacements[url] = url if remote and not offline else ""
    for row in references:
        row["urlToImage"] = replacements[row["urlToImage"]]
    embedded = sum(uri.startswith("brief-image:") for uri in replacements.values())
    remote = sum(uri.startswith(("http://", "https://")) for uri in replacements.values())
    print(f"  photos: {embedded}/{len(urls)} embedded ({len(assets)} unique), {remote} online fallbacks")
    return assets


def embed_data(html: str, *, offline: bool = False) -> tuple[str, int, int]:
    """Insert the data files as window.__BRIEF__, just before the scripts."""
    payload: dict[str, object] = {}
    bytes_in = 0
    for url in data_urls():
        path = Path(url)
        if not path.is_file():
            raise SystemExit(f"bundle: missing required issue data {url}")
        try:
            payload[url] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"bundle: invalid issue data {url}: {error}")
        bytes_in += path.stat().st_size

    if not payload or "data/world_covered.json" not in payload:
        raise SystemExit("bundle: no issue data; run the pipeline or use demo.py first")

    # Keep only the World and Other payloads needed by the bundle.
    world = payload["data/world_covered.json"]
    if not isinstance(world, list):
        raise SystemExit("bundle: World data must be a list of stories")
    payload["data/world_covered.json"] = [row for row in world
                                         if isinstance(row, dict) and type(row.get("briefing_rank")) is int]
    tracked = payload.get("data/world_tracked.json")
    if isinstance(tracked, list):
        other = newest_first([row for row in tracked if isinstance(row, dict)
                              and (row.get("classification") or {}).get("category") == "Other"])
        other.sort(key=lambda row: {"high": 0, "medium": 1, "low": 2}.get(
            str((row.get("classification") or {}).get("significance") or ""), 3))
        payload["data/world_tracked.json"] = other[:5]
    assets = inline_images(payload, offline=offline)

    block = (
        "<script>window.__BRIEF__=" + escape_json(payload) + ";\n"
        "(() => { const images=" + escape_json(assets) + ";\n"
        "function hydrate(value) {\n"
        "  if (!value || typeof value !== 'object') return;\n"
        "  if (Object.hasOwn(images, value.urlToImage)) value.urlToImage = images[value.urlToImage];\n"
        "  Object.values(value).forEach(hydrate);\n"
        "}\n"
        "hydrate(window.__BRIEF__); })();</script>\n"
    )
    # Before the first inlined script, which is where the reader is defined.
    marker = "<script>"
    index = html.find(marker)
    html = html[:index] + block + html[index:] if index >= 0 else html + block
    return html, len(payload), bytes_in




def stylesheet_url() -> str | None:
    """The Google Fonts URL from index.html, if it is Google's at all."""
    source = INDEX.read_text(encoding="utf-8")
    match = re.search(r'<link href="(https://fonts\.googleapis\.com/[^"]+)"', source)
    if not match:
        return None
    return match.group(1) if font_url(match.group(1)) else None


def fonts_css() -> str | None:
    """The Google Fonts stylesheet, with its woff2 files inlined as data URIs."""
    url = stylesheet_url()
    if not url:
        return None

    try:
        response = requests.get(
            url,
            headers={"User-Agent": BROWSER_UA},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        css = response.text
    except requests.RequestException:
        return None

    # Subset comments identify the ranges Google serves.
    blocks = re.split(r"/\*\s*([a-z0-9\-]+)\s*\*/", css)
    kept: list[str] = []
    for name, block in zip(blocks[1::2], blocks[2::2]):
        if name in KEEP_SUBSETS:
            kept.append(block)
    if not kept:
        return None

    faces, unreadable = merge_faces(kept)
    if len(faces) != len(kept):
        print(f"  fonts: {len(kept)} faces share {len(faces)} file(s)")
    if unreadable:
        print(f"  fonts: {unreadable} block(s) skipped — no readable weight")

    remote = {u for block in faces for u in re.findall(r"url\((https://[^)]+)\)", block)}
    wanted = sorted(u for u in remote if font_url(u))
    refused = sorted(remote - set(wanted))
    if refused:
        print(f"  fonts: refusing {len(refused)} URL(s) off the allowlist")

    embedded = 0
    for wanted_url in wanted:
        try:
            font = requests.get(
                wanted_url, headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT
            )
            if font.status_code != 200:
                continue
            encoded = base64.b64encode(font.content).decode("ascii")
            for index, block in enumerate(faces):
                faces[index] = block.replace(
                    wanted_url, f"data:font/woff2;base64,{encoded}"
                )
            embedded += 1
        except requests.RequestException:
            continue

    print(f"  fonts: {embedded} file(s) inlined, subsets {', '.join(KEEP_SUBSETS)}")
    # Drop unresolved font faces so the HTML works offline.
    faces = [block for block in faces if "url(https://" not in block]
    return "".join(faces) if embedded and faces else None


def face_weight(block: str) -> int | None:
    """The font weight a block declares, or None if it does not declare a number."""
    match = re.search(r"font-weight:\s*(\d+)", block)
    try:
        return int(match.group(1)) if match else None
    except (TypeError, ValueError):
        return None


def merge_faces(blocks: list[str]) -> tuple[list[str], int]:
    """One @font-face per file, not one per weight."""
    groups: dict[tuple[str, str, str, str], list[tuple[str, int]]] = {}
    unreadable = 0
    for block in blocks:
        url = re.search(r"url\((https://[^)]+)\)", block)
        family = re.search(r"font-family:\s*'([^']+)'", block)
        style = re.search(r"font-style:\s*([^;]+);", block)
        weight = face_weight(block)
        span = re.search(r"unicode-range:\s*([^;]+);", block)
        if not (url and family and style) or weight is None:
            unreadable += 1
            continue
        key = (
            family.group(1),
            style.group(1).strip(),
            url.group(1),
            span.group(1).strip() if span else "",
        )
        groups.setdefault(key, []).append((block, weight))

    merged: list[str] = []
    for entries in groups.values():
        block, _ = entries[0]
        weights = [weight for _, weight in entries]
        low, high = min(weights), max(weights)
        if low != high:
            block = re.sub(
                r"font-weight:\s*\d+", f"font-weight: {low} {high}", block, count=1
            )
        merged.append(block)
    return merged, unreadable


def strip_preconnect(html: str) -> tuple[str, int]:
    """Drop the preconnect hints, which point at a host the bundle no longer uses."""
    count = len(re.findall(r'<link rel="preconnect"[^>]*>', html))
    html = re.sub(r'\s*<link rel="preconnect"[^>]*>', "", html)
    return html, count


def inline_fonts(html: str, *, offline: bool = False) -> tuple[str, bool]:
    css = None if offline else fonts_css()
    if not css:
        print("  fonts: could not be inlined — the paper will use system faces")
        html = re.sub(r'<link href="https://fonts\.googleapis\.com/[^"]+" rel="stylesheet">', "", html)
        return html, False
    # Replace the remote link to avoid font requests when reading offline.
    html = re.sub(
        r'<link href="https://fonts\.googleapis\.com/[^"]+" rel="stylesheet">',
        "<style>\n" + css + "\n</style>",
        html,
    )
    return html, True




def build(output: Path, *, offline: bool = False) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    html = INDEX.read_text(encoding="utf-8")

    html, style_count = inline_stylesheet(html)
    if style_count:
        html, script_count = inline_scripts(html)
    else:
        raise SystemExit("bundle: index.html has no local stylesheet to inline")

    html, files, data_bytes = embed_data(html, offline=offline)
    html, fonts_ok = inline_fonts(html, offline=offline)
    html, preconnects = strip_preconnect(html)

    atomic_write(output, html)
    size = output.stat().st_size

    print()
    print("=" * 68)
    print("Bundled paper")
    print("=" * 68)
    print(f"  stylesheets inlined : {style_count}")
    print(f"  scripts inlined     : {script_count}")
    print(f"  data files embedded : {files} ({data_bytes / 1024:.0f} KB of JSON)")
    print(f"  fonts               : {'inlined' if fonts_ok else 'system fallback'}")
    print(f"  dead preconnects    : {preconnects} removed")
    print(f"  written             : {output} ({size / 1024:.0f} KB)")
    print()
    print("  Opens with no server and no network.")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build one self-contained phone issue.")
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--offline", action="store_true", help="make no network requests; use system fonts and existing embedded photos")
    args = parser.parse_args()
    build(args.output, offline=args.offline)


if __name__ == "__main__":
    main()
