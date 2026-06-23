"""
CityArts Teacher Finder — Flask web frontend.

Run:
    python app.py
Then open http://localhost:5000
"""
import io
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from src.db import DB_PATH, get_conn

app = Flask(__name__)


def _get_stats() -> dict:
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
            f"""SELECT school_name, city, state, district_name,
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
    state = request.args.get("state", "").upper()
    search = request.args.get("q", "").strip()
    only_email = request.args.get("only_email", "0") == "1"
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

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with get_conn() as conn:
        total = conn.execute(
            f"""SELECT COUNT(*) FROM staff s
                JOIN schools sc ON sc.id = s.school_id {where}""",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT s.teacher_name, s.title, s.email, s.resolution_method,
                       sc.school_name, sc.city, sc.state, sc.district_name,
                       sc.website_url
                FROM staff s
                JOIN schools sc ON sc.id = s.school_id {where}
                ORDER BY sc.state, sc.school_name, s.teacher_name
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "rows": [dict(r) for r in rows],
    })


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
    app.run(debug=True, port=5000)
