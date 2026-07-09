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


# Common school/district abbreviations that should stay uppercase rather than
# be title-cased (e.g. "ABILENE ISD" -> "Abilene ISD", not "Abilene Isd").
_SHOUTY_KEEP_UPPER = {
    "II", "III", "IV", "V", "VI",
    "HS", "MS", "ES", "PK", "DAEP",
    "ISD", "CISD", "USD", "LEA", "NCES",
}

_MC_NAME_RE = re.compile(r"\bMc([a-z])")


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
        for migration in [
            "ALTER TABLE staff ADD COLUMN added_at TEXT",
            "ALTER TABLE schools ADD COLUMN school_level TEXT",
            "ALTER TABLE schools ADD COLUMN is_arts_school INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass
    print("Database initialised.")
