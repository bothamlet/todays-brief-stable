#!/usr/bin/env python3
"""Render the top of an issue as a JPEG."""

from __future__ import annotations

import sys
import os
import re
from pathlib import Path

PAPER = Path("_site/paper.html")
DEFAULT_OUTPUT = Path("_site/frontpage.jpg")

WIDTH = 390
HEIGHT = 780

# Use a denser viewport for phone displays.
SCALE = 2

# JPEG keeps the Telegram upload small.
QUALITY = 86

# Allow fonts and the initial layout to settle before capture.
SETTLE_MS = 3500


def find_browser() -> Path | None:
    """The Chromium already on this machine, newest first, or None."""
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
