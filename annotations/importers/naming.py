"""
Matching directory names against metadata titles.

The trap: two tools can both claim to "remove the diacritics" from a name and disagree
about what that means. One *deletes* the offending character outright; the other
*transliterates* it to its ASCII base. A name that went through one rule cannot be
recovered into a name that went through the other, because deletion destroys information
that transliteration keeps.

So nothing here derives a stable key from a directory name. All it does is guess which
metadata row goes with which directory, and it always reports which rule produced the
guess — anything short of an exact match has to leave a trace.
"""
from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def fold_ascii(s: str) -> str:
    """Transliteration: a letter carrying a diacritic becomes its ASCII base letter."""
    decomposed = unicodedata.normalize("NFKD", s)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    ascii_only = ascii_only.encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub("_", ascii_only.lower()).strip("_")


def strip_diacritic_chars(s: str) -> str:
    """Deletion: a non-ASCII letter is dropped, leaving nothing in its place."""
    ascii_only = s.encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub("_", ascii_only.lower()).strip("_")


def match_metadata_title(dir_name: str, titles: dict[str, str]) -> tuple[str | None, str]:
    """
    dir_name: the directory name on disk
    titles:   {original metadata title: stable key}
    Returns (key, match_method), or (None, "none") when nothing matches.

    The methods are tried most certain first, and the one that succeeded is returned
    alongside the answer. A caller that only got the key would have no way to tell an exact
    match from a lucky one.
    """
    d_strip = strip_diacritic_chars(dir_name)
    d_fold = fold_ascii(dir_name)

    for title, key in titles.items():
        if title == dir_name:
            return key, "exact"
    for title, key in titles.items():
        if strip_diacritic_chars(title) == d_strip:
            return key, "diacritics_stripped"
    for title, key in titles.items():
        if fold_ascii(title) == d_fold:
            return key, "folded"
    return None, "none"
