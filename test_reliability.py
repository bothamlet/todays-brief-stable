"""Network-free regression checks for edition publication and source outages."""
from __future__ import annotations
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import common
import fetch_coverage
import fetch_sports
import fetch_lietuva
import fetch_tech
import llm
import rss_feeds

ROOT = Path(__file__).resolve().parent

class TestCredentials(unittest.TestCase):
    def test_missing_key_does_not_send_a_request(self):
        session = Mock()
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'credentials.json'
            fixture.write_text(json.dumps({'deepseek': {'key': 'unused-test-key'}}))
            with patch.dict(os.environ, {}, clear=True), patch.object(llm, 'AUTH_FALLBACK', fixture, create=True):
                self.assertIsNone(llm.ask_json('system', 'prompt', session=session))
        session.post.assert_not_called()

    def test_reads_and_trims_explicit_key(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': '  configured-test-key  '}):
            self.assertEqual(llm.get_api_key(), 'configured-test-key')


class TestFeedHealth(unittest.TestCase):
    def test_successful_empty_feed_is_not_an_outage(self):
        response = Mock(status_code=200, content=b'<rss version="2.0"><channel/></rss>')
        with patch('rss_feeds.requests.get', return_value=response):
            items = rss_feeds.fetch_feed('Fixture', 'https://example.com/rss')
        self.assertEqual(items, [])
        self.assertTrue(items.ok)
        rss_feeds.require_healthy_feeds({'fixture': items}, 'Fixture')

    def test_http_error_and_html_document_are_failed_feeds(self):
        for response in [Mock(status_code=503), Mock(status_code=200, content=b'<html/>')]:
            with self.subTest(response=response), patch('rss_feeds.requests.get', return_value=response):
                items = rss_feeds.fetch_feed('Fixture', 'https://example.com/rss')
                self.assertFalse(items.ok)
                with self.assertRaises(SystemExit):
                    rss_feeds.require_healthy_feeds({'fixture': items}, 'Fixture')

    def test_beats_do_not_overwrite_when_feeds_fail(self):
        failed = {'fixture': rss_feeds.FeedItems(ok=False)}
        for module, collector in [(fetch_sports, 'collect'), (fetch_lietuva, 'collect'), (fetch_tech, 'from_feeds')]:
            with self.subTest(module=module.__name__), patch.object(module, 'fetch_many', return_value=failed), patch.object(module, 'save_json') as save:
                with self.assertRaises(SystemExit):
                    getattr(module, collector)()
                save.assert_not_called()

    def test_coverage_outage_preserves_previous_output(self):
        with patch.object(fetch_coverage, 'fetch_many', return_value={'fixture': rss_feeds.FeedItems(ok=False)}):
            with self.assertRaises(SystemExit):
                fetch_coverage.fetch_rss_by_lean()

    def test_quiet_day_still_ranks_medium_world_story(self):
        rows = [{'title': 'A quieter world event', 'url': 'https://example.com/event',
                 'classification': {'category': 'World', 'significance': 'medium', 'topic': 'event', 'region': 'europe'}}]
        with patch.object(fetch_coverage, 'first_existing', return_value=Path('fixture')), patch.object(fetch_coverage, 'load_json', return_value=rows), patch.object(fetch_coverage, 'get_api_key', return_value=None), patch.object(fetch_coverage, 'save_json') as save, contextlib.redirect_stdout(io.StringIO()):
            fetch_coverage.run_pipeline()
        self.assertEqual(save.call_args.args[0][0]['briefing_rank'], 0)

class TestPublication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / 'run.sh', self.root / 'run.sh')
        shutil.copy(ROOT / 'common.py', self.root / 'common.py')
        (self.root / 'requests.py').write_text('')
        (self.root / 'llm.py').write_text('def get_api_key(): return "fixture"\n')
        for name in ['fetch_world', 'cluster_stories', 'classify_articles', 'track_stories', 'fetch_coverage', 'fetch_europe', 'fetch_sports', 'fetch_lietuva', 'fetch_tech']:
            (self.root / f'{name}.py').write_text('')
        (self.root / 'data').mkdir()
        for name in ['world_tracked.json', 'world_covered.json', 'built.json', 'briefing.db']:
            (self.root / 'data' / name).write_text('old')
        (self.root / 'track_stories.py').write_text("from pathlib import Path\nPath('data/world_tracked.json').write_text('new')\nPath('data/briefing.db').write_text('new')\n")
        (self.root / 'fetch_europe.py').write_text("from pathlib import Path\nPath('data/europe.json').write_text('new')\n")
        (self.root / 'fetch_sports.py').write_text('raise SystemExit(7)\n')

    def run_pipeline(self, *args):
        return subprocess.run(['bash', 'run.sh', *args], cwd=self.root, capture_output=True, text=True)

    def assert_restored(self):
        for name in ['world_tracked.json', 'world_covered.json', 'built.json', 'briefing.db']:
            self.assertEqual((self.root / 'data' / name).read_text(), 'old')
        self.assertFalse((self.root / 'data/europe.json').exists())

    def test_failed_run_restores_history_and_removes_new_files(self):
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assert_restored()
        self.assertNotIn('No such file', result.stderr)

    def test_keep_going_cannot_publish_or_stamp_a_failed_run(self):
        result = self.run_pipeline('--keep-going')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assert_restored()

    def test_no_log_really_disables_log_files(self):
        self.run_pipeline('--no-log')
        self.assertFalse((self.root / 'logs').exists())

    def test_delivery_lock_is_inherited_by_the_pipeline(self):
        (self.root / 'fetch_sports.py').write_text('')
        result = subprocess.run(['bash', '-c', 'exec 9>"$PWD/.run.lock"; flock -n 9; export BRIEF_LOCK_HELD="$PWD"; ./run.sh'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_independent_run_is_rejected_while_delivery_holds_lock(self):
        import fcntl
        with (self.root / '.run.lock').open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_pipeline()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already in progress', result.stderr)
        self.assert_restored()

    def test_full_success_stamps_issue(self):
        (self.root / 'fetch_sports.py').write_text('')
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('built_at', json.loads((self.root / 'data/built.json').read_text()))
        self.assertEqual((self.root / 'data/world_tracked.json').read_text(), 'new')

class TestAtomicOutput(unittest.TestCase):
    def test_failed_replace_leaves_old_file_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'issue.json'
            path.write_text('old')
            with patch('common.os.replace', side_effect=OSError('fixture')):
                with self.assertRaises(OSError):
                    common.save_json([{'title': 'new'}], path)
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(list(Path(tmp).iterdir()), [path])


class TestModelResponse(unittest.TestCase):
    def test_malformed_success_response_degrades_without_raising(self):
        import llm
        from unittest.mock import Mock
        for body in [[], None, {'choices': ['bad']}, {'choices': [{'message': {'content': 7}}]}]:
            response = Mock(status_code=200)
            response.json.return_value = body
            with self.subTest(body=body), patch('llm.requests.post', return_value=response):
                self.assertIsNone(llm.ask_json('json fixture', 'fixture', api_key='fixture'))


if __name__ == "__main__":
    unittest.main()
