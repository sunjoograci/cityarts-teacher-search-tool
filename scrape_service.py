"""
Standalone scraper service — deploy this on Render/Railway/Fly (NOT Vercel).

Playwright needs a real, long-running process, which Vercel's serverless
functions can't provide. This service exposes the same scrape endpoints the
main app already talks to, runs the scrape with Playwright, then commits the
updated data/schools.db back to GitHub so the Vercel-hosted app picks it up
on its next auto-deploy.

Run locally:
    python scrape_service.py
Run in production (Render):
    gunicorn scrape_service:app --bind 0.0.0.0:$PORT --timeout 600
"""
import asyncio
import os
import subprocess
import threading
import time

from flask import Flask, jsonify, request

from src.db import DB_PATH

app = Flask(__name__)

SHARED_SECRET = os.environ.get("SCRAPE_SHARED_SECRET")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # e.g. "sunjoograci/cityarts-teacher-search-tool"
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "cityarts-scraper-bot")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "scraper-bot@users.noreply.github.com")

_scrape_lock = threading.Lock()
_scrape_state: dict = {
    "running": False,
    "current": 0,
    "total": 0,
    "current_school": "",
    "last_status": "",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "states": [],
    "rescrape": False,
    "published": None,
    "stop_requested": False,
}


def _check_auth() -> bool:
    if not SHARED_SECRET:
        return True
    return request.headers.get("X-Scrape-Secret") == SHARED_SECRET


def _publish_db() -> str | None:
    """Commit and push the updated database so Vercel redeploys with fresh data.

    Returns an error message on failure, or None on success/skip.
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return None
    try:
        remote = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
        run = lambda *cmd: subprocess.run(cmd, check=True, capture_output=True, text=True)
        run("git", "config", "user.name", GIT_USER_NAME)
        run("git", "config", "user.email", GIT_USER_EMAIL)
        run("git", "add", str(DB_PATH))
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], capture_output=True
        )
        if status.returncode == 0:
            return None  # nothing changed
        run("git", "commit", "-m", "Automated scrape update")
        run("git", "push", remote, "HEAD:main")
        return None
    except subprocess.CalledProcessError as exc:
        return f"git publish failed: {exc.stderr}"


def _run_scrape_thread(states: list[str], rescrape: bool) -> None:
    from src.scraper import run_scraper

    def on_progress(current, total, school_name, status):
        _scrape_state["current"] = current
        _scrape_state["total"] = total
        _scrape_state["current_school"] = school_name
        _scrape_state["last_status"] = status
        # A real per-school terminal status means this school just finished —
        # publish now so partial progress survives even if the service
        # restarts or the run is stopped mid-batch. "scraping" is the
        # pre-scrape announcement; "ingesting"/"starting" are run-setup
        # phases during which nothing new is in the DB yet.
        if status not in ("scraping", "ingesting", "starting"):
            publish_error = _publish_db()
            _scrape_state["published"] = publish_error or "ok"

    def should_stop():
        return _scrape_state["stop_requested"]

    try:
        asyncio.run(run_scraper(
            states, rescrape_missed=rescrape, on_progress=on_progress, should_stop=should_stop,
        ))
    except Exception as exc:
        _scrape_state["error"] = str(exc)
    finally:
        _scrape_state["running"] = False
        _scrape_state["finished_at"] = time.time()
        publish_error = _publish_db()
        _scrape_state["published"] = publish_error or "ok"


@app.route("/scrape/start", methods=["POST"])
def scrape_start():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    with _scrape_lock:
        if _scrape_state["running"]:
            return jsonify({"error": "A scrape is already running."}), 409
        data = request.get_json(silent=True) or {}
        states = [s.upper() for s in data.get("states", ["TX"])]
        rescrape = bool(data.get("rescrape", False))
        _scrape_state.update({
            "running": True,
            "current": 0,
            "total": 0,
            "current_school": "Preparing…",
            "last_status": "",
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
            "states": states,
            "rescrape": rescrape,
            "published": None,
            "stop_requested": False,
        })
    threading.Thread(target=_run_scrape_thread, args=(states, rescrape), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/scrape/stop", methods=["POST"])
def scrape_stop():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    with _scrape_lock:
        if not _scrape_state["running"]:
            return jsonify({"error": "No scrape is running."}), 409
        _scrape_state["stop_requested"] = True
    return jsonify({"ok": True})


@app.route("/scrape/status")
def scrape_status():
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401
    s = dict(_scrape_state)
    elapsed = None
    eta = None
    if s["started_at"]:
        elapsed = time.time() - s["started_at"]
        if s["current"] > 0 and s["total"] > 0 and s["running"]:
            rate = s["current"] / elapsed
            remaining = s["total"] - s["current"]
            eta = remaining / rate if rate > 0 else None
    s["elapsed"] = elapsed
    s["eta"] = eta
    return jsonify(s)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True, use_reloader=False)
