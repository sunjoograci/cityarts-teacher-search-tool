"""
One-time repair pass: normalize ALL-CAPS school/district/city/teacher/title
values already stored in the database to normal title case (e.g.
"CARY CHRISTIE" -> "Cary Christie", "ABILENE ISD" -> "Abilene ISD").

Going forward, src.ingest and src.scraper.save_staff already apply this same
normalization when writing new data — this script only needs to be run once
against rows that predate that fix. Respects DB_PATH like the rest of the app,
so on the Oracle deployment this can be run as:

    docker exec cityarts python -m scripts.normalize_casing

Usage:
    python -m scripts.normalize_casing            # apply changes
    python -m scripts.normalize_casing --dry-run   # preview counts only
"""
import argparse
import logging
import sqlite3

from src.db import get_conn, smart_title_case

log = logging.getLogger(__name__)


def _normalize_schools(conn: sqlite3.Connection, dry_run: bool) -> int:
    rows = conn.execute("SELECT id, school_name, city, district_name FROM schools").fetchall()
    changed = 0
    for row in rows:
        new_name = smart_title_case(row["school_name"])
        new_city = smart_title_case(row["city"])
        new_district = smart_title_case(row["district_name"])
        if (new_name, new_city, new_district) == (row["school_name"], row["city"], row["district_name"]):
            continue
        changed += 1
        if not dry_run:
            conn.execute(
                "UPDATE schools SET school_name=?, city=?, district_name=? WHERE id=?",
                (new_name, new_city, new_district, row["id"]),
            )
    return changed


def _normalize_staff(conn: sqlite3.Connection, dry_run: bool) -> int:
    rows = conn.execute("SELECT id, teacher_name, title FROM staff").fetchall()
    changed = 0
    skipped = 0
    for row in rows:
        new_name = smart_title_case(row["teacher_name"])
        new_title = smart_title_case(row["title"])
        if (new_name, new_title) == (row["teacher_name"], row["title"]):
            continue
        changed += 1
        if not dry_run:
            try:
                conn.execute(
                    "UPDATE staff SET teacher_name=?, title=? WHERE id=?",
                    (new_name, new_title, row["id"]),
                )
            except sqlite3.IntegrityError:
                # Normalizing collided with an existing (school_id, teacher_name)
                # row (e.g. both "KATE REECE" and "Kate Reece" already exist for
                # the same school) — leave this one alone rather than aborting.
                skipped += 1
                changed -= 1
    if skipped:
        log.warning("Skipped %d staff row(s) due to a name collision after normalizing.", skipped)
    return changed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Normalize ALL-CAPS names already in the database.")
    parser.add_argument("--dry-run", action="store_true", help="Report how many rows would change without writing.")
    args = parser.parse_args()

    with get_conn() as conn:
        schools_changed = _normalize_schools(conn, args.dry_run)
        staff_changed = _normalize_staff(conn, args.dry_run)

    verb = "Would update" if args.dry_run else "Updated"
    log.info("%s %d school row(s) and %d staff row(s).", verb, schools_changed, staff_changed)


if __name__ == "__main__":
    main()
