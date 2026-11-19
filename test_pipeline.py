#!/usr/bin/env python3
"""Regression tests for pipeline stages and helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch
from collections import Counter
from datetime import datetime, timezone
import json

from common import dedupe_by_url, newest_first, parse_timestamp
from publishers import Allowlist, forms, normalise
from textutil import ordinal, shared_words, significant_tokens


def must(stamp: datetime | None) -> datetime:
    """Narrow a parsed timestamp for the type checker, failing loudly if absent."""
    assert stamp is not None, "expected a parsed timestamp"
    return stamp


class TestParseTimestamp(unittest.TestCase):
    """Feeds mix ISO 8601, English RFC 822 and Lithuanian RFC 822."""

    def test_iso8601(self) -> None:
        self.assertEqual(
            parse_timestamp("2026-09-15T23:54:48+00:00"),
            datetime(2026, 9, 15, 23, 54, 48, tzinfo=timezone.utc),
        )

    def test_iso8601_zulu(self) -> None:
        self.assertEqual(
            parse_timestamp("2026-09-15T23:54:48Z"),
            datetime(2026, 9, 15, 23, 54, 48, tzinfo=timezone.utc),
        )

    def test_rfc822_english(self) -> None:
        got = must(parse_timestamp("Wed, 12 Aug 2026 12:40:17 +0300"))
        self.assertEqual((got.year, got.month, got.day), (2026, 8, 12))

    def test_rfc822_lithuanian(self) -> None:
        """Accept Lithuanian weekday and month names."""
        got = must(parse_timestamp("Ant, 15 Rug 2026 23:44:00 EEST"))
        self.assertEqual((got.year, got.month, got.day), (2026, 9, 15))

    def test_lithuanian_rug_is_september_not_august(self) -> None:
        """Disambiguate abbreviated Lithuanian summer and fall months."""
        september = must(parse_timestamp("Ant, 15 Rug 2026 23:44:00 EEST"))
        august = must(parse_timestamp("Pen, 3 Rugp 2026 10:00:00 +0300"))
        self.assertEqual(september.month, 9)
        self.assertEqual(august.month, 8)

    def test_garbage_is_none_not_an_exception(self) -> None:
        for value in ("not a date", "", None, "   "):
            with self.subTest(value=value):
                self.assertIsNone(parse_timestamp(value))

    def test_naive_timestamp_gets_utc(self) -> None:
        """Nothing downstream should have to guess a timezone."""
        got = must(parse_timestamp("2026-09-15T23:54:48"))
        self.assertIsNotNone(got.tzinfo)


class TestNewestFirst(unittest.TestCase):
    """It RETURNS a sorted list; it does not sort in place."""

    def setUp(self) -> None:
        self.rows = [
            {"title": "old", "publishedAt": "Mon, 14 Sep 2026 10:00:00 GMT"},
            {"title": "none", "publishedAt": None},
            {"title": "new", "publishedAt": "Tue, 15 Sep 2026 10:00:00 GMT"},
        ]

    def test_returns_sorted(self) -> None:
        got = newest_first(self.rows)
        self.assertEqual([r["title"] for r in got], ["new", "old", "none"])

    def test_callers_must_use_the_return_value(self) -> None:
        """The trap, asserted directly: calling it changes nothing in place."""
        original = list(self.rows)
        newest_first(self.rows)
        self.assertEqual(self.rows, original, "newest_first must not sort in place")

    def test_undated_sorts_last(self) -> None:
        got = newest_first(self.rows)
        self.assertEqual(got[-1]["title"], "none")


class TestDedupeByUrl(unittest.TestCase):
    def test_drops_repeated_urls(self) -> None:
        rows = [
            {"title": "a", "url": "https://example.com/1"},
            {"title": "b", "url": "https://example.com/1"},
            {"title": "c", "url": "https://example.com/2"},
        ]
        self.assertEqual(len(dedupe_by_url(rows)), 2)

    def test_falls_back_to_title_when_url_missing(self) -> None:
        rows = [
            {"title": "same headline", "url": None},
            {"title": "same headline", "url": None},
        ]
        self.assertEqual(len(dedupe_by_url(rows)), 1)


class TestNormalisePublisher(unittest.TestCase):
    """Google credits one outlet under several names across items."""

    def test_every_spelling_of_one_outlet_agrees(self) -> None:
        for spelling in ("AP News", "apnews.com", "AP-News"):
            with self.subTest(spelling=spelling):
                self.assertIn("apnews", forms(spelling), "all reduce to apnews")

    def test_a_reversed_domain_reaches_the_same_forms(self) -> None:
        """Keep feed records with a recognized publisher alias."""
        shared = forms("Yahoo Sports") & forms("sports.yahoo.com")
        self.assertIn("sportsyahoo", shared)

        gate = Allowlist({"yahoo sports"})
        self.assertIn("Yahoo Sports", gate)
        self.assertIn("sports.yahoo.com", gate)
        self.assertIn("ca.sports.yahoo.com", gate)

    def test_a_name_split_differently_reaches_the_same_forms(self) -> None:
        """Preserve unseen stories while sorting by recency."""
        self.assertIn("kaunodiena", forms("kauno diena"))
        self.assertIn("kaunodiena", forms("KaunoDiena.lt"))

    def test_strips_the_domain_suffix(self) -> None:
        self.assertIn("si", forms("si.com"))
        self.assertIn("vz", forms("vz.lt"))

    def test_a_betting_site_reduces_to_nothing_trusted(self) -> None:
        gate = Allowlist({"sports illustrated", "yahoo sports"}, exact={"si.com"})
        self.assertNotIn("sportsbook.fanduel.com", gate)

    def test_diacritics_fold_rather_than_split(self) -> None:
        """Lithuanian names carry letters that are not a-z."""
        self.assertEqual(normalise("Verslo žinios"), "verslozinios")
        self.assertIn("siauliunaujienos", forms("Šiaulių naujienos"))

    def test_empty_is_empty(self) -> None:
        self.assertEqual(forms(""), frozenset())
        self.assertEqual(forms(None), frozenset())
        self.assertEqual(normalise(""), "")


class TestAllowlist(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = Allowlist(
            {"lrt", "delfi", "15min", "reuters", "ap news", "npr", "sports illustrated"},
            exact={"si.com"},
        )

    def test_admits_long_names_by_substring(self) -> None:
        """"KaunoDiena.lt" has to match the "kauno diena" entry."""
        gate = Allowlist({"kauno diena", "lrt televizija"})
        self.assertIn("KaunoDiena.lt", gate)
        self.assertIn("LRT televizija", gate)
    def test_short_names_match_exactly_only(self) -> None:
        """Do not confuse similarly named sources."""
        self.assertIn("NPR", self.gate)
        self.assertNotIn("Armenpress", self.gate)

    def test_exact_domains_admitted(self) -> None:
        """Sports Illustrated's domain is an abbreviation sharing no substring."""
        self.assertIn("si.com", self.gate)
        self.assertIn("Sports Illustrated", self.gate)

    def test_rejects_the_obvious(self) -> None:
        for name in (
            "sportsbook.fanduel.com",
            "Pippen Ain't Easy",
            "Sactown Sports 1140 AM",
            "",
            "   ",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, self.gate)

    def test_non_string_is_rejected_not_raised(self) -> None:
        self.assertNotIn(None, self.gate)


class TestTextutil(unittest.TestCase):
    def test_ordinal(self) -> None:
        cases = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 21: "21st"}
        for number, want in cases.items():
            with self.subTest(number=number):
                self.assertEqual(ordinal(number), want)

    def test_significant_tokens_drops_stopwords(self) -> None:
        got = significant_tokens("The Houthis seize the Red Sea islands")
        self.assertIn("houthis", got)
        self.assertIn("islands", got)
        self.assertNotIn("the", got)

    def test_shared_words_is_prefix_aware(self) -> None:
        """Headlines about one running story seldom repeat a word exactly."""
        self.assertTrue(shared_words({"houthi"}, {"houthis"}))
        self.assertTrue(shared_words({"indonesia"}, {"indonesian"}))

    def test_shared_words_ignores_short_prefixes(self) -> None:
        self.assertFalse(shared_words({"war"}, {"warsaw"}))


class TestLietuvaFilters(unittest.TestCase):
    """Import here, not at module scope: these pull in requests."""

    @classmethod
    def setUpClass(cls) -> None:
        import fetch_lietuva

        cls.lt = fetch_lietuva

    def test_gossip_and_horoscopes_are_junk(self) -> None:
        for title in (
            "Palmiros horoskopas trečiadieniui, rugsėjo 16 d.",
            "Šie Zodiako ženklai – ypatingi",
            "Orai: bus šilta ir lietinga",
            "Orai.lt: po sausesnio trečiadienio sugrįš lietus",
            "Žvaigždės atostogauja: įžymybės Kroatijoje",
        ):
            with self.subTest(title=title):
                self.assertTrue(self.lt.is_junk({"title": title}))

    def test_real_news_is_not_junk(self) -> None:
        for title in (
            "Numušus bepilotį, Lietuva ir Lenkija žada sutelkti pajėgas",
            "Seimas balsuos dėl biudžeto",
            "Pavojus atšauktas: į Lietuvą įskridęs dronas sunaikintas",
        ):
            with self.subTest(title=title):
                self.assertFalse(self.lt.is_junk({"title": title}))

    def test_lithuanian_news_is_recognised(self) -> None:
        for title in (
            "Kaune atidarytas naujas tiltas",
            "Vilniuje vyks NATO susitikimas",
            "Lietuva siunčia paramą",
            "Alytaus meras atsistatydino",
            "Trakai ruošiasi sezonui",
        ):
            with self.subTest(title=title):
                self.assertTrue(self.lt.is_about_lithuania({"title": title}))

    def test_unanchored_place_names_do_not_match_mid_word(self) -> None:
        """Ignore Lithuanian place tokens embedded inside unrelated words."""
        self.assertFalse(
            self.lt.is_about_lithuania(
                {
                    "title": "Atrakcionas nuplėšė galvos odą 25-erių metų dalyvei",
                    "description": "Incidentas įvyko Indijoje, Maharaštros valstijoje.",
                }
            )
        )

    def test_foreign_news_is_excluded(self) -> None:
        for title in (
            "Wall Street stabtelėjo dėl JAV iždo obligacijų",
            "NBA svarsto galimybę įsigyti Eurolygą",
            "Karo istoriko prognozė: Rusijoje vienas lūžių taškas",
        ):
            with self.subTest(title=title):
                self.assertFalse(self.lt.is_about_lithuania({"title": title}))


class TestTopicGrouping(unittest.TestCase):
    """The fallback grouping, by shared title words."""

    @classmethod
    def setUpClass(cls) -> None:
        import fetch_lietuva

        cls.lt = fetch_lietuva

    def test_identical_stories_merge(self) -> None:
        groups = self.lt.group_by_tokens(
            [
                {"title": "Numušus bepilotį Lietuvoje, žada sutelkti pajėgas"},
                {"title": "Numušus bepilotį Lietuvoje, žada sutelkti pajėgas"},
            ]
        )
        self.assertEqual(len(groups), 1)

    def test_unrelated_stories_do_not_merge(self) -> None:
        """Group transitively related titles."""
        groups = self.lt.group_by_tokens(
            [
                {"title": "Numušus bepilotį Lietuvoje"},
                {"title": "Vilniuje nulaužtas Lietuvos vėliavos kotas"},
                {"title": "Naujas restoranas Kaune atidarytas"},
            ]
        )
        self.assertEqual(len(groups), 3, "unrelated headlines must not merge")

    def test_cross_language_duplicates_stay_apart_for_the_fallback(self) -> None:
        """Documents the limit that put the model in charge."""
        groups = self.lt.group_by_tokens(
            [
                {"title": "Numušus bepilotį, Lietuva ir Lenkija žada sutelkti pajėgas"},
                {"title": "NATO jets shoot down a drone in Lithuanian airspace"},
            ]
        )
        self.assertEqual(len(groups), 2, "word matching cannot join these")

    def test_grouping_output_budget_scales_with_input(self) -> None:
        """Reject invalid model grouping without dropping articles."""
        small = self.lt.GROUP_TOKENS_BASE + self.lt.GROUP_TOKENS_PER_HEADLINE * 10
        large = self.lt.GROUP_TOKENS_BASE + self.lt.GROUP_TOKENS_PER_HEADLINE * 600
        self.assertGreater(large, small)
        self.assertGreaterEqual(small, 300)
        self.assertLessEqual(self.lt.GROUP_LIMIT, 120, "input cap must stay sane")

    def test_feed_rank_guard(self) -> None:
        self.assertEqual(self.lt.feed_rank({"feed_rank": 3}), 3)
        self.assertEqual(self.lt.feed_rank({}), 50)
        self.assertEqual(self.lt.feed_rank({"feed_rank": "x"}), 50)


class TestSportsInterests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import fetch_sports

        cls.sp = fetch_sports

    def test_specific_beats_the_general(self) -> None:
        """Sabonis is NBA · Lietuva, not Basketball."""
        key, _label, matched = self.sp.interest_for(
            "Domantas Sabonis trade talks", "basketball"
        )
        self.assertEqual(key, "lt_player")
        self.assertTrue(matched)

    def test_falls_back_when_nothing_matches(self) -> None:
        """No keyword, so the feed's own fallback applies and matched is False."""
        key, _label, matched = self.sp.interest_for(
            "Team signs a new player", "nba"
        )
        self.assertEqual(key, "nba")
        self.assertFalse(matched)

    def test_keyword_match_is_reported_even_when_it_equals_the_fallback(self) -> None:
        """The distinction the naive fixture got wrong."""
        key, _label, matched = self.sp.interest_for("Some NBA roundup", "nba")
        self.assertEqual(key, "nba")
        self.assertTrue(matched, "the headline names NBA, so this is a real match")

    def test_general_feed_contributes_only_keyword_matches(self) -> None:
        """A feed too broad to assume anything returns no fallback."""
        key, label, matched = self.sp.interest_for("Hegelmann išsiskyrė su treneriu", None)
        self.assertIsNone(key)
        self.assertIsNone(label)
        self.assertFalse(matched)

    def test_kaunas_alone_is_not_a_zalgiris_signal(self) -> None:
        """A city is not a team."""
        key, _label, _matched = self.sp.interest_for("Kauno futbolo klubas laimėjo", None)
        self.assertNotEqual(key, "zalgiris")


class TestSportsLeagues(unittest.TestCase):
    """The tags the page's filter tabs run on — see LEAGUES in fetch_sports.py."""

    @classmethod
    def setUpClass(cls) -> None:
        import fetch_sports

        cls.sp = fetch_sports

    def test_specific_beats_map_to_their_competition(self) -> None:
        for interest, league in (
            ("okc", "NBA"),
            ("lt_player", "NBA"),
            ("nba", "NBA"),
            ("zalgiris", "EUROLEAGUE"),
            ("euroleague", "EUROLEAGUE"),
            ("f1", "F1"),
        ):
            with self.subTest(interest=interest):
                self.assertEqual(self.sp.league_for(interest), league)

    def test_general_basketball_belongs_to_no_competition(self) -> None:
        """The deliberate gap."""
        self.assertEqual(self.sp.league_for("basketball"), "")
        self.assertEqual(self.sp.league_for("lt_basket"), "")

    def test_unknown_and_missing_interests_are_empty_not_a_crash(self) -> None:
        self.assertEqual(self.sp.league_for("something-new"), "")
        self.assertEqual(self.sp.league_for(None), "")

    def test_every_league_is_one_of_the_page_tabs(self) -> None:
        """A league the page has no tab for would be reachable only under ALL."""
        tabs = {"NBA", "EUROLEAGUE", "F1"}
        for interest, league in self.sp.LEAGUES.items():
            with self.subTest(interest=interest):
                self.assertIn(league, tabs)


class TestBundleEscaping(unittest.TestCase):
    """Escape embedded text that could close a script element."""

    @classmethod
    def setUpClass(cls) -> None:
        import bundle

        cls.bundle = bundle

    def test_json_escapes_every_angle_bracket(self) -> None:
        payload = {"title": "Firm </script> closes tag", "n": 1}
        escaped = self.bundle.escape_json(payload)
        self.assertNotIn("<", escaped)
        # And it still parses back to exactly what went in.
        self.assertEqual(json.loads(escaped), payload)

    def test_json_escapes_brackets_inside_values_not_just_at_the_ends(self) -> None:
        payload = [{"t": "a < b"}, {"t": "<html>"}]
        escaped = self.bundle.escape_json(payload)
        self.assertNotIn("<", escaped)
        self.assertEqual(json.loads(escaped), payload)

    def test_script_text_cannot_close_its_own_element(self) -> None:
        escaped = self.bundle.escape_script("var x = '</script>';")
        self.assertNotIn("</script", escaped)
        self.assertIn("<\\/script", escaped)

    def test_script_escape_leaves_ordinary_javascript_alone(self) -> None:
        source = "const a = 1 < 2; // less than, not a tag"
        self.assertEqual(self.bundle.escape_script(source), source)

    def test_local_asset_refuses_paths_that_leave_the_project(self) -> None:
        for bad in ("/etc/passwd", "../../secrets", "https://example.com/x.js", ""):
            with self.subTest(path=bad):
                self.assertIsNone(self.bundle.local_asset(bad))

    def test_local_asset_accepts_a_real_project_file(self) -> None:
        self.assertIsNotNone(self.bundle.local_asset("render.js"))
        self.assertIsNone(self.bundle.local_asset("no-such-file.js"))

    def test_font_url_only_allows_google(self) -> None:
        for good in (
            "https://fonts.gstatic.com/s/inter/v13/abc.woff2",
            "https://fonts.googleapis.com/css2?family=Inter",
        ):
            with self.subTest(url=good):
                self.assertTrue(self.bundle.font_url(good))
        for bad in (
            "https://evil.example/s/font.woff2",
            "http://fonts.gstatic.com/s/x.woff2",
            "file:///etc/passwd",
            # A host that merely ends with the allowlisted one.
            "https://fonts.gstatic.com.evil.example/x.woff2",
        ):
            with self.subTest(url=bad):
                self.assertFalse(self.bundle.font_url(bad))

    def test_faces_sharing_one_file_become_one_face(self) -> None:
        """A variable font is served once and referenced per weight."""
        template = (
            "@font-face {{ font-family: 'Inter'; font-style: normal;"
            " font-weight: {weight}; unicode-range: U+0000-00FF;"
            " src: url(https://fonts.gstatic.com/s/inter/v13/one.woff2) }}"
        )
        blocks = [template.format(weight=w) for w in (300, 400, 500, 600, 700)]
        merged, unreadable = self.bundle.merge_faces(blocks)
        self.assertEqual(unreadable, 0)
        self.assertEqual(len(merged), 1)
        # Widened to the range the single file demonstrably covers.
        self.assertIn("font-weight: 300 700", merged[0])

    def test_faces_with_distinct_files_stay_distinct(self) -> None:
        template = (
            "@font-face {{ font-family: 'Inter'; font-style: normal;"
            " font-weight: {weight}; unicode-range: U+0000-00FF;"
            " src: url(https://fonts.gstatic.com/s/inter/v13/{name}.woff2) }}"
        )
        blocks = [
            template.format(weight=400, name="a"),
            template.format(weight=700, name="b"),
        ]
        merged, _ = self.bundle.merge_faces(blocks)
        self.assertEqual(len(merged), 2)

    def test_subsets_are_never_folded_together(self) -> None:
        """Same file, different unicode ranges — latin against latin-ext."""
        template = (
            "@font-face {{ font-family: 'Inter'; font-style: normal;"
            " font-weight: {weight}; unicode-range: {span};"
            " src: url(https://fonts.gstatic.com/s/inter/v13/one.woff2) }}"
        )
        blocks = [
            template.format(weight=400, span="U+0000-00FF"),
            template.format(weight=400, span="U+0100-02AF"),
        ]
        merged, _ = self.bundle.merge_faces(blocks)
        self.assertEqual(len(merged), 2)

    def test_an_unreadable_weight_is_counted_not_silently_dropped(self) -> None:
        """A dropped block is a font that never arrives in the paper."""
        blocks = [
            "@font-face { font-family: 'Inter'; font-style: normal;"
            " font-weight: bold; src: url(https://fonts.gstatic.com/s/x.woff2) }"
        ]
        merged, unreadable = self.bundle.merge_faces(blocks)
        self.assertEqual(merged, [])
        self.assertEqual(unreadable, 1)


class TestTelegramMessage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import telegram

        cls.tg = telegram

    def setUp(self):
        self.rows = [{"title": f"Fixture story {i}", "briefing_rank": i} for i in range(4)]
        fixture = patch.object(self.tg, "load_json", return_value=self.rows)
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_caption_says_what_the_paper_is(self) -> None:
        text = self.tg.caption()
        self.assertIn("in the briefing", text)
        self.assertLessEqual(len(text), self.tg.CAPTION_LIMIT)

    def test_stale_is_announced_before_anything_else(self) -> None:
        """The point of the message is being readable without opening it."""
        fresh = self.tg.caption()
        stale = self.tg.caption(stale=True)
        self.assertTrue(stale.startswith("PREVIOUS ISSUE"))
        self.assertGreater(len(stale), len(fresh))
        self.assertLessEqual(len(stale), self.tg.CAPTION_LIMIT)

    def test_the_document_is_not_also_named_in_the_form_fields(self) -> None:
        """A string `document` field is read as a URL, not as the upload."""
        fields = self.tg.document_fields("123")
        self.assertNotIn("document", fields)
        self.assertEqual(fields.get("chat_id"), "123")


class TestTechBeat(unittest.TestCase):
    """Test technology source selection and ranking."""

    @classmethod
    def setUpClass(cls) -> None:
        import fetch_tech

        cls.tech = fetch_tech

    def test_a_labs_announcement_outranks_reporting(self) -> None:
        release = {"source": "OpenAI", "title": "Introducing GPT-6 Astra"}
        report = {"source": "Ars Technica", "title": "OpenAI announces GPT-6 Astra"}
        self.assertLess(
            self.tech.source_tier(release), self.tech.source_tier(report)
        )

    def test_reporting_outranks_a_labs_marketing(self) -> None:
        """Prefer original announcements over reports that repeat them."""
        marketing = {
            "source": "OpenAI",
            "title": "Helping older adults use AI in everyday life",
        }
        report = {"source": "Wired", "title": "OpenAI ships a new model"}
        self.assertGreater(
            self.tech.source_tier(marketing), self.tech.source_tier(report)
        )

    def test_a_labs_research_post_is_not_treated_as_marketing(self) -> None:
        """Only the opening words decide, and these do not open that way."""
        for title in (
            "Our framework for reporting model misalignment",
            "NVIDIA Vera Rubin NVL72 Delivers Leading Performance",
            "Perplexity trusts GPT-6 Astra with end-to-end systems",
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    self.tech.source_tier({"source": "NVIDIA", "title": title}), 0
                )

    def test_marketing_shape_only_applies_to_primary_sources(self) -> None:
        """"Why the EU banned..." is ordinary reporting in a newsroom's hands."""
        self.assertEqual(
            self.tech.source_tier(
                {"source": "France 24", "title": "Why the EU banned a model"}
            ),
            1,
        )

    def test_round_robin_stops_one_theme_taking_the_page(self) -> None:
        """Preserve space for policy and independent reporting."""
        articles: list[dict[str, object]] = [
            {
                "interest": "policy",
                "title": f"regulation {i}",
                "url": f"https://example.com/p{i}",
                "publishedAt": f"2026-09-16T{i % 24:02d}:00:00+00:00",
            }
            for i in range(50)
        ]
        # One interest from each of the other four themes.
        for interest in ("models", "chips", "safety", "products"):
            articles.append(
                {
                    "interest": interest,
                    "title": interest,
                    "url": f"https://example.com/{interest}",
                    "publishedAt": "2026-09-16T12:00:00+00:00",
                }
            )

        chosen = self.tech.select(articles)
        themes = Counter(
            self.tech.BUCKETS.get(str(a.get("interest")), "industry") for a in chosen
        )

        self.assertLessEqual(themes["industry"], self.tech.MAX_PER_INTEREST)
        self.assertLessEqual(len(chosen), self.tech.MAX_STORIES)
        # And the quiet themes are not crowded out by the loud one.
        for theme in ("ai", "infrastructure", "safety", "products"):
            with self.subTest(theme=theme):
                self.assertEqual(themes[theme], 1)

    def test_every_interest_belongs_to_a_theme_the_walk_visits(self) -> None:
        """An interest with no bucket is silently dropped from the page."""
        for key, _label, _kw in self.tech.INTERESTS:
            with self.subTest(interest=key):
                self.assertIn(key, self.tech.BUCKETS)
                self.assertIn(self.tech.BUCKETS[key], self.tech.BUCKET_ORDER)
        self.assertEqual(len(set(self.tech.BUCKETS.values())), len(self.tech.BUCKET_ORDER))

    def test_junk_filters_deals_and_film(self) -> None:
        junk = [
            "The best VPN deals of the day",
            "Save 40% on this laptop",
            "Sponsored: the future of work",
            "Lionsgate releases a new trailer for Sunrise",
            "Apple shares surge on AI hopes",
        ]
        for title in junk:
            with self.subTest(title=title):
                self.assertTrue(self.tech.is_junk({"title": title}))

    def test_junk_keeps_real_news(self) -> None:
        keep = [
            "Introducing GPT-6 Astra",
            "NVIDIA Vera Rubin NVL72 Delivers Leading Performance",
            "EU regulators ban a model",
            "Anthropic and OpenAI want to embed safety evaluators",
        ]
        for title in keep:
            with self.subTest(title=title):
                self.assertFalse(self.tech.is_junk({"title": title}))

    def test_interests_are_ordered_technical_first(self) -> None:
        """The round-robin walks this order, so the order is the editorial call."""
        keys = [key for key, _label, _kw in self.tech.INTERESTS]
        self.assertEqual(keys[0], "models")
        self.assertEqual(keys[-1], "policy")

    def test_keywords_avoid_landing_inside_unrelated_words(self) -> None:
        """Plain substring matching, so a short keyword is a false positive."""
        for text, wrong in (
            ("The flaw in the urban museum budget", "policy"),
            ("A flawless urban planning tool", "policy"),
        ):
            with self.subTest(text=text):
                key, _label, _matched = self.tech.interest_for(text, None)
                self.assertNotEqual(key, wrong)


if __name__ == "__main__":
    unittest.main(verbosity=2)
