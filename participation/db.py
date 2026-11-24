"""Tiny database layer that runs the same SQL on SQLite (local dev) and Postgres (Vercel).

SQL is written with ``?`` placeholders and portable constructs only
(``ON CONFLICT DO NOTHING``, ``RETURNING id``, ``COALESCE``). Timestamps are ISO-8601 UTC
strings and flags are 0/1 integers so both engines behave identically.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA_VERSION = 1


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Conn:
    """Uniform wrapper: every row comes back as a plain dict."""

    def __init__(self, raw, pg):
        self.raw = raw
        self.pg = pg

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def _exec(self, sql, params):
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    def all(self, sql, params=()):
        cur = self._exec(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def one(self, sql, params=()):
        cur = self._exec(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def scalar(self, sql, params=()):
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    def run(self, sql, params=()):
        """Execute a write; returns the affected row count."""
        return self._exec(sql, params).rowcount

    def insert(self, sql, params=()):
        """Execute an INSERT and return the new primary key."""
        if self.pg:
            cur = self._exec(sql + " RETURNING id", params)
            return cur.fetchone()["id"]
        return self._exec(sql, params).lastrowid

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass


def _sqlite_path(app):
    path = app.config["SQLITE_PATH"]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


def connect(app):
    url = app.config.get("DATABASE_URL")
    if url:
        import psycopg
        from psycopg.rows import dict_row

        # prepare_threshold=None keeps us compatible with pgbouncer (Neon's pooled endpoint).
        raw = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None, connect_timeout=10)
        return Conn(raw, True)
    raw = sqlite3.connect(_sqlite_path(app), timeout=15)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL") if not app.config.get("EPHEMERAL_DB") else None
    return Conn(raw, False)


def get_db() -> Conn:
    if "db" not in g:
        g.db = connect(current_app)
        ensure_schema(g.db, current_app)
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        if exc is not None:
            db.rollback()
        db.close()


# --------------------------------------------------------------------------- schema

def _ddl(pg):
    pk = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return [
        f"""CREATE TABLE IF NOT EXISTS users (
            id {pk},
            username TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'lecturer',
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS students (
            id {pk},
            name TEXT NOT NULL,
            moodle_id TEXT UNIQUE,
            card_uid TEXT UNIQUE,
            pin_hash TEXT,
            is_demo INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS subjects (
            id {pk},
            name TEXT NOT NULL,
            code TEXT,
            lecturer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            is_demo INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS enrollments (
            id {pk},
            subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(subject_id, student_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS lectures (
            id {pk},
            subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            lecture_date TEXT NOT NULL,
            start_time TEXT,
            session_open INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT,
            track_exit INTEGER NOT NULL DEFAULT 1,
            min_minutes INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS attendance (
            id {pk},
            lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            checked_in_at TEXT NOT NULL,
            left_at TEXT,
            duration_min INTEGER,
            method TEXT NOT NULL DEFAULT 'nfc',
            UNIQUE(lecture_id, student_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS scan_events (
            id {pk},
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            lecture_id INTEGER REFERENCES lectures(id) ON DELETE CASCADE,
            card_uid TEXT,
            student_id INTEGER REFERENCES students(id) ON DELETE SET NULL,
            outcome TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS api_tokens (
            id {pk},
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS login_attempts (
            id {pk},
            throttle_key TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id)",
        "CREATE INDEX IF NOT EXISTS idx_lectures_subject_date ON lectures(subject_id, lecture_date)",
        "CREATE INDEX IF NOT EXISTS idx_scan_events_lecture ON scan_events(lecture_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_scan_events_user ON scan_events(user_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(throttle_key, created_at)",
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    ]


_ready_for = set()


def ensure_schema(db: Conn, app):
    """Create tables once per process; safe if several cold starts race (advisory lock on Postgres)."""
    marker = app.config.get("DATABASE_URL") or app.config.get("SQLITE_PATH")
    if marker in _ready_for:
        return
    try:
        if db.pg:
            db.run("SELECT pg_advisory_xact_lock(724001)")
        for stmt in _ddl(db.pg):
            db.run(stmt)
        if db.scalar("SELECT COUNT(*) FROM schema_version") == 0:
            db.run("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    _ready_for.add(marker)


def reset_schema_cache():
    _ready_for.clear()
