#!/usr/bin/env python3
"""
fetch_coverage.py — Step 5 of the daily news pipeline.

For every high-significance story, gather multi-outlet coverage per political
lean and attach it as `coverage: {left: [...], center: [...], right: [...]}`.

Two sources, merged per lean and de-duplicated by URL:

  * RSS (default) — a fixed roster of 2 left / 2 centre / 2 right outlets, each
    read via several desk feeds and matched locally on keywords. No key, no quota,
    no rate limiting, no domain guessing, and no third-party index deciding what
    exists. This is what makes the Guardian and the NYT reachable at all, both of
    which NewsAPI does not index.
  * NewsAPI (optional) — off by default. Keyword queries restricted to lean
    domains, which adds breadth beyond the fixed roster. Turn on with USE_NEWSAPI.

Feeds are fetched once per run, not once per story, so adding stories is free.

Nothing is fabricated — a lean with no hits gets an empty list.

Set USE_NEWSAPI = True (plus NEWSAPI_KEY) to widen coverage beyond the roster.

Input:  data/world_clustered.json if it exists, else data/world_filtered.json
Output: data/world_covered.json  (input articles, high-significance ones enriched)

Requires NEWSAPI_KEY when USE_NEWSAPI. Keyword extraction uses DEEPSEEK_API_KEY
(optional — falls back to a stopword heuristic if unset or unavailable).

Usage:
    export NEWSAPI_KEY="..."
    python fetch_coverage.py                      # normal run
    python fetch_coverage.py --check-domains      # probe each configured domain
    python fetch_coverage.py --list-sources       # dump every available source
    python fetch_coverage.py --find-domain NAME   # look up a domain string
    python fetch_coverage.py --probe D1 [D2 ...]  # test arbitrary domain strings
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

try:
    import requests
except ImportError:
    sys.exit(
        "ERROR: missing dependency 'requests'. Install it with:\n"
        "    pip install -r requirements.txt"
    )

# Sibling modules. Python puts this script's own directory on sys.path; pyright
# analyses each file standalone and cannot see them, hence the directives.
from briefing import (  # pyright: ignore[reportMissingImports]
    assign_briefing_topics,
    briefing_candidates,
    dedupe_briefing_coverage,
    report_briefing,
    select_briefing,
)
from common import (  # pyright: ignore[reportMissingImports]
    dedupe_by_url,
    fail as _fail,
    first_existing,
    load_json,
    save_json,
)
from llm import get_api_key  # pyright: ignore[reportMissingImports]
from rss_feeds import fetch_many  # pyright: ignore[reportMissingImports]
from textutil import (  # pyright: ignore[reportMissingImports]
    STOPWORDS,
    significant_tokens,
    title_overlap,
)


def fail(message: str) -> NoReturn:
    """Exit with an error message.

    A local wrapper rather than a direct import, because pyright cannot see
    through the suppressed sibling import: `common.fail`'s NoReturn is invisible
    to it, so this function "returns None" and every `if bad: fail(...)` leaves
    the code after it "possibly unbound" — 20-odd spurious errors. The trailing
    raise is unreachable; it exists so the declared NoReturn is true to the type
    checker.
    """
    _fail(message)
    raise SystemExit(1)

# --- Configuration -----------------------------------------------------------

NEWSAPI_URL = "https://newsapi.org/v2/everything"
NEWSAPI_SOURCES_URL = "https://newsapi.org/v2/top-headlines/sources"
NEWSAPI_KEY_ENV = "NEWSAPI_KEY"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_MODEL = os.environ.get("COVERAGE_MODEL", "deepseek-flash")

# Pipeline order is: fetch_world -> cluster_stories -> classify_articles ->
# track_stories -> here. So the TRACKED file is preferred: it is the classified
# set plus each story's `history` block, which is what the developing cue needs.
INPUT_CANDIDATES = (
    Path("data/world_tracked.json"),  # preferred: classified AND tracked
    Path("data/world_filtered.json"),  # fallback: no history, cue degrades to hidden
    Path("data/world_clustered.json"),  # fallback: unclassified
    Path("data/world_raw.json"),  # fallback: unclustered
)
OUTPUT_PATH = Path("data/world_covered.json")

LOOKBACK_HOURS = 48  # how far back to search for coverage
ARTICLES_PER_LEAN = 10  # NewsAPI pageSize per lean query
REQUEST_TIMEOUT = 30.0  # seconds per call
QUERY_DELAY = 1.0  # polite gap between NewsAPI calls (free tier is rate limited)
KEYWORD_MAX_TOKENS = 100  # the reply is a tiny JSON object
MAX_QUERY_TERMS = 2  # NewsAPI ANDs bare terms, so keep the required set tiny
DIAGNOSE_EMPTY_LEANS = True  # +1 count-only query per lean that returns nothing
DIAGNOSTIC_LOW_VOLUME_MAX = 10  # below this many total matches, call it low volume
# If more than this fraction of lean queries fail, the run is degraded (usually
# rate limiting). We then preserve the existing output and write a .partial file
# instead, so a bad run can never clobber a good one.
MAX_FAILED_LEAN_FRACTION = 0.5
DOMAIN_PROBE_HOURS = 24 * 7  # window used by --check-domains
# Below this many articles per week (~7/day), a domain will usually read 0 for
# any specific story query, so treat it as effectively dead for coverage.
DOMAIN_PROBE_MIN_ARTICLES = 50

LEANS = ("left", "center", "right")

# --- Source toggles ----------------------------------------------------------
USE_RSS = True  # primary: a fixed outlet roster, matched locally, free + unmetered
# Optional breadth from NewsAPI keyword queries. Off by default: RSS found strictly
# more, needs no key, and cannot be rate limited. Turn on to widen the net.
USE_NEWSAPI = False

RSS_WORKERS = 8  # parallel feed fetches; feeds are independent hosts
RSS_TIMEOUT = 20.0  # seconds per feed
MAX_RSS_HITS_PER_LEAN = 2  # frames kept per lean; the card shows at most this

# How many content words a candidate headline must share with the story headline.
#
# Matching on the primary term alone was far too loose: a story about Israel's
# destruction of south Lebanon pulled in "UK bans goods from Israeli settlements"
# and "Israel's E1 plan explained", because every one of them contains "Israel".
# Requiring ALL terms instead was far too strict, dropping 75% of frames and
# emptying six stories completely — a legitimate Ukraine drone story lost six of
# its seven frames to "drone strikes" not being verbatim in the headline.
#
# Sharing two content words with the story headline is the calibrated middle:
# it drops the West Bank stories while keeping every story covered at least once.
MIN_TITLE_OVERLAP = 2
# How many high-significance stories to gather coverage for. Classification now
# yields ~66 high-significance stories a day, and fetching coverage for every one
# produces ~2000 items — far more than a briefing can use. Raised this if you
# want the full set.
MAX_STORIES_FOR_COVERAGE = 20

# ---------------------------------------------------------------------------
# Briefing selection — which covered stories are worth showing, in what order.
#
# Ranking by importance alone filled four of the five slots with one Iran-Saudi
# escalation: a base attack, a pipeline attack, a ship strike and a summit. Each
# was genuinely distinct and genuinely significant; together they were one event
# monopolising the page. Selection still favours importance, but caps how many
# slots any single topic may take.
#
# Recomputed here rather than in the UI so render.js and write_desk.py read the
# same answer instead of each re-implementing the rule and drifting apart.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RSS OUTLET ROSTER — the fixed set we read every day.
#
# A constant roster keeps the left/centre/right balance predictable day to day.
# RSS also removes the API key, the 100/day quota, the 429s and the domain-string
# guessing in one move.
#
# REPUTABLE SOURCES ONLY: established newsrooms with published editorial
# standards and a corrections practice. Aggregators, tabloids, advocacy outlets
# and opinion magazines are excluded on purpose even where their feeds work:
# Breitbart, the Daily Caller and the Daily Signal (advocacy), the New York Post
# (tabloid), HuffPost (aggregator), National Review and Reason (opinion).
#
# The right bucket is thin as a result, and that is a finding rather than a gap
# in the config — see the note on it below.
#
# Each outlet lists several desk feeds, so a story is covered regardless of which
# section it lands in (world, sport, business). Every URL below was fetched live
# and confirmed to return items.
#
# One exception: Reuters has no public feed, so it goes through Google News RSS.
# See the note on that entry — its links are Google redirects, not reuters.com.
#
# Lean ratings are approximate, drawn from commonly published bias references.
# Move outlets between buckets freely.
# ---------------------------------------------------------------------------
RSS_OUTLETS: dict[str, dict[str, list[str]]] = {
    "left": {
        "The New York Times": [
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        ],
        "The Washington Post": [
            "https://feeds.washingtonpost.com/rss/world",
            "https://feeds.washingtonpost.com/rss/national",
        ],
        "The Guardian": [
            "https://www.theguardian.com/world/rss",
            "https://www.theguardian.com/us-news/rss",
        ],
        "NPR": [
            "https://feeds.npr.org/1001/rss.xml",
        ],
        "ProPublica": [
            "https://www.propublica.org/feeds/propublica/main",
        ],
        "The Atlantic": [
            "https://www.theatlantic.com/feed/all/",
        ],
    },
    "center": {
        # Reuters publishes no usable RSS: feeds.reuters.com no longer resolves,
        # and reuters.com answers automated requests with 401/404. Google News RSS
        # bridges it and returns real Reuters headlines attributed to Reuters, but
        # note the trade-off: article links are news.google.com redirects rather
        # than canonical reuters.com URLs. They do resolve when followed.
        "Reuters": [
            "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US:en",
        ],
        # AP publishes no public feed either (apnews.com answers 403). Same Google
        # News bridge, same redirect-link caveat as Reuters.
        "AP News": [
            "https://news.google.com/rss/search?q=site%3Aapnews.com&hl=en-US&gl=US&ceid=US:en",
        ],
        "BBC News": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.bbci.co.uk/sport/rss.xml",
            "https://feeds.bbci.co.uk/news/business/rss.xml",
        ],
        "The Economist": [
            "https://www.economist.com/the-world-this-week/rss.xml",
        ],
        "Financial Times": [
            "https://www.ft.com/world?format=rss",
        ],
        "Al Jazeera": [
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
        "Le Monde": [
            "https://www.lemonde.fr/en/rss/une.xml",
        ],
        "The Straits Times": [
            "https://www.straitstimes.com/news/world/rss.xml",
        ],
    },
    # The thin bucket, and structurally so rather than by choice: the mainstream
    # right-leaning newsrooms publish no usable free feed. The Telegraph returns
    # 402 (paywall), The Times 404, The Australian and the Washington Examiner 403,
    # and the Wall Street Journal has no public RSS at all. What is left is Fox
    # News' news operation plus two smaller but properly edited newsrooms.
    "right": {
        "Fox News": [
            "https://moxie.foxnews.com/google-publisher/latest.xml",
            "https://moxie.foxnews.com/google-publisher/world.xml",
            "https://moxie.foxnews.com/google-publisher/sports.xml",
        ],
        "The Dispatch": [
            "https://thedispatch.com/feed/",
        ],
        "National Post": [
            "https://nationalpost.com/feed",
        ],
    },
}

# ---------------------------------------------------------------------------
# OUTLET DOMAINS — edit freely; these are a starting point, not an endorsement.
#
# Grouped per commonly published media-bias references (AllSides / Ad Fontes
# style ratings). Swap in whichever outlets you actually want to hear from.
#
# Bare domains only — no scheme, no "www.", no path. Run --list-sources to see
# every outlet NewsAPI exposes, and --check-domains to confirm yours work.
# ---------------------------------------------------------------------------
OUTLETS_BY_LEAN: dict[str, list[str]] = {
    # Every domain below was probed live and returns >=50 articles/week, which is
    # roughly the floor for ever matching a specific story in a 48h window.
    #
    # Adding domains here is FREE: they are OR'd into a single `domains` param,
    # so a lean costs one request no matter how many outlets it lists.
    #
    # NewsAPI's index is not the obvious one. Confirmed dead (0 results/week):
    # nytimes.com, theguardian.com, reuters.com, msnbc.com, us.cnn.com,
    # nationalreview.com, washingtontimes.com, pjmedia.com, bloomberg.com.
    # Never trust /sources' domain strings — always re-check with --check-domains.
    #
    # Trailing comment = measured articles/week and approximate bias rating.
    "left": [
        "huffpost.com",  # 293   Left
        "latimes.com",  # 291   Lean Left
        "npr.org",  # 269   Lean Left
        "nbcnews.com",  # 145   Lean Left
        "cnn.com",  # 107   Lean Left
    ],
    "center": [
        "usatoday.com",  # 3196  Center
        "bbc.co.uk",  # 1149  Center
        "time.com",  # 70    Center
        "apnews.com",  # 50    Center
    ],
    # Note: NewsAPI's high-volume right-leaning options skew further right than
    # the left/centre buckets, because the mainstream-right outlets (National
    # Review, Washington Times, WSJ) are all absent from the index. Trim to taste.
    "right": [
        "nypost.com",  # 1674  Lean Right
        "foxnews.com",  # 712   Right
        "breitbart.com",  # 374   Right, further
        "dailycaller.com",  # 151   Right, further
        "reason.com",  # 122   Libertarian / Lean Right
        "dailysignal.com",  # 103   Right
    ],
}

# Used by the offline keyword fallback.


# --- Helpers -----------------------------------------------------------------


def get_newsapi_key() -> str:
    """Read the NewsAPI key from the environment, or exit with a hint."""
    key = os.environ.get(NEWSAPI_KEY_ENV, "").strip()
    if not key:
        fail(
            f"{NEWSAPI_KEY_ENV} is not set. Get a free key at https://newsapi.org "
            "and then run:\n"
            f'    export {NEWSAPI_KEY_ENV}="your_key_here"'
        )
    return key


# --- Keyword extraction ------------------------------------------------------


def fallback_terms(title: str) -> list[str]:
    """Crude offline keyword extraction: drop stopwords, prefer proper nouns."""
    cleaned = re.sub(r"\s+[-–—|]\s+[^-–—|]+$", "", title)  # drop " - Outlet" tail
    words = re.findall(r"[A-Za-z][A-Za-z'\-]+", cleaned)
    keep = [w for w in words if w.lower() not in STOPWORDS and len(w) > 2]
    proper = [w for w in keep if w[0].isupper()]
    return (proper or keep)[:MAX_QUERY_TERMS]


def deepseek_terms(
    session: requests.Session, api_key: str, title: str, description: str | None
) -> list[str] | None:
    """Ask DeepSeek for the most distinctive terms. Returns None on any failure."""
    system = (
        "You pick search terms for finding news coverage of a story.\n\n"
        "Given a headline, reply with a single json object containing one key "
        '"terms" whose value is a list of at most 2 of the most distinctive '
        "search terms — proper nouns, named entities, places.\n\n"
        "Fewer terms means more matches, so choose only the two that best "
        "identify this specific story. Prefer named entities over generic "
        "words. No boolean operators, no quotes.\n\n"
        'Example reply: {"terms": ["Houthis", "Bab el-Mandeb"]}'
    )
    user = f"Headline: {title}\nSummary: {(description or '').strip() or '(none)'}"

    payload = {
        "model": DEEPSEEK_MODEL,
        "max_tokens": KEYWORD_MAX_TOKENS,
        # temperature 0 so the same story yields the same terms on every run. At
        # the default the model returned ["Ben Shelton","US Open"] on one run and
        # ["Ben Shelton","Alexander Zverev"] on the next, which made coverage
        # counts — and therefore the left/centre/right balance — jitter daily.
        "temperature": 0,
        # Same lesson as classify_articles.py: reasoning tokens are billed
        # against max_tokens and can consume the whole budget, yielding empty
        # content. We only need a few words, so thinking is pure overhead.
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    try:
        response = session.post(
            DEEPSEEK_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return None
        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            return None
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return None
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None

        raw = json.loads(text[start : end + 1]).get("terms", [])
        if isinstance(raw, str):  # tolerate a bare string instead of a list
            raw = [raw]
        terms = [str(t).strip() for t in raw if str(t).strip()]
        return terms[:MAX_QUERY_TERMS] or None
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return None


def format_term(term: str) -> str:
    """Quote multi-word or hyphenated terms so NewsAPI treats them as a phrase."""
    term = term.strip()
    if " " in term or "-" in term:
        return f'"{term}"'
    return term


def build_query(terms: list[str]) -> str:
    """OR-join the terms so any one of them can match.

    NewsAPI ANDs bare space-separated terms, so `Houthis Yemen pipeline`
    requires all three and matches almost nothing. `Houthis OR Yemen` matches
    either, which is what we want when hunting for coverage of one story.
    """
    return " OR ".join(format_term(t) for t in terms[:MAX_QUERY_TERMS])


def extract_terms(
    session: requests.Session,
    deepseek_key: str | None,
    article: dict[str, Any],
) -> list[str]:
    """Best-effort distinctive search terms for a story, preferring the model.

    Returns raw terms, not a query string: NewsAPI needs them OR-joined into its
    own syntax, while RSS matching just wants the plain strings.
    """
    title = (article.get("title") or "").strip()

    terms: list[str] | None = None
    if deepseek_key:
        terms = deepseek_terms(
            session, deepseek_key, title, article.get("description")
        )

    if not terms:
        terms = fallback_terms(title)

    if not terms:
        terms = [title[:60]] if title else []

    return terms


# --- RSS coverage ------------------------------------------------------------

# Feed fetching and parsing live in rss_feeds.py, shared with fetch_world.py.
# This file used to carry its own copy, which silently drifted: it served 140px
# Guardian thumbnails long after the shared module had been fixed to request
# full-size CDN crops.


def fetch_rss_by_lean() -> dict[str, list[dict[str, Any]]]:
    """Fetch every configured feed once, grouped by lean. Never raises.

    Done once per run rather than once per story, so the cost is fixed no matter
    how many stories need coverage.
    """
    out: dict[str, list[dict[str, Any]]] = {lean: [] for lean in LEANS}
    jobs = [
        (lean, outlet, url)
        for lean, outlets in RSS_OUTLETS.items()
        for outlet, urls in outlets.items()
        for url in urls
    ]
    if not jobs:
        return out

    # fetch_many keys its results by URL, so each maps back to its lean.
    by_url = fetch_many([(outlet, url) for _, outlet, url in jobs], workers=RSS_WORKERS)
    for lean, _outlet, url in jobs:
        out[lean].extend(by_url.get(url, []))

    return out


def match_score(item: dict[str, Any], terms: list[str]) -> int:
    """How many of `terms` appear in the item. 0 means no match."""
    haystack = f"{item.get('title') or ''} {item.get('description') or ''}".lower()
    return sum(1 for term in terms if term and term.lower() in haystack)



def rss_coverage(
    items: list[dict[str, Any]], terms: list[str], story_title: str
) -> list[dict[str, Any]]:
    """Feed items that are actually about this story, best-ranked first.

    Three guards, each added because its absence produced visible nonsense:

    1. The title must overlap the story headline by MIN_TITLE_OVERLAP content
       words. Without it, a single broad term like "Israel" matched every Israel
       headline on the wire, so a Lebanon story showed West Bank settlements.
    2. There is no any-term fallback. It once caught a too-narrow primary, but
       it did more harm than good: a Philippines ferry story fell back to
       "Philippines" and pulled in an unrelated naval story. An empty lean is
       honest; a lean padded with another story is misleading.
    3. One frame per outlet, because an outlet filing three times filled the
       whole group with a single perspective.
    """
    if not terms:
        return []

    story_tokens = significant_tokens(story_title)
    hits: list[tuple[int, dict[str, Any]]] = []

    for item in items:
        title = str(item.get("title") or "")
        if title_overlap(title, story_tokens) < MIN_TITLE_OVERLAP:
            continue
        hits.append((match_score(item, terms), item))

    hits.sort(key=lambda pair: -pair[0])

    picked: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for _, item in hits:
        source = str(item.get("source") or "")
        if source in seen_sources:
            continue
        seen_sources.add(source)
        picked.append(item)
        if len(picked) >= MAX_RSS_HITS_PER_LEAN:
            break

    return picked


# --- Coverage fetching -------------------------------------------------------


def summarise_article(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten a raw NewsAPI article to the fields we keep for coverage."""
    return {
        "title": raw.get("title"),
        "description": raw.get("description"),
        "url": raw.get("url"),
        "urlToImage": raw.get("urlToImage"),
        "source": (raw.get("source") or {}).get("name"),
        "publishedAt": raw.get("publishedAt"),
        "via": "newsapi",
    }


def fetch_lean_coverage(
    session: requests.Session,
    api_key: str,
    lean: str,
    query: str,
    domains: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    """Run one lean's query. Never raises.

    Returns (articles, ok). ok=False means the call itself failed, so we cannot
    know whether the story was covered — worth distinguishing from a real zero
    before spending a diagnostic request.
    """
    if not domains:
        return [], False

    params = {
        "q": query,
        "domains": ",".join(domains),
        "from": (
            datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%S"),
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": ARTICLES_PER_LEAN,
    }

    try:
        response = session.get(
            NEWSAPI_URL,
            params=params,
            headers={"X-Api-Key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        print(f"      ! {lean}: request failed: {exc}", file=sys.stderr)
        return [], False

    if response.status_code != 200:
        print(
            f"      ! {lean}: HTTP {response.status_code}: {response.text[:140]}",
            file=sys.stderr,
        )
        return [], False

    try:
        body = response.json()
    except ValueError:
        print(f"      ! {lean}: response was not json", file=sys.stderr)
        return [], False

    if body.get("status") != "ok":
        print(
            f"      ! {lean}: {body.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        return [], False

    results = []
    for raw in body.get("articles", []):
        title = (raw.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue
        results.append(summarise_article(raw))

    return results, True


def count_matches(
    session: requests.Session,
    api_key: str,
    *,
    query: str | None = None,
    domains: list[str] | None = None,
    lookback_hours: int = LOOKBACK_HOURS,
) -> int | None:
    """Count matching articles without downloading them (pageSize=1).

    Reads only `totalResults`, so this is as cheap as a NewsAPI call gets.
    Returns None if the count itself could not be determined.
    """
    params: dict[str, Any] = {
        "from": (
            datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        ).strftime("%Y-%m-%dT%H:%M:%S"),
        "language": "en",
        "pageSize": 1,
    }
    if query:
        params["q"] = query
    if domains:
        params["domains"] = ",".join(domains)

    try:
        response = session.get(
            NEWSAPI_URL,
            params=params,
            headers={"X-Api-Key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return None
        body = response.json()
        if body.get("status") != "ok":
            return None
        total = body.get("totalResults")
        return int(total) if isinstance(total, int) else None
    except (requests.RequestException, ValueError, TypeError):
        return None


def check_domains(api_key: str) -> None:
    """Probe every configured domain individually. Run with --check-domains.

    A domain NewsAPI does not index returns 0 for every query, which silently
    empties a whole lean. Probing each one in isolation says which.
    """
    session = requests.Session()
    window = f"the last {DOMAIN_PROBE_HOURS // 24} days"
    print(f"Probing each configured domain over {window} (no keyword filter)...\n")

    dead: list[tuple[str, str]] = []
    weak: list[tuple[str, str, int]] = []
    for lean in LEANS:
        domains = OUTLETS_BY_LEAN.get(lean, [])
        print(f"{lean}:")
        for domain in domains:
            total = count_matches(
                session, api_key, domains=[domain], lookback_hours=DOMAIN_PROBE_HOURS
            )
            if total is None:
                verdict, shown = "request failed", "?"
            elif total == 0:
                verdict, shown = "NOT INDEXED — dead", "0"
                dead.append((lean, domain))
            elif total < DOMAIN_PROBE_MIN_ARTICLES:
                verdict, shown = "ok but LOW VOLUME — will usually read 0", str(total)
                weak.append((lean, domain, total))
            else:
                verdict, shown = "ok", f"{total}"
            print(f"  {domain:30} {shown:>8}  {verdict}")
            time.sleep(QUERY_DELAY)

        # Does the combined list work, or does one dead entry poison it all?
        combined = count_matches(
            session, api_key, domains=domains, lookback_hours=DOMAIN_PROBE_HOURS
        )
        shown = "?" if combined is None else str(combined)
        print(f"  {'(combined list)':30} {shown:>8}")
        time.sleep(QUERY_DELAY)
        print()

    if dead:
        print("Dead domains — every story will show 0 for these leans:")
        for lean, domain in dead:
            print(f"  {lean}: {domain}")
    if weak:
        print(
            f"\nLow volume (< {DOMAIN_PROBE_MIN_ARTICLES} articles/week) — still fine,"
            " but expect frequent 0s:"
        )
        for lean, domain, total in weak:
            print(f"  {lean}: {domain} ({total})")
    if not dead and not weak:
        print("All configured domains returned healthy results.")
    elif dead:
        print("\nReplace the dead ones with domains that returned results above.")


def strip_domain(url: str) -> str:
    """Turn a source URL into the bare domain NewsAPI's `domains` param expects.

    NewsAPI wants `bbc.co.uk`, not `https://www.bbc.co.uk/news`.
    """
    cleaned = re.sub(r"^https?://", "", (url or "").strip())
    cleaned = re.sub(r"^www\.", "", cleaned)
    return cleaned.split("/")[0].split("?")[0]


def fetch_source_index(api_key: str) -> list[dict[str, Any]]:
    """Fetch NewsAPI's source list (id, name, url) — a single request."""
    try:
        response = requests.get(
            NEWSAPI_SOURCES_URL,
            params={"language": "en"},
            headers={"X-Api-Key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        fail(f"could not reach NewsAPI: {exc}")

    if response.status_code != 200:
        fail(f"source list HTTP {response.status_code}: {response.text[:160]}")

    try:
        body = response.json()
    except ValueError:
        fail("source list response was not json")

    if body.get("status") != "ok":
        fail(f"NewsAPI error: {body.get('message', 'unknown error')}")

    return body.get("sources", [])


def find_domain(api_key: str, needle: str) -> None:
    """Search NewsAPI's own source list for an outlet's canonical domain string."""
    sources = fetch_source_index(api_key)
    needle_l = needle.lower()
    hits = [
        s
        for s in sources
        if needle_l in (s.get("name") or "").lower()
        or needle_l in (s.get("url") or "").lower()
    ]

    if not hits:
        print(
            f"No source matching {needle!r} "
            f"({len(sources)} sources in NewsAPI's index).\n"
            "List everything available with:\n"
            "    python3 fetch_coverage.py --list-sources"
        )
        return

    print(f"{len(hits)} match(es) for {needle!r}:\n")
    for source in sorted(hits, key=lambda s: str(s.get("name") or "")):
        print(
            f"  {str(source.get('name'))[:32]:32} "
            f"{strip_domain(str(source.get('url') or '')):30} "
            f"id={source.get('id')}"
        )

    print("\nConfirm one with:  python3 fetch_coverage.py --probe <domain>")
    print(
        "Note: /sources lists top-headlines publishers, while /v2/everything may\n"
        "index a different set — always confirm with --probe before trusting it."
    )


def list_sources(api_key: str) -> None:
    """Dump every source NewsAPI exposes for your plan.

    Note this covers top-headlines publishers. /v2/everything searches a far
    larger corpus, so this is a menu of known-good domains, not the full set.
    Confirm any domain with --probe.
    """
    sources = fetch_source_index(api_key)
    if not sources:
        print("NewsAPI returned no sources.")
        return

    rows = sorted(
        (
            str(s.get("name") or "?"),
            strip_domain(str(s.get("url") or "")),
            str(s.get("category") or ""),
            str(s.get("country") or ""),
        )
        for s in sources
    )
    width = max(len(r[0]) for r in rows)

    print(f"{len(rows)} sources available:\n")
    for name, domain, category, country in rows:
        print(f"  {name:<{width}}  {domain:<30} {category:<12} {country}")

    print(
        "\nThese are top-headlines publishers. /v2/everything indexes many more,\n"
        "so a domain absent here may still work — verify with --probe."
    )


def probe_domains(api_key: str, domains: list[str]) -> None:
    """Probe arbitrary domain strings against /v2/everything.

    This is the ground truth: a domain that returns 0 here will return 0 for
    every story, silently emptying a lean.
    """
    session = requests.Session()
    window = f"the last {DOMAIN_PROBE_HOURS // 24} days"
    print(f"Probing {len(domains)} domain string(s) over {window}...\n")

    working: list[str] = []
    dead: list[str] = []
    for domain in domains:
        total = count_matches(
            session, api_key, domains=[domain], lookback_hours=DOMAIN_PROBE_HOURS
        )
        if total is None:
            verdict, shown = "request failed", "?"
        elif total == 0:
            verdict, shown = "no results", "0"
            dead.append(domain)
        elif total < DOMAIN_PROBE_MIN_ARTICLES:
            verdict, shown = "WORKS but low volume", str(total)
            working.append(domain)
        else:
            verdict, shown = "WORKS", str(total)
            working.append(domain)
        print(f"  {domain:38} {shown:>8}  {verdict}")
        time.sleep(QUERY_DELAY)

    print()
    if working:
        print("Working — paste into OUTLETS_BY_LEAN:")
        for domain in working:
            print(f"  {domain}")
    if dead:
        print("\nDead — drop or replace:")
        for domain in dead:
            print(f"  {domain}")


def diagnose_empty_lean(
    session: requests.Session, api_key: str, lean: str, query: str
) -> None:
    """Explain a zero-result lean: no coverage, or a query that's too narrow.

    Logs only the unrestricted match count, so this costs one count-only call
    rather than a full article fetch.
    """
    total = count_matches(session, api_key, query=query)

    if total is None:
        print(
            f"      {lean}=0 (0 domain-restricted; total count unavailable)",
            file=sys.stderr,
        )
        return

    if total == 0:
        reading = "query too narrow — no matches anywhere, widen the terms"
    elif total < DIAGNOSTIC_LOW_VOLUME_MAX:
        reading = "genuine low volume"
    else:
        reading = "matches widely — these outlets just didn't cover it"

    print(
        f"      {lean}=0 (0 domain-restricted; {total} total matches exist) "
        f"— {reading}",
        file=sys.stderr,
    )


COVERAGE_SAME_STORY_PROMPT = """\
You check whether candidate articles cover the SAME story as a given headline.

Candidates were matched by shared words, which is not sufficient. A story about
"Ukraine drone strikes kill four" pulls in "Moscow claims Ukraine planned to
start a war"; a story about Israel's destruction of south Lebanon pulls in a
West Bank settlements story. Each pair is about the same war and the same
country. Neither pair is about the same story.

Same story means the same specific event, decision or development: one news item,
not one subject area. Sharing a country, a conflict or a theme is NOT enough.
Differing only in how it is framed or headlined is fine; those ARE the same story.

Reply with a single json object and nothing else:
{"same_story": [2, 5]}
List only the candidate numbers covering the same specific story, in any order.
If none do, return an empty list."""


def parse_indices(text: str, upper_bound: int) -> list[int] | None:
    """Pull a list of 1-based candidate numbers out of a model reply."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    raw = parsed.get("same_story")
    if not isinstance(raw, list):
        return None

    out: list[int] = []
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= upper_bound and number not in out:
            out.append(number)
    return out


def verify_same_story(
    session: requests.Session,
    api_key: str | None,
    story_title: str,
    frames: list[tuple[str, dict[str, Any]]],
) -> list[int]:
    """Which candidate frames cover the same specific story. Never raises.

    Returns 1-based indices into `frames`. On any failure it keeps everything,
    because silently dropping coverage is worse than keeping a loose match.
    """
    everything = list(range(1, len(frames) + 1))
    if not api_key or len(frames) < 2:
        return everything

    listing = [
        f"{number}. [{lean}] {item.get('source')}: {str(item.get('title') or '')[:150]}"
        for number, (lean, item) in enumerate(frames, 1)
    ]
    user = (
        f"Story: {story_title[:200]}\n\n"
        "Candidates:\n" + "\n".join(listing) + "\n\n"
        "Which candidates cover that same specific story?"
    )

    payload = {
        "model": DEEPSEEK_MODEL,
        "max_tokens": 200,
        "temperature": 0,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": COVERAGE_SAME_STORY_PROMPT},
            {"role": "user", "content": user},
        ],
    }

    try:
        response = session.post(
            DEEPSEEK_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            return everything
        choices = response.json().get("choices") or []
        if not choices:
            return everything
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return everything

    if not text:
        return everything

    indices = parse_indices(text, len(frames))
    return everything if indices is None else indices


# --- Output ------------------------------------------------------------------


def report(stories: list[dict[str, Any]]) -> None:
    """Print per-story coverage counts per lean, split by source."""
    print()
    print("=" * 74)
    print("Coverage found per story")
    print("=" * 74)

    grand_total = 0
    for story in stories:
        coverage = story.get("coverage", {})
        counts = ", ".join(f"{lean}={len(coverage.get(lean, []))}" for lean in LEANS)

        items = [i for lean in LEANS for i in coverage.get(lean, [])]
        n_rss = sum(1 for i in items if i.get("via") == "rss")
        grand_total += len(items)

        title = (story.get("title") or "(no headline)").strip()
        print(f"  {title[:66]}")
        print(f"      {counts}")
        print(f"      sources: {n_rss} rss, {len(items) - n_rss} newsapi")
        print(f"      terms: {story.get('search_terms', [])}")

    print()
    print(
        f"{len(stories)} high-significance stories, "
        f"{grand_total} coverage articles found"
    )
    print(f"Written to {OUTPUT_PATH}")


# --- Entry point -------------------------------------------------------------


def run_pipeline() -> None:
    input_path = first_existing(INPUT_CANDIDATES)
    articles = load_json(input_path)
    print(f"Input: {input_path} ({len(articles)} stories)")

    newsapi_key = get_newsapi_key() if USE_NEWSAPI else ""
    session = requests.Session()

    deepseek_key = get_api_key()
    print(f"Keywords: {DEEPSEEK_MODEL if deepseek_key else 'stopword fallback'}")

    active = []
    if USE_RSS:
        n_outlets = sum(len(o) for o in RSS_OUTLETS.values())
        n_feeds = sum(len(u) for o in RSS_OUTLETS.values() for u in o.values())
        active.append(f"RSS ({n_outlets} outlets, {n_feeds} feeds)")
    if USE_NEWSAPI:
        active.append("NewsAPI")
    print(f"Sources: {', '.join(active) or 'none configured'}")

    if not USE_RSS and not USE_NEWSAPI:
        fail("both USE_RSS and USE_NEWSAPI are False — nothing to fetch.")

    high_significance = [
        a
        for a in articles
        if (a.get("classification") or {}).get("significance") == "high"
    ]
    stories = high_significance[:MAX_STORIES_FOR_COVERAGE]

    if not stories:
        print("No high-significance stories — writing input through unchanged.")
        save_json(articles, OUTPUT_PATH)
        print(f"Written to {OUTPUT_PATH}")
        return

    if len(high_significance) > len(stories):
        print(
            f"Covering the top {len(stories)} of {len(high_significance)} "
            f"high-significance stories (MAX_STORIES_FOR_COVERAGE)."
        )

    # Feeds do not depend on the story, so fetch them once for the whole run.
    rss_items: dict[str, list[dict[str, Any]]] = {lean: [] for lean in LEANS}
    if USE_RSS:
        print("Fetching feeds...")
        rss_items = fetch_rss_by_lean()
        summary = "  ".join(f"{lean}={len(rss_items[lean])}" for lean in LEANS)
        print(f"  {summary} items cached")

    print(f"\nGathering coverage for {len(stories)} high-significance stories...")

    attempts = 0
    failures = 0
    for position, story in enumerate(stories, start=1):
        title = (story.get("title") or "(no headline)").strip()
        print(f"\n  [{position}/{len(stories)}] {title[:66]}")

        terms = extract_terms(session, deepseek_key, story)
        query = build_query(terms)
        story["search_terms"] = terms
        story["search_query"] = query
        print(f"      terms: {terms}")

        coverage: dict[str, list[dict[str, Any]]] = {}
        for lean in LEANS:
            newsapi_hits: list[dict[str, Any]] = []
            ok = False
            if USE_NEWSAPI:
                newsapi_hits, ok = fetch_lean_coverage(
                    session, newsapi_key, lean, query, OUTLETS_BY_LEAN.get(lean, [])
                )
                attempts += 1
                if not ok:
                    failures += 1

            rss_hits = (
                rss_coverage(
                    rss_items.get(lean, []), terms, str(story.get("title") or "")
                )
                if USE_RSS
                else []
            )
            merged = dedupe_by_url(newsapi_hits + rss_hits)
            coverage[lean] = merged

            n_rss = sum(1 for i in merged if i.get("via") == "rss")
            n_api = len(merged) - n_rss
            detail = f"{n_rss} rss" + (f" + {n_api} newsapi" if n_api else "")
            print(f"      {lean}: {len(merged)} ({detail})")

            # Only worth asking NewsAPI why, if RSS also found nothing and the
            # query itself succeeded.
            if not merged and USE_NEWSAPI and ok and DIAGNOSE_EMPTY_LEANS:
                time.sleep(QUERY_DELAY)
                diagnose_empty_lean(session, newsapi_key, lean, query)

            if USE_NEWSAPI:
                time.sleep(QUERY_DELAY)

        # Semantic check. Word overlap cannot separate "same war" from "same
        # story": "Ukraine drone strikes kill four" and "Moscow claims Ukraine
        # planned to start a war" share ukraine and russia and nothing else. One
        # judgement covers every candidate for this story.
        frames: list[tuple[str, dict[str, Any]]] = [
            (lean, item) for lean in LEANS for item in coverage.get(lean, [])
        ]
        if frames:
            keep = verify_same_story(
                session, deepseek_key, str(story.get("title") or ""), frames
            )
            if len(keep) < len(frames):
                print(
                    f"      same-story check: kept {len(keep)} of "
                    f"{len(frames)} frames"
                )
            coverage = {lean: [] for lean in LEANS}
            for number in keep:
                lean, item = frames[number - 1]
                coverage[lean].append(item)

        story["coverage"] = coverage

    # Which stories make the page, and in what order. Done once, here, so
    # render.js and write_desk.py read the same answer instead of each
    # re-implementing the rule in their own language.
    #
    # Topic labels come from one call that sees every candidate at once: the
    # per-story classifier cannot be consistent with itself, since it never sees
    # its own previous labels.
    candidates = briefing_candidates(articles)
    assign_briefing_topics(candidates)

    briefing = select_briefing(articles)
    dedupe_briefing_coverage(briefing)
    for rank, story in enumerate(briefing):
        story["briefing_rank"] = rank

    # Never overwrite good output with a degraded run. With RSS as the primary
    # source, NewsAPI failing is only disqualifying if RSS came back empty too.
    rss_total = sum(len(items) for items in rss_items.values())
    degraded = attempts and failures / attempts > MAX_FAILED_LEAN_FRACTION
    if degraded and (not USE_RSS or rss_total == 0):
        partial = OUTPUT_PATH.with_suffix(".partial.json")
        save_json(articles, partial)
        print(
            f"\nWARNING: {failures}/{attempts} NewsAPI queries failed and RSS "
            "returned nothing — the run is degraded (usually rate limiting).\n"
            f"Left the existing {OUTPUT_PATH} untouched and wrote this run to\n"
            f"{partial} instead."
        )
        report(stories)
        return

    save_json(articles, OUTPUT_PATH)
    report(stories)
    report_briefing(briefing)


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        run_pipeline()
        return

    api_key = get_newsapi_key()
    command, rest = argv[0], argv[1:]

    if command == "--check-domains":
        check_domains(api_key)
    elif command == "--list-sources":
        list_sources(api_key)
    elif command == "--probe":
        if not rest:
            fail(
                "--probe needs at least one domain, e.g.\n"
                "    python3 fetch_coverage.py --probe reuters.com www.reuters.com"
            )
        # Accept both `--probe a.com b.com` and `--probe a.com,b.com`. The comma
        # form survives a shell dropping line-continuation backslashes, which
        # would otherwise silently glue two domains into one garbage string.
        domains = [
            d for chunk in rest for d in chunk.replace(",", " ").split() if d
        ]
        if not domains:
            fail("--probe got no usable domains.")
        probe_domains(api_key, domains)
    elif command == "--find-domain":
        if not rest:
            fail(
                "--find-domain needs a search term, e.g.\n"
                "    python3 fetch_coverage.py --find-domain guardian"
            )
        if rest[0].startswith("--"):
            fail(
                f"--find-domain got {rest[0]!r}, which looks like another flag.\n"
                "Run one command at a time:\n"
                "    python3 fetch_coverage.py --find-domain guardian\n"
                "    python3 fetch_coverage.py --list-sources"
            )
        find_domain(api_key, " ".join(rest))
    else:
        fail(
            f"unknown argument {command!r}. Options: "
            "--check-domains, --list-sources, --probe, --find-domain"
        )


if __name__ == "__main__":
    main()
