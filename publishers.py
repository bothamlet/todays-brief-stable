"""Match and normalize publisher names."""

from __future__ import annotations

import re

# Google sometimes appends a country in brackets: "Delfi (Lietuva)".
_NOISE = re.compile(r"\s*[\(\[].*?[\)\]]\s*$")

_TLDS = frozenset(
    {
        "com", "net", "org", "co", "uk", "us", "eu",
        "lt", "lv", "ee", "pl", "de", "fr", "it", "es", "gr", "tr", "nl", "se",
    }
)


# Strip accents before matching publisher names.
_DIACRITICS = str.maketrans(
    {"ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i", "š": "s", "ū": "u", "ų": "u", "ž": "z"}
)


def forms(name: str | None) -> frozenset[str]:
    """Every canonical spelling of one name."""
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


# Match short names exactly to avoid substring collisions.
SUBSTRING_MIN = 5


class Allowlist:
    """An allowlist of publishers, matched on canonical name forms."""

    def __init__(
        self,
        names: frozenset[str] | set[str] | tuple[str, ...],
        *,
        exact: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
    ) -> None:
        known: set[str] = set()
        self._display: dict[str, str] = {}
        for value in names:
            for canonical in forms(value):
                known.add(canonical)
                current = self._display.get(canonical)
                if current is None or ("." in current and "." not in value):
                    self._display[canonical] = value
        # Domain aliases match names such as si.com and Sports Illustrated.
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
        """The outlet's own spelling for a name, or the name unchanged."""
        for candidate in forms(name):
            for canonical, shown in self._display.items():
                if candidate == canonical or canonical in candidate:
                    return shown
        return name

    def __len__(self) -> int:
        return len(self._substring) + len(self._exact)
