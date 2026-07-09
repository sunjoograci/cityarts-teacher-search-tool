"""
CityArts Teacher Finder — Flask web frontend.

Run:
    python app.py
Then open http://localhost:5000
"""
import asyncio
import io
import os
import sqlite3
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_file

from src.db import DB_PATH, get_conn, group_teacher_rows, init_db

app = Flask(__name__)

init_db()

# ---------------------------------------------------------------------------
# Optional remote scraper service (needed on Vercel, which can't run
# Playwright). If SCRAPER_SERVICE_URL is set, scrape requests are proxied
# there instead of running locally.
# ---------------------------------------------------------------------------
_SCRAPER_SERVICE_URL = os.environ.get("SCRAPER_SERVICE_URL", "").rstrip("/")
_SCRAPE_SHARED_SECRET = os.environ.get("SCRAPE_SHARED_SECRET")

# ---------------------------------------------------------------------------
# Background scrape job state
# ---------------------------------------------------------------------------

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
    "stop_requested": False,
}


def _run_scrape_thread(states: list[str], rescrape: bool) -> None:
    from src.scraper import run_scraper

    def on_progress(current, total, school_name, status):
        _scrape_state["current"] = current
        _scrape_state["total"] = total
        _scrape_state["current_school"] = school_name
        _scrape_state["last_status"] = status

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


_ON_VERCEL = bool(os.environ.get("VERCEL"))


def _proxy_headers() -> dict:
    return {"X-Scrape-Secret": _SCRAPE_SHARED_SECRET} if _SCRAPE_SHARED_SECRET else {}


@app.route("/api/scrape/start", methods=["POST"])
def api_scrape_start():
    if _SCRAPER_SERVICE_URL:
        resp = requests.post(
            f"{_SCRAPER_SERVICE_URL}/scrape/start",
            json=request.get_json(silent=True) or {},
            headers=_proxy_headers(),
            timeout=15,
        )
        return jsonify(resp.json()), resp.status_code
    if _ON_VERCEL:
        return jsonify({"error": "Scraping isn't configured yet — set SCRAPER_SERVICE_URL to point at a deployed scraper service."}), 403
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
            "stop_requested": False,
        })
    threading.Thread(target=_run_scrape_thread, args=(states, rescrape), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/scrape/stop", methods=["POST"])
def api_scrape_stop():
    if _SCRAPER_SERVICE_URL:
        resp = requests.post(
            f"{_SCRAPER_SERVICE_URL}/scrape/stop",
            headers=_proxy_headers(),
            timeout=15,
        )
        return jsonify(resp.json()), resp.status_code
    with _scrape_lock:
        if not _scrape_state["running"]:
            return jsonify({"error": "No scrape is running."}), 409
        _scrape_state["stop_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/scrape/status")
def api_scrape_status():
    if _SCRAPER_SERVICE_URL:
        resp = requests.get(
            f"{_SCRAPER_SERVICE_URL}/scrape/status",
            headers=_proxy_headers(),
            timeout=15,
        )
        return jsonify(resp.json()), resp.status_code
    s = dict(_scrape_state)
    elapsed = None
    eta = None
    started_at_iso = None
    if s["started_at"]:
        import datetime
        elapsed = time.time() - s["started_at"]
        started_at_iso = datetime.datetime.fromtimestamp(
            s["started_at"], tz=datetime.timezone.utc
        ).isoformat()
        if s["current"] > 0 and s["total"] > 0 and s["running"]:
            rate = s["current"] / elapsed
            remaining = s["total"] - s["current"]
            eta = remaining / rate if rate > 0 else None
    s["elapsed"] = elapsed
    s["eta"] = eta
    s["started_at_iso"] = started_at_iso
    # Count teachers added during this scrape session
    if started_at_iso and DB_PATH.exists():
        with get_conn() as conn:
            s["new_teachers"] = conn.execute(
                "SELECT COUNT(*) FROM staff WHERE added_at >= ?", (started_at_iso,)
            ).fetchone()[0]
    else:
        s["new_teachers"] = 0
    return jsonify(s)


def _get_stats() -> dict:
    if not DB_PATH.exists():
        return {"schools_total": 0, "schools_with_url": 0, "scraped": 0,
                "teachers_total": 0, "teachers_with_email": 0}
    with get_conn() as conn:
        schools_total = conn.execute("SELECT COUNT(*) FROM schools").fetchone()[0]
        schools_with_url = conn.execute(
            "SELECT COUNT(*) FROM schools WHERE website_url IS NOT NULL"
        ).fetchone()[0]
        scraped = conn.execute(
            "SELECT COUNT(*) FROM schools WHERE scraped=1"
        ).fetchone()[0]
        teachers_total = conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0]
        teachers_with_email = conn.execute(
            "SELECT COUNT(*) FROM staff WHERE email IS NOT NULL AND email != ''"
        ).fetchone()[0]
    return {
        "schools_total": schools_total,
        "schools_with_url": schools_with_url,
        "scraped": scraped,
        "teachers_total": teachers_total,
        "teachers_with_email": teachers_with_email,
    }


@app.route("/")
def index():
    stats = _get_stats()
    return render_template("index.html", stats=stats)


@app.route("/api/stats")
def api_stats():
    return jsonify(_get_stats())


@app.route("/api/schools")
def api_schools():
    state = request.args.get("state", "").upper()
    search = request.args.get("q", "").strip()
    only_url = request.args.get("only_url", "0") == "1"
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    clauses, params = [], []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if search:
        clauses.append("(school_name LIKE ? OR city LIKE ? OR district_name LIKE ?)")
        params += [f"%{search}%"] * 3
    if only_url:
        clauses.append("website_url IS NOT NULL")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM schools {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id, school_name, city, state, district_name,
                       website_url, scraped, scrape_status
                FROM schools {where}
                ORDER BY school_name
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "rows": [dict(r) for r in rows],
    })


@app.route("/api/teachers")
def api_teachers():
    import datetime
    state = request.args.get("state", "").upper()
    search = request.args.get("q", "").strip()
    only_email = request.args.get("only_email", "0") == "1"
    added_since = request.args.get("added_since", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    clauses, params = [], []
    if state:
        clauses.append("sc.state = ?")
        params.append(state)
    if search:
        clauses.append(
            "(s.teacher_name LIKE ? OR sc.school_name LIKE ? OR sc.city LIKE ?)"
        )
        params += [f"%{search}%"] * 3
    if only_email:
        clauses.append("s.email IS NOT NULL AND s.email != ''")
    if added_since:
        now = datetime.datetime.now(datetime.timezone.utc)
        if added_since == "today":
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif added_since == "week":
            cutoff = now - datetime.timedelta(days=7)
        elif added_since == "month":
            cutoff = now - datetime.timedelta(days=30)
        else:
            try:
                cutoff = datetime.datetime.fromisoformat(added_since)
            except ValueError:
                cutoff = None
        if cutoff:
            clauses.append("s.added_at >= ?")
            params.append(cutoff.isoformat())

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.teacher_name, s.title, s.email, s.resolution_method,
                       sc.school_name, sc.city, sc.state, sc.district_name,
                       sc.website_url
                FROM staff s
                JOIN schools sc ON sc.id = s.school_id {where}
                ORDER BY sc.state, sc.school_name, s.teacher_name""",
            params,
        ).fetchall()

    grouped = group_teacher_rows(rows)
    total = len(grouped)
    start = (page - 1) * per_page
    page_rows = grouped[start:start + per_page]

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "rows": page_rows,
    })


@app.route("/api/art-schools")
def api_art_schools():
    state = request.args.get("state", "").upper()
    search = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    clauses = ["is_arts_school = 1"]
    params = []
    if state:
        clauses.append("sc.state = ?")
        params.append(state)
    if search:
        clauses.append("(sc.school_name LIKE ? OR sc.city LIKE ? OR sc.district_name LIKE ?)")
        params += [f"%{search}%"] * 3

    where = "WHERE " + " AND ".join(clauses)

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM schools sc {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT sc.id, sc.school_name, sc.city, sc.state, sc.district_name,
                       sc.website_url, sc.scraped, sc.scrape_status,
                       COUNT(s.id) as teacher_count
                FROM schools sc
                LEFT JOIN staff s ON s.school_id = sc.id
                {where}
                GROUP BY sc.id
                ORDER BY sc.scraped DESC, sc.school_name
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "rows": [dict(r) for r in rows],
    })


@app.route("/api/teachers/<ids>", methods=["DELETE"])
def api_delete_teacher(ids):
    id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if not id_list:
        return jsonify({"error": "Invalid id"}), 400
    with get_conn() as conn:
        placeholders = ",".join("?" * len(id_list))
        changes = conn.execute(
            f"DELETE FROM staff WHERE id IN ({placeholders})", id_list
        ).rowcount
    if changes:
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


@app.route("/api/schools/<int:school_id>", methods=["DELETE"])
def api_delete_school(school_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM staff WHERE school_id = ?", (school_id,))
        changes = conn.execute("DELETE FROM schools WHERE id = ?", (school_id,)).rowcount
    if changes:
        return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


@app.route("/export/teachers.xlsx")
def export_teachers_xlsx():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    state = request.args.get("state", "").upper()
    only_email = request.args.get("only_email", "0") == "1"

    clauses, params = [], []
    if state:
        clauses.append("sc.state = ?")
        params.append(state)
    if only_email:
        clauses.append("s.email IS NOT NULL AND s.email != ''")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT s.teacher_name, s.title, s.email, s.resolution_method,
                       sc.school_name, sc.city, sc.state, sc.district_name,
                       sc.website_url
                FROM staff s
                JOIN schools sc ON sc.id = s.school_id {where}
                ORDER BY sc.state, sc.school_name, s.teacher_name""",
            params,
        ).fetchall()

    rows = group_teacher_rows(rows)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Art Teachers"

    headers = [
        "Teacher Name", "Title", "Email", "Contact Method",
        "School", "City", "State", "District", "School Website",
    ]
    header_fill = PatternFill("solid", fgColor="1A3C5E")
    header_font = Font(bold=True, color="FFFFFF")

    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row in rows:
        email_val = row["email"] or ""
        method = row["resolution_method"] or ""
        is_form = method in ("send_message_button", "contact_form") or email_val.startswith("http")
        ws.append([
            row["teacher_name"],
            row["title"],
            "reach out on website" if is_form else email_val,
            method,
            row["school_name"],
            row["city"],
            row["state"],
            row["district_name"],
            row["website_url"] or "",
        ])

    col_widths = [22, 24, 28, 16, 32, 16, 6, 28, 36]
    for i, width in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"art_teachers{'_' + state if state else ''}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/export/schools.xlsx")
def export_schools_xlsx():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    state = request.args.get("state", "").upper()
    clauses, params = [], []
    if state:
        clauses.append("state = ?")
        params.append(state)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT school_name, city, state, district_name,
                       website_url, scraped, scrape_status
                FROM schools {where}
                ORDER BY school_name""",
            params,
        ).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Schools"

    headers = ["School", "City", "State", "District", "Website", "Scraped", "Status"]
    header_fill = PatternFill("solid", fgColor="1A3C5E")
    header_font = Font(bold=True, color="FFFFFF")

    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row in rows:
        ws.append([
            row["school_name"],
            row["city"],
            row["state"],
            row["district_name"],
            row["website_url"] or "",
            "Yes" if row["scraped"] else "No",
            row["scrape_status"] or "",
        ])

    col_widths = [32, 16, 6, 28, 40, 8, 24]
    for i, width in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"schools{'_' + state if state else ''}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    if not DB_PATH.exists():
        print("No database found. Run: python main.py ingest --states KS")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True, use_reloader=False)
