"""NFC Participation System – OTH Amberg-Weiden."""
import os
import secrets
import tempfile

from flask import Flask, jsonify, render_template, request

from . import services
from .db import close_db, get_db


def create_app(test_config=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        static_folder=os.path.join(root, "public", "static"),
        static_url_path="/static",
        template_folder="templates",
    )

    on_vercel = bool(os.environ.get("VERCEL"))
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]

    # Without a real database on Vercel the filesystem is read-only except /tmp (data is wiped on cold start).
    ephemeral = on_vercel and not database_url
    default_sqlite = os.path.join(tempfile.gettempdir(), "participation.db") if on_vercel else os.path.join(root, "data", "participation.db")

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if on_vercel:
            raise RuntimeError("SECRET_KEY environment variable is required in production.")
        secret = secrets.token_hex(32)  # dev only: sessions reset on every restart

    app.config.update(
        SECRET_KEY=secret,
        DATABASE_URL=database_url,
        SQLITE_PATH=os.environ.get("SQLITE_PATH", default_sqlite),
        EPHEMERAL_DB=ephemeral,
        ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD"),
        TIMEZONE=os.environ.get("APP_TIMEZONE", "Europe/Berlin"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=on_vercel,
        PERMANENT_SESSION_LIFETIME=12 * 3600,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        LOGIN_MAX_FAILURES=8,
        LOGIN_WINDOW_MINUTES=15,
        SESSION_MAX_HOURS=6,
        EXIT_GRACE_SECONDS=120,
        # Participation grade estimate (same thresholds as the original prototype).
        GRADE_STEPS=[(90, "1.0"), (80, "2.0"), (65, "3.0"), (50, "4.0")],
        LOWEST_GRADE="5.0",
        APP_NAME="Participation System",
    )
    if test_config:
        app.config.update(test_config)

    from .security import init_security
    from .views.auth import bp as auth_bp
    from .views.live import bp as live_bp
    from .views.staff import bp as staff_bp

    init_security(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(live_bp)
    app.teardown_appcontext(close_db)

    app.jinja_env.filters.update(
        time=services.fmt_time, date=services.fmt_date, datetime=services.fmt_datetime,
    )
    # Cache-buster for /static assets. On Vercel `public/` lives on the CDN (not in the function bundle),
    # so use the deployment id there and the file's mtime locally.
    asset_version = (os.environ.get("VERCEL_DEPLOYMENT_ID") or os.environ.get("VERCEL_GIT_COMMIT_SHA") or "")[-8:]
    if not asset_version:
        try:
            asset_version = str(int(os.path.getmtime(os.path.join(app.static_folder, "app.css"))))
        except OSError:
            asset_version = "1"

    def asset(name):
        from flask import url_for
        return url_for("static", filename=name, v=asset_version)

    app.jinja_env.globals.update(session_active=services.session_active, asset=asset)

    @app.before_request
    def _bootstrap():
        if request.endpoint in (None, "static"):
            return
        db = get_db()
        if not app.config.get("_admin_checked"):
            services.ensure_admin(db, app.config)
            app.config["_admin_checked"] = True

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def _error(e):
        code = getattr(e, "code", 500) or 500
        msg = {400: "That request couldn't be processed (the form may have expired — reload and try again).",
               403: "You don't have access to this page.", 404: "We couldn't find that page.",
               413: "That file is too large.", 500: "Something went wrong on our side."}.get(code, "Error")
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": msg}), code
        return render_template("error.html", code=code, message=msg), code

    @app.route("/healthz")
    def healthz():
        get_db().scalar("SELECT 1")
        return jsonify({"ok": True, "database": "postgres" if app.config["DATABASE_URL"] else "sqlite",
                        "ephemeral": app.config["EPHEMERAL_DB"]})

    return app
