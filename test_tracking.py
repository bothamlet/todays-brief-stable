import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from track_stories import SCHEMA, track


class TestTracking(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.addCleanup(self.db.close)

    def edition(self, day, *stories):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=day)
        records = [{"title": title, "url": url} for title, url in stories]
        track(self.db, records, now, now.date().isoformat())
        return [story["history"]["days_running"] for story in records]

    def test_shared_generic_words_do_not_merge_unrelated_events(self):
        self.edition(0, ("Police report deadly fire at Lima warehouse", "old"))
        self.assertEqual(
            self.edition(1, ("Police report deadly fire at Oslo school", "new")),
            [1],
        )

    def test_exact_url_has_priority_over_earlier_title_match(self):
        self.edition(0, ("Baltic ferry rescue teams search overnight", "shared"))
        self.assertEqual(
            self.edition(
                1,
                ("Baltic ferry rescue teams search offshore", "new"),
                ("Passengers airlifted from vessel", "shared"),
            ),
            [1, 2],
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM sightings WHERE seen_day = '2026-10-02'")
            .fetchone()[0],
            2,
        )

    def test_competing_equal_title_matches_do_not_share_history(self):
        self.edition(0, ("Baltic ferry rescue teams search overnight", "old"))
        self.assertEqual(
            self.edition(
                1,
                ("Baltic ferry rescue teams search offshore", "north"),
                ("Baltic ferry rescue teams search inland", "south"),
            ),
            [1, 1],
        )

    def test_close_title_with_new_url_continues_once_per_day(self):
        first = ("Baltic ferry rescue teams search overnight", "old")
        next_day = ("Baltic ferry rescue teams search offshore", "new")
        self.assertEqual(self.edition(0, first), [1])
        self.assertEqual(self.edition(0, first), [1])
        self.assertEqual(self.edition(1, next_day), [2])
        self.assertEqual(self.edition(1, next_day), [2])

    def test_two_stories_with_the_same_shared_url_are_ambiguous(self):
        self.edition(0, ("Baltic ferry rescue teams search overnight", "shared"))
        next_day = (
            ("Baltic ferry rescue teams search offshore", "shared"),
            ("Police report deadly fire at Oslo school", "shared"),
        )
        self.assertEqual(self.edition(1, *next_day), [1, 1])
        self.assertEqual(self.edition(1, *next_day), [1, 1])
        self.assertEqual(self.edition(2, *next_day), [2, 2])
