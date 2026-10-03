"""Offline asset and install portability regression checks."""
from __future__ import annotations
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bundle
import demo

ROOT = Path(__file__).resolve().parent

class TestOfflinePackage(unittest.TestCase):
    def test_sample_build_requires_no_network_and_preserves_local_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch('requests.get', side_effect=AssertionError('network request')):
            output = Path(tmp) / 'sample.html'
            with contextlib.redirect_stdout(io.StringIO()):
                demo.build_sample(output)
            text = output.read_text()
            self.assertIn('"demo":true', text)
            self.assertIn('data:image/png;base64,', text)
            self.assertNotIn('<link href="https://fonts.googleapis.com', text)
            self.assertNotIn('<script src=', text)
            self.assertNotIn('data/stats.json', text)

    def test_failed_photo_fetch_preserves_online_fallback(self):
        data = {'story': {'urlToImage': 'https://example.com/photo.jpg'}}
        with patch.object(bundle, 'download_image', return_value=None):
            bundle.inline_images(data)
        self.assertEqual(data['story']['urlToImage'], 'https://example.com/photo.jpg')

    def test_offline_mode_omits_remote_photos_without_downloads(self):
        data = {'story': {'urlToImage': 'https://example.com/photo.jpg'}}
        with patch.object(bundle, 'download_image', side_effect=AssertionError('download')):
            bundle.inline_images(data, offline=True)
        self.assertEqual(data['story']['urlToImage'], '')

    def test_large_photo_is_embedded_without_changing_its_bytes(self):
        import base64
        photo = b'\xff\xd8\xff' + b'x' * (3 * 1024 * 1024)
        data = {'story': {'urlToImage': 'https://example.com/photo.jpg'}}
        with patch.object(bundle, 'download_image', return_value=photo):
            bundle.inline_images(data)
        uri = data['story']['urlToImage']
        self.assertTrue(uri.startswith('data:image/jpeg;base64,'))
        self.assertEqual(base64.b64decode(uri.split(',', 1)[1]), photo)

    def test_failed_embedding_does_not_preserve_unsafe_urls(self):
        for url in ['javascript:alert(1)', 'https://[', 'https://user:pass@example.com/photo']:
            data = {'story': {'urlToImage': url}}
            with patch.object(bundle, 'download_image', return_value=None):
                bundle.inline_images(data)
            self.assertEqual(data['story']['urlToImage'], '')

    def test_malformed_photo_url_is_an_omitted_asset(self):
        self.assertIsNone(bundle.download_image('https://['))

    def test_photo_budget_counts_duplicate_appearances(self):
        data = {'stories': [{'urlToImage': 'https://example.com/image'} for _ in range(3)]}
        with patch.object(bundle, 'download_image', return_value=b'\xff\xd8\xfffixture'), patch.object(bundle, 'IMAGE_BUDGET', 1):
            bundle.inline_images(data)
        self.assertTrue(all(row['urlToImage'] == 'https://example.com/image' for row in data['stories']))

    def test_missing_fonts_remove_remote_stylesheet(self):
        html = (ROOT / 'index.html').read_text()
        with patch.object(bundle, 'fonts_css', return_value=None):
            result, ok = bundle.inline_fonts(html)
        self.assertFalse(ok)
        self.assertNotIn('<link href="https://fonts.googleapis.com', result)

if __name__ == '__main__':
    unittest.main()
