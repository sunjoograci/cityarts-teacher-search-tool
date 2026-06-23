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
    print("Database initialised.")
