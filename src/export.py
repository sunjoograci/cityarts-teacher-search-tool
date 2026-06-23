"""
Step 4 — Export results to CSV.

Usage:
    python -m src.export --states TX KS [--out results.csv]
"""
import argparse
import csv
import logging
import sys
from pathlib import Path

from .db import get_conn

log = logging.getLogger(__name__)


def export_csv(states: list[str], out_path: str | None = None) -> int:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                sc.state,
                sc.district_name,
                sc.school_name,
                sc.city,
                s.teacher_name,
                s.title,
                s.email,
                s.resolution_method,
                sc.website_url AS school_website
            FROM staff s
            JOIN schools sc ON sc.id = s.school_id
            WHERE sc.state IN ({})
            ORDER BY sc.state, sc.district_name, sc.school_name, s.teacher_name
            """.format(",".join("?" * len(states))),
            states,
        ).fetchall()

    if not rows:
        log.warning("No results found for states: %s", states)
        return 0

    fieldnames = ["state", "district_name", "school_name", "city", "teacher_name", "title", "email", "resolution_method", "school_website"]

    if out_path:
        fh = open(out_path, "w", newline="", encoding="utf-8")
    else:
        fh = sys.stdout

    try:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            r = dict(row)
            # Contact form records store a URL in the email column — replace with label
            if r.get("resolution_method") in ("send_message_button", "contact_form") or (
                r.get("email", "") or ""
            ).startswith("http"):
                r["email"] = "reach out on website"
            writer.writerow(r)
    finally:
        if out_path:
            fh.close()

    if out_path:
        log.info("Exported %d rows to %s", len(rows), out_path)
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Export teacher contacts to CSV.")
    parser.add_argument("--states", nargs="+", default=["TX", "KS"])
    parser.add_argument("--out", default=None, help="Output CSV path (default: stdout)")
    args = parser.parse_args()
    n = export_csv([s.upper() for s in args.states], args.out)
    if args.out:
        print(f"Exported {n} contacts.")


if __name__ == "__main__":
    main()
