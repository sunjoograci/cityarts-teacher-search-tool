"""
Stage 5 — Art teacher classification.

Two functions matter: `is_art_related(text)` decides whether a title/
department string names an arts role at all, and `extract_discipline(text)`
narrows that down to a discipline the CityArts consumer can filter on.

Word-boundary matched, exclude-phrases-stripped-first so a compound string
like "Visual Arts / Language Arts Co-Chair" doesn't get thrown out just
because it also mentions language arts.

Only ever pass a *title* or *department* string here — never a person's
`name` field. The "Art as a surname/first name" false positive (spec: "Art
Johnson") is a name-field problem, not a title-field problem, and is
avoided by construction rather than by regex heuristics that would be
unreliable against real names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases that must never trigger an art match, even though they contain
# "art" or "arts" as a substring. Order doesn't matter — all stripped before
# the include check runs.
_EXCLUDE_PHRASES = [
    r"language arts",
    r"\bela\b",
    r"martial arts",
    r"culinary arts",
    r"liberal arts",
    r"industrial arts",
    r"arts and sciences",
    r"arts integration coordinator",
    r"arts\s*\+\s*academics",
    r"arts and academics",
]
_EXCLUDE_RE = re.compile("|".join(_EXCLUDE_PHRASES), re.IGNORECASE)

# term -> discipline. Checked in this order; first match wins. Longer/more
# specific phrases are listed before their generic parents so e.g. "studio
# art" is classified before the bare "art" fallback.
_DISCIPLINE_TERMS: list[tuple[str, str]] = [
    (r"studio art", "visual art"),
    (r"visual art\w*", "visual art"),
    (r"ceramics?", "visual art"),
    (r"sculpture", "visual art"),
    (r"drawing", "visual art"),
    (r"painting", "visual art"),
    (r"printmaking", "visual art"),
    (r"ap art history", "visual art"),
    (r"art department chair", "visual art"),
    (r"digital art\w*", "media"),
    (r"digital media", "media"),
    (r"media arts?", "media"),
    (r"graphic design", "media"),
    (r"animation", "media"),
    (r"film", "media"),
    (r"photography", "media"),
    (r"theatre|theater|drama", "theatre"),
    (r"dance", "dance"),
    (r"choir|chorus|orchestra|band|music", "music"),
    (r"fine arts?", "unknown"),
    # "steam" was removed from this list: STEAM is Science/Tech/Engineering/
    # Arts/Math — not an arts-role signal — and worse, campus names like
    # "STEAM Academy at Mambrino" appear in the title/campus column of
    # district-wide directories, which made every employee of such a school
    # (~100 people, teachers of everything) classify as an art teacher.
    # Plural only: "Specials" is the elementary art/music/PE rotation, but
    # the singular "special" appears in decidedly non-art roles ("Special
    # Education", "Special Services Director", "Special Programs") and was
    # matching all of them.
    (r"specials", "unknown"),
    (r"enrichment", "unknown"),
    (r"\bart\b", "visual art"),
]
_DISCIPLINE_RE = [(re.compile(r"\b(" + pat + r")\b", re.IGNORECASE), disc) for pat, disc in _DISCIPLINE_TERMS]


@dataclass
class ArtClassification:
    is_art: bool
    discipline: str  # "visual art" | "theatre" | "dance" | "music" | "media" | "unknown"
    matched_term: str | None


def _strip_excludes(text: str) -> str:
    return _EXCLUDE_RE.sub(" ", text or "")


def is_art_related(text: str) -> bool:
    """True if this title/department string names an arts role.

    Exclude phrases are stripped first, so "3rd Grade Language Arts" and
    "Arts + Academics" never match, while "Visual Arts / Language Arts
    Co-Chair" still matches on "Visual Arts".
    """
    cleaned = _strip_excludes(text)
    return any(rx.search(cleaned) for rx, _ in _DISCIPLINE_RE)


def extract_discipline(text: str) -> str:
    """Best-guess discipline for an already-confirmed art role.

    Returns "unknown" for generic terms (fine arts, specials, enrichment,
    STEAM) that don't specify a discipline on their own — per spec, do not
    guess a discipline from a generic label.
    """
    cleaned = _strip_excludes(text)
    for rx, discipline in _DISCIPLINE_RE:
        if rx.search(cleaned):
            return discipline
    return "unknown"


def classify(title: str, department: str | None = None) -> ArtClassification:
    """Classify a title (optionally with a department string for context)."""
    combined = " ".join(t for t in (title, department) if t)
    cleaned = _strip_excludes(combined)
    for rx, discipline in _DISCIPLINE_RE:
        m = rx.search(cleaned)
        if m:
            return ArtClassification(is_art=True, discipline=discipline, matched_term=m.group(1))
    return ArtClassification(is_art=False, discipline="unknown", matched_term=None)
