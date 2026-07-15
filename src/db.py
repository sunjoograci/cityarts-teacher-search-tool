"""SQLite database setup and helpers."""
import os
import re
import sqlite3
from pathlib import Path

_default_db = Path(__file__).parent.parent / "data" / "schools.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_default_db)))


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Generational suffixes that must stay uppercase — "Robert Smith III" must
# not become "Robert Smith Iii".
_ROMAN_SUFFIX_RE = re.compile(r"^(?:II|III|IV|V)$", re.IGNORECASE)
_MC_NAME_RE = re.compile(r"\bMc([a-z])")


def _recase_word(word: str) -> str:
    if _ROMAN_SUFFIX_RE.match(word.strip(".,")):
        return word.upper()
    # Capitalize each alphabetic run, so hyphens/apostrophes reset the
    # capital: "O'BRIEN-SMITH" -> "O'Brien-Smith".
    recased = re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), word)
    # "MCALLISTER" -> capitalize() alone gives "Mcallister"; fix the
    # second capital a Mc-prefixed surname actually has.
    return _MC_NAME_RE.sub(lambda m: "Mc" + m.group(1).upper(), recased)


def normalize_person_name(name: str) -> str:
    """Recase scraped names that arrived ALL-CAPS or all-lowercase (both are
    site-formatting artifacts, e.g. table headers styled with CSS
    text-transform that innerText preserves). Words that already have mixed
    case are left untouched — "McAllister" must not become "Mcallister", so
    only fully-upper or fully-lower words are rewritten.
    """
    return " ".join(
        _recase_word(w) if (w.isupper() or w.islower()) else w
        for w in name.split()
    )


def normalize_existing_names() -> int:
    """One-time cleanup pass over rows saved before names were normalized on
    save. Returns the number of rows changed. If recasing a name collides
    with an existing properly-cased row for the same school (UNIQUE
    constraint), the badly-cased row is the duplicate and is deleted.
    """
    changed = 0
    with get_conn() as conn:
        rows = conn.execute("SELECT id, teacher_name FROM staff").fetchall()
        for row in rows:
            fixed = normalize_person_name(row["teacher_name"])
            if fixed == row["teacher_name"]:
                continue
            try:
                conn.execute(
                    "UPDATE staff SET teacher_name=? WHERE id=?", (fixed, row["id"])
                )
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM staff WHERE id=?", (row["id"],))
            changed += 1
    return changed


# Common school/district abbreviations that should stay uppercase rather than
# be title-cased (e.g. "ABILENE ISD" -> "Abilene ISD", not "Abilene Isd").
# Used for school/city/district names (see src/ingest.py) — a different
# scope from normalize_person_name above, which is for people's names.
_SHOUTY_KEEP_UPPER = {
    "II", "III", "IV", "V", "VI",
    "HS", "MS", "ES", "PK", "DAEP",
    "ISD", "CISD", "USD", "LEA", "NCES",
}


def _looks_shouty(s: str) -> bool:
    """True if s has at least one letter and no lowercase letters —
    i.e. it looks like ALL-CAPS text rather than intentional mixed case."""
    return bool(re.search(r"[A-Z]", s)) and not re.search(r"[a-z]", s)


def smart_title_case(s: str | None) -> str | None:
    """Convert ALL-CAPS strings to normal title case.

    NCES's raw school data (and some district staff directories) publish
    names in ALL CAPS. Strings that already have lowercase letters are
    assumed to be normally formatted already and are left untouched, so
    this never mangles legitimately mixed-case input.
    """
    if not s or not _looks_shouty(s):
        return s
    titled = s.title()
    titled = re.sub(
        r"[A-Za-z][A-Za-z'-]*",
        lambda m: m.group(0).upper() if m.group(0).upper() in _SHOUTY_KEEP_UPPER else m.group(0),
        titled,
    )
    titled = _MC_NAME_RE.sub(lambda m: "Mc" + m.group(1).upper(), titled)
    return titled


def group_teacher_rows(rows) -> list[dict]:
    """Merge staff rows for the same teacher across multiple schools in one
    district into a single row, joining school (and city) names with commas.

    Rows are expected to have at least teacher_name, district_name, state,
    and school_name keys. Optional id/city/title/email/resolution_method/
    website_url (or school_website) keys are merged/picked as available.
    """
    order = []
    groups: dict[tuple, dict] = {}
    for raw in rows:
        row = dict(raw)
        key = (row.get("teacher_name"), row.get("district_name"), row.get("state"))
        g = groups.get(key)
        if g is None:
            g = dict(row)
            g["_schools"] = []
            g["_cities"] = []
            g["_ids"] = []
            g["_has_email"] = False
            groups[key] = g
            order.append(key)
        if row.get("school_name") and row["school_name"] not in g["_schools"]:
            g["_schools"].append(row["school_name"])
        if row.get("city") and row["city"] not in g["_cities"]:
            g["_cities"].append(row["city"])
        if row.get("id") is not None:
            g["_ids"].append(row["id"])
        has_email = bool(row.get("email"))
        if has_email and not g["_has_email"]:
            for field in ("title", "email", "resolution_method", "website_url", "school_website"):
                if field in row:
                    g[field] = row[field]
            g["_has_email"] = True

    result = []
    for key in order:
        g = groups[key]
        g["school_name"] = ", ".join(sorted(g["_schools"]))
        if g["_cities"]:
            g["city"] = ", ".join(sorted(g["_cities"]))
        if g["_ids"]:
            g["ids"] = g["_ids"]
        for k in ("_schools", "_cities", "_ids", "_has_email"):
            del g[k]
        result.append(g)
    return result


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schools (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nces_id       TEXT UNIQUE,
                school_name   TEXT NOT NULL,
                city          TEXT,
                state         TEXT NOT NULL,
                district_name TEXT,
                website_url   TEXT,
                scraped       INTEGER DEFAULT 0,
                scrape_status TEXT,
                school_level  TEXT,
                is_arts_school INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS staff (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                school_id         INTEGER NOT NULL REFERENCES schools(id),
                teacher_name      TEXT NOT NULL,
                title             TEXT,
                email             TEXT,
                resolution_method TEXT DEFAULT 'unresolved',
                added_at          TEXT,
                UNIQUE(school_id, teacher_name)
            );
        """)
        _migrate(conn)
    print("Database initialised.")


# ---------------------------------------------------------------------------
# Migration — adds the entity-resolution / access-strategy / classification
# columns from the pipeline rebuild. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each column is added individually and OperationalError (column
# already exists) is swallowed, making this safe to call on every startup.
# ---------------------------------------------------------------------------

_SCHOOLS_NEW_COLUMNS = [
    ("entity_type", "TEXT"),               # SCHOOL | DISTRICT | PARENT_ORG | PROGRAM | NOT_A_K12 | AMBIGUOUS
    ("parent_entity", "TEXT"),
    ("parent_entity_type", "TEXT"),
    ("resolution_confidence", "REAL"),
    ("resolution_note", "TEXT"),
    ("domain", "TEXT"),
    ("directory_url", "TEXT"),
    ("directory_access_method", "TEXT"),   # BLANK_FORM_SUBMIT | STATIC_HTML | XHR_JSON | PAGINATED | IFRAME | PDF
    ("paths_attempted", "TEXT"),           # JSON array of {url, status, rejected_because}
    ("status", "TEXT"),                    # OK | NO_TEACHERS_LISTED | NO_DIRECTORY_FOUND | AUTH_REQUIRED |
                                            # AMBIGUOUS_ENTITY | NOT_A_SCHOOL | PROGRAM_REDIRECTED | BLOCKED
    ("needs_human_review", "INTEGER DEFAULT 0"),
    ("school_level", "TEXT"),
    ("is_arts_school", "INTEGER DEFAULT 0"),
]

_STAFF_NEW_COLUMNS = [
    ("discipline", "TEXT"),                # visual art | theatre | dance | music | media | unknown
    ("email_source", "TEXT"),              # MAILTO | CF_DECODED | JS_DECODED | OCR | INFERRED
    ("email_verified", "INTEGER DEFAULT 0"),
    ("evidence_url", "TEXT"),
    ("added_at", "TEXT"),
    ("extraction_strategy", "TEXT"),       # semantic_card | name_title_line | table_row |
                                           # generic_block | mailto_context | text_scan
]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _migrate(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        for column, coltype in _SCHOOLS_NEW_COLUMNS:
            _ensure_column(conn, "schools", column, coltype)
        for column, coltype in _STAFF_NEW_COLUMNS:
            _ensure_column(conn, "staff", column, coltype)
        conn.commit()
    finally:
        if own:
            conn.close()
