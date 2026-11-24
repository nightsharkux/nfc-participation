#!/usr/bin/env python3
"""Import data from the original prototype's students.db into this system.

    python scripts/import_legacy.py "C:/path/to/students.db"                 # -> local SQLite (data/participation.db)
    DATABASE_URL="postgresql://..." python scripts/import_legacy.py students.db   # -> your Postgres (e.g. the Vercel/Neon one)

* The old database is opened read-only and is never modified.
* Imported: lecturers (except the default 'admin', whose old password hash is NOT carried over), students, subjects,
  enrolments, lectures and attendance. Imported lecturers must choose a new password at first sign-in.
* Safe to re-run: students are matched by card UID, subjects/lectures by name + date.
* Student card UIDs and names are personal data – make sure you are allowed to put them on the target system.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from participation import create_app  # noqa: E402
from participation import services  # noqa: E402
from participation.db import get_db, utcnow_iso  # noqa: E402


def to_utc(ts, tz):
    if not ts:
        return utcnow_iso()
    try:
        naive = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return utcnow_iso()
    return naive.replace(tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("legacy_db", help="path to the old students.db")
    ap.add_argument("--timezone", default="Europe/Berlin", help="timezone the old naive timestamps were recorded in")
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{os.path.abspath(args.legacy_db)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    tz = ZoneInfo(args.timezone)

    os.environ.setdefault("SECRET_KEY", "import-script")
    app = create_app()
    with app.app_context():
        db = get_db()
        services.ensure_admin(db, app.config)
        admin = db.one("SELECT id FROM users WHERE username = ?", (app.config["ADMIN_USERNAME"],))
        stats = dict(users=0, students=0, subjects=0, enrolments=0, lectures=0, attendance=0)

        user_map = {}
        for r in src.execute("SELECT * FROM lecturers"):
            existing = db.one("SELECT id FROM users WHERE username = ?", (r["username"],))
            if r["username"] == "admin" or existing:
                user_map[r["id"]] = (existing or admin)["id"]
                continue
            user_map[r["id"]] = db.insert(
                "INSERT INTO users (username, name, password_hash, role, must_change_password, created_at) VALUES (?,?,?,?,?,?)",
                (r["username"], r["name"], r["password_hash"], r["role"], 1, utcnow_iso()),
            )
            stats["users"] += 1

        student_map = {}
        for r in src.execute("SELECT * FROM students"):
            uid = services.normalize_uid(r["card_uid"])
            existing = db.one("SELECT id FROM students WHERE card_uid = ?", (uid,)) if uid else None
            if existing:
                student_map[r["id"]] = existing["id"]
                continue
            moodle = (r["moodle_id"] or "").strip() or None
            if moodle and db.one("SELECT id FROM students WHERE moodle_id = ?", (moodle,)):
                moodle = None  # keep the student, drop the clashing Moodle ID
            student_map[r["id"]] = db.insert(
                "INSERT INTO students (name, moodle_id, card_uid, created_at) VALUES (?,?,?,?)",
                (r["name"], moodle, uid, utcnow_iso()),
            )
            stats["students"] += 1

        subject_map = {}
        for r in src.execute("SELECT * FROM subjects"):
            owner = user_map.get(r["lecturer_id"], admin["id"])
            existing = db.one("SELECT id FROM subjects WHERE name = ? AND lecturer_id = ?", (r["subject_name"], owner))
            if existing:
                subject_map[r["id"]] = existing["id"]
                continue
            subject_map[r["id"]] = db.insert("INSERT INTO subjects (name, lecturer_id, created_at) VALUES (?,?,?)", (r["subject_name"], owner, utcnow_iso()))
            stats["subjects"] += 1

        for r in src.execute("SELECT * FROM subject_students"):
            sid, stu = subject_map.get(r["subject_id"]), student_map.get(r["student_id"])
            if sid and stu:
                stats["enrolments"] += db.run("INSERT INTO enrollments (subject_id, student_id) VALUES (?,?) ON CONFLICT (subject_id, student_id) DO NOTHING", (sid, stu))

        lecture_map = {}
        for r in src.execute("SELECT * FROM lectures"):
            sid = subject_map.get(r["subject_id"])
            if not sid:
                continue  # lecture without a subject can't be attached to anything
            existing = db.one("SELECT id FROM lectures WHERE subject_id = ? AND name = ? AND lecture_date = ?", (sid, r["lecture_name"], r["lecture_date"]))
            if existing:
                lecture_map[r["id"]] = existing["id"]
                continue
            lecture_map[r["id"]] = db.insert(
                "INSERT INTO lectures (subject_id, name, lecture_date, track_exit, min_minutes, created_at) VALUES (?,?,?,?,?,?)",
                (sid, r["lecture_name"], r["lecture_date"], 0, 0, utcnow_iso()),
            )
            stats["lectures"] += 1

        for r in src.execute("SELECT * FROM attendance"):
            lid, stu = lecture_map.get(r["lecture_id"]), student_map.get(r["student_id"])
            if lid and stu:
                stats["attendance"] += db.run(
                    "INSERT INTO attendance (lecture_id, student_id, checked_in_at, method) VALUES (?,?,?,?) ON CONFLICT (lecture_id, student_id) DO NOTHING",
                    (lid, stu, to_utc(r["timestamp"], tz), "legacy"),
                )
        db.commit()
        target = "Postgres (DATABASE_URL)" if app.config["DATABASE_URL"] else app.config["SQLITE_PATH"]
        print(f"Imported into {target}: " + ", ".join(f"{v} {k}" for k, v in stats.items()))
        print("Legacy lectures were imported with exit taps off (the prototype only recorded one tap).")


if __name__ == "__main__":
    main()
