#!/usr/bin/env python3
"""
test_pipeline.py — tests for the shared modules.

    python3 test_pipeline.py

These cover the plumbing every step depends on: timestamp parsing, the publisher
allowlist, token matching, and the two beat scripts' filters. Nothing here touches
the network, so it runs in under a second.

Why this file exists: every bug in this pipeline that survived to the page was
found by accident, reading output that looked subtly wrong. The publisher allowlist
rejected the Associated Press for a week; it accepted Armenpress because "npr" is a
substring of "a-r-m-e-n-p-r-e-s-s"; Kauno diena's dates silently became None;
``newest_first`` was called as a statement and did nothing; a place-name list with
an unanchored "trak" classified an Indian amusement park as Lithuanian news. Each
of those is one assertion away from being caught on the spot, and several were
fixed and immediately re-broken while fixing something adjacent.
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import datetime, timezone
import json

from common import dedupe_by_url, newest_first, parse_timestamp
from publishers import Allowlist, forms, normalise  # pyright: ignore[reportMissingImports]
from textutil import ordinal, shared_words, significant_tokens


def must(stamp: datetime | None) -> datetime:
    """Narrow a parsed timestamp for the type checker, failing loudly if absent.

    unittest's assertIsNotNone does not narrow for pyright, so every `got.year`
    after one was reported as an attribute of None.
    """
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
        """Kauno diena sends Lithuanian weekday and month names.

        email.utils reads these as English and rejects them, so every date from
        that feed silently became None — the items were then kept as "undated" and
        sorted arbitrarily. The day is parsed too but only the date matters here.
        """
        got = must(parse_timestamp("Ant, 15 Rug 2026 23:44:00 EEST"))
        self.assertEqual((got.year, got.month, got.day), (2026, 9, 15))

    def test_lithuanian_rug_is_september_not_august(self) -> None:
        """Lithuanian abbreviates Rugpjūtis (Aug) and Rugsėjis (Sep) alike.

        Mapping "rug" to August would date every Kauno diena item a month out, so
        the observed evidence decides it: items carrying "Rug" were a day old in
        mid-September.
        """
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
    """It RETURNS a sorted list; it does not sort in place.

    Called as a bare statement it does nothing at all, which is exactly what
    happened in both beat scripts: the list stayed in collection order — every
    direct feed before every Google item — so "freshest first" was really
    "whichever feed was fetched first", and LRT kept losing its slot on the
    Lietuva page because of it.
    """

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
    """Google credits one outlet under several names across items.

    These assert on forms(), not normalise(). A name with two words has two
    accepted canonical spellings — sorted and unsorted — because each covers a
    shape the other misses, so there is no single answer to compare against.
    """

    def test_every_spelling_of_one_outlet_agrees(self) -> None:
        for spelling in ("AP News", "apnews.com", "AP-News"):
            with self.subTest(spelling=spelling):
                self.assertIn("apnews", forms(spelling), "all reduce to apnews")

    def test_a_reversed_domain_reaches_the_same_forms(self) -> None:
        """The bug that dropped 105 Yahoo items a run.

        A domain puts the subdomain first, so the words arrive in the opposite
        order from the outlet's own name. These two do not reduce to the SAME set
        — the two-word name has both spellings and the domain has one — so the
        assertion is that they share a spelling, which is what matching uses, and
        that a gate built from either admits both.
        """
        shared = forms("Yahoo Sports") & forms("sports.yahoo.com")
        self.assertIn("sportsyahoo", shared)

        gate = Allowlist({"yahoo sports"})
        self.assertIn("Yahoo Sports", gate)
        self.assertIn("sports.yahoo.com", gate)
        self.assertIn("ca.sports.yahoo.com", gate)

    def test_a_name_split_differently_reaches_the_same_forms(self) -> None:
        """The case sorting alone breaks, and the reason both forms are needed.

        KaunoDiena arrives as one word and "kauno diena" as two, so there is
        nothing for the sorted form to reorder — only the unsorted concatenation
        connects them.
        """
        self.assertIn("kaunodiena", forms("kauno diena"))
        self.assertIn("kaunodiena", forms("KaunoDiena.lt"))

    def test_strips_the_domain_suffix(self) -> None:
        self.assertIn("si", forms("si.com"))
        self.assertIn("vz", forms("vz.lt"))

    def test_a_betting_site_reduces_to_nothing_trusted(self) -> None:
        gate = Allowlist({"sports illustrated", "yahoo sports"}, exact={"si.com"})
        self.assertNotIn("sportsbook.fanduel.com", gate)

    def test_diacritics_fold_rather_than_split(self) -> None:
        """Lithuanian names carry letters that are not a-z.

        Unhandled, ž splits "Verslo žinios" into "verslo" and "inios", and the
        canonical form becomes a rearrangement of half a name.
        """
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
        """The Armenpress bug.

        "npr" is a substring of "a-r-m-e-n-p-r-e-s-s", so substring matching put an
        Armenian wire service on the Lithuanian page. Names under five characters
        are matched whole.
        """
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
        """Headlines about one running story seldom repeat a word exactly.

        Exact matching missed the same ferry disaster across three days —
        "Indonesian ferry capsizes" against "off Indonesia".
        """
        self.assertTrue(shared_words({"houthi"}, {"houthis"}))
        self.assertTrue(shared_words({"indonesia"}, {"indonesian"}))

    def test_shared_words_ignores_short_prefixes(self) -> None:
        self.assertFalse(shared_words({"war"}, {"warsaw"}))


class TestLietuvaFilters(unittest.TestCase):
    """Import here, not at module scope: these pull in requests."""

    @classmethod
    def setUpClass(cls) -> None:
        import fetch_lietuva  # pyright: ignore[reportMissingImports]

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
        """The amusement-park bug.

        "trak" stands for Trakai. Unanchored it matches inside A-trak-cionas, so a
        story about a scalp injury at an amusement park in Maharashtra counted as
        Lithuanian news and led the page.
        """
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
    """The fallback grouping, by shared title words.

    These call group_by_tokens, not group_topics: the latter asks the model, and a
    test suite that needs a network and an API key is a test suite that gets
    skipped. Same-event judgement across languages lives in the prompt; what is
    testable here is the word-matching fallback and the guards around it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import fetch_lietuva  # pyright: ignore[reportMissingImports]

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
        """The union bug.

        Topics used to union their tokens as they filled, so every admission
        loosened the match: the first card became LRT and Delfi on a drone incident
        plus lrytas.lt on a broken flagpole, presented as one event covered three
        ways.
        """
        groups = self.lt.group_by_tokens(
            [
                {"title": "Numušus bepilotį Lietuvoje"},
                {"title": "Vilniuje nulaužtas Lietuvos vėliavos kotas"},
                {"title": "Naujas restoranas Kaune atidarytas"},
            ]
        )
        self.assertEqual(len(groups), 3, "unrelated headlines must not merge")

    def test_cross_language_duplicates_stay_apart_for_the_fallback(self) -> None:
        """Documents the limit that put the model in charge.

        The word matcher cannot see these as one event — they share nothing — which
        is why one drone incident appeared as four cards until group_topics sent
        the whole list to the model instead. This test records the reason rather
        than the behaviour we want, so nobody "fixes" the prompt back out.
        """
        groups = self.lt.group_by_tokens(
            [
                {"title": "Numušus bepilotį, Lietuva ir Lenkija žada sutelkti pajėgas"},
                {"title": "NATO jets shoot down a drone in Lithuanian airspace"},
            ]
        )
        self.assertEqual(len(groups), 2, "word matching cannot join these")

    def test_grouping_output_budget_scales_with_input(self) -> None:
        """The bug that made every run fall back silently.

        The prompt asks for one entry per headline. At 600 candidates with a fixed
        1500-token budget the reply truncated, the json never parsed, and grouping
        quietly reverted to word matching — which is why the page showed one drone
        incident four times while the console said "model unavailable".
        """
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
        import fetch_sports  # pyright: ignore[reportMissingImports]

        cls.sp = fetch_sports

    def test_specific_beats_the_general(self) -> None:
        """Sabonis is NBA · Lietuva, not Basketball."""
        key, _label, matched = self.sp.interest_for(
            "Domantas Sabonis trade talks", "basketball"
        )
        self.assertEqual(key, "lt_player")
        self.assertTrue(matched)

    def test_falls_back_when_nothing_matches(self) -> None:
        """No keyword, so the feed's own fallback applies and matched is False.

        Careful with the fixture: "Some NBA roundup" looks like it should match
        nothing, but "nba" IS the keyword for the NBA interest, so it reports a
        real match. The flag means "the text named this interest", not "the tag
        happens to equal the fallback".
        """
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
        """A feed too broad to assume anything returns no fallback.

        15min covers all Lithuanian sport; left to fall back it labelled a Kaunas
        football club's coaching change as EuroLeague · Žalgiris.
        """
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
        import fetch_sports  # pyright: ignore[reportMissingImports]

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
        """The deliberate gap.

        "Basketball" and "Krepšinis · Lietuva" are the international and
        national-team beats. Filing them under NBA or EuroLeague would stamp a
        competition label on a story that is not about that competition, so they
        carry an empty league and show under ALL only.
        """
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


class TestStatsHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import fetch_stats  # pyright: ignore[reportMissingImports]

        cls.fs = fetch_stats

    def test_standings_entries_is_case_insensitive(self) -> None:
        """Jolpica capitalises its keys, and a miss returns nothing.

        The first version of fetch_stats.py looked up "driverStandings" against a
        payload keyed "DriverStandings". The lookup returned an empty list rather
        than raising, so both championship tables vanished from the page while the
        race result beside them rendered perfectly — a failure with no error in
        it, found only by counting the panels the run produced.
        """
        lists = [
            {
                "season": "2026",
                "round": "18",
                "DriverStandings": [{"position": "1"}, {"position": "2"}],
            }
        ]
        self.assertEqual(len(self.fs.standings_entries(lists, "driver")), 2)
        # And the older/lowercase spelling still works, in case it changes back.
        lowercase = [{"driverstandings": [{"position": "1"}]}]
        self.assertEqual(len(self.fs.standings_entries(lowercase, "driver")), 1)

    def test_standings_entries_returns_nothing_for_a_miss(self) -> None:
        """Not an exception, so callers must check the length themselves."""
        self.assertEqual(self.fs.standings_entries([{"DriverStandings": []}], "driver"), [])
        self.assertEqual(self.fs.standings_entries([], "driver"), [])
        self.assertEqual(
            self.fs.standings_entries([{"DriverStandings": []}], "constructor"), []
        )

    def test_news_panels_name_a_league_the_page_tags_stories_with(self) -> None:
        """A mistyped tag here is a panel that silently never appears.

        The EuroLeague news panel matches stories by their `league`, so a tag that
        no story carries produces an empty list, then no panel, then no error —
        the same quiet-failure shape as the standings lookup.
        """
        import fetch_sports  # pyright: ignore[reportMissingImports]

        known = set(fetch_sports.LEAGUES.values())
        self.assertTrue(known, "LEAGUES must not be empty for this to mean anything")
        for league, label, note in self.fs.NEWS_PANELS:
            with self.subTest(league=league):
                self.assertIn(league, known)
                self.assertTrue(label, "a panel needs a name to print")
                self.assertTrue(note, "the note is what explains the missing half")

    def test_stat_or_dash_hides_an_offseason_zero(self) -> None:
        """A team that has played no games must not read "OPP PPG 0.0".

        In September every average comes back as "0.0", which is a real and
        alarming-looking statistic rather than an absence.
        """
        for blank in ("0", "0.0", "0.00", "", None, "—", "-", "  "):
            with self.subTest(blank=blank):
                self.assertEqual(self.fs._stat_or_dash(blank), "—")

    def test_stat_or_dash_keeps_a_real_value(self) -> None:
        for value in ("119.0", "48.4", "292", "1:34:23.754", "+4.351"):
            with self.subTest(value=value):
                self.assertEqual(self.fs._stat_or_dash(value), value)

    def test_ordinal_suffix_covers_the_teens(self) -> None:
        """11th, 12th and 13th are the cases a `% 10` table alone gets wrong."""
        expected = {
            1: "st", 2: "nd", 3: "rd", 4: "th", 10: "th",
            11: "th", 12: "th", 13: "th",
            21: "st", 22: "nd", 23: "rd", 30: "th",
            111: "th", 112: "th", 113: "th", 121: "st",
        }
        for number, suffix in expected.items():
            with self.subTest(number=number):
                self.assertEqual(self.fs._ordinal_suffix(number), suffix)

    def test_conference_names_are_short_enough_not_to_wrap(self) -> None:
        """The spotlight column is 96px; "1st Western Conference" took two lines."""
        self.assertEqual(
            self.fs.SHORT_CONFERENCE.get("Western Conference"), "West"
        )
        self.assertEqual(
            self.fs.SHORT_CONFERENCE.get("Eastern Conference"), "East"
        )


class TestBundleEscaping(unittest.TestCase):
    """The two escapes standing between a headline and a broken paper.

    Both guard the same failure: an HTML parser ends a <script> element at the
    first `</script`, wherever it appears — inside a string, inside a comment,
    inside JSON. A headline containing that sequence would truncate the page
    there, and because it happens in the bundle rather than the source, it would
    happen only on the phone.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import bundle  # pyright: ignore[reportMissingImports]

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
        """A variable font is served once and referenced per weight.

        Inlining block by block wrote the same base64 five times over: twenty
        blocks, six files, and half of a 2.4MB bundle was one font saying the same
        thing at five weights.
        """
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
        """Same file, different unicode ranges — latin against latin-ext.

        Folding those would give one subset the other's range, and on this paper
        that means the Lithuanian pages losing their diacritics.
        """
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
        import telegram  # pyright: ignore[reportMissingImports]

        cls.tg = telegram

    def test_caption_says_what_the_paper_is(self) -> None:
        text = self.tg.caption()
        self.assertIn("in the briefing", text)
        self.assertLessEqual(len(text), self.tg.CAPTION_LIMIT)

    def test_stale_is_announced_before_anything_else(self) -> None:
        """The point of the message is being readable without opening it.

        Learning that the run failed later, from a date under the masthead, is
        learning it in the wrong order.
        """
        fresh = self.tg.caption()
        stale = self.tg.caption(stale=True)
        self.assertTrue(stale.startswith("YESTERDAY'S ISSUE"))
        self.assertGreater(len(stale), len(fresh))
        self.assertLessEqual(len(stale), self.tg.CAPTION_LIMIT)

    def test_the_document_is_not_also_named_in_the_form_fields(self) -> None:
        """A string `document` field is read as a URL, not as the upload.

        Sending the paper failed with "failed to get HTTP URL content" — an error
        about a URL nobody sent — because the filename was in `data` as well as in
        the multipart part. Telegram reads the string, ignores the upload beside
        it, and goes looking for that URL.
        """
        fields = self.tg.document_fields("123")
        self.assertNotIn("document", fields)
        self.assertEqual(fields.get("chat_id"), "123")


class TestTechBeat(unittest.TestCase):
    """The Tech page was rebuilt around one complaint, and these hold it up.

    It read the world roster and returned "Congress is under pressure to act on
    AI" and "Sanders and Bannon warn of AI dangers" — twenty-four stories about
    AI, two of them about a thing anybody had made. The fix is a beat with its own
    sources and a round-robin that no single angle can take.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import fetch_tech  # pyright: ignore[reportMissingImports]

        cls.tech = fetch_tech

    def test_a_labs_announcement_outranks_reporting(self) -> None:
        release = {"source": "OpenAI", "title": "Introducing GPT-6 Astra"}
        report = {"source": "Ars Technica", "title": "OpenAI announces GPT-6 Astra"}
        self.assertLess(
            self.tech.source_tier(release), self.tech.source_tier(report)
        )

    def test_reporting_outranks_a_labs_marketing(self) -> None:
        """The finding that took two runs to see.

        Ordered the other way, "Helping older adults use AI in everyday life" beat
        Ars Technica for a slot — because it came from the lab, which looked like
        primary sourcing and was really a publishing cadence. A lab's marketing
        is neither news about something that exists nor independent.
        """
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

    def test_round_robin_stops_one_interest_taking_the_page(self) -> None:
        """The complaint, as a test.

        Fifty stories on one interest against one each on six others. Flat ranking
        by recency returns fifty of the first and none of the rest, which is
        precisely how this page came to be about legislation.
        """
        articles: list[dict[str, object]] = [
            {
                "interest": "policy",
                "title": f"regulation {i}",
                "url": f"https://example.com/p{i}",
                "publishedAt": f"2026-09-16T{i % 24:02d}:00:00+00:00",
            }
            for i in range(50)
        ]
        for key in ("models", "chips", "research", "safety", "products", "business"):
            articles.append(
                {
                    "interest": key,
                    "title": key,
                    "url": f"https://example.com/{key}",
                    "publishedAt": "2026-09-16T12:00:00+00:00",
                }
            )

        chosen = self.tech.select(articles)
        counts = Counter(str(a.get("interest")) for a in chosen)

        self.assertLessEqual(counts["policy"], self.tech.MAX_PER_INTEREST)
        self.assertLessEqual(len(chosen), self.tech.MAX_STORIES)
        # And the quiet interests are not crowded out by the loud one.
        for key in ("models", "chips", "research", "safety", "products", "business"):
            with self.subTest(interest=key):
                self.assertEqual(counts[key], 1)

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
        """Plain substring matching, so a short keyword is a false positive.

        "law" matches "flaw", "ban" matches "urban", "eu" matches "museum".
        """
        for text, wrong in (
            ("The flaw in the urban museum budget", "policy"),
            ("A flawless urban planning tool", "policy"),
        ):
            with self.subTest(text=text):
                key, _label, _matched = self.tech.interest_for(text, None)
                self.assertNotEqual(key, wrong)


if __name__ == "__main__":
    unittest.main(verbosity=2)
