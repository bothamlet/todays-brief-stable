#!/usr/bin/env python3
"""
textutil.py — headline text handling shared across the pipeline.

This lives apart from any one step because four of them need it. It used to sit
in cluster_stories.py, which meant fetch_coverage.py and track_stories.py had to
import the entire clustering module — and its threshold, its union-find and its
config — just to tokenise a headline.

Two ideas run through everything here:

* Content words, not all words. "the", "and" and "said" carry no identity.
* Prefix-aware matching at 4+ characters, because headlines about one running
  story seldom repeat a word exactly: "Houthi" meets "Houthis", "South" meets
  "Southern", "Indonesia" meets "Indonesian".
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MIN_TOKEN_LENGTH = 3
PREFIX_MIN_LENGTH = 4

STOPWORDS = {
    # articles, conjunctions, prepositions
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "of", "in", "on", "at", "to", "for", "with", "by", "from",
    "as", "into", "about", "after", "before", "between", "over", "under",
    "during", "through", "up", "down", "out", "off", "again", "further", "once",
    "here", "there", "when", "where", "why", "how", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "too", "very", "just", "now",
    # verbs and auxiliaries that appear in every headline
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "doing", "have", "has", "had", "having", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "says", "say", "said",
    # pronouns and determiners
    "its", "his", "her", "their", "our", "your", "my", "who", "what", "which",
    "it", "he", "she", "they", "we", "you", "i", "us",
    # live-blog and desk furniture
    "top", "live", "update", "updates", "breaking", "latest", "new", "amid",
}


def clean_title(title: str) -> str:
    """Strip publisher noise: a trailing ' - Outlet' and a leading 'Outlet:'.

    The leading strip is deliberately narrow — one word, no spaces. When it was
    `[A-Z][^:]{1,25}:` it swallowed real headline text: "Ferry disaster: Indonesia
    toll rises to 20" became "Indonesia toll rises to 20", losing the two words
    that identify it. Clustering lost the overlap, and the day-to-day tracker
    could not recognise the same ferry sinking two days running.
    """
    text = title or ""
    text = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,30}$", "", text)  # trailing " - Outlet"
    text = re.sub(r"^[A-Z][A-Za-z0-9]{1,13}:\s*", "", text)  # leading "Outlet: "
    return text.strip()


def significant_tokens(title: str) -> set[str]:
    """Lowercased content words from a headline."""
    words = re.findall(r"[a-z0-9]+", clean_title(title).lower())
    return {w for w in words if w not in STOPWORDS and len(w) >= MIN_TOKEN_LENGTH}


def words_match(left: str, right: str) -> bool:
    """Exact, or a shared prefix long enough not to be coincidence."""
    if left == right:
        return True
    if len(left) >= PREFIX_MIN_LENGTH and right.startswith(left):
        return True
    return len(right) >= PREFIX_MIN_LENGTH and left.startswith(right)


def shared_words(left: Iterable[str], right: Iterable[str]) -> set[str]:
    """Words the two sets share, prefix-aware.

    Returns the words from `right` that `left` matches, so callers can score the
    match by how distinctive those are.
    """
    reference = set(right)
    return {
        other
        for word in left
        for other in reference
        if words_match(word, other)
    }


def word_frequencies(titles: Iterable[str]) -> dict[str, int]:
    """How many of the given headlines each content word appears in.

    Used to weight overlap: "ferry" and "indonesia" identify a story, while
    "dead" and "attack" appear in half the corpus and would merge unrelated
    conflicts.
    """
    frequencies: dict[str, int] = {}
    for title in titles:
        for word in significant_tokens(title):
            frequencies[word] = frequencies.get(word, 0) + 1
    return frequencies


def topic_key(label: str) -> str:
    """Canonical form of a topic label.

    The classifier returned both "indonesia ferry disaster" and "indonesia ferry
    disasters" for one event, so an exact-match cap let the same story take two
    briefing slots. Naive singularisation is enough at this length, and skipping
    words ending in "ss" keeps "press" and "business" intact.
    """
    return " ".join(
        word[:-1]
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss")
        else word
        for word in label.lower().split()
    )


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for matching text."""
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (text or "").lower())).strip()


def ordinal(number: int) -> str:
    """1 -> 1st, 2 -> 2nd, 3 -> 3rd, 11 -> 11th."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def title_overlap(candidate_title: str, story_tokens: set[str]) -> int:
    """How many content words a candidate headline shares with a story."""
    return len(shared_words(significant_tokens(candidate_title), story_tokens))
