"""CSRF protection, auth decorators, security headers."""
import secrets
from functools import wraps
from urllib.parse import urlparse

from flask import abort, g, jsonify, redirect, request, session, url_for

from . import services
from .db import get_db


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def safe_next(target):
    """Only allow same-site relative redirects."""
    if target and target.startswith("/") and not target.startswith("//") and not urlparse(target).netloc:
        return target
    return None


def init_security(app):
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _load_user_and_csrf():
        if request.endpoint in (None, "static"):
            return
        # CSRF: every state-changing request except token-authenticated bridge calls.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith("/api/bridge/"):
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
            if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
                abort(400)
        g.user = None
        g.student = None
        if session.get("user_id"):
            g.user = services.get_user(get_db(), session["user_id"])
            if not g.user:
                session.clear()
        elif session.get("student_id"):
            g.student = get_db().one("SELECT id, name, moodle_id, pin_hash FROM students WHERE id = ?", (session["student_id"],))
            if not g.student:
                session.clear()

        # Force first-time credential changes before anything else.
        allowed = {"auth.logout", "auth.account_password", "auth.student_pin", "healthz"}
        pending = (g.user and g.user["must_change_password"]) or (g.student and session.get("must_set_pin"))
        if pending and request.endpoint not in allowed:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "Finish setting your new credentials first."}), 403
            return redirect(url_for("auth.account_password") if g.user else url_for("auth.student_pin"))

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if request.endpoint and request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp


def _deny(api):
    if api or request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "Please sign in."}), 401
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def staff_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not g.get("user"):
            return _deny(False)
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not g.get("user"):
            return _deny(False)
        if g.user["role"] != "admin":
            abort(403)
        return view(*a, **kw)
    return wrapper


def student_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not g.get("student"):
            return _deny(False)
        return view(*a, **kw)
    return wrapper
