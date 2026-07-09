"""
Stage 1 — Entity resolution.

Never scrape a name. Resolve every input into exactly one of:
    SCHOOL | DISTRICT | PARENT_ORG | PROGRAM | NOT_A_K12 | AMBIGUOUS

before any HTTP request is made. Anchored to the local NCES CCD public
school/district extract (data/nces_ccd.csv, already downloaded by
src/ingest.py) rather than to search-engine ranking. Relationships NCES has
no concept of — a charter-holding nonprofit, a program living inside a host
school — are supplied by small curated tables below (KNOWN_PARENT_ORGS,
KNOWN_PROGRAMS, DISTRICT_PARENT_ORGS), each with a note on how it was
grounded, so they can be audited and extended rather than trusted blindly.

NCES PSS (private schools) is not wired in yet — a name with zero CCD hits
is flagged NOT_A_K12 for human review rather than guessed at, per the
"fabricating a result is the worst outcome" rule in the project spec.
"""
from __future__ import annotations

import csv
import difflib
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CCD_CSV = DATA_DIR / "nces_ccd.csv"

ENTITY_TYPES = {"SCHOOL", "DISTRICT", "PARENT_ORG", "PROGRAM", "NOT_A_K12", "AMBIGUOUS"}

OPEN_STATUSES = {"Open", "New", "1", "3", ""}

FUZZY_CUTOFF = 0.82


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# NCES/common abbreviations expanded before comparison. Order doesn't matter;
# applied per-token. Deliberately excludes short tokens like "el"/"ms"/"hs" —
# those collide with common words and proper nouns ("El Paso", "El Dorado")
# and corrupt names instead of normalizing them; prefix/fuzzy matching
# handles those suffix conventions fine without a blanket expansion.
_ABBREV_EXPANSIONS = {
    "isd": "independent school district",
    "usd": "unified school district",
    "csd": "consolidated school district",
    "elem": "elementary",
    "jr": "junior",
    "sr": "senior",
}

# Dropped only for the "stripped" comparison form (acronym derivation and
# loose matching) — never used to alter the resolved/displayed name.
_STOPWORDS = {"the", "of", "at", "and", "a", "an", "for"}


def normalize_name(raw: str) -> str:
    """Lowercase, fold +/& to 'and', expand abbreviations, collapse whitespace.

    This is what makes 'NEISD' comparable to 'North East ISD' and what makes
    'Texas School of Fine Arts' land close enough to 'Texas School of the
    Arts' for the fuzzy matcher to bridge the gap.
    """
    s = raw.strip().lower()
    s = s.replace("+", " and ").replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [_ABBREV_EXPANSIONS.get(tok, tok) for tok in s.split()]
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def strip_stopwords(s: str) -> str:
    return " ".join(t for t in s.split() if t not in _STOPWORDS)


def acronym_of(normalized: str) -> str:
    """First letters of significant (non-stopword) words.

    'north east independent school district' -> 'neisd'. This is what lets a
    bare acronym input resolve against a fully-spelled-out NCES district name
    without a hardcoded lookup table.
    """
    words = [w for w in normalized.split() if w not in _STOPWORDS]
    return "".join(w[0] for w in words if w)


def _fuzzy_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY_CUTOFF


def _domain_from_website(website: str) -> str | None:
    website = (website or "").strip()
    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website
    netloc = urllib.parse.urlparse(website).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc or None


# ---------------------------------------------------------------------------
# Curated relationships NCES doesn't encode
#
# Each entry documents where the fact came from. These are deliberately a
# short, auditable list rather than an attempt to algorithmically infer
# nonprofit/charter-holder relationships from web search ranking.
# ---------------------------------------------------------------------------

KNOWN_PARENT_ORGS = {
    "texas center for arts and academics": {
        "canonical_name": "Texas Center for Arts + Academics",
        "domain": "artsacademics.org",
        "children": ["Fort Worth Academy of Fine Arts", "Texas School of the Arts"],
        "note": (
            "501(c)(3) nonprofit that holds the charter for two single-school "
            "LEAs (Fort Worth Academy of Fine Arts, Texas School of the Arts) "
            "plus two non-K12 conservatories (Texas Boys Choir, Texas Dance "
            "Conservatory). Employs administrative staff, not classroom "
            "teachers, so it is never a terminal scrape target. "
            "Source: artsacademics.org self-description, confirmed via web search."
        ),
    },
}

# Program name (normalized) -> host-school relationship. A program never
# terminates a crawl; it redirects to its host school's domain.
KNOWN_PROGRAMS = {
    "digital arts and technology academy at adams middle school": {
        "canonical_name": "Digital Arts & Technology Academy at Adams Middle School",
        "state": "TX",
        "parent_entity": "John Adams Middle School",
        "parent_entity_type": "SCHOOL",
        "domain": "adams.gpisd.org",
        "note": (
            "Academy/program operating inside John Adams Middle School, "
            "Grand Prairie ISD. NCES CCD lists it under its own NCESSCH id "
            "(482142002143) because CCD has no PROGRAM concept, but it has "
            "no independent staff directory — teachers belong to Adams MS. "
            "Name-collides with an unrelated NM charter school, 'Digital "
            "Arts and Technology Academy' (datacharter.org, Albuquerque, "
            "NCESSCH 350006000853). Disambiguated here by the explicit "
            "'at Adams Middle School' host-school signal in the input name; "
            "without that signal this would be AMBIGUOUS."
        ),
    },
}

# District/single-school-LEA (normalized LEA_NAME) -> the nonprofit that
# actually holds its charter. When present, this overrides the plain
# NCES-district as the resolved parent_entity, so e.g. Fort Worth Academy of
# Fine Arts' parent comes back as "Texas Center for Arts + Academics", not
# the pass-through single-school LEA of the same name.
DISTRICT_PARENT_ORGS = {
    "burnham wood charter school district": {
        "canonical_name": "El Paso Education Initiative",
        "note": (
            "Charter-holding nonprofit for Burnham Wood Charter School "
            "District, per operator-supplied ground truth for this task. "
            "Not independently confirmed via NCES (which has no PARENT_ORG "
            "field) — flagged for human confirmation if this relationship "
            "is load-bearing."
        ),
        "confirmed": False,
    },
    "fort worth academy of fine arts": {
        "canonical_name": "Texas Center for Arts + Academics",
        "domain": "artsacademics.org",
        "note": "Single-school charter LEA; charter held by TCA+A. Source: artsacademics.org.",
        "confirmed": True,
    },
    "texas school of the arts": {
        "canonical_name": "Texas Center for Arts + Academics",
        "domain": "artsacademics.org",
        "note": "Single-school charter LEA; charter held by TCA+A. Source: artsacademics.org.",
        "confirmed": True,
    },
}

# Names with zero NCES hits that are known non-K12 orgs or known false-positive
# collisions, keyed by normalize_name(). Used only to enrich resolution_note —
# never to fabricate an nces_id.
NOT_A_K12_NOTES = {
    "texas school of fine arts": (
        "A distinct private, non-accredited art studio called 'Texas School "
        "of Fine Arts' operates in Austin, TX and does not appear in NCES "
        "CCD — consistent with it not being a K-12 institution. Rejected as "
        "the match for this input on state/entity-type grounds; the NCES "
        "hit 'Texas School of the Arts' (Fort Worth) is used instead."
    ),
}


# ---------------------------------------------------------------------------
# NCES CCD index
# ---------------------------------------------------------------------------

class NCESIndex:
    def __init__(self, csv_path: Path = CCD_CSV) -> None:
        self.by_norm_school: dict[str, list[dict]] = {}
        self.by_norm_district: dict[str, list[dict]] = {}
        self._load(csv_path)

    def _load(self, path: Path) -> None:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                if row.get("SY_STATUS_TEXT", "") not in OPEN_STATUSES:
                    continue
                sch_norm = normalize_name(row.get("SCH_NAME", ""))
                lea_norm = normalize_name(row.get("LEA_NAME", ""))
                if sch_norm:
                    self.by_norm_school.setdefault(sch_norm, []).append(row)
                if lea_norm:
                    self.by_norm_district.setdefault(lea_norm, []).append(row)

    def _lookup(self, index: dict[str, list[dict]], norm: str, state: str | None) -> list[dict]:
        """Exact -> prefix -> fuzzy, in that order, each tier trying the
        whole corpus but the *prefix and fuzzy* tiers are strictly scoped to
        `state` when given: a short/generic name like "West Elem" is common
        enough that fuzzy matching against the full 100k-row national corpus
        will find a superficially-similar name in a totally unrelated state
        (e.g. "Desmet Elem" in SD) almost every time. An exact name match
        across states is kept even when it doesn't match `state` — that's a
        real same-named-school collision worth surfacing as ambiguous, not
        noise to filter away.
        """
        exact = list(index.get(norm, []))
        if exact:
            if state:
                filtered = [r for r in exact if r.get("ST", "").upper() == state.upper()]
                return filtered or exact
            return exact

        prefix_rows: list[dict] = []
        for key, key_rows in index.items():
            # Colloquial short name is a prefix of the official name, e.g.
            # "Burnham Wood" -> "Burnham Wood Charter School District".
            if key.startswith(norm + " ") or norm.startswith(key + " "):
                prefix_rows.extend(key_rows)
        if prefix_rows:
            if state:
                return [r for r in prefix_rows if r.get("ST", "").upper() == state.upper()]
            return prefix_rows

        candidate_keys = index.keys()
        if state:
            candidate_keys = [
                k for k in candidate_keys
                if any(r.get("ST", "").upper() == state.upper() for r in index[k])
            ]
        fuzzy_rows: list[dict] = []
        for match in difflib.get_close_matches(norm, candidate_keys, n=5, cutoff=FUZZY_CUTOFF):
            fuzzy_rows.extend(index[match])
        if state:
            fuzzy_rows = [r for r in fuzzy_rows if r.get("ST", "").upper() == state.upper()]
        return fuzzy_rows

    def find_schools(self, name: str, state: str | None = None) -> list[dict]:
        return self._lookup(self.by_norm_school, normalize_name(name), state)

    def find_districts(self, name: str, state: str | None = None) -> dict[str, list[dict]]:
        norm = normalize_name(name)
        rows = self._lookup(self.by_norm_district, norm, state)
        # Acronym fallback ("NEISD" won't dict-match "north east independent
        # school district" directly, but its acronym will).
        if not rows and re.fullmatch(r"[a-z]{2,8}", norm.replace(" ", "")):
            target = norm.replace(" ", "")
            for dnorm, drows in self.by_norm_district.items():
                if acronym_of(dnorm) == target:
                    if state and drows[0].get("ST", "").upper() != state.upper():
                        continue
                    rows.extend(drows)
        by_leaid: dict[str, list[dict]] = {}
        for r in rows:
            by_leaid.setdefault(r["LEAID"], []).append(r)
        return by_leaid


_index: NCESIndex | None = None


def get_index() -> NCESIndex:
    global _index
    if _index is None:
        _index = NCESIndex()
    return _index


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class EntityResolution:
    input_name: str
    resolved_name: str | None = None
    nces_id: str | None = None
    entity_type: str = "AMBIGUOUS"
    parent_entity: str | None = None
    parent_entity_type: str | None = None
    resolution_confidence: float = 0.0
    resolution_note: str = ""
    domain: str | None = None
    state: str | None = None
    children: list[str] = field(default_factory=list)
    needs_human_review: bool = False


def _attach_district_parent_org(result: EntityResolution, lea_name: str) -> None:
    """Overwrite parent_entity/parent_entity_type if this LEA is known to be
    a pass-through for a charter-holding nonprofit."""
    info = DISTRICT_PARENT_ORGS.get(normalize_name(lea_name))
    if not info:
        return
    result.parent_entity = info["canonical_name"]
    result.parent_entity_type = "PARENT_ORG"
    result.resolution_note = (result.resolution_note + " " + info["note"]).strip()
    if not info.get("confirmed", True):
        result.needs_human_review = True


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(
    input_name: str,
    state_hint: str | None = None,
    city_hint: str | None = None,
    index: NCESIndex | None = None,
) -> EntityResolution:
    index = index or get_index()
    norm = normalize_name(input_name)
    stripped = strip_stopwords(norm)
    result = EntityResolution(input_name=input_name, state=state_hint)

    # 1. Known parent org (checked before any NCES lookup — these entities
    #    are often *not* in NCES at all, and even when a same-named LEA
    #    exists, e.g. via a charter school with the org's name, the org
    #    itself is the correct classification).
    for key, info in KNOWN_PARENT_ORGS.items():
        if _fuzzy_eq(norm, key) or _fuzzy_eq(stripped, strip_stopwords(key)):
            result.resolved_name = info["canonical_name"]
            result.entity_type = "PARENT_ORG"
            result.domain = info["domain"]
            result.resolution_confidence = 0.95
            result.resolution_note = info["note"]
            result.children = list(info["children"])
            return result

    # 2. Known program (also checked before general NCES resolution, since
    #    NCES has no PROGRAM concept and would otherwise misclassify this as
    #    a plain SCHOOL).
    for key, info in KNOWN_PROGRAMS.items():
        if _fuzzy_eq(norm, key) or _fuzzy_eq(stripped, strip_stopwords(key)):
            hits = index.find_schools(input_name, state=info.get("state"))
            result.resolved_name = info["canonical_name"]
            result.entity_type = "PROGRAM"
            result.nces_id = hits[0]["NCESSCH"] if hits else None
            result.domain = info["domain"]
            result.parent_entity = info["parent_entity"]
            result.parent_entity_type = info["parent_entity_type"]
            result.resolution_confidence = 0.9
            result.resolution_note = info["note"]
            result.state = info.get("state", state_hint)
            return result

    school_hits = index.find_schools(input_name, state=state_hint)
    district_hits = index.find_districts(input_name, state=state_hint)

    # A "district" hit that is really just a single school masquerading as
    # its own one-campus LEA (common for standalone charters, e.g. Fort
    # Worth Academy of Fine Arts, Texas School of the Arts) should resolve
    # as SCHOOL — unless the input is naming the collective generically
    # (e.g. "NEISD") rather than one specific campus.
    for leaid, rows in list(district_hits.items()):
        distinct_school_names = {r["SCH_NAME"] for r in rows}
        exact_row = next((r for r in rows if normalize_name(r["SCH_NAME"]) == norm), None)
        if exact_row or len(distinct_school_names) == 1:
            chosen = exact_row or rows[0]
            if not any(h["NCESSCH"] == chosen["NCESSCH"] for h in school_hits):
                school_hits.append(chosen)
            del district_hits[leaid]

    # 3. School match (only when there's no competing district-name match —
    #    a bare acronym like "NEISD" is a district, not any one campus).
    if school_hits and not district_hits:
        distinct_states = {h["ST"] for h in school_hits}
        distinct_names = {h["NCESSCH"] for h in school_hits}
        if len(distinct_names) > 1 and len(distinct_states) > 1 and not state_hint and not city_hint:
            result.entity_type = "AMBIGUOUS"
            result.needs_human_review = True
            result.resolution_note = (
                f"{len(school_hits)} NCES school matches across states "
                f"{sorted(distinct_states)}; no disambiguating signal (state/city) supplied."
            )
            return result
        if state_hint:
            school_hits = sorted(school_hits, key=lambda h: h.get("ST", "").upper() != state_hint.upper())
        best = school_hits[0]
        result.resolved_name = best["SCH_NAME"].title()
        result.nces_id = best["NCESSCH"]
        result.entity_type = "SCHOOL"
        result.state = best["ST"]
        result.domain = _domain_from_website(best.get("WEBSITE", ""))
        result.parent_entity = best["LEA_NAME"].title()
        result.parent_entity_type = "DISTRICT"
        exact = norm == normalize_name(best["SCH_NAME"])
        result.resolution_confidence = 1.0 if exact else 0.72
        if not exact:
            result.resolution_note = (
                f"Fuzzy match on name (input {input_name!r} vs NCES "
                f"{best['SCH_NAME'].title()!r})."
            )
            note = NOT_A_K12_NOTES.get(norm) or NOT_A_K12_NOTES.get(stripped)
            if note:
                result.resolution_note += " " + note
        _attach_district_parent_org(result, best["LEA_NAME"])
        return result

    # 4. District match.
    if district_hits:
        if len(district_hits) > 1 and not state_hint and not city_hint:
            result.entity_type = "AMBIGUOUS"
            result.needs_human_review = True
            states = sorted({rows[0]["ST"] for rows in district_hits.values()})
            result.resolution_note = (
                f"{len(district_hits)} distinct NCES districts match across "
                f"states {states}; no disambiguating signal supplied."
            )
            return result
        leaid, rows = next(iter(district_hits.items()))
        result.resolved_name = rows[0]["LEA_NAME"].title()
        result.nces_id = leaid
        result.entity_type = "DISTRICT"
        result.state = rows[0]["ST"]
        result.children = sorted({r["SCH_NAME"].title() for r in rows})
        result.domain = _domain_from_website(rows[0].get("WEBSITE", ""))
        exact = norm == normalize_name(rows[0]["LEA_NAME"])
        result.resolution_confidence = 1.0 if exact else 0.85
        if not exact:
            result.resolution_note = (
                f"Resolved via acronym/fuzzy match on district name "
                f"(input {input_name!r} vs NCES {rows[0]['LEA_NAME'].title()!r})."
            )
        _attach_district_parent_org(result, rows[0]["LEA_NAME"])
        return result

    # 5. Zero NCES hits — not in the public CCD dataset at all. Could be a
    #    private school (NCES PSS not wired in yet), a nonprofit, or a non-K12
    #    org. Never guess; always flag for human review.
    result.entity_type = "NOT_A_K12"
    result.resolution_confidence = 0.3
    result.needs_human_review = True
    note = NOT_A_K12_NOTES.get(norm) or NOT_A_K12_NOTES.get(stripped)
    result.resolution_note = note or (
        "No NCES CCD match (public schools/districts only — NCES PSS "
        "private-school lookup is not yet wired up). Likely a non-K12 "
        "org, a private school outside this dataset, or a corrupted name. "
        "Flagged for human review rather than guessed."
    )
    return result


# ---------------------------------------------------------------------------
# Containment graph / dedup helper
# ---------------------------------------------------------------------------

def build_containment_key(result: EntityResolution) -> str:
    """A stable key for collapsing multiple input rows that resolve into the
    same subtree (e.g. TCA+A, FWAFA, and TeSA all trace back to the same
    parent_org) so they're crawled once instead of three times.
    """
    if result.entity_type == "PARENT_ORG":
        return f"org:{normalize_name(result.resolved_name or result.input_name)}"
    if result.parent_entity and result.parent_entity_type == "PARENT_ORG":
        return f"org:{normalize_name(result.parent_entity)}"
    if result.nces_id:
        return f"nces:{result.nces_id}"
    return f"unresolved:{normalize_name(result.input_name)}"
