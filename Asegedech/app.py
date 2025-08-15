import os
import sqlite3
from datetime import datetime
from flask import (
    Flask, jsonify, request, session,
    send_from_directory, redirect, url_for
)
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# ---------- App / Paths ----------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "site.db")
SECRET = os.environ.get("FLASK_SECRET", "change-me")

# Serve all your static files (index.html, Volunteer.html, admin_tasks.html, images, etc.)
app = Flask(__name__, static_folder=APP_DIR, static_url_path="")
app.secret_key = SECRET
CORS(app, supports_credentials=True)


# ---------- DB Helpers ----------
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = get_db()
    cur = con.cursor()

    # Admins (login)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      created_at TEXT NOT NULL
    )""")

    # Tasks (what admins create; volunteers see active ones)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      description TEXT,
      max_volunteers INTEGER,
      slot_duration_mins INTEGER DEFAULT 60,
      type TEXT CHECK(type IN ('recurring','event')) NOT NULL DEFAULT 'recurring',
      days_of_week TEXT,   -- CSV e.g. "Mon,Wed,Fri"
      time_windows TEXT,   -- "09:00-12:00|14:00-17:00"
      event_dates TEXT,    -- CSV ISO dates: "2025-08-20,2025-08-21"
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )""")

    # Optional: appointments table (you can ignore if not using)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id INTEGER NOT NULL,
      date TEXT NOT NULL,
      start_time TEXT NOT NULL,
      end_time TEXT NOT NULL,
      phone TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""")

    con.commit()
    con.close()

def seed_admin():
    """Seed both 'admin@example.com' and 'admin' (password 'admin')."""
    con = get_db()
    cur = con.cursor()

    def ensure(email):
        cur.execute("SELECT id FROM admins WHERE email=?", (email,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO admins (email, password_hash, created_at) VALUES (?,?,?)",
                (email, generate_password_hash("admin"), datetime.utcnow().isoformat())
            )

    ensure("admin@example.com")
    ensure("admin")
    con.commit()
    con.close()


# ---------- Serialization helpers ----------
def row_to_task(r):
    return {
        "id": r["id"],
        "title": r["title"],
        "description": r["description"] or "",
        "maxVolunteers": r["max_volunteers"],
        "slotDurationMins": r["slot_duration_mins"],
        "type": r["type"],
        "daysOfWeek": [d for d in (r["days_of_week"] or "").split(",") if d],
        "timeWindows": [
            {"start": p.split("-")[0], "end": p.split("-")[1]}
            for p in (r["time_windows"] or "").split("|") if "-" in p
        ],
        "eventDates": [d for d in (r["event_dates"] or "").split(",") if d],
        "active": bool(r["active"]),
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"]
    }

def parse_time_windows(windows):
    """
    Expect list like [{"start":"09:00","end":"12:00"}, ...]
    Store as "09:00-12:00|14:00-17:00"
    """
    parts = []
    for w in (windows or []):
        s = (w.get("start") or "").strip()
        e = (w.get("end") or "").strip()
        if s and e:
            parts.append(f"{s}-{e}")
    return "|".join(parts)

def to_csv(items):
    return ",".join([x for x in (items or []) if x])


# ---------- HTML Routes ----------
@app.route("/")
def home():
    # Your landing page unchanged
    return send_from_directory(APP_DIR, "index.html")

# Admin UI (works at /admin and /admin_tasks.html)
@app.route("/admin")
@app.route("/admin/")
def admin_ui():
    # support logout via ?logout=1
    if request.args.get("logout"):
        session.clear()
    return send_from_directory(APP_DIR, "admin_tasks.html")

@app.route("/admin_tasks")
@app.route("/admin_tasks.html")
def admin_legacy():
    # Keep this for compatibility; preserves ?logout=1 and other params
    if request.args.get("logout"):
        session.clear()
    return send_from_directory(APP_DIR, "admin_tasks.html")

# Serve other static files (Volunteer.html, images, etc.)
@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(APP_DIR, path)


# ---------- Auth API ----------
@app.post("/api/login")
def api_login():
    data = request.get_json(force=True) or {}
    identifier = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    # Allow "admin" OR "admin@example.com"
    if "@" in identifier:
        query = "SELECT id, email, password_hash FROM admins WHERE email=?"
        params = (identifier,)
    else:
        query = "SELECT id, email, password_hash FROM admins WHERE email IN (?, ?)"
        params = (identifier, f"{identifier}@example.com")

    con = get_db()
    cur = con.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    con.close()

    if row and check_password_hash(row["password_hash"], password):
        session["admin_id"] = row["id"]
        session["admin_email"] = row["email"]
        return jsonify({"ok": True, "email": row["email"]})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401

@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/me")
def api_me():
    if session.get("admin_id"):
        return jsonify({"ok": True, "email": session.get("admin_email")})
    return jsonify({"ok": False})


# ---------- Public API (Volunteer consumes this) ----------
@app.get("/api/tasks")
def api_tasks_public():
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id DESC")
    out = [row_to_task(r) for r in cur.fetchall()]
    con.close()
    return jsonify(out)


# ---------- Admin Task CRUD ----------
def require_admin():
    if not session.get("admin_id"):
        return False
    return True

@app.get("/api/admin/tasks")
def api_admin_list():
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    out = [row_to_task(r) for r in cur.fetchall()]
    con.close()
    return jsonify(out)

@app.post("/api/admin/tasks")
def api_admin_create():
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    now = datetime.utcnow().isoformat()
    description = (data.get("description") or "").strip()
    max_vol = data.get("maxVolunteers", None)
    slot_mins = int(data.get("slotDurationMins") or 60)
    type_ = data.get("type") if data.get("type") in ("recurring", "event") else "recurring"

    days_csv = to_csv(data.get("daysOfWeek"))
    windows_csv = parse_time_windows(data.get("timeWindows"))
    dates_csv = to_csv(data.get("eventDates"))
    active = 1 if data.get("active") else 0

    con = get_db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO tasks (title, description, max_volunteers, slot_duration_mins, type,
                           days_of_week, time_windows, event_dates, active, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (title, description, max_vol, slot_mins, type_, days_csv, windows_csv, dates_csv, active, now, now))
    con.commit()
    cur.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,))
    task = row_to_task(cur.fetchone())
    con.close()
    return jsonify(task), 201

@app.put("/api/admin/tasks/<int:task_id>")
def api_admin_update(task_id):
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    now = datetime.utcnow().isoformat()
    description = (data.get("description") or "").strip()
    max_vol = data.get("maxVolunteers", None)
    slot_mins = int(data.get("slotDurationMins") or 60)
    type_ = data.get("type") if data.get("type") in ("recurring", "event") else "recurring"

    days_csv = to_csv(data.get("daysOfWeek"))
    windows_csv = parse_time_windows(data.get("timeWindows"))
    dates_csv = to_csv(data.get("eventDates"))
    active = 1 if data.get("active") else 0

    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT id FROM tasks WHERE id=?", (task_id,))
    if not cur.fetchone():
        con.close()
        return jsonify({"error": "Task not found"}), 404

    cur.execute("""
        UPDATE tasks
        SET title=?, description=?, max_volunteers=?, slot_duration_mins=?, type=?,
            days_of_week=?, time_windows=?, event_dates=?, active=?, updated_at=?
        WHERE id=?
    """, (title, description, max_vol, slot_mins, type_,
          days_csv, windows_csv, dates_csv, active, now, task_id))
    con.commit()
    cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    task = row_to_task(cur.fetchone())
    con.close()
    return jsonify(task)

@app.delete("/api/admin/tasks/<int:task_id>")
def api_admin_delete(task_id):
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401
    con = get_db()
    cur = con.cursor()
    cur.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ---------- (Optional) Appointments ----------
@app.post("/api/appointments")
def api_appointments_create():
    data = request.get_json(force=True) or {}
    task_id = data.get("taskId")
    date = (data.get("date") or "").strip()
    start = (data.get("startTime") or "").strip()
    end = (data.get("endTime") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not (task_id and date and start and end and phone):
        return jsonify({"error": "Missing fields"}), 400
    if start >= end:
        return jsonify({"error": "End time must be after start time"}), 400

    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT * FROM tasks WHERE id=? AND active=1", (task_id,))
    task = cur.fetchone()
    if not task:
        con.close()
        return jsonify({"error": "Task not found or inactive"}), 404

    # Capacity check: count overlaps on same date
    cur.execute("SELECT start_time, end_time FROM appointments WHERE task_id=? AND date=?", (task_id, date))
    existing = cur.fetchall()
    num_overlap = sum(start < r["end_time"] and r["start_time"] < end for r in existing)
    max_vol = task["max_volunteers"]
    if max_vol is not None and num_overlap >= max_vol:
        con.close()
        return jsonify({"error": "That time slot is full."}), 409

    cur.execute("""INSERT INTO appointments (task_id, date, start_time, end_time, phone, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (task_id, date, start, end, phone, datetime.utcnow().isoformat()))
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ---------- Health ----------
@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})


# ---------- Main ----------
if __name__ == "__main__":
    init_db()
    seed_admin()
    app.run(host="127.0.0.1", port=5000, debug=True)
