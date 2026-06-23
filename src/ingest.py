"""
Step 1 — School ingestion from NCES CCD public school dataset.

Usage:
    python -m src.ingest --states TX KS [--limit 10]

The NCES CCD file is downloaded automatically if not present in data/.
Current URL targets the 2022-23 Public School Characteristics file.
"""
import argparse
import csv
import io
import logging
import sqlite3
import urllib.request
from pathlib import Path

from .db import get_conn, init_db

log = logging.getLogger(__name__)

# NCES CCD candidate URLs in priority order (most recent first).
# NCES frequently changes filenames; we try each until one responds 200.
NCES_CANDIDATE_URLS = [
    # 2024-25 preliminary (confirmed May 2025 release)
    "https://nces.ed.gov/sites/default/files/data-asset/ccd-common-core-data/2025/08/2024-25-common-core-data-ccd-preliminary-directory-files/2025046%20Preliminary%20Data%20Release%20CCD%20Nonfiscal_0.zip",
    # 2018-19 fallback (confirmed on pubschuniv.asp)
    "https://nces.ed.gov/ccd/Data/zip/ccd_sch_029_1819_w_0a_04082019_csv.zip",
]

DATA_DIR = Path(__file__).parent.parent / "data"
RAW_ZIP = DATA_DIR / "nces_ccd.zip"
RAW_CSV = DATA_DIR / "nces_ccd.csv"


MANUAL_DOWNLOAD_INSTRUCTIONS = """
----------------------------------------------------------------------
MANUAL DOWNLOAD REQUIRED
----------------------------------------------------------------------
The automatic NCES download failed. Please download the school data
manually:

  1. Go to: https://nces.ed.gov/ccd/files.asp
  2. Select:  School Year -> most recent year
              Level -> School
              File Type -> Directory (CSV)
  3. Download the ZIP and extract the CSV.
  4. Save the CSV to:
       {csv_path}
  5. Re-run:  python main.py ingest --states TX KS


Alternatively, use the ELSI Table Generator:
  https://nces.ed.gov/ccd/elsi/tableGenerator.aspx
----------------------------------------------------------------------
"""


def download_nces(force: bool = False) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if RAW_CSV.exists() and not force:
        log.info("NCES CSV already present, skipping download.")
        return

    if not RAW_ZIP.exists() or force:
        downloaded = False
        for url in NCES_CANDIDATE_URLS:
            try:
                log.info("Trying NCES URL: %s", url)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 200:
                        RAW_ZIP.write_bytes(resp.read())
                        log.info("Download complete from %s", url)
                        downloaded = True
                        break
            except Exception as exc:
                log.debug("  Failed (%s): %s", url, exc)

        if not downloaded:
            print(MANUAL_DOWNLOAD_INSTRUCTIONS.format(csv_path=RAW_CSV))
            raise SystemExit(
                "Automatic download failed. See instructions above to download manually."
            )

    import zipfile
    with zipfile.ZipFile(RAW_ZIP) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("No CSV found in NCES zip archive.")
        # Prefer the school-level file (contains "sch") over the LEA file
        school_csvs = [n for n in csv_names if "_sch_" in n.lower()]
        chosen = school_csvs[0] if school_csvs else csv_names[0]
        log.info("Extracting %s", chosen)
        with zf.open(chosen) as src, open(RAW_CSV, "wb") as dst:
            dst.write(src.read())
    log.info("Extracted %s", RAW_CSV.name)


def _normalize_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw in ("†", "–", "N", "M"):
        return None
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw


def ingest_schools(states: list[str], limit: int | None = None) -> int:
    download_nces()
    init_db()
    states_upper = {s.upper() for s in states}
    inserted = 0
    with get_conn() as conn:
        with open(RAW_CSV, encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # State column varies by file vintage
                state = (row.get("ST") or row.get("STABR") or "").strip().upper()
                if state not in states_upper:
                    continue
                # Skip closed schools
                status = (row.get("SY_STATUS_TEXT") or row.get("STATUS") or "").strip()
                if status not in ("Open", "New", "1", "3", ""):
                    continue
                nces_id = row.get("NCESSCH", "").strip()
                school_name = row.get("SCH_NAME", "").strip()
                city = row.get("LCITY", "").strip()
                district_name = row.get("LEA_NAME", "").strip()
                website_url = _normalize_url(row.get("WEBSITE", ""))
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO schools
                            (nces_id, school_name, city, state, district_name, website_url)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (nces_id, school_name, city, state, district_name, website_url),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        inserted += 1
                        if limit and inserted >= limit:
                            break
                except sqlite3.Error as exc:
                    log.warning("DB error for %s: %s", school_name, exc)
    log.info("Ingested %d schools for states: %s", inserted, states_upper)
    return inserted


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest NCES high school data.")
    parser.add_argument("--states", nargs="+", default=["TX", "KS"], help="State abbreviations")
    parser.add_argument("--limit", type=int, default=None, help="Max schools to ingest (for testing)")
    parser.add_argument("--force", action="store_true", help="Re-download NCES data")
    args = parser.parse_args()
    n = ingest_schools(args.states, args.limit)
    print(f"Done. {n} schools ingested.")


if __name__ == "__main__":
    main()
