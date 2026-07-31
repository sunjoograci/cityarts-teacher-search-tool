# CITYarts Teacher Finder

An automated tool that finds art teacher contacts at public schools across
the US. School by school, it discovers staff directories, extracting names
and titles and resolving verified email addresses so that outreach can happen
at scale instead of one manually-researched school at a time.

## The problem

CITYarts, an arts education nonprofit, needs contact info for art teachers
at public schools nationwide in order to reach out about programs and
resources. Historically this meant someone manually visiting each school's
website, hunting for a staff directory, identifying who teaches art, and
copying down (or guessing) an email address. This slow, tedious process
doesn't scale beyond a handful of schools a day.

The tool I created replaces that manual research: point it at a list of states (or
the whole country) and it ingests the official list of public schools,
crawls each school's website for a staff directory, identifies art
teachers, and resolves an email address for each one. What used to
be days of staff time turns into an unattended batch job.

## How it works

The pipeline runs in stages:

1. **Ingest** — downloads the [NCES Common Core of Data (CCD)](https://nces.ed.gov/ccd/)
   public school directory and loads schools (name, address, website,
   district, level) into a local SQLite database.
2. **Scrape** — uses [Playwright](https://playwright.dev/) to visit each
   school's website, locate its staff/faculty directory (handling
   pagination, JS-rendered content, blocked/slow-loading pages, and a
   handful of common site layouts), and extract staff names, titles, and
   any visible email addresses. A classifier narrows the results down to
   art/visual-arts teachers specifically.
3. **Enrich** — for staff without a scraped email, resolves one via a
   three-tier fallback (see below).
4. **Export** — results are available as CSV/XLSX, or browsable in the
   included web UI.

### Three-tier email resolution

Not every staff directory publishes email addresses directly, so
`src/enrich.py` resolves them in priority order, stopping at the first
match:

1. **Scraped directly** — if the school's own staff page already lists an
   email (as plain text, a `mailto:` link, or a JS/Cloudflare-obfuscated
   address that gets decoded), that's used as-is. This is the fastest and
   most reliable source, since it's exactly what the school published.
2. **Hunter.io domain search** — if no email was found on the page, the
   teacher's name and the school's domain are sent to the
   [Hunter.io](https://hunter.io/) API, which searches for a known email
   at that domain.
3. **Pattern generation + SMTP verification** — if Hunter comes up empty,
   common email patterns are generated from the teacher's name and domain
   (`first.last@domain`, `flast@domain`, `first@domain`, etc.), and each
   candidate is checked against the domain's mail server via a live SMTP
   `RCPT TO` handshake (no message is actually sent). The first pattern the
   mail server confirms as deliverable is kept.

Every resolved contact records which tier found it (`scraped`, `hunter`, or
`smtp_verified`), so results can be filtered or spot-checked by confidence
level.

## Tech stack

- **Python** — pipeline and web app
- **Playwright** — headless browser automation for scraping JS-rendered
  staff directories
- **SQLite** — local storage for schools and staff records
- **Flask** — web UI for browsing results, triggering scrapes, and
  exporting data
- **Hunter.io API** — email discovery fallback
- **NCES CCD** — public dataset of every K-12 public school in the US

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/sunjoograci/cityarts-teacher-search-tool.git
cd cityarts-teacher-search-tool
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure your `.env`

Copy the example file and fill in your own keys:

```bash
cp .env.example .env
```

At minimum, set `HUNTER_API_KEY` (get one at
[hunter.io](https://hunter.io/)) to enable tier 2 of email resolution.
Everything else in `.env.example` is optional and only needed for
distributed/remote deployment setups — see the comments in that file.

### 3. Run it

**Command line**, stage by stage:

```bash
python main.py ingest --states TX KS --limit 10   # load a small sample of schools
python main.py scrape --states TX KS --limit 10   # crawl their staff directories
python main.py enrich --limit 50                   # resolve missing emails
python main.py export --states TX KS --out results.csv
```

Drop `--limit` to run against every ingested school. Run
`python main.py ingest --states TX KS` with no `--limit` to pull in the
full national dataset filtered to those states (or use
`--states` with all 50 state codes for the whole country).

**Web UI**, for a point-and-click experience with progress bars and an
exportable results table:

```bash
python app.py
```

Then open `http://localhost:5000`.

A packaged, no-Python-required desktop build is also available — see
[`DESKTOP_APP_SETUP.md`](DESKTOP_APP_SETUP.md) for that version.

## Demo

A recording of the tool in action is linked/included separately.

## Tests

```bash
pytest
```
