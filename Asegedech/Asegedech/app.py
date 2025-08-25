import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, session, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------- Config ----------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "appointments.db"))

app = Flask(__name__, static_url_path="", static_folder=".")
app.secret_key = os.environ.get("FLASK_SECRET", "super-secret-key")
app.url_map.strict_slashes = False  # avoid 301->GET issues on POST routes

# --- Session + CORS fixes for cross-origin frontends ---
# Set your frontend origin (e.g., http://localhost:5173 or https://your.site)
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")

# Cross-site cookie configuration.
# In production behind HTTPS, keep SESSION_COOKIE_SECURE=1 (default below).
app.config.update(
    SESSION_COOKIE_NAME="volunteer_admin",
    SESSION_COOKIE_SAMESITE="None",  # required when frontend and backend are on different origins
    SESSION_COOKIE_SECURE=bool(int(os.environ.get("SESSION_COOKIE_SECURE", "1"))),  # 1 in prod, 0 for local HTTP
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

# CORS must allow credentials and the exact origin for cross-origin sessions to work.
CORS(
    app,
    origins=[FRONTEND_ORIGIN],
    supports_credentials=True,
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# --------------- DB helpers ---------------
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    con = get_db()
    cur = con.cursor()

    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      created_at TEXT NOT NULL
    )""")

    # tasks
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT UNIQUE NOT NULL,
      description TEXT,
      max_volunteers INTEGER,
      slot_duration_mins INTEGER DEFAULT 60,
      type TEXT DEFAULT 'event',
      active INTEGER DEFAULT 1,
      created_by_admin INTEGER DEFAULT 1,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )""")

    # appointments
    cur.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id INTEGER NOT NULL,
      date TEXT NOT NULL,          -- YYYY-MM-DD
      start_time TEXT NOT NULL,    -- HH:MM
      end_time TEXT NOT NULL,      -- HH:MM
      phone TEXT NOT NULL,
      name TEXT,
      status TEXT DEFAULT 'active',    -- 'active' or 'canceled'
      canceled_at TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""")

    con.commit()
    con.close()

def migrate_db():
    con = get_db()
    cur = con.cursor()

    # ensure columns on appointments
    cur.execute("PRAGMA table_info(appointments)")
    cols = [r["name"] for r in cur.fetchall()]
    if "name" not in cols:
        cur.execute("ALTER TABLE appointments ADD COLUMN name TEXT")
    if "status" not in cols:
        cur.execute("ALTER TABLE appointments ADD COLUMN status TEXT DEFAULT 'active'")
    if "canceled_at" not in cols:
        cur.execute("ALTER TABLE appointments ADD COLUMN canceled_at TEXT")

    # ensure created_by_admin on tasks
    cur.execute("PRAGMA table_info(tasks)")
    tcols = [r["name"] for r in cur.fetchall()]
    if "created_by_admin" not in tcols:
        cur.execute("ALTER TABLE tasks ADD COLUMN created_by_admin INTEGER DEFAULT 1")
        cur.execute("UPDATE tasks SET created_by_admin = 1 WHERE created_by_admin IS NULL")

    # unique booking per (task, date) while active — partial unique index
    cur.execute("PRAGMA index_list('appointments')")
    idx_names = [r["name"] for r in cur.fetchall()]
    if "idx_appointments_task_date" in idx_names:
        cur.execute("DROP INDEX IF EXISTS idx_appointments_task_date")
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_task_date_active
        ON appointments(task_id, date)
        WHERE status = 'active'
    """)

    con.commit()
    con.close()

def seed_admin():
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT id FROM users WHERE email='admin'")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
            ("admin", generate_password_hash("admin"), datetime.utcnow().isoformat()),
        )
        con.commit()
    con.close()

def require_admin():
    return session.get("admin_id") is not None

# ---------------- Static route ----------------
@app.route("/")
def home():
    path = os.path.join(APP_DIR, "Volunteer.html")
    return send_from_directory(APP_DIR, "Volunteer.html") if os.path.exists(path) else "<h3>Server running.</h3>"

# ---------------- Auth (admin) ----------------
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return jsonify({"error": "Missing credentials"}), 400

    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT id, email, password_hash FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    con.close()

    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    # Persist session cookie with cross-site settings
    session.permanent = True
    session["admin_id"] = row["id"]
    session["admin_email"] = row["email"]
    return jsonify({"ok": True})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/me", methods=["GET"])
def api_me():
    if not require_admin():
        return jsonify({"ok": False})
    return jsonify({"ok": True, "email": session.get("admin_email")})

# ----------- Admin: tasks CRUD (admin-only) -----------
@app.route("/api/admin/tasks", methods=["GET"])
def api_admin_tasks():
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401
    con = get_db()
    cur = con.cursor()
    cur.execute("""
      SELECT id,
             title,
             description,
             max_volunteers AS maxVolunteers,
             slot_duration_mins AS slotDurationMins,
             type,
             active,
             created_by_admin,
             created_at,
             updated_at
        FROM tasks
       WHERE created_by_admin = 1
       ORDER BY created_at DESC
    """)
    items = [dict(r) for r in cur.fetchall()]
    con.close()
    return jsonify(items)

@app.route("/api/admin/task", methods=["POST"])
def api_admin_save_task():
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    task_id = data.get("id")
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    capacity = data.get("capacity")
    duration = data.get("duration") or 60

    if not title:
        return jsonify({"error": "Title required"}), 400

    now = datetime.utcnow().isoformat()
    con = get_db()
    cur = con.cursor()

    if task_id:
        cur.execute("""
          UPDATE tasks
             SET title=?,
                 description=?,
                 max_volunteers=?,
                 slot_duration_mins=?,
                 active=1,
                 created_by_admin=1,
                 updated_at=?
           WHERE id=? AND created_by_admin=1
        """, (title, description or None, capacity, duration, now, task_id))
    else:
        # claim existing volunteer-created title if present
        cur.execute("SELECT id FROM tasks WHERE title=?", (title,))
        row = cur.fetchone()
        if row:
            cur.execute("""
              UPDATE tasks
                 SET description=?,
                     max_volunteers=?,
                     slot_duration_mins=?,
                     active=1,
                     created_by_admin=1,
                     updated_at=?
               WHERE id=?
            """, (description or None, capacity, duration, now, row["id"]))
        else:
            cur.execute("""
              INSERT INTO tasks (title, description, max_volunteers, slot_duration_mins, type, active, created_by_admin, created_at, updated_at)
              VALUES (?, ?, ?, ?, 'event', 1, 1, ?, ?)
            """, (title, description or None, capacity, duration, now, now))

    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/admin/task/<int:task_id>", methods=["DELETE"])
def api_admin_delete_task(task_id):
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401
    con = get_db()
    cur = con.cursor()
    cur.execute("DELETE FROM tasks WHERE id=? AND created_by_admin=1", (task_id,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

# ----------- Admin: appointments (read-only, active only) -----------
@app.route("/api/admin/appointments", methods=["GET"])
def api_admin_appointments():
    if not require_admin():
        return jsonify({"error": "Unauthorized"}), 401
    con = get_db()
    cur = con.cursor()
    cur.execute("""
      SELECT a.id, a.date, a.start_time, a.end_time, a.phone, a.name,
             a.created_at, a.status,
             t.title AS task_title
        FROM appointments a
        JOIN tasks t ON a.task_id = t.id
       WHERE a.status = 'active'
       ORDER BY a.created_at DESC
    """)
    out = [dict(row) for row in cur.fetchall()]
    con.close()
    return jsonify(out)

# ----------- Public booking (no login) -----------
@app.route("/api/public_book", methods=["POST"])
def api_public_book():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    task_title = (data.get("taskTitle") or "").strip()
    date = (data.get("date") or "").strip()
    start_time = (data.get("time") or data.get("startTime") or "").strip()
    if not all([name, phone, task_title, date, start_time]):
        return jsonify({"error": "Please fill all fields."}), 400

    try:
        t0 = datetime.strptime(start_time, "%H:%M")
    except ValueError:
        return jsonify({"error": "Invalid time format"}), 400

    con = get_db()
    cur = con.cursor()

    # ensure a task row; volunteer-created ones are hidden (active=0, created_by_admin=0)
    cur.execute("SELECT id, slot_duration_mins FROM tasks WHERE title=?", (task_title,))
    row = cur.fetchone()
    if row:
        task_id = row["id"]
        slot_mins = int(row["slot_duration_mins"] or 60)
    else:
        now = datetime.utcnow().isoformat()
        cur.execute("""
          INSERT INTO tasks (title, description, max_volunteers, slot_duration_mins, type, active, created_by_admin, created_at, updated_at)
          VALUES (?, ?, ?, ?, 'event', 0, 0, ?, ?)
        """, (task_title, "Public-submitted task", 1, 60, now, now))
        task_id = cur.lastrowid
        slot_mins = 60

    # block if same (task, date) already has an ACTIVE booking
    cur.execute("""
      SELECT COUNT(*) AS c FROM appointments
       WHERE task_id=? AND date=? AND status='active'
    """, (task_id, date))
    if (cur.fetchone()["c"] or 0) > 0:
        con.close()
        return jsonify({"error": "This day is already booked for that task."}), 409

    end_time = (t0 + timedelta(minutes=slot_mins)).strftime("%H:%M")
    cur.execute("""
      INSERT INTO appointments (task_id, date, start_time, end_time, phone, name, status, created_at)
      VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
    """, (task_id, date, start_time, end_time, phone, name, datetime.utcnow().isoformat()))
    con.commit()
    con.close()
    return jsonify({"ok": True}), 201

# ----------- Public cancel (no login) -----------
@app.route("/api/public_cancel", methods=["POST"])
def api_public_cancel():
    """
    Body: { phone, taskTitle, date, time? }
    Marks the matching active booking as canceled.
    """
    data = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip()
    task_title = (data.get("taskTitle") or "").strip()
    date = (data.get("date") or "").strip()
    time_opt = (data.get("time") or "").strip()

    if not all([phone, task_title, date]):
        return jsonify({"error": "Missing phone, taskTitle, or date"}), 400

    con = get_db()
    cur = con.cursor()

    cur.execute("SELECT id FROM tasks WHERE title=?", (task_title,))
    row = cur.fetchone()
    if not row:
        con.close()
        return jsonify({"error": "Booking not found."}), 404
    task_id = row["id"]

    if time_opt:
        cur.execute("""
          UPDATE appointments
             SET status='canceled', canceled_at=?
           WHERE task_id=? AND date=? AND phone=? AND start_time=? AND status='active'
        """, (datetime.utcnow().isoformat(), task_id, date, phone, time_opt))
    else:
        cur.execute("""
          UPDATE appointments
             SET status='canceled', canceled_at=?
           WHERE task_id=? AND date=? AND phone=? AND status='active'
        """, (datetime.utcnow().isoformat(), task_id, date, phone))

    if cur.rowcount == 0:
        con.close()
        return jsonify({"error": "Active booking not found."}), 404

    con.commit()
    con.close()
    return jsonify({"ok": True})

# ----------- Public list tasks (admin-created only) -----------
@app.route("/api/tasks", methods=["GET"])
def api_tasks_public():
    con = get_db()
    cur = con.cursor()
    cur.execute("""
      SELECT id,
             title,
             description,
             max_volunteers AS maxVolunteers,
             slot_duration_mins AS slotDurationMins,
             active
        FROM tasks
       WHERE active=1 AND created_by_admin=1
       ORDER BY created_at DESC
    """)
    items = [dict(r) for r in cur.fetchall()]
    con.close()
    return jsonify(items)

# ----------- (Optional) Dev: reset admin to admin/admin -----------
@app.route("/api/dev/reset_admin", methods=["POST"])
def dev_reset_admin():
    con = get_db()
    cur = con.cursor()
    cur.execute("SELECT id FROM users WHERE email='admin'")
    if cur.fetchone():
        cur.execute("UPDATE users SET password_hash=? WHERE email='admin'", (generate_password_hash("admin"),))
    else:
        cur.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
            ("admin", generate_password_hash("admin"), datetime.utcnow().isoformat()),
        )
    con.commit()
    con.close()
    return jsonify({"ok": True})

# Always serve the login form
@app.route("/admin_login.html", methods=["GET"])
def serve_admin_login():
    return send_from_directory(APP_DIR, "admin_login.html")

# Protect the admin panel: show login if not authenticated
@app.route("/admin_tasks.html", methods=["GET"])
def serve_admin_tasks():
    if not require_admin():
        return send_from_directory(APP_DIR, "admin_login.html")
    return send_from_directory(APP_DIR, "admin_tasks.html")

# --------------- Main ---------------
if __name__ == "__main__":
    init_db()
    migrate_db()
    seed_admin()
    # print("ROUTES:", app.url_map)  # uncomment to debug routes
    app.run(host="0.0.0.0", port=5000, debug=True)
