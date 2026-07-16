"""
CityArts Teacher Finder — packaged desktop launcher.

This is the PyInstaller entry point. It is NOT app.py's `python app.py` path
(that stays the server-side / dev entry point, unchanged) — this file wraps
it for a double-click, no-terminal experience:

  1. Point this run at the shared central server (REMOTE_INGEST_URL/SECRET),
     baked in at build time by CI (see desktop_config.py.example).
  2. Make sure Chromium is present (first launch only), showing a progress
     window instead of a silently "hung" app.
  3. Start the existing Flask app (templates/index.html, all /api/* routes
     — untouched) on a background thread.
  4. Open the user's default browser to it.
  5. Show a small "app is running" window so there's an obvious, visible
     way to stop it (closing a windowed/-noconsole exe has no console to
     Ctrl+C).
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from src.paths import is_frozen, user_data_dir

# ---------------------------------------------------------------------------
# Build-time config: REMOTE_INGEST_URL / REMOTE_INGEST_SECRET are baked into
# desktop_config.py by the GitHub Actions build (see
# .github/workflows/build-desktop-app.yml). That file is gitignored and
# never committed with a real secret. Local `python desktop_app.py` runs
# fall back to whatever's already in the environment (e.g. a local .env),
# same as app.py does on its own.
# ---------------------------------------------------------------------------
try:
    import desktop_config  # type: ignore
    os.environ.setdefault("REMOTE_INGEST_URL", desktop_config.REMOTE_INGEST_URL)
    os.environ.setdefault("REMOTE_INGEST_SECRET", desktop_config.REMOTE_INGEST_SECRET)
except ImportError:
    pass

# Playwright's Chromium download and cache go to a stable per-user
# location, not wherever the exe happens to be running from.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(user_data_dir() / "playwright-browsers"))


def _find_free_port(preferred: int = 5000) -> int:
    for port in (preferred, 5050, 5100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chromium_installed() -> bool:
    browsers_path = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    if not os.path.isdir(browsers_path):
        return False
    return any(name.startswith("chromium-") for name in os.listdir(browsers_path))


_PERCENT_RE = re.compile(r"(\d{1,3})%")


def _install_chromium(status_cb) -> None:
    """Run `playwright install chromium`, reporting progress via status_cb(
    percent_or_none, message). Uses the driver directly (not `python -m
    playwright`) because a frozen onefile exe has no separate interpreter
    to re-invoke with `-m`."""
    if not is_frozen():
        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        env = os.environ.copy()
    else:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        driver_executable, driver_cli = compute_driver_executable()
        cmd = [str(driver_executable), str(driver_cli), "install", "chromium"]
        env = get_driver_env()

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, creationflags=creationflags,
    )
    status_cb(None, "Downloading browser component (one-time, ~150MB)…")
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        m = _PERCENT_RE.search(line)
        if m:
            status_cb(int(m.group(1)), line)
        else:
            status_cb(None, line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Chromium install failed (exit code {proc.returncode}).")
    status_cb(100, "Done.")


def _run_chromium_install_with_ui() -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("CityArts Teacher Finder — first-time setup")
    root.geometry("420x140")
    root.resizable(False, False)

    label = tk.Label(root, text="Preparing the scraper (first launch only)…", wraplength=380, justify="left")
    label.pack(pady=(20, 10), padx=20)

    progress = ttk.Progressbar(root, orient="horizontal", length=380, mode="determinate")
    progress.pack(padx=20)

    detail = tk.Label(root, text="", wraplength=380, justify="left", fg="#555")
    detail.pack(pady=(8, 0), padx=20)

    error_holder: dict = {}

    def worker():
        def status_cb(percent, message):
            def update():
                if percent is not None:
                    progress["mode"] = "determinate"
                    progress["value"] = percent
                else:
                    progress["mode"] = "indeterminate"
                detail.config(text=message[:120])
            root.after(0, update)
        try:
            _install_chromium(status_cb)
        except Exception as exc:  # surfaced in the window, not lost to a vanished console
            error_holder["error"] = str(exc)
        finally:
            root.after(0, root.destroy)

    progress.start(15)
    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()

    if "error" in error_holder:
        _fatal_error_window(
            "Setup failed",
            "Couldn't download the browser component needed to scrape school "
            f"websites.\n\nDetails: {error_holder['error']}\n\n"
            "Check your internet connection and try running the app again.",
        )
        sys.exit(1)


def _all_states_marker():
    from src.paths import user_data_dir
    return user_data_dir() / "all_states_ingested.marker"


def _run_school_ingest_with_ui() -> None:
    """Load the national school directory for every state up front, on
    first launch, instead of the web app's default lazy per-state ingest
    (run_scraper() ingests a state the first time someone scrapes it). A
    non-technical user clicking "Start Scraping" and seeing "Ingesting
    school list..." followed immediately by "0 schools processed" (a real
    bug we hit and fixed separately, but the underlying wait itself is
    also just confusing UX for a packaged app) is a worse experience than
    paying that wait once, here, with an explicit "why is this taking a
    while" message.

    Marked with a file in user_data_dir(), not just the in-memory
    _topped_up_states set, because this needs to survive across app
    launches (each launch is a fresh process) — without it every single
    startup would re-scan the ~100k-row national CSV for nothing.

    Failure here is NOT fatal: unlike the Chromium install (scraping
    literally cannot run without it), a failed eager ingest just means
    run_scraper()'s existing lazy per-state top-up (with its own error
    surfacing in the Scrape tab) does the job later instead. No need for
    two different failure-handling paths for the same underlying call.
    """
    marker = _all_states_marker()
    if marker.exists():
        from src.ingest import ALL_US_STATES
        from src.scraper import mark_states_ingested
        mark_states_ingested(ALL_US_STATES)
        return

    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("CityArts Teacher Finder — first-time setup")
    root.geometry("420x140")
    root.resizable(False, False)

    label = tk.Label(
        root, text="Loading the national school directory (first launch only)…",
        wraplength=380, justify="left",
    )
    label.pack(pady=(20, 10), padx=20)

    progress = ttk.Progressbar(root, orient="horizontal", length=380, mode="indeterminate")
    progress.pack(padx=20)

    detail = tk.Label(root, text="", wraplength=380, justify="left", fg="#555")
    detail.pack(pady=(8, 0), padx=20)

    def worker():
        from src.ingest import ALL_US_STATES, ingest_schools
        from src.scraper import mark_states_ingested

        def on_progress(scanned, inserted):
            def update():
                detail.config(text=f"Scanned {scanned:,} records, loaded {inserted:,} schools…")
            root.after(0, update)
        try:
            ingest_schools(ALL_US_STATES, on_progress=on_progress)
            marker.write_text("done")
            mark_states_ingested(ALL_US_STATES)
        except Exception as exc:
            # Non-fatal (see docstring) — just log it; the lazy per-state
            # path in run_scraper() will retry and surface any real error
            # once the user actually starts a scrape.
            print(f"Eager school ingest failed, will retry lazily on scrape: {exc}")
        finally:
            root.after(0, root.destroy)

    progress.start(15)
    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()


def _fatal_error_window(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
    except Exception:
        pass  # last resort: process just exits


def _run_status_window(url: str) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("CityArts Teacher Finder")
    root.geometry("420x160")
    root.resizable(False, False)

    tk.Label(
        root,
        text="CityArts Teacher Finder is running.",
        font=("Segoe UI", 11, "bold"),
    ).pack(pady=(20, 6))
    tk.Label(root, text=f"Open in your browser: {url}", wraplength=380).pack(pady=(0, 6))
    tk.Label(
        root,
        text="Keep this window open while scraping. Closing it stops the app.",
        wraplength=380, fg="#555",
    ).pack(pady=(0, 12))

    tk.Button(root, text="Open in browser", command=lambda: webbrowser.open(url)).pack(pady=(0, 6))
    tk.Button(root, text="Quit", command=lambda: os._exit(0)).pack()

    root.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))
    root.mainloop()


def main() -> None:
    if not _chromium_installed():
        _run_chromium_install_with_ui()

    _run_school_ingest_with_ui()

    port = _find_free_port()
    os.environ["PORT"] = str(port)

    from app import app  # local import: only after env vars above are set

    def serve():
        from werkzeug.serving import run_simple
        run_simple("127.0.0.1", port, app, threaded=True)

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    webbrowser.open(url)

    _run_status_window(url)


if __name__ == "__main__":
    main()
