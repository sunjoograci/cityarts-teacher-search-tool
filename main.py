"""
CityArts Teacher Finder — main CLI.

Commands:
    ingest   Download NCES data and populate the schools table.
    scrape   Crawl school websites for art teacher staff pages.
    enrich   Resolve missing emails via Hunter.io and/or SMTP verification.
    export   Write a CSV of found teachers.

Quick test run (10 schools, TX + KS):
    python main.py ingest --states TX KS --limit 10
    python main.py scrape --states TX KS --limit 10
    python main.py enrich --limit 50
    python main.py export --states TX KS --out results.csv
"""
import argparse
import logging
import sys


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        prog="cityarts",
        description="Find art teacher contacts at US high schools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Download NCES data and ingest schools.")
    p_ingest.add_argument("--states", nargs="+", default=["TX", "KS"])
    p_ingest.add_argument("--limit", type=int, default=None, help="Max schools (for testing)")
    p_ingest.add_argument("--force", action="store_true", help="Re-download NCES file")
    p_ingest.add_argument(
        "--file", default=None,
        help="Path to a local NCES data file (.csv, .xls, .xlsx). Skips automatic download.",
    )

    # scrape
    p_scrape = sub.add_parser("scrape", help="Scrape school staff directories.")
    p_scrape.add_argument("--states", nargs="+", default=["TX", "KS"])
    p_scrape.add_argument("--limit", type=int, default=None)

    # enrich
    p_enrich = sub.add_parser("enrich", help="Enrich emails via Hunter.io / SMTP.")
    p_enrich.add_argument("--limit", type=int, default=None)

    # check-contacts
    p_contacts = sub.add_parser(
        "check-contacts",
        help="Re-visit schools and look for 'send a message' links for staff with no email.",
    )
    p_contacts.add_argument("--states", nargs="+", default=["KS"])
    p_contacts.add_argument("--limit", type=int, default=None)
    p_contacts.add_argument("--reset", action="store_true", help="Re-check records already marked contact_form")

    # export
    p_export = sub.add_parser("export", help="Export results as CSV.")
    p_export.add_argument("--states", nargs="+", default=["TX", "KS"])
    p_export.add_argument("--out", default=None, help="Output file path (default: stdout)")

    args = parser.parse_args()

    if args.command == "ingest":
        from pathlib import Path
        from src.ingest import ingest_schools
        n = ingest_schools(args.states, args.limit, source_file=Path(args.file) if args.file else None)
        print(f"Ingested {n} schools.")

    elif args.command == "scrape":
        import asyncio
        from src.scraper import run_scraper
        asyncio.run(run_scraper([s.upper() for s in args.states], args.limit))

    elif args.command == "enrich":
        from src.enrich import enrich_staff
        enrich_staff(args.limit)

    elif args.command == "check-contacts":
        import asyncio
        from src.contact_forms import run_contact_form_check
        asyncio.run(run_contact_form_check([s.upper() for s in args.states], args.limit, args.reset))

    elif args.command == "export":
        from src.export import export_csv
        n = export_csv([s.upper() for s in args.states], args.out)
        if args.out:
            print(f"Exported {n} contacts to {args.out}")


if __name__ == "__main__":
    main()
