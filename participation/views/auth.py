from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import services
from ..db import get_db
from ..security import safe_next, staff_required, student_required

bp = Blueprint("auth", __name__)


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user") or g.get("student"):
        return redirect(url_for("staff.dashboard"))
    error = None
    login_type = request.form.get("login_type", "staff")
    if request.method == "POST":
        db = get_db()
        username = request.form.get("username", "").strip()
        secret = request.form.get("password", "").strip()
        key = f"{login_type}:{username.lower()}:{_client_ip()}"
        if services.throttle_blocked(db, key):
            error = "Too many failed attempts. Please wait a few minutes and try again."
        elif login_type == "student":
            student, needs_pin = services.verify_student(db, username, secret)
            if student:
                services.throttle_clear(db, key)
                session.clear()
                session["student_id"] = student["id"]
                session["must_set_pin"] = needs_pin
                session.permanent = True
                return redirect(url_for("auth.student_pin") if needs_pin else url_for("auth.student_home"))
            services.throttle_fail(db, key)
            error = "Invalid Moodle ID or PIN."
        else:
            user = services.verify_staff(db, username, secret)
            if user:
                services.throttle_clear(db, key)
                session.clear()
                session["user_id"] = user["id"]
                session.permanent = True
                return redirect(safe_next(request.args.get("next")) or url_for("staff.dashboard"))
            services.throttle_fail(db, key)
            error = "Invalid username or password."
    return render_template("login.html", error=error, login_type=login_type)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- staff account

@bp.route("/account", methods=["GET"])
@staff_required
def account():
    return render_template("account.html", tokens=services.list_tokens(get_db(), g.user["id"]), new_token=session.pop("_new_token", None))


@bp.route("/account/password", methods=["GET", "POST"])
@staff_required
def account_password():
    error = None
    if request.method == "POST":
        db = get_db()
        current, new, confirm = request.form.get("current", ""), request.form.get("new", ""), request.form.get("confirm", "")
        row = db.one("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],))
        if not check_password_hash(row["password_hash"], current):
            error = "Current password is wrong."
        elif len(new) < 10:
            error = "Use at least 10 characters for the new password."
        elif new != confirm:
            error = "The new passwords don't match."
        elif new == current:
            error = "Choose a different password than the current one."
        else:
            db.run("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (generate_password_hash(new), g.user["id"]))
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("auth.account"))
    return render_template("password.html", error=error, forced=bool(g.user["must_change_password"]))


@bp.route("/account/tokens", methods=["POST"])
@staff_required
def create_token():
    token = services.create_token(get_db(), g.user["id"], request.form.get("label", ""))
    session["_new_token"] = token
    return redirect(url_for("auth.account") + "#tokens")


@bp.route("/account/tokens/<int:token_id>/delete", methods=["POST"])
@staff_required
def delete_token(token_id):
    db = get_db()
    db.run("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, g.user["id"]))
    db.commit()
    flash("Token revoked.", "success")
    return redirect(url_for("auth.account") + "#tokens")


# ---------------------------------------------------------------- student portal

@bp.route("/me")
@student_required
def student_home():
    db = get_db()
    stats = services.compute_stats(db, student_id=g.student["id"])
    card = db.one("SELECT card_uid FROM students WHERE id = ?", (g.student["id"],))
    return render_template("student_home.html", stats=stats, has_card=bool(card and card["card_uid"]))


@bp.route("/me/pin", methods=["GET", "POST"])
@student_required
def student_pin():
    error = None
    forced = bool(session.get("must_set_pin"))
    if request.method == "POST":
        db = get_db()
        new, confirm = request.form.get("new", ""), request.form.get("confirm", "")
        if len(new) < 6:
            error = "Use at least 6 characters."
        elif new != confirm:
            error = "The PINs don't match."
        elif not forced and not check_password_hash(g.student["pin_hash"] or "", request.form.get("current", "")):
            error = "Current PIN is wrong."
        else:
            db.run("UPDATE students SET pin_hash = ? WHERE id = ?", (generate_password_hash(new), g.student["id"]))
            db.commit()
            session.pop("must_set_pin", None)
            flash("PIN saved. Use it together with your Moodle ID next time.", "success")
            return redirect(url_for("auth.student_home"))
    return render_template("student_pin.html", error=error, forced=forced)
