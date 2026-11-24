import calendar as pycal
import secrets
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from .. import services
from ..db import get_db, utcnow_iso
from ..security import admin_required, staff_required

bp = Blueprint("staff", __name__)


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_subject(subject_id):
    subject = services.get_subject(get_db(), subject_id)
    if not subject:
        abort(404)
    if not services.can_access_subject(g.user, subject):
        abort(403)
    return subject


def load_lecture(lecture_id):
    lecture = services.get_lecture(get_db(), lecture_id)
    if not lecture:
        abort(404)
    if not services.can_access_subject(g.user, lecture):
        abort(403)
    return lecture


# ---------------------------------------------------------------- dashboard & calendar

@bp.route("/")
def dashboard():
    if g.get("student"):
        return redirect(url_for("auth.student_home"))
    if not g.get("user"):
        return redirect(url_for("auth.login"))
    return render_template("dashboard.html", d=services.dashboard_data(get_db(), g.user))


@bp.route("/calendar")
@staff_required
def calendar_view():
    today = date.fromisoformat(services.today_str())
    try:
        y, m = (int(x) for x in request.args.get("month", "").split("-"))
        first = date(y, m, 1)
    except ValueError:
        first = today.replace(day=1)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    lectures = services.list_lectures(get_db(), g.user, date_from=weeks[0][0].isoformat(), date_to=weeks[-1][-1].isoformat(), order="ASC")
    by_day = {}
    for l in lectures:
        by_day.setdefault(l["lecture_date"], []).append(l)
    return render_template("calendar.html", weeks=weeks, by_day=by_day, first=first, today=today,
                           prev_month=prev_month.strftime("%Y-%m"), next_month=next_month.strftime("%Y-%m"))


# ---------------------------------------------------------------- subjects

@bp.route("/subjects", methods=["GET", "POST"])
@staff_required
def subjects():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        code = request.form.get("code", "").strip() or None
        if not name:
            flash("Subject name is required.", "error")
        else:
            sid = db.insert("INSERT INTO subjects (name, code, lecturer_id, created_at) VALUES (?,?,?,?)", (name, code, g.user["id"], utcnow_iso()))
            db.commit()
            flash(f"Subject “{name}” created. Now add students and lectures.", "success")
            return redirect(url_for("staff.subject_detail", subject_id=sid))
    return render_template("subjects.html", subjects=services.list_subjects(db, g.user))


@bp.route("/subjects/<int:subject_id>")
@staff_required
def subject_detail(subject_id):
    db = get_db()
    subject = load_subject(subject_id)
    enrolled = services.enrolled_students(db, subject_id)
    ids = {s["id"] for s in enrolled}
    stats = {s["student_id"]: s for s in services.compute_stats(db, subject_id=subject_id)}
    return render_template(
        "subject_detail.html", subject=subject, enrolled=enrolled, stats=stats,
        candidates=[s for s in db.all("SELECT id, name, moodle_id FROM students ORDER BY name") if s["id"] not in ids],
        lectures=services.list_lectures(db, g.user, subject_id=subject_id),
    )


@bp.route("/subjects/<int:subject_id>/enroll", methods=["POST"])
@staff_required
def subject_enroll(subject_id):
    db = get_db()
    load_subject(subject_id)
    ids = [_int(x) for x in request.form.getlist("student_id") if _int(x)]
    n = 0
    for sid in ids:
        n += db.run("INSERT INTO enrollments (subject_id, student_id) VALUES (?,?) ON CONFLICT (subject_id, student_id) DO NOTHING", (subject_id, sid))
    db.commit()
    flash(f"{n} student(s) enrolled." if n else "Choose at least one student.", "success" if n else "error")
    return redirect(url_for("staff.subject_detail", subject_id=subject_id))


@bp.route("/subjects/<int:subject_id>/unenroll", methods=["POST"])
@staff_required
def subject_unenroll(subject_id):
    db = get_db()
    load_subject(subject_id)
    db.run("DELETE FROM enrollments WHERE subject_id = ? AND student_id = ?", (subject_id, _int(request.form.get("student_id"), 0)))
    db.commit()
    flash("Student removed from this subject.", "success")
    return redirect(url_for("staff.subject_detail", subject_id=subject_id))


@bp.route("/subjects/<int:subject_id>/rename", methods=["POST"])
@staff_required
def subject_rename(subject_id):
    db = get_db()
    load_subject(subject_id)
    name = request.form.get("name", "").strip()
    if name:
        db.run("UPDATE subjects SET name = ?, code = ? WHERE id = ?", (name, request.form.get("code", "").strip() or None, subject_id))
        db.commit()
        flash("Subject updated.", "success")
    return redirect(url_for("staff.subject_detail", subject_id=subject_id))


@bp.route("/subjects/<int:subject_id>/delete", methods=["POST"])
@staff_required
def subject_delete(subject_id):
    db = get_db()
    load_subject(subject_id)
    db.run("DELETE FROM subjects WHERE id = ?", (subject_id,))
    db.commit()
    flash("Subject deleted together with its lectures and attendance.", "success")
    return redirect(url_for("staff.subjects"))


@bp.route("/subjects/<int:subject_id>/import", methods=["POST"])
@staff_required
def subject_import(subject_id):
    load_subject(subject_id)
    return _do_import(subject_id, url_for("staff.subject_detail", subject_id=subject_id))


# ---------------------------------------------------------------- lectures

@bp.route("/lectures", methods=["GET", "POST"])
@staff_required
def lectures():
    db = get_db()
    subjects = services.list_subjects(db, g.user)
    if request.method == "POST":
        subject_id = _int(request.form.get("subject_id"))
        name = request.form.get("name", "").strip()
        day = request.form.get("lecture_date", "").strip()
        start = request.form.get("start_time", "").strip()
        repeat = min(max(_int(request.form.get("repeat"), 1), 1), 30)
        min_minutes = min(max(_int(request.form.get("min_minutes"), 0), 0), 600)
        track_exit = 1 if request.form.get("track_exit") else 0
        subject = services.get_subject(db, subject_id) if subject_id else None
        if not subject or not services.can_access_subject(g.user, subject):
            flash("Choose one of your subjects.", "error")
        elif not name or not services.valid_date(day):
            flash("Lecture name and a valid date are required.", "error")
        elif start and not services.valid_time(start):
            flash("Start time must look like 10:15.", "error")
        else:
            d0 = date.fromisoformat(day)
            for i in range(repeat):
                db.insert(
                    "INSERT INTO lectures (subject_id, name, lecture_date, start_time, track_exit, min_minutes, created_at) VALUES (?,?,?,?,?,?,?)",
                    (subject_id, f"{name} {i + 1}" if repeat > 1 else name, (d0 + timedelta(weeks=i)).isoformat(), start or None, track_exit, min_minutes, utcnow_iso()),
                )
            db.commit()
            flash(f"{repeat} lecture(s) created." if repeat > 1 else f"Lecture “{name}” created.", "success")
            return redirect(url_for("staff.lectures", subject=subject_id))
    sel = _int(request.args.get("subject"))
    return render_template("lectures.html", lectures=services.list_lectures(db, g.user, subject_id=sel), subjects=subjects,
                           selected=sel, today=services.today_str())


@bp.route("/lectures/<int:lecture_id>/edit", methods=["POST"])
@staff_required
def lecture_edit(lecture_id):
    db = get_db()
    lecture = load_lecture(lecture_id)
    name = request.form.get("name", "").strip()
    day = request.form.get("lecture_date", "").strip()
    start = request.form.get("start_time", "").strip()
    if not name or not services.valid_date(day) or (start and not services.valid_time(start)):
        flash("Name, a valid date and (optionally) a valid time are required.", "error")
    else:
        db.run(
            "UPDATE lectures SET name = ?, lecture_date = ?, start_time = ?, track_exit = ?, min_minutes = ? WHERE id = ?",
            (name, day, start or None, 1 if request.form.get("track_exit") else 0, min(max(_int(request.form.get("min_minutes"), 0), 0), 600), lecture_id),
        )
        db.commit()
        flash("Lecture updated.", "success")
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/") and not (request.form.get("back") or "").startswith("//") else url_for("live.session_page", lecture_id=lecture["id"]))


@bp.route("/lectures/<int:lecture_id>/delete", methods=["POST"])
@staff_required
def lecture_delete(lecture_id):
    db = get_db()
    load_lecture(lecture_id)
    db.run("DELETE FROM lectures WHERE id = ?", (lecture_id,))
    db.commit()
    flash("Lecture deleted.", "success")
    return redirect(url_for("staff.lectures"))


@bp.route("/sessions")
@staff_required
def sessions():
    db = get_db()
    today = services.today_str()
    week_ago = (date.fromisoformat(today) - timedelta(days=14)).isoformat()
    week_ahead = (date.fromisoformat(today) + timedelta(days=14)).isoformat()
    items = services.list_lectures(db, g.user, date_from=week_ago, date_to=week_ahead, order="DESC")
    return render_template("sessions.html", lectures=items, today=today)


# ---------------------------------------------------------------- students

def _do_import(subject_id, back):
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(back)
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("The file must be UTF-8 encoded CSV.", "error")
        return redirect(back)
    res = services.import_students_csv(get_db(), text, subject_id)
    flash(f"Import finished: {res['created']} created, {res['updated']} updated"
          + (f", {res['enrolled']} enrolled" if subject_id else "") + ".", "success")
    for err in res["errors"][:6]:
        flash(err, "error")
    return redirect(back)


@bp.route("/students", methods=["GET", "POST"])
@staff_required
def students():
    db = get_db()
    form = {"name": "", "moodle_id": "", "card_uid": request.args.get("uid", "")}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        sid, err = services.add_student(db, form["name"], form["moodle_id"], form["card_uid"])
        if err:
            flash(err, "error")
        else:
            flash(f"{form['name'].strip()} registered.", "success")
            return redirect(url_for("staff.students"))
    q = request.args.get("q", "").strip().lower()
    rows = db.all(
        "SELECT st.id, st.name, st.moodle_id, st.card_uid, st.pin_hash, "
        "(SELECT COUNT(*) FROM enrollments e WHERE e.student_id = st.id) AS subjects "
        "FROM students st ORDER BY st.name"
    )
    if q:
        rows = [r for r in rows if q in r["name"].lower() or q in (r["moodle_id"] or "").lower() or q in (r["card_uid"] or "").lower()]
    return render_template("students.html", students=rows, form=form, q=q, subjects=services.list_subjects(db, g.user))


@bp.route("/students/import", methods=["POST"])
@staff_required
def students_import():
    subject_id = _int(request.form.get("subject_id"))
    if subject_id:
        load_subject(subject_id)
    return _do_import(subject_id, url_for("staff.students"))


@bp.route("/students/<int:student_id>")
@staff_required
def student_detail(student_id):
    db = get_db()
    st = db.one("SELECT id, name, moodle_id, card_uid, pin_hash FROM students WHERE id = ?", (student_id,))
    if not st:
        abort(404)
    stats = services.compute_stats(db, g.user, student_id=student_id)
    return render_template("student_detail.html", st=st, stats=stats)


@bp.route("/students/<int:student_id>/update", methods=["POST"])
@staff_required
def student_update(student_id):
    db = get_db()
    st = db.one("SELECT id FROM students WHERE id = ?", (student_id,))
    if not st:
        abort(404)
    name = request.form.get("name", "").strip()
    moodle = request.form.get("moodle_id", "").strip() or None
    uid_raw = request.form.get("card_uid", "").strip()
    uid = services.normalize_uid(uid_raw) if uid_raw else None
    if not name:
        flash("Name is required.", "error")
    elif uid_raw and not uid:
        flash("Card UID must be 4–20 hex characters.", "error")
    elif moodle and db.one("SELECT id FROM students WHERE moodle_id = ? AND id <> ?", (moodle, student_id)):
        flash("That Moodle ID belongs to another student.", "error")
    elif uid and db.one("SELECT id FROM students WHERE card_uid = ? AND id <> ?", (uid, student_id)):
        flash("That card is already linked to another student.", "error")
    else:
        db.run("UPDATE students SET name = ?, moodle_id = ?, card_uid = ? WHERE id = ?", (name, moodle, uid, student_id))
        db.commit()
        flash("Student updated.", "success")
    return redirect(url_for("staff.student_detail", student_id=student_id))


@bp.route("/students/<int:student_id>/reset-pin", methods=["POST"])
@staff_required
def student_reset_pin(student_id):
    db = get_db()
    db.run("UPDATE students SET pin_hash = NULL WHERE id = ?", (student_id,))
    db.commit()
    flash("PIN reset. The student can sign in with their card UID once and choose a new PIN.", "success")
    return redirect(url_for("staff.student_detail", student_id=student_id))


@bp.route("/students/<int:student_id>/delete", methods=["POST"])
@admin_required
def student_delete(student_id):
    db = get_db()
    db.run("DELETE FROM students WHERE id = ?", (student_id,))
    db.commit()
    flash("Student and their attendance records deleted.", "success")
    return redirect(url_for("staff.students"))


# ---------------------------------------------------------------- stats & exports

@bp.route("/stats")
@staff_required
def stats():
    db = get_db()
    subject_id = _int(request.args.get("subject"))
    if subject_id:
        load_subject(subject_id)
    rows = services.compute_stats(db, g.user, subject_id=subject_id)
    only_risk = request.args.get("risk") == "1"
    if only_risk:
        rows = [r for r in rows if r["status"] == "risk"]
    return render_template("stats.html", rows=rows, subjects=services.list_subjects(db, g.user), selected=subject_id, only_risk=only_risk)


def _csv_response(text, filename):
    return Response(text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.route("/export/lecture/<int:lecture_id>.csv")
@staff_required
def export_lecture(lecture_id):
    lecture = load_lecture(lecture_id)
    return _csv_response(services.lecture_csv(get_db(), lecture), f"participation-lecture-{lecture_id}.csv")


@bp.route("/export/subject/<int:subject_id>.csv")
@staff_required
def export_subject(subject_id):
    subject = load_subject(subject_id)
    return _csv_response(services.subject_csv(get_db(), subject), f"participation-subject-{subject_id}.csv")


# ---------------------------------------------------------------- admin

@bp.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    db = get_db()
    if request.method == "POST":
        err = services.create_user(db, request.form.get("username", ""), request.form.get("name", ""),
                                   request.form.get("password", ""), request.form.get("role", "lecturer"))
        flash(err or "Account created. They must change the password at first sign-in.", "error" if err else "success")
        return redirect(url_for("staff.admin_users"))
    return render_template("admin_users.html", users=services.list_users(db),
                           demo=bool(db.one("SELECT id FROM students WHERE is_demo = 1")))


@bp.route("/admin/users/<int:user_id>/reset", methods=["POST"])
@admin_required
def admin_user_reset(user_id):
    db = get_db()
    temp = secrets.token_urlsafe(9)
    n = db.run("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?", (generate_password_hash(temp), user_id))
    db.commit()
    flash(f"Temporary password: {temp} (shown once — they must change it at next sign-in)." if n else "User not found.", "success" if n else "error")
    return redirect(url_for("staff.admin_users"))


@bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    if user_id == g.user["id"]:
        flash("You can't delete your own account.", "error")
    else:
        db = get_db()
        db.run("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        flash("Account deleted along with their subjects and lectures.", "success")
    return redirect(url_for("staff.admin_users"))


@bp.route("/admin/demo", methods=["POST"])
@admin_required
def admin_demo():
    db = get_db()
    if request.form.get("action") == "clear":
        services.clear_demo(db)
        flash("Demo data removed.", "success")
    else:
        err = services.load_demo(db, g.user["id"])
        flash(err or "Demo data loaded: 12 students, 2 subjects, weekly lectures with realistic attendance.", "error" if err else "success")
    return redirect(url_for("staff.admin_users"))
