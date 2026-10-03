#!/usr/bin/env python3
"""
frontpage.py — render the top of today's paper as a picture.

The paper itself travels as a file, and a file in a chat shows up as a filename.
A picture of the front page turns the morning message into something you can read
from the notification, and gives the chat a newspaper rather than an attachment.

It renders the *bundled* copy, not the served one — `_site/paper.html` — because
that is the file being sent, so what the picture shows and what the attachment
holds cannot drift apart. It also means no server has to be running to take the
shot, which matters for something that fires on a laptop's own schedule.

Choosing a browser is the fiddly part. Playwright's Python package pins a browser
revision and downloads it on demand; this machine already has one, and `launch()`
refuses to use it because the revision number differs. So the executable is found
on disk instead of fetched — 641MB of browser is already there and asking for it
again would be a strange way to render one screenshot.

A missing browser is not fatal. deliver.sh sends the paper without a picture and
says so, because a paper that arrives is worth more than a picture that does.

Usage:
    python3 frontpage.py                 # writes _site/frontpage.jpg
    python3 frontpage.py /tmp/lead.jpg
"""

from __future__ import annotations

import sys
import os
import re
from pathlib import Path

PAPER = Path("_site/paper.html")
DEFAULT_OUTPUT = Path("_site/frontpage.jpg")

# The phone's width, and how far down the page the picture runs. Long enough for
# the masthead, the lead photograph, the headline and the standfirst; short
# enough that the image is still legible once a chat scales it down.
#
# Separate integers rather than a viewport dict, and the dicts are written out at
# the call sites below. Playwright types those arguments as TypedDicts, which a
# plain dict[str, int] is not assignable to — only a literal is.
WIDTH = 390
HEIGHT = 780

# Twice the pixels. A phone screen is dense and a screenshot of a phone-width
# page at 1x looks soft the moment it is displayed at any size at all.
SCALE = 2

# JPEG rather than PNG. This is a photograph of text; PNG would carry it losslessly
# at three or four times the bytes, over mobile data, every morning.
QUALITY = 86

# Playwright waits for the load event, then this long for webfonts to swap in and
# the lead photo to arrive. Without it the picture is of a page mid-render: the
# masthead in a fallback serif, or an empty frame where the photograph goes.
SETTLE_MS = 3500


def find_browser() -> Path | None:
    """The Chromium already on this machine, newest first, or None.

    Both layouts Playwright has used are searched, because which one exists
    depends on which version downloaded it. Nothing here is pinned to a revision
    number: pinning one means this script breaks the next time a browser is
    installed, which is a bad trade for a decorative image.
    """
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright")))
    if not root.is_dir():
        return None
    found = sorted(root.glob("chromium-*/chrome-linux*/chrome"))
    found += sorted(
        root.glob(
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
        )
    )
    def revision(path: Path) -> int:
        match = re.search(r"-(\d+)", str(path))
        return int(match.group(1)) if match else 0
    return max(found, key=revision) if found else None


def capture(output: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright is not installed (pip install playwright)")
        return False

    browser = find_browser()
    if browser is None:
        print("  no chromium found under ~/.cache/ms-playwright")
        print("  install one with:  playwright install chromium")
        return False

    if not PAPER.is_file():
        print(f"  {PAPER} is missing — run bundle.py first")
        return False

    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        instance = pw.chromium.launch(executable_path=str(browser))
        try:
            page = instance.new_page(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=SCALE,
            )
            page.goto(PAPER.resolve().as_uri(), wait_until="load")
            page.wait_for_timeout(SETTLE_MS)
            page.screenshot(
                path=str(output),
                type="jpeg",
                quality=QUALITY,
                clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT},
            )
        finally:
            instance.close()

    return output.is_file()


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    output.unlink(missing_ok=True)

    if not capture(output):
        # Non-zero so deliver.sh can decide, but the message says what to do.
        raise SystemExit("frontpage: no picture produced")

    size = output.stat().st_size
    print(
        f"  front page: {output} "
        f"({size / 1024:.0f} KB, {WIDTH * SCALE}x{HEIGHT * SCALE})"
    )


if __name__ == "__main__":
    main()
