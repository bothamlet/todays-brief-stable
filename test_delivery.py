"""Delivery status, caption ordering, receipts and shell retry regressions."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import telegram

ROOT = Path(__file__).resolve().parent

class TestDelivery(unittest.TestCase):
    def test_rank_zero_is_the_first_headline_and_is_counted(self):
        rows = [{'title': f'Story {i}', 'briefing_rank': i} for i in range(4)]
        with patch.object(telegram, 'load_json', return_value=rows):
            text = telegram.caption()
        self.assertEqual(text, '4 in the briefing\n• Story 0\n• Story 1\n• Story 2')

    def test_stale_message_uses_actual_issue_date(self):
        with patch.object(telegram, 'load_json', return_value=[{'title': 'Story', 'briefing_rank': 0}]), patch.object(telegram, 'issue_date', return_value='2026-09-01'):
            self.assertTrue(telegram.caption(True).startswith('PREVIOUS ISSUE (2026-09-01)'))

    def test_main_failure_and_skip_have_distinct_statuses(self):
        with patch.object(telegram, 'env', return_value='fixture'), patch.object(telegram, 'deliver', return_value=False), patch.object(sys, 'argv', ['telegram.py']):
            self.assertEqual(telegram.main(), 1)
        with patch.object(telegram, 'env', return_value=''), patch.object(sys, 'argv', ['telegram.py']):
            self.assertEqual(telegram.main(), 3)

    def test_text_only_propagates_failure(self):
        with patch.object(telegram, 'env', return_value='fixture'), patch.object(telegram, 'send_briefing', return_value=False), patch.object(sys, 'argv', ['telegram.py', '--text-only']):
            self.assertEqual(telegram.main(), 1)

    def test_retry_does_not_duplicate_accepted_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / 'paper.html'
            paper.write_text('fixture issue')
            with patch.object(telegram, 'PAPER', paper), patch.object(telegram, 'send_briefing', return_value=True) as brief, patch.object(telegram, 'send_paper', side_effect=[False, True]) as send:
                self.assertFalse(telegram.deliver('fixture', 'chat', False))
                self.assertTrue(telegram.deliver('fixture', 'chat', False))
                self.assertEqual(brief.call_count, 1)
                self.assertEqual(send.call_count, 2)
            paper.write_text('next issue')
            with patch.object(telegram, 'PAPER', paper), patch.object(telegram, 'send_briefing', return_value=True) as brief, patch.object(telegram, 'send_paper', return_value=True):
                self.assertTrue(telegram.deliver('fixture', 'chat', False))
                brief.assert_called_once()

class TestDeliveryShell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / 'deliver.sh', self.root / 'deliver.sh')
        (self.root / 'bin').mkdir()
        (self.root / '_site').mkdir()
        (self.root / '_site/frontpage.jpg').write_text('old photo')
        (self.root / 'run.sh').write_text('#!/bin/bash\nexit "${TEST_PIPELINE_STATUS:-0}"\n')
        (self.root / 'run.sh').chmod(0o700)
        stub = self.root / 'bin/python3'
        stub.write_text('''#!/bin/bash
case "$1" in
-) cat >/dev/null; exit 0;;
frontpage.py) exit 1;;
telegram.py) echo attempt >> attempts; exit "${TEST_SEND_STATUS:-1}";;
*) exit 0;;
esac
''')
        stub.chmod(0o700)

    def run_delivery(self, send=1, pipeline=0):
        env = os.environ.copy()
        env.update(PATH=str(self.root / 'bin') + ':' + env['PATH'], TEST_SEND_STATUS=str(send), TEST_PIPELINE_STATUS=str(pipeline), BRIEF_NETWORK_WAIT_SECONDS='0', BRIEF_DELIVERY_WAIT_SECONDS='0', BRIEF_PIPELINE_WAIT_SECONDS='0')
        return subprocess.run(['bash', 'deliver.sh'], cwd=self.root, env=env, capture_output=True, text=True)

    def test_failed_delivery_retries_and_exits_nonzero(self):
        result = self.run_delivery()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len((self.root / 'attempts').read_text().splitlines()), 3)
        self.assertNotIn('Delivered in', result.stdout)
        self.assertFalse((self.root / '_site/frontpage.jpg').exists())

    def test_failed_build_is_reported_even_if_delivery_also_fails(self):
        self.assertNotEqual(self.run_delivery(pipeline=7).returncode, 0)

    def test_successful_stale_delivery_reports_build_failure(self):
        self.assertEqual(self.run_delivery(send=0, pipeline=7).returncode, 7)

    def test_missing_credentials_skip_without_retry(self):
        result = self.run_delivery(send=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len((self.root / 'attempts').read_text().splitlines()), 1)
        self.assertIn('skipped', result.stdout)

if __name__ == '__main__':
    unittest.main()
