"""Domain logic: scans, rosters, statistics, CSV import/export, tokens, demo data."""
import csv
import hashlib
import io
import random
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from .db import utcnow_iso

# ------------------------------------------------------------------ time helpers

def tz():
    return ZoneInfo(current_app.config["TIMEZONE"])


def today_str():
    return datetime.now(tz()).date().isoformat()


def parse_iso(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def local_dt(value):
    dt = parse_iso(value)
    return dt.astimezone(tz()) if dt else None


def fmt_time(value):
    dt = local_dt(value)
    return dt.strftime("%H:%M") if dt else ""


def fmt_datetime(value):
    dt = local_dt(value)
    return dt.strftime("%d %b %Y, %H:%M") if dt else ""


def fmt_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%a %d %b %Y")
    except (TypeError, ValueError):
        return value or ""


def valid_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def valid_time(value):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value or ""))


# ------------------------------------------------------------------ identifiers

UID_RE = re.compile(r"^[0-9A-F]{4,20}$")


def normalize_uid(raw):
    """'04:a2-3b c4' -> '04A23BC4'. Returns None when it is not a plausible NFC UID."""
    if not raw:
        return None
    uid = re.sub(r"[\s:\-]", "", str(raw)).upper()
    return uid if UID_RE.match(uid) else None


# ------------------------------------------------------------------ users

def ensure_admin(db, config):
    if db.scalar("SELECT COUNT(*) FROM users") > 0:
        return
    password = config.get("ADMIN_PASSWORD")
    generated = False
    if not password:
        password = secrets.token_urlsafe(9)
        generated = True
    db.insert(
        "INSERT INTO users (username, name, password_hash, role, must_change_password, created_at) VALUES (?,?,?,?,?,?)",
        (config["ADMIN_USERNAME"], "System Admin", generate_password_hash(password), "admin", 1, utcnow_iso()),
    )
    db.commit()
    if generated:
        print(f"[participation] Created admin '{config['ADMIN_USERNAME']}' with one-time password: {password}")


def get_user(db, user_id):
    return db.one("SELECT id, username, name, role, must_change_password FROM users WHERE id = ?", (user_id,))


def verify_staff(db, username, password):
    row = db.one("SELECT * FROM users WHERE username = ?", (username,))
    if row and check_password_hash(row["password_hash"], password):
        return row
    return None


def verify_student(db, moodle_id, secret):
    """Returns (student, needs_pin). A student without a PIN signs in once with their card UID."""
    st = db.one("SELECT * FROM students WHERE moodle_id = ?", (moodle_id,))
    if not st:
        return None, False
    if st["pin_hash"]:
        return (st, False) if check_password_hash(st["pin_hash"], secret) else (None, False)
    uid = normalize_uid(secret)
    if uid and st["card_uid"] and secrets.compare_digest(uid, st["card_uid"]):
        return st, True
    return None, False


def list_users(db):
    return db.all("SELECT id, username, name, role, created_at FROM users ORDER BY name")


def create_user(db, username, name, password, role):
    username, name = username.strip(), name.strip()
    if not username or not name:
        return "Username and name are required."
    if role not in ("lecturer", "admin"):
        return "Unknown role."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if db.one("SELECT id FROM users WHERE username = ?", (username,)):
        return "That username already exists."
    db.insert(
        "INSERT INTO users (username, name, password_hash, role, must_change_password, created_at) VALUES (?,?,?,?,?,?)",
        (username, name, generate_password_hash(password), role, 1, utcnow_iso()),
    )
    db.commit()
    return None


# ------------------------------------------------------------------ API tokens (reader bridge)

def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(db, user_id, label):
    token = "nfcp_" + secrets.token_urlsafe(32)
    db.insert(
        "INSERT INTO api_tokens (user_id, label, token_hash, created_at) VALUES (?,?,?,?)",
        (user_id, (label or "Reader").strip()[:60] or "Reader", _hash_token(token), utcnow_iso()),
    )
    db.commit()
    return token


def user_for_token(db, token):
    if not token or not token.startswith("nfcp_"):
        return None
    row = db.one(
        "SELECT u.id, u.username, u.name, u.role, t.id AS token_id FROM api_tokens t "
        "JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
        (_hash_token(token),),
    )
    if row:
        db.run("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow_iso(), row["token_id"]))
    return row


def list_tokens(db, user_id):
    return db.all("SELECT id, label, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY id DESC", (user_id,))


# ------------------------------------------------------------------ login throttling

def throttle_blocked(db, key):
    cfg = current_app.config
    since = (datetime.now(timezone.utc) - timedelta(minutes=cfg["LOGIN_WINDOW_MINUTES"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = db.scalar("SELECT COUNT(*) FROM login_attempts WHERE throttle_key = ? AND created_at >= ?", (key, since))
    return n >= cfg["LOGIN_MAX_FAILURES"]


def throttle_fail(db, key):
    db.run("INSERT INTO login_attempts (throttle_key, created_at) VALUES (?,?)", (key, utcnow_iso()))
    old = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.run("DELETE FROM login_attempts WHERE created_at < ?", (old,))
    db.commit()


def throttle_clear(db, key):
    db.run("DELETE FROM login_attempts WHERE throttle_key = ?", (key,))
    db.commit()


# ------------------------------------------------------------------ access control

def is_admin(user):
    return bool(user and user["role"] == "admin")


def can_access_subject(user, subject):
    return bool(subject and (is_admin(user) or subject["lecturer_id"] == user["id"]))


# ------------------------------------------------------------------ subjects

def list_subjects(db, user):
    sql = (
        "SELECT s.id, s.name, s.code, s.lecturer_id, u.name AS lecturer_name, "
        "(SELECT COUNT(*) FROM enrollments e WHERE e.subject_id = s.id) AS students, "
        "(SELECT COUNT(*) FROM lectures l WHERE l.subject_id = s.id) AS lectures "
        "FROM subjects s JOIN users u ON u.id = s.lecturer_id "
    )
    if is_admin(user):
        return db.all(sql + "ORDER BY s.name")
    return db.all(sql + "WHERE s.lecturer_id = ? ORDER BY s.name", (user["id"],))


def get_subject(db, subject_id):
    return db.one(
        "SELECT s.id, s.name, s.code, s.lecturer_id, u.name AS lecturer_name FROM subjects s "
        "JOIN users u ON u.id = s.lecturer_id WHERE s.id = ?",
        (subject_id,),
    )


def enrolled_students(db, subject_id):
    return db.all(
        "SELECT st.id, st.name, st.moodle_id, st.card_uid FROM enrollments e "
        "JOIN students st ON st.id = e.student_id WHERE e.subject_id = ? ORDER BY st.name",
        (subject_id,),
    )


# ------------------------------------------------------------------ lectures

_LECTURE_SQL = (
    "SELECT l.id, l.name, l.lecture_date, l.start_time, l.session_open, l.opened_at, l.track_exit, l.min_minutes, "
    "s.id AS subject_id, s.name AS subject_name, s.code AS subject_code, s.lecturer_id, u.name AS lecturer_name, "
    "(SELECT COUNT(*) FROM enrollments e WHERE e.subject_id = s.id) AS enrolled, "
    "(SELECT COUNT(*) FROM attendance a WHERE a.lecture_id = l.id) AS present "
    "FROM lectures l JOIN subjects s ON s.id = l.subject_id JOIN users u ON u.id = s.lecturer_id "
)


def get_lecture(db, lecture_id):
    return db.one(_LECTURE_SQL + "WHERE l.id = ?", (lecture_id,))


def list_lectures(db, user, subject_id=None, date_from=None, date_to=None, order="DESC", limit=None):
    where, params = [], []
    if not is_admin(user):
        where.append("s.lecturer_id = ?")
        params.append(user["id"])
    if subject_id:
        where.append("s.id = ?")
        params.append(subject_id)
    if date_from:
        where.append("l.lecture_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("l.lecture_date <= ?")
        params.append(date_to)
    sql = _LECTURE_SQL + (("WHERE " + " AND ".join(where) + " ") if where else "")
    order = "ASC" if order == "ASC" else "DESC"
    sql += f"ORDER BY l.lecture_date {order}, l.start_time {order}, l.id {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.all(sql, params)


def session_active(lecture):
    """A session counts as open only for SESSION_MAX_HOURS, so a forgotten tab can't leave it open all week."""
    if not lecture or not lecture["session_open"] or not lecture["opened_at"]:
        return False
    age = datetime.now(timezone.utc) - parse_iso(lecture["opened_at"])
    return age <= timedelta(hours=current_app.config["SESSION_MAX_HOURS"])


def set_session(db, lecture_id, open_):
    if open_:
        db.run("UPDATE lectures SET session_open = 1, opened_at = ? WHERE id = ?", (utcnow_iso(), lecture_id))
    else:
        db.run("UPDATE lectures SET session_open = 0 WHERE id = ?", (lecture_id,))
    db.commit()


def open_session_for_user(db, user):
    """The session a reader bridge belonging to `user` should write into: their most recently opened one."""
    since = (datetime.now(timezone.utc) - timedelta(hours=current_app.config["SESSION_MAX_HOURS"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    sql = _LECTURE_SQL + "WHERE l.session_open = 1 AND l.opened_at >= ? "
    params = [since]
    if not is_admin(user):
        sql += "AND s.lecturer_id = ? "
        params.append(user["id"])
    sql += "ORDER BY l.opened_at DESC LIMIT 1"
    return db.one(sql, params)


# ------------------------------------------------------------------ scanning

def log_event(db, user_id, lecture_id, uid, student_id, outcome, message):
    return db.insert(
        "INSERT INTO scan_events (user_id, lecture_id, card_uid, student_id, outcome, message, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, lecture_id, uid, student_id, outcome, message, utcnow_iso()),
    )


def process_scan(db, lecture, raw_uid, user_id, method="nfc"):
    """Handle one card tap for a lecture. Returns a dict the UI/bridge can show directly."""
    uid = normalize_uid(raw_uid)

    def done(outcome, message, ok=False, student=None):
        log_event(db, user_id, lecture["id"], uid or str(raw_uid)[:40], student["id"] if student else None, outcome, message)
        db.commit()
        return {"ok": ok, "outcome": outcome, "message": message, "uid": uid, "student": student["name"] if student else None}

    if not uid:
        return done("invalid", "That doesn't look like a valid card UID.")
    if not session_active(lecture):
        return done("closed", "This session is not open. Start it before scanning.")

    student = db.one("SELECT id, name FROM students WHERE card_uid = ?", (uid,))
    if not student:
        return done("unknown", f"Unknown card {uid}. Register it under Students.")
    if not db.one("SELECT id FROM enrollments WHERE subject_id = ? AND student_id = ?", (lecture["subject_id"], student["id"])):
        return done("not_enrolled", f"{student['name']} is not enrolled in {lecture['subject_name']}.", student=student)

    now = datetime.now(timezone.utc)
    row = db.one("SELECT id, checked_in_at, left_at FROM attendance WHERE lecture_id = ? AND student_id = ?", (lecture["id"], student["id"]))

    if not row:
        n = db.run(
            "INSERT INTO attendance (lecture_id, student_id, checked_in_at, method) VALUES (?,?,?,?) "
            "ON CONFLICT (lecture_id, student_id) DO NOTHING",
            (lecture["id"], student["id"], now.strftime("%Y-%m-%dT%H:%M:%SZ"), method),
        )
        if n:
            return done("checked_in", f"Welcome, {student['name']}", ok=True, student=student)
        row = db.one("SELECT id, checked_in_at, left_at FROM attendance WHERE lecture_id = ? AND student_id = ?", (lecture["id"], student["id"]))

    first = parse_iso(row["checked_in_at"])
    if row["left_at"]:
        return done("already_out", f"{student['name']} already checked out at {fmt_time(row['left_at'])}.", student=student)
    too_soon = (now - first).total_seconds() < current_app.config["EXIT_GRACE_SECONDS"]
    if not lecture["track_exit"] or too_soon:
        return done("duplicate", f"{student['name']} is already checked in ({fmt_time(row['checked_in_at'])}).", student=student)

    minutes = int((now - first).total_seconds() // 60)
    db.run("UPDATE attendance SET left_at = ?, duration_min = ? WHERE id = ?", (now.strftime("%Y-%m-%dT%H:%M:%SZ"), minutes, row["id"]))
    return done("checked_out", f"Goodbye, {student['name']} ({minutes} min)", ok=True, student=student)


def manual_mark(db, lecture, student_id, user_id):
    student = db.one("SELECT id, name FROM students WHERE id = ?", (student_id,))
    if not student:
        return {"ok": False, "message": "Student not found."}
    if not db.one("SELECT id FROM enrollments WHERE subject_id = ? AND student_id = ?", (lecture["subject_id"], student_id)):
        return {"ok": False, "message": f"{student['name']} is not enrolled."}
    n = db.run(
        "INSERT INTO attendance (lecture_id, student_id, checked_in_at, method) VALUES (?,?,?,?) "
        "ON CONFLICT (lecture_id, student_id) DO NOTHING",
        (lecture["id"], student_id, utcnow_iso(), "manual"),
    )
    msg = f"{student['name']} marked present manually." if n else f"{student['name']} was already present."
    log_event(db, user_id, lecture["id"], None, student_id, "manual" if n else "duplicate", msg)
    db.commit()
    return {"ok": True, "message": msg}


def manual_unmark(db, lecture, student_id, user_id):
    student = db.one("SELECT id, name FROM students WHERE id = ?", (student_id,))
    n = db.run("DELETE FROM attendance WHERE lecture_id = ? AND student_id = ?", (lecture["id"], student_id))
    name = student["name"] if student else "Student"
    msg = f"{name} removed from this lecture." if n else f"{name} was not marked."
    if n:
        log_event(db, user_id, lecture["id"], None, student_id, "removed", msg)
    db.commit()
    return {"ok": True, "message": msg}


def counts_toward_stats(lecture_min, duration_min):
    return lecture_min == 0 or (duration_min is not None and duration_min >= lecture_min)


def session_state(db, lecture, since_id=0, initial=False):
    rows = db.all(
        "SELECT st.id, st.name, st.moodle_id, a.checked_in_at, a.left_at, a.duration_min, a.method "
        "FROM enrollments e JOIN students st ON st.id = e.student_id "
        "LEFT JOIN attendance a ON a.lecture_id = ? AND a.student_id = st.id "
        "WHERE e.subject_id = ? ORDER BY st.name",
        (lecture["id"], lecture["subject_id"]),
    )
    roster = []
    for r in rows:
        roster.append({
            "id": r["id"], "name": r["name"], "moodle_id": r["moodle_id"] or "",
            "present": bool(r["checked_in_at"]), "left": bool(r["left_at"]),
            "in": fmt_time(r["checked_in_at"]), "out": fmt_time(r["left_at"]),
            "minutes": r["duration_min"], "method": r["method"] or "",
        })
    if initial:
        evs = db.all(
            "SELECT id, outcome, message, card_uid, created_at FROM scan_events WHERE lecture_id = ? ORDER BY id DESC LIMIT 30",
            (lecture["id"],),
        )[::-1]
    else:
        evs = db.all(
            "SELECT id, outcome, message, card_uid, created_at FROM scan_events WHERE lecture_id = ? AND id > ? ORDER BY id ASC LIMIT 100",
            (lecture["id"], since_id),
        )
    events = [{"id": e["id"], "outcome": e["outcome"], "message": e["message"], "uid": e["card_uid"] or "", "time": fmt_time(e["created_at"])} for e in evs]
    last_id = events[-1]["id"] if events else since_id
    present = sum(1 for r in roster if r["present"])
    return {
        "lecture": {"id": lecture["id"], "name": lecture["name"], "open": session_active(lecture), "track_exit": bool(lecture["track_exit"])},
        "counts": {"enrolled": len(roster), "present": present, "left": sum(1 for r in roster if r["left"])},
        "roster": roster,
        "events": events,
        "last_event_id": last_id,
    }


# ------------------------------------------------------------------ students

def add_student(db, name, moodle_id, card_uid, is_demo=0):
    name = (name or "").strip()
    moodle_id = (moodle_id or "").strip() or None
    card_raw = (card_uid or "").strip()
    uid = normalize_uid(card_raw) if card_raw else None
    if not name:
        return None, "Student name is required."
    if card_raw and not uid:
        return None, "Card UID must be 4–20 hex characters (e.g. 04A23BC4)."
    if moodle_id and db.one("SELECT id FROM students WHERE moodle_id = ?", (moodle_id,)):
        return None, f"Moodle ID {moodle_id} is already registered."
    if uid and db.one("SELECT name FROM students WHERE card_uid = ?", (uid,)):
        other = db.one("SELECT name FROM students WHERE card_uid = ?", (uid,))
        return None, f"That card is already linked to {other['name']}."
    sid = db.insert(
        "INSERT INTO students (name, moodle_id, card_uid, is_demo, created_at) VALUES (?,?,?,?,?)",
        (name, moodle_id, uid, is_demo, utcnow_iso()),
    )
    db.commit()
    return sid, None


def import_students_csv(db, text, subject_id=None):
    """Upsert students from CSV (columns: name, moodle_id, card_uid). Returns a summary dict."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {"created": 0, "updated": 0, "enrolled": 0, "errors": ["The file is empty."]}
    fields = {f.strip().lower(): f for f in reader.fieldnames if f}

    def col(*names):
        for n in names:
            if n in fields:
                return fields[n]
        return None

    c_name, c_moodle, c_uid = col("name", "student", "student name"), col("moodle_id", "moodle", "moodle id"), col("card_uid", "uid", "card", "card uid")
    if not c_name:
        return {"created": 0, "updated": 0, "enrolled": 0, "errors": ["Missing a 'name' column."]}
    res = {"created": 0, "updated": 0, "enrolled": 0, "errors": []}
    for i, row in enumerate(reader, start=2):
        if i > 5002:
            res["errors"].append("Stopped after 5000 rows.")
            break
        name = (row.get(c_name) or "").strip()
        moodle = (row.get(c_moodle) or "").strip() if c_moodle else ""
        uid_raw = (row.get(c_uid) or "").strip() if c_uid else ""
        if not name:
            continue
        existing = db.one("SELECT id FROM students WHERE moodle_id = ?", (moodle,)) if moodle else None
        if existing:
            uid = normalize_uid(uid_raw) if uid_raw else None
            if uid_raw and not uid:
                res["errors"].append(f"Row {i}: invalid card UID '{uid_raw}'.")
            else:
                clash = db.one("SELECT id FROM students WHERE card_uid = ? AND id <> ?", (uid, existing["id"])) if uid else None
                if clash:
                    res["errors"].append(f"Row {i}: card {uid} belongs to another student.")
                else:
                    db.run("UPDATE students SET name = ?, card_uid = COALESCE(?, card_uid) WHERE id = ?", (name, uid, existing["id"]))
                    res["updated"] += 1
            sid = existing["id"]
        else:
            sid, err = add_student(db, name, moodle, uid_raw)
            if err:
                res["errors"].append(f"Row {i}: {err}")
                continue
            res["created"] += 1
        if subject_id and sid:
            n = db.run("INSERT INTO enrollments (subject_id, student_id) VALUES (?,?) ON CONFLICT (subject_id, student_id) DO NOTHING", (subject_id, sid))
            res["enrolled"] += n
    db.commit()
    return res


# ------------------------------------------------------------------ statistics

def percent_to_grade(pct):
    for threshold, grade in current_app.config["GRADE_STEPS"]:
        if pct >= threshold:
            return grade
    return current_app.config["LOWEST_GRADE"]


def status_for(pct, total):
    if total == 0:
        return "none"
    if pct >= 80:
        return "good"
    if pct >= 50:
        return "ok"
    return "risk"


def _in_clause(ids):
    return "(" + ",".join("?" for _ in ids) + ")"


def compute_stats(db, user=None, subject_id=None, student_id=None):
    """One pass: enrollments + held lectures + attendance -> per student/subject numbers (no N+1)."""
    where, params = [], []
    if user is not None and not is_admin(user):
        where.append("s.lecturer_id = ?")
        params.append(user["id"])
    if subject_id:
        where.append("s.id = ?")
        params.append(subject_id)
    if student_id:
        where.append("e.student_id = ?")
        params.append(student_id)
    enrolls = db.all(
        "SELECT e.student_id, st.name AS student_name, st.moodle_id, s.id AS subject_id, s.name AS subject_name "
        "FROM enrollments e JOIN students st ON st.id = e.student_id JOIN subjects s ON s.id = e.subject_id "
        + (("WHERE " + " AND ".join(where) + " ") if where else "")
        + "ORDER BY st.name, s.name",
        params,
    )
    if not enrolls:
        return []
    subject_ids = sorted({e["subject_id"] for e in enrolls})
    held = db.all(
        "SELECT id, subject_id, name, lecture_date, min_minutes FROM lectures "
        f"WHERE lecture_date <= ? AND (lecture_date < ? OR opened_at IS NOT NULL) "
        f"AND subject_id IN {_in_clause(subject_ids)} ORDER BY lecture_date DESC, id DESC",
        [today_str(), today_str(), *subject_ids],
    )
    held_by_subject = {}
    for l in held:
        held_by_subject.setdefault(l["subject_id"], []).append(l)
    lecture_meta = {l["id"]: l for l in held}
    att_sql = (
        "SELECT a.student_id, a.lecture_id, a.duration_min FROM attendance a JOIN lectures l ON l.id = a.lecture_id "
        f"WHERE l.subject_id IN {_in_clause(subject_ids)}"
    )
    att_params = list(subject_ids)
    if student_id:
        att_sql += " AND a.student_id = ?"
        att_params.append(student_id)
    attended = {}
    for a in db.all(att_sql, att_params):
        meta = lecture_meta.get(a["lecture_id"])
        if meta and counts_toward_stats(meta["min_minutes"], a["duration_min"]):
            attended.setdefault((a["student_id"], meta["subject_id"]), set()).add(a["lecture_id"])

    out = []
    for e in enrolls:
        lectures = held_by_subject.get(e["subject_id"], [])
        got = attended.get((e["student_id"], e["subject_id"]), set())
        total = len(lectures)
        pct = round(len(got) / total * 100, 1) if total else 0.0
        out.append({
            "student_id": e["student_id"], "name": e["student_name"], "moodle_id": e["moodle_id"] or "",
            "subject_id": e["subject_id"], "subject": e["subject_name"],
            "attended": len(got), "total": total, "percentage": pct,
            "grade": percent_to_grade(pct), "status": status_for(pct, total),
            "attended_lectures": [l for l in lectures if l["id"] in got],
            "missed_lectures": [l for l in lectures if l["id"] not in got],
        })
    return out


def dashboard_data(db, user):
    today = today_str()
    subjects = list_subjects(db, user)
    stats = compute_stats(db, user)
    per_subject = {}
    for s in stats:
        d = per_subject.setdefault(s["subject_id"], {"pcts": [], "risk": 0})
        if s["total"]:
            d["pcts"].append(s["percentage"])
        d["risk"] += s["status"] == "risk"
    subject_rows = []
    for s in subjects:
        d = per_subject.get(s["id"], {"pcts": [], "risk": 0})
        avg = round(sum(d["pcts"]) / len(d["pcts"]), 1) if d["pcts"] else None
        subject_rows.append({**s, "avg": avg, "risk": d["risk"], "status": status_for(avg or 0, 1 if avg is not None else 0)})

    todays = list_lectures(db, user, date_from=today, date_to=today, order="ASC")
    upcoming = list_lectures(db, user, date_from=(date.fromisoformat(today) + timedelta(days=1)).isoformat(), order="ASC", limit=5)
    recent = [l for l in list_lectures(db, user, date_to=today, limit=12) if l["enrolled"]][::-1]
    bars = [{"id": l["id"], "label": l["name"], "subject": l["subject_name"], "date": l["lecture_date"],
             "code": l["subject_code"] or l["subject_name"][:4], "present": l["present"], "enrolled": l["enrolled"], "pct": round(l["present"] / l["enrolled"] * 100)} for l in recent]
    at_risk = sorted([s for s in stats if s["status"] == "risk"], key=lambda s: s["percentage"])[:8]

    scope = "" if is_admin(user) else "AND s.lecturer_id = ?"
    sp = [] if is_admin(user) else [user["id"]]
    activity = db.all(
        "SELECT st.name, l.name AS lecture, s.name AS subject, a.checked_in_at, a.method FROM attendance a "
        "JOIN students st ON st.id = a.student_id JOIN lectures l ON l.id = a.lecture_id JOIN subjects s ON s.id = l.subject_id "
        f"WHERE 1=1 {scope} ORDER BY a.checked_in_at DESC, a.id DESC LIMIT 8",
        sp,
    )
    checkins_today = db.scalar(
        "SELECT COUNT(*) FROM attendance a JOIN lectures l ON l.id = a.lecture_id JOIN subjects s ON s.id = l.subject_id "
        f"WHERE l.lecture_date = ? {scope}",
        [today, *sp],
    )
    students_total = db.scalar("SELECT COUNT(*) FROM students")
    return {
        "kpis": {"students": students_total, "subjects": len(subjects),
                 "lectures": sum(s["lectures"] for s in subjects), "checkins_today": checkins_today},
        "subjects": subject_rows, "todays": todays, "upcoming": upcoming, "bars": bars,
        "at_risk": at_risk, "activity": activity,
        "live": [l for l in todays if session_active(l)],
    }


# ------------------------------------------------------------------ CSV export

def csv_safe(value):
    """Neutralise spreadsheet formula injection."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([csv_safe(c) for c in r])
    return "\ufeff" + buf.getvalue()  # BOM so Excel reads umlauts correctly


def lecture_csv(db, lecture):
    state = session_state(db, lecture, initial=True)
    rows = [["Name", "Moodle ID", "Status", "Check-in", "Check-out", "Minutes", "Method"]]
    for r in state["roster"]:
        rows.append([r["name"], r["moodle_id"], "present" if r["present"] else "absent", r["in"], r["out"], r["minutes"] if r["minutes"] is not None else "", r["method"]])
    return make_csv(rows)


def subject_csv(db, subject):
    lectures = db.all("SELECT id, name, lecture_date FROM lectures WHERE subject_id = ? ORDER BY lecture_date, id", (subject["id"],))
    stats = {s["student_id"]: s for s in compute_stats(db, subject_id=subject["id"])}
    present = {}
    for a in db.all("SELECT a.student_id, a.lecture_id FROM attendance a JOIN lectures l ON l.id = a.lecture_id WHERE l.subject_id = ?", (subject["id"],)):
        present.setdefault(a["student_id"], set()).add(a["lecture_id"])
    header = ["Name", "Moodle ID"] + [f"{l['lecture_date']} {l['name']}" for l in lectures] + ["Attended", "Held", "Percent", "Grade estimate"]
    rows = [header]
    for st in enrolled_students(db, subject["id"]):
        s = stats.get(st["id"], {})
        got = present.get(st["id"], set())
        rows.append([st["name"], st["moodle_id"] or ""] + ["1" if l["id"] in got else "" for l in lectures]
                    + [s.get("attended", 0), s.get("total", 0), s.get("percentage", 0), s.get("grade", "")])
    return make_csv(rows)


# ------------------------------------------------------------------ demo data

DEMO_NAMES = ["Anna Becker", "Jonas Weber", "Lea Fischer", "Noah Schneider", "Mia Hoffmann", "Elias Wagner",
              "Sophie Koch", "Paul Richter", "Emma Klein", "Luca Wolf", "Hannah Neumann", "Felix Braun"]


def load_demo(db, lecturer_id):
    if db.one("SELECT id FROM students WHERE is_demo = 1"):
        return "Demo data is already loaded."
    rng = random.Random(42)
    sids = []
    for i, name in enumerate(DEMO_NAMES, start=1):
        sid, _ = add_student(db, name, f"demo{i:03d}", f"DE00{i:04X}", is_demo=1)
        sids.append(sid)
    today = date.fromisoformat(today_str())
    plans = [("Production Management", "PM", sids[:9], [0.95, 0.9, 0.85, 0.7, 0.6, 0.5, 0.3, 0.9, 0.2]),
             ("Principles of Accounting", "ACC", sids[3:], [0.9, 0.8, 0.75, 0.65, 0.55, 0.4, 0.95, 0.85, 0.15])]
    for subj, code, members, rates in plans:
        sub_id = db.insert("INSERT INTO subjects (name, code, lecturer_id, is_demo, created_at) VALUES (?,?,?,?,?)", (subj, code, lecturer_id, 1, utcnow_iso()))
        for sid in members:
            db.run("INSERT INTO enrollments (subject_id, student_id) VALUES (?,?)", (sub_id, sid))
        for w in range(8, -3, -1):
            d = today - timedelta(weeks=w)
            lid = db.insert(
                "INSERT INTO lectures (subject_id, name, lecture_date, start_time, track_exit, min_minutes, created_at) VALUES (?,?,?,?,?,?,?)",
                (sub_id, f"Lecture {9 - w}" if w >= 0 else f"Lecture {9 + abs(w)}", d.isoformat(), "10:00", 1, 0, utcnow_iso()),
            )
            if d <= today and d != today:
                for sid, rate in zip(members, rates):
                    if rng.random() < rate:
                        t_in = datetime(d.year, d.month, d.day, 8, 55, tzinfo=timezone.utc) + timedelta(minutes=rng.randint(0, 12))
                        stay = rng.randint(70, 95)
                        t_out = t_in + timedelta(minutes=stay)
                        db.run("INSERT INTO attendance (lecture_id, student_id, checked_in_at, left_at, duration_min, method) VALUES (?,?,?,?,?,?)",
                               (lid, sid, t_in.strftime("%Y-%m-%dT%H:%M:%SZ"), t_out.strftime("%Y-%m-%dT%H:%M:%SZ"), stay, "nfc"))
    db.commit()
    return None


def clear_demo(db):
    db.run("DELETE FROM students WHERE is_demo = 1")
    db.run("DELETE FROM subjects WHERE is_demo = 1")
    db.commit()
