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
