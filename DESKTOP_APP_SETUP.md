# Running the scraper on your own computer

This lets you scrape school websites from your home internet connection
instead of the shared server — schools that block the server's connection
often work fine from a home connection. Everything you find still lands in
the same shared database everyone else sees on the website.

You do **not** need to install Python, or anything else beforehand.

## 1. Download

Get the installer for your computer from the link your team sent you:

- Windows: `CityArtsTeacherFinder.exe`
- Mac: `CityArtsTeacherFinder-mac.zip` — unzip it first, you'll get
  `CityArtsTeacherFinder.app`

## 2. First open: your computer will warn you

Neither file is signed with a paid developer certificate, so Windows and Mac
both show a warning the very first time. This is expected — click through
it once, the way you would for any other app you've downloaded from
someone who isn't the App Store or Microsoft Store.

**Windows — "Windows protected your PC":**
1. Click **More info**
2. Click **Run anyway**

**Mac — "cannot be opened because the developer cannot be verified":**
1. Right-click (or Control-click) `CityArtsTeacherFinder.app`
2. Click **Open**
3. Click **Open** again in the dialog that appears

You only have to do this once per computer.

## 3. First launch: a one-time download

The first time it opens, you'll see a small window that says something
like "Preparing the scraper (first launch only)…" with a progress bar.
It's downloading the browser component it needs (about 150MB) — this can
take a few minutes depending on your internet speed. **Don't close the
window** — it isn't stuck, just leave it. This only happens once; every
launch after this is fast.

## 4. Using it

Once it's ready, your normal web browser opens automatically to the same
search tool you already know from the website — pick a state, hit
**Start Scrape**, and watch the progress bar. A small separate window
titled "CityArts Teacher Finder" also appears — **keep that window open**
while you're scraping. Closing it stops the app. When you're done for the
day, just close that window.

Results you find are sent straight to the shared server as they come in —
you don't need to export or send anyone a file.

## Troubleshooting

- **Nothing happens after clicking "Run anyway" / "Open"** — give it 10–15
  seconds; it's starting up in the background before it opens your browser.
- **The progress window seems frozen** — check your internet connection;
  the download will resume/retry rather than restart from zero if you
  relaunch the app.
- **A browser tab opens but shows an error / won't connect** — close the
  app entirely (including the small status window) and reopen it.
- Still stuck? Send a screenshot to whoever gave you the download link.
