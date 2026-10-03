#!/usr/bin/env python3
"""
bundle.py — fold the whole paper into one file that opens from anywhere.

The page is served from a web server: five files it loads and seven it fetches.
That is fine on a server and useless as an attachment. A phone opening a file
from a chat has no origin to fetch against — every browser refuses `fetch()` on a
file:// URL — so a copy of this folder on a phone opens to a masthead with no news
under it. Nothing is broken; there is simply nothing to fetch from.

So this writes a single `paper.html` with everything inside it: the stylesheet,
the two scripts, all seven data files, and the fonts and available photos. It opens from
Telegram, from a download folder, from anywhere, with no network at all — which
is the point. The morning paper should be readable on a train.

Three things are not as easy as concatenation:

  The data has to be embedded, not fetched. render.js reads window.__BRIEF__ when
  it exists and falls back to fetch when it does not, which is why the same
  script still works on the hosted copy. Nothing here edits the rendering.

  The fonts have to come with it. The masthead is Fraunces and the body is Inter;
  without them an offline copy falls back to the system serif and stops looking
  like the paper it is. Only the latin and latin-ext subsets are kept — latin-ext
  because Lithuanian is not decoration here, it is a whole page, and its
  diacritics (ą č ę ė į š ų ū ž) live there and nowhere else.

  `</script>` inside embedded content ends the script tag early. Both the JSON and
  the JavaScript are escaped for it, because the failure is a page that renders
  half a newspaper and stops — or, at the top of the file, no page at all.

Usage:
    python3 bundle.py                     # writes _site/paper.html
    python3 bundle.py /tmp/paper.html
"""

from __future__ import annotations

import base64
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from common import atomic_write, newest_first
from pathlib import Path

import requests

INDEX = Path("index.html")
DEFAULT_OUTPUT = Path("_site/paper.html")

# Latin and latin-ext only. Google's stylesheet carries Cyrillic, Greek and
# Vietnamese blocks for each weight as well, and none of them are ever drawn.
KEEP_SUBSETS = ("latin", "latin-ext")

# Only these hosts, for the font files named inside the stylesheet. The URLs come
# from a document this script downloaded, and a script that fetches whatever URL
# a downloaded document names is one that will one day fetch something it was
# never meant to — the shape the linters call SSRF. The stylesheet is Google's and
# the files are Google's; nothing else has any business being requested here.
FONT_HOSTS = ("fonts.gstatic.com", "fonts.googleapis.com")

# A browser user agent, for Google Fonts only — deliberately not rss_feeds'
# USER_AGENT, which is the right thing to send a newsroom and the wrong thing to
# send this.
#
# Google serves the stylesheet by capability. Asked as "news-pipeline/1.0" it
# answers with 2.5KB of truetype and no subset comments at all, so there is
# nothing to split on and nothing to inline. Asked as Chrome it answers with
# 19KB, fifty @font-face blocks, the /* latin */ comments to filter by, and the
# same faces as woff2 — a quarter of the weight, and the only version that can be
# subset at all.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

TIMEOUT = 20.0


def local_asset(name: str) -> Path | None:
    """A project-relative file named by a tag in index.html, or None.

    The names come from our own markup, so this is not a boundary being defended.
    It is the shape of the thing: a bundler that reads a path out of a file and
    opens it should decline the one that leaves the directory, because the day the
    input is not ours it will not be obvious that it used to be.
    """
    if not name or name.startswith(("/", "http:", "https:", "//")):
        return None
    path = Path(name)
    if ".." in path.parts:
        return None
    return path if path.is_file() else None


def font_url(url: str) -> bool:
    """Whether a URL found in the Google stylesheet is one to download."""
    return url.startswith("https://") and url.split("/")[2] in FONT_HOSTS


# --- Inlining ----------------------------------------------------------------


def escape_script(text: str) -> str:
    """Make text safe to sit inside a <script> element.

    An HTML parser ends a script block at the first `</script`, wherever it
    appears — inside a string, inside a comment, inside JSON. A news headline
    containing that sequence would truncate the page there.
    """
    return text.replace("</script", "<\\/script")


def escape_json(payload: object) -> str:
    """JSON safe to sit inside a <script> element.

    `json.dumps` already escapes control characters; the only sequence that can
    still end the element early is the literal `<`, so it is written as the
    equivalent unicode escape. `\\u003c` parses back to `<`, so nothing downstream
    can tell the difference.
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c")


def data_urls() -> list[str]:
    """Every data file render.js declares, read from render.js itself.

    Derived rather than listed so every wired section must have an input. A
    missing file fails packaging instead of sending an incomplete fresh issue.
    """
    source = Path("render.js").read_text(encoding="utf-8")
    names = re.findall(r'\w+_URL = "([^"]+)"', source)
    # Only the ones that resolve inside the project. Same reasoning as
    # local_asset: these are ours, and the refusal should not depend on that.
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


IMAGE_LIMIT = 2 * 1024 * 1024
IMAGE_BUDGET = 12 * 1024 * 1024


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
        with requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=12,
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


def inline_images(payload: dict[str, object], *, offline: bool = False) -> None:
    """Replace all photo references with embedded images or an empty fallback."""
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
    urls = list(dict.fromkeys(row["urlToImage"] for row in references))
    replacements = {}
    spent = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        downloader = lambda url: download_image(url) if not offline or url.startswith("data:") else None
        for url, data in zip(urls, pool.map(downloader, urls)):
            uri = image_uri(data) if data else None
            # Count duplicate appearances: JSON embeds each occurrence separately.
            appearances = sum(row["urlToImage"] == url for row in references)
            cost = len(uri or "") * appearances
            if uri and spent + cost <= IMAGE_BUDGET:
                replacements[url] = uri
                spent += cost
            else:
                replacements[url] = ""
    for row in references:
        row["urlToImage"] = replacements[row["urlToImage"]]
    print(f"  photos: {sum(bool(uri) for uri in replacements.values())}/{len(urls)} embedded")


def embed_data(html: str, *, offline: bool = False) -> tuple[str, int, int]:
    """Insert the data files as window.__BRIEF__, just before the scripts.

    Placed before them rather than after because render.js reads it at load; a
    later script tag would run second and the loader would look in an object that
    does not exist yet, find nothing, and fall through to a fetch that cannot
    work.
    """
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

    # The delivered copy only needs ranked World stories and the Other category.
    # The pipeline's full intermediate roster stays on disk for later stages.
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
    inline_images(payload, offline=offline)

    block = (
        "<script>window.__BRIEF__=" + escape_json(payload) + ";</script>\n"
    )
    # Before the first inlined script, which is where the reader is defined.
    marker = "<script>"
    index = html.find(marker)
    html = html[:index] + block + html[index:] if index >= 0 else html + block
    return html, len(payload), bytes_in


# --- Fonts -------------------------------------------------------------------


def stylesheet_url() -> str | None:
    """The Google Fonts URL from index.html, if it is Google's at all.

    Checked rather than taken on trust, for the same reason the font files are:
    the URL is regex-matched out of a file and handed to requests.get.
    """
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
            # See BROWSER_UA. Without one of these the response is truetype and
            # carries no subset comments, so there is nothing here to inline.
            headers={"User-Agent": BROWSER_UA},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        css = response.text
    except requests.RequestException:
        return None

    # Split on the subset comments Google emits before each block, so the blocks
    # that will never be drawn can be dropped before anything is downloaded.
    blocks = re.split(r"/\*\s*([a-z0-9\-]+)\s*\*/", css)
    kept: list[str] = []
    for name, block in zip(blocks[1::2], blocks[2::2]):
        if name in KEEP_SUBSETS:
            kept.append(block)
    if not kept:
        return None

    # Collapse the duplicate references before anything is downloaded, so each
    # file is fetched once and embedded once.
    faces, unreadable = merge_faces(kept)
    if len(faces) != len(kept):
        print(f"  fonts: {len(kept)} faces share {len(faces)} file(s)")
    if unreadable:
        print(f"  fonts: {unreadable} block(s) skipped — no readable weight")

    # Every distinct font file the faces refer to, split into the ones to
    # fetch and the ones to refuse, once rather than inside the loop.
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
    # Drop any face that could not be embedded; an offline issue never retains
    # a remote stylesheet or font file as a fallback.
    faces = [block for block in faces if "url(https://" not in block]
    return "".join(faces) if embedded and faces else None


def face_weight(block: str) -> int | None:
    """The font weight a block declares, or None if it does not declare a number.

    Google always emits numeric weights, but `@font-face` also permits `normal`
    and `bold`, and this is called on a stylesheet it downloaded rather than on
    one written here. Returning None rather than raising keeps the decision at the
    call site, where a block that cannot be read can be counted and reported — a
    silently dropped block is a font that does not arrive in a bundled paper,
    which looks like a design choice and is not.
    """
    match = re.search(r"font-weight:\s*(\d+)", block)
    try:
        return int(match.group(1)) if match else None
    except (TypeError, ValueError):
        return None


def merge_faces(blocks: list[str]) -> tuple[list[str], int]:
    """One @font-face per file, not one per weight.

    Google emits a separate block for every weight in the requested family, and
    with a browser user agent several of those blocks name the *same* file: a
    variable font covering 300 to 700 is served once and then referenced five
    times. Inlining block by block writes that file's base64 five times over —
    twenty blocks, six files, and 1.2MB of the bundle's 2.4MB saying the same
    thing in different words.

    Sharing one file across two weights is also the proof that the file is
    variable, so the weight can be widened to the range it demonstrably covers
    rather than assumed. Blocks are keyed on the unicode range as well as the
    file, so two subsets are never folded together.
    """
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
    """Drop the preconnect hints, which point at a host the bundle no longer uses.

    index.html preconnects to fonts.googleapis.com and fonts.gstatic.com so the
    hosted copy can begin its font handshake early. The bundle carries its fonts
    inside it, so what is left is a DNS lookup and a TLS handshake for nothing —
    on a phone with no signal, a wait before a page that needed no network at all.
    """
    count = len(re.findall(r'<link rel="preconnect"[^>]*>', html))
    html = re.sub(r'\s*<link rel="preconnect"[^>]*>', "", html)
    return html, count


def inline_fonts(html: str, *, offline: bool = False) -> tuple[str, bool]:
    css = None if offline else fonts_css()
    if not css:
        print("  fonts: could not be inlined — the paper will use system faces")
        html = re.sub(r'<link href="https://fonts\.googleapis\.com/[^"]+" rel="stylesheet">', "", html)
        return html, False
    # The remote link is replaced, not supplemented: leaving it in would make the
    # bundled file reach for the network on open, which is the one thing a bundle
    # exists to avoid.
    html = re.sub(
        r'<link href="https://fonts\.googleapis\.com/[^"]+" rel="stylesheet">',
        "<style>\n" + css + "\n</style>",
        html,
    )
    return html, True


# --- Entry point -------------------------------------------------------------


def build(output: Path, *, offline: bool = False) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    html = INDEX.read_text(encoding="utf-8")

    html, style_count = inline_stylesheet(html)
    if style_count:
        html, script_count = inline_scripts(html)
    else:
        # Refusing rather than shipping a paper with no styling and no rendering:
        # a bundle that half-works is worse than a build that fails, because the
        # failure arrives silently on a phone at seven in the morning.
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
