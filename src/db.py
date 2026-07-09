"""SQLite database setup and helpers."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "schools.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
                scrape_status TEXT
            );

            CREATE TABLE IF NOT EXISTS staff (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                school_id         INTEGER NOT NULL REFERENCES schools(id),
                teacher_name      TEXT NOT NULL,
                title             TEXT,
                email             TEXT,
                resolution_method TEXT DEFAULT 'unresolved',
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
