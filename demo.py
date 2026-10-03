#!/usr/bin/env python3
"""Build a labeled sample phone issue without credentials or network calls."""
from __future__ import annotations
import argparse
import base64
import os
import shutil
import struct
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bundle
from common import save_json

ROOT = Path(__file__).resolve().parent


def illustration(color: tuple[int, int, int]) -> str:
    """A small original raster illustration, generated with the standard library."""
    width, height = 480, 270
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            mix = 0.65 + 0.35 * (x + y) / (width + height)
            rows.extend(int(channel * mix) for channel in color)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    image = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    image += chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')


def sample_data() -> dict[str, object]:
    date = '2026-01-01T07:00:00+00:00'
    def story(title, description, color=(25, 117, 139)) -> dict[str, Any]:
        return {'title': title, 'description': description, 'source': 'Sample newsroom',
                'url': 'https://example.com', 'urlToImage': illustration(color), 'publishedAt': date}
    world = story('A morning paper that travels with you', 'DEMO: these fictional stories illustrate the interface. This issue was built without a network connection.')
    world['briefing_rank'] = 0
    world['coverage'] = {'left': [story('One event, more than one headline', 'DEMO: use the arrows to compare another sample framing.')],
                         'center': [story('Read a second outlet’s framing', 'DEMO: every link points to example.com; these are not real reports.')], 'right': []}
    other = story('A small tool with a daily routine', 'DEMO: collection, history, packaging, and delivery are separate stages.', (156, 115, 35))
    other['classification'] = {'category': 'Other', 'significance': 'medium'}
    return {'built': {'built_at': date, 'demo': True}, 'world_covered': [world], 'world_tracked': [other],
            'europe': [story('A place for the rest of Europe’s news', 'DEMO: this section adds stories outside the front-page briefing.', (81, 70, 152))],
            'lietuva': [story('Lietuvos naujienos vienoje vietoje', 'DEMO: pavyzdinis tekstas, skirtas lietuviškų raidžių ir maketo patikrai.', (171, 109, 35))],
            'tech': [story('Technical news, selected for a short read', 'DEMO: the tech beat balances announcements, research, and reporting.', (49, 102, 159))],
            'sports': [story('The competitions you follow', 'DEMO: the sports beat selects headlines for its configured interests.', (181, 95, 39))]}


def build_sample(output: Path) -> None:
    output = output.resolve()
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='todays-brief-demo-') as temp:
        folder = Path(temp)
        for asset in ['index.html', 'styles.css', 'app.js', 'render.js']:
            shutil.copy(ROOT / asset, folder / asset)
        for name, payload in sample_data().items():
            save_json(payload, folder / 'data' / f'{name}.json')
        try:
            os.chdir(folder)
            bundle.build(output, offline=True)
        finally:
            os.chdir(previous)
    print(f'Sample issue (fictional news): {output}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=ROOT / '_site/demo.html')
    build_sample(parser.parse_args().output)


if __name__ == '__main__':
    main()
