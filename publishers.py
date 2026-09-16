"""
publishers.py — who is allowed on the page.

The beat pages (sports, Lietuva) each mix two kinds of source:

  Direct feeds   A newsroom's own RSS. The publisher is the feed, so nothing
                 needs checking — choosing the feed was the editorial decision.

  Google News    A topic search. Google is an aggregator, not a newsroom: it
                 returns a betting site as readily as a newspaper, so every item
                 from one passes through an allowlist first.

This module is that allowlist. It was written twice before — once in
fetch_sports.py and once in fetch_lietuva.py — and the two had already drifted on
how they matched a name, which is how one of them ended up dropping the
Associated Press for a week.

The matching is deliberately forgiving about *form* and strict about *identity*.
Google credits the same outlet as "AP News", "apnews.com" and "The Associated
Press" on three different items, so names are reduced to letters and digits before
comparison. What survives that reduction still has to match something on the list.
"""

from __future__ import annotations

import re

# Google sometimes appends a country in brackets: "Delfi (Lietuva)".
_NOISE = re.compile(r"\s*[\(\[].*?[\)\]]\s*$")

# Generic and country-code endings a domain may carry. Dropped from the end of a
# name, because the outlet is "apnews", not "apnews com".
_TLDS = frozenset(
    {
        "com", "net", "org", "co", "uk", "us", "eu",
        "lt", "lv", "ee", "pl", "de", "fr", "it", "es", "gr", "tr", "nl", "se",
    }
)


# Lithuanian names carry diacritics that would otherwise split a word in two:
# "Verslo žinios" became "iniosverslo", because ž is not a-z. Folded to their
# base letters first, so the canonical form is the name the outlet actually uses.
_DIACRITICS = str.maketrans(
    {"ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i", "š": "s", "ū": "u", "ų": "u", "ž": "z"}
)


def forms(name: str | None) -> frozenset[str]:
    """Every canonical spelling of one name. Matched if ANY of them agrees.

    Two spellings are needed because a publisher's name reaches us in two shapes,
    and each breaks the other:

      "Yahoo Sports" vs "sports.yahoo.com" — a domain puts the subdomain first, so
        the words arrive reversed. Sorting them fixes this one.
      "kauno diena" vs "KaunoDiena.lt" — there the site writes the two words as
        one, so there is nothing to sort. Concatenating in the given order fixes
        this one.

    Neither form alone covers both, which the tests found immediately: sorting
    alone dropped KaunoDiena, and the pre-sorting version dropped 105 Yahoo items
    a run.
    """
    cleaned = _NOISE.sub("", (name or "").strip()).lower().translate(_DIACRITICS)
    words = [word for word in re.split(r"[^a-z0-9]+", cleaned) if word]
    if len(words) > 1 and words[-1] in _TLDS:
        words.pop()
    if not words:
        return frozenset()
    return frozenset({"".join(words), "".join(sorted(words))})


def normalise(name: str | None) -> str:
    """One canonical form, for display and tests. See forms() for the full rule."""
    candidates = sorted(forms(name))
    return candidates[0] if candidates else ""


# Below this length a name is matched exactly rather than as a substring. Three
# characters inside a longer word is how "Armenpress" passed as "npr" — a-r-m-e-n-
# p-r-e-s-s contains npr — putting an Armenian wire service on the Lithuanian page.
# Concise names are still supported, they just have to match the whole thing.
SUBSTRING_MIN = 5


class Allowlist:
    """An allowlist of publishers, matched on canonical name forms."""

    def __init__(
        self,
        names: frozenset[str] | set[str] | tuple[str, ...],
        *,
        exact: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
    ) -> None:
        # Canonicalised once at construction: this runs per item and would
        # otherwise redo the whole roster hundreds of times a run.
        known: set[str] = set()
        # Canonical form -> the roster's own spelling, for display. A name without
        # a dot wins over a domain, so LRT reads "LRT" rather than "lrt.lt".
        self._display: dict[str, str] = {}
        for value in names:
            for canonical in forms(value):
                known.add(canonical)
                current = self._display.get(canonical)
                if current is None or ("." in current and "." not in value):
                    self._display[canonical] = value
        # "si.com" and "Sports Illustrated" share no letters in common, so
        # abbreviations have to be listed. Anything this short is matched exactly,
        # because at two or three characters a substring test finds matches inside
        # unrelated words — "npr" inside "a-r-m-e-n-p-r-e-s-s".
        self._substring = frozenset(k for k in known if len(k) >= SUBSTRING_MIN)
        self._exact = frozenset(k for k in known if len(k) < SUBSTRING_MIN)
        for value in exact:
            self._exact |= forms(value)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        candidates = forms(name)
        if not candidates:
            return False
        if candidates & self._exact:
            return True
        for candidate in candidates:
            if any(candidate == known or known in candidate for known in self._substring):
                return True
        return False

    def display(self, name: str) -> str:
        """The outlet's own spelling for a name, or the name unchanged.

        Google credits LRT as "lrt.lt" from its <source> element while LRT's own
        feed says "LRT", so one page showed the same newsroom under two names —
        "lrt.lt" on two cards and "LRT" on a third.
        """
        for candidate in forms(name):
            for canonical, shown in self._display.items():
                if candidate == canonical or canonical in candidate:
                    return shown
        return name

    def __len__(self) -> int:
        return len(self._substring) + len(self._exact)
