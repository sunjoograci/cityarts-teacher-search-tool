# Running the scraper on your own computer

This lets you scrape school websites from your home internet connection
instead of the shared server — schools that block the server's connection
often work fine from a home connection. Everything you find still lands in
the same shared database everyone else sees on the website.

You do **not** need to install Python, or anything else beforehand.

## 1. Download

Get the installer for your computer from the link your team sent you.

- **Windows:** `CityArtsTeacherFinder.exe`
- **Mac:** there are two versions — you need the one matching your Mac's
  chip, or it won't open at all. Check first: **Apple menu → About This
  Mac**.
  - Says **"Chip"** (e.g. Apple M1/M2/M3/M4) → download
    `CityArtsTeacherFinder-mac-ARM64.zip`
  - Says **"Processor"** (e.g. Intel Core i5/i7) → download
    `CityArtsTeacherFinder-mac-X64.zip`

  Unzip it, you'll get `CityArtsTeacherFinder.app`.

## 2. First open: your computer will warn you

Neither file is signed with a paid developer certificate, so Windows and Mac
both show a warning the very first time. This is expected — click through
it once, the way you would for any other app you've downloaded from
someone who isn't the App Store or Microsoft Store.

**Windows — "Windows protected your PC":**
1. Click **More info**
2. Click **Run anyway**

**Mac:** neither the app nor the .exe is signed with a paid developer
certificate ($99/year, which we don't have), so macOS Gatekeeper blocks it
by default. Which warning you see depends on your macOS version:

- **"Apple could not verify... is free of malware"** (macOS Sequoia and
  newer — right-click no longer bypasses this one):
  1. Try to open the app once and dismiss the warning.
  2. Open **System Settings → Privacy & Security**, scroll down — you'll
     see a note that `CityArtsTeacherFinder` was blocked, with an **Open
     Anyway** button. Click it and authenticate.
  3. Double-click the app again, click **Open Anyway** in the next dialog.

  Or, faster, in Terminal:
  ```
  xattr -cr ~/Downloads/CityArtsTeacherFinder.app
  ```
  (adjust the path if you moved the app first), then double-click it
  normally.

- **"cannot be opened because the developer cannot be verified"** (older
  macOS):
  1. Right-click (or Control-click) `CityArtsTeacherFinder.app`
  2. Click **Open**
  3. Click **Open** again in the dialog that appears

You only have to do this once per computer.

## 3. First launch: two one-time setup steps

The first time it opens, you'll see a couple of small windows in a row —
neither is stuck, just leave them:

1. **"Preparing the scraper (first launch only)…"** — downloading the
   browser component it needs (about 150MB). Can take a few minutes
   depending on your internet speed.
2. **"Loading the national school directory (first launch only)…"** —
   downloading the list of every public school in the country so it's
   ready the moment you pick a state to scrape. Can also take a minute or
   two.

Both only happen once; every launch after this is fast.

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

- **"You can't open the application because it is not supported on this
  Mac"** — you downloaded the wrong version. Check **Apple menu → About
  This Mac** for "Chip" vs "Processor" and grab the matching file (see
  Download step above).
- **Nothing happens after clicking "Run anyway" / "Open"** — give it 10–15
  seconds; it's starting up in the background before it opens your browser.
- **The progress window seems frozen** — check your internet connection;
  the download will resume/retry rather than restart from zero if you
  relaunch the app.
- **A browser tab opens but shows an error / won't connect** — close the
  app entirely (including the small status window) and reopen it.
- Still stuck? Send a screenshot to whoever gave you the download link.
