"""Optional browser tests for the offline phone issue (requirements-browser.txt)."""
from __future__ import annotations
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
import demo
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

class TestPhoneIssue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest('Install requirements-browser.txt for browser checks')
        cls.temp = tempfile.TemporaryDirectory()
        cls.paper = Path(cls.temp.name) / 'paper.html'
        with contextlib.redirect_stdout(io.StringIO()):
            demo.build_sample(cls.paper)
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(args=['--no-sandbox'])
        except Exception:
            cls.pw.stop()
            cls.temp.cleanup()
            if os.environ.get('REQUIRE_BROWSER_TESTS'):
                raise
            raise unittest.SkipTest('Install Chromium with playwright install chromium')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.requests = []
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.route('http://**/*', self.block_request)
        self.page.route('https://**/*', self.block_request)
        self.page.goto(self.paper.as_uri())
        self.page.wait_for_selector('#worldStories .news-card')

    def block_request(self, route):
        self.requests.append(route.request.url)
        route.abort()

    def test_issue_renders_offline_with_no_external_assets(self):
        self.assertEqual(self.page.locator('.page').count(), 6)
        self.assertEqual(self.page.locator('#pageDots button').count(), 6)
        self.assertIn('fictional', self.page.locator('#worldUpdated').inner_text())
        self.assertNotIn('Loading', self.page.locator('body').inner_text())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.errors, [])
        self.assertNotIn('user-scalable=no', self.page.locator('meta[name=viewport]').get_attribute('content'))

    def test_card_arrow_cannot_switch_section_and_vertical_keys_stay_local(self):
        self.page.locator('#worldStories .card-nav-btn').last.focus()
        self.page.keyboard.press('ArrowRight')
        self.assertEqual(self.page.locator('.page.in-view').get_attribute('data-category'), 'world')
        self.assertIn('second outlet', self.page.locator('#worldStories .card-headline-link').first.inner_text())
        self.page.keyboard.press('ArrowDown')
        self.assertEqual(self.page.locator('.page.in-view').get_attribute('data-category'), 'world')

    def test_navigation_is_named_touch_sized_and_hides_inactive_sections(self):
        europe = self.page.get_by_role('button', name='Europe', exact=True)
        self.assertGreaterEqual(europe.bounding_box()['width'], 44)
        europe.click()
        self.assertEqual(europe.get_attribute('aria-current'), 'page')
        self.assertEqual(self.page.locator('.page.in-view').get_attribute('data-category'), 'europe')
        self.assertTrue(self.page.locator('[data-category=world]').evaluate('(node) => node.inert'))
        self.assertEqual(self.page.get_by_role('link').count(), 1)

    def test_swipe_moves_forward_and_touch_cancel_snaps_back(self):
        self.page.evaluate('''() => {
          const wrapper = document.getElementById('pagesWrapper');
          const point = (x, y) => new Touch({identifier: 1, target: wrapper, clientX: x, clientY: y});
          const fire = (type, x, y) => wrapper.dispatchEvent(new TouchEvent(type, {bubbles: true, cancelable: true, touches: type === 'touchend' ? [] : [point(x, y)]}));
          fire('touchstart', 300, 300); fire('touchmove', 170, 305); fire('touchend', 170, 305);
        }''')
        self.assertEqual(self.page.locator('.page.in-view').get_attribute('data-category'), 'europe')
        self.page.evaluate('''() => {
          const wrapper = document.getElementById('pagesWrapper');
          const t = x => new Touch({identifier: 1, target: wrapper, clientX: x, clientY: 300});
          wrapper.dispatchEvent(new TouchEvent('touchstart', {touches: [t(300)], bubbles: true}));
          wrapper.dispatchEvent(new TouchEvent('touchmove', {touches: [t(200)], bubbles: true, cancelable: true}));
          wrapper.dispatchEvent(new TouchEvent('touchcancel', {touches: [], bubbles: true}));
        }''')
        self.assertEqual(self.page.locator('.page.in-view').get_attribute('data-category'), 'europe')
        self.assertIn('-390px', self.page.locator('#pagesWrapper').get_attribute('style'))

    def test_small_phone_titles_and_landscape_fit(self):
        self.page.set_viewport_size({'width': 320, 'height': 640})
        self.assertTrue(self.page.locator('.page-title').evaluate_all('(nodes) => nodes.every(n => n.scrollWidth <= n.clientWidth)'))
        self.page.set_viewport_size({'width': 844, 'height': 390})
        frame = self.page.locator('.phone-frame').bounding_box()
        self.assertLessEqual(frame['height'], 390)
        self.assertEqual(frame['width'], 844)

    def test_missing_data_settles_loading_labels(self):
        self.page.goto((Path(__file__).resolve().parent / 'index.html').as_uri())
        self.page.wait_for_selector('.page-empty')
        self.assertNotIn('Loading', self.page.locator('body').inner_text())
        self.assertNotIn('python3', self.page.locator('body').inner_text())

if __name__ == '__main__':
    unittest.main()
