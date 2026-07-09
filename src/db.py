"""SQLite database setup and helpers."""
import os
import sqlite3
from pathlib import Path

_default_db = Path(__file__).parent.parent / "data" / "schools.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_default_db)))


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
<<<<<<< HEAD
        _migrate(conn)
=======
        for migration in [
            "ALTER TABLE staff ADD COLUMN added_at TEXT",
            "ALTER TABLE schools ADD COLUMN school_level TEXT",
            "ALTER TABLE schools ADD COLUMN is_arts_school INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb
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
]

_STAFF_NEW_COLUMNS = [
    ("discipline", "TEXT"),                # visual art | theatre | dance | music | media | unknown
    ("email_source", "TEXT"),              # MAILTO | CF_DECODED | JS_DECODED | OCR | INFERRED
    ("email_verified", "INTEGER DEFAULT 0"),
    ("evidence_url", "TEXT"),
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
