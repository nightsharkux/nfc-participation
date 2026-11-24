"""Live session page + JSON APIs (browser scanning, manual marking, reader bridge)."""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from .. import services
from ..db import get_db
from ..security import staff_required
from .staff import load_lecture

bp = Blueprint("live", __name__)


def _json():
    return request.get_json(silent=True) or {}


@bp.route("/session/<int:lecture_id>")
@staff_required
def session_page(lecture_id):
    db = get_db()
    lecture = load_lecture(lecture_id)
    state = services.session_state(db, lecture, initial=True)
    return render_template("session.html", lecture=lecture, state=state, active=services.session_active(lecture))


@bp.route("/session/<int:lecture_id>/start", methods=["POST"])
@staff_required
def session_start(lecture_id):
    load_lecture(lecture_id)
    services.set_session(get_db(), lecture_id, True)
    flash("Session started — cards can now be tapped.", "success")
    return redirect(url_for("live.session_page", lecture_id=lecture_id))


@bp.route("/session/<int:lecture_id>/end", methods=["POST"])
@staff_required
def session_end(lecture_id):
    load_lecture(lecture_id)
    services.set_session(get_db(), lecture_id, False)
    flash("Session ended.", "success")
    return redirect(url_for("live.session_page", lecture_id=lecture_id))


@bp.route("/api/session/<int:lecture_id>/state")
@staff_required
def api_state(lecture_id):
    lecture = load_lecture(lecture_id)
    since = request.args.get("since", type=int, default=0)
    return jsonify(services.session_state(get_db(), lecture, since_id=since))


@bp.route("/api/session/<int:lecture_id>/scan", methods=["POST"])
@staff_required
def api_scan(lecture_id):
    """Used by the browser (Web NFC on Android) and by manual UID entry."""
    lecture = load_lecture(lecture_id)
    uid = _json().get("uid", "")
    method = "nfc-web" if _json().get("source") == "webnfc" else "manual-uid"
    return jsonify(services.process_scan(get_db(), lecture, uid, g.user["id"], method=method))


@bp.route("/api/session/<int:lecture_id>/mark", methods=["POST"])
@staff_required
def api_mark(lecture_id):
    lecture = load_lecture(lecture_id)
    student_id = _json().get("student_id")
    if not isinstance(student_id, int):
        return jsonify({"ok": False, "message": "student_id required"}), 400
    fn = services.manual_unmark if _json().get("present") is False else services.manual_mark
    return jsonify(fn(get_db(), lecture, student_id, g.user["id"]))


@bp.route("/api/scans/latest")
@staff_required
def api_latest_scan():
    """Lets the 'register student' form pick up a card tapped on a reader bridge."""
    db = get_db()
    since = request.args.get("since", type=int)
    if since is None:
        return jsonify({"ok": True, "last_id": db.scalar("SELECT COALESCE(MAX(id), 0) FROM scan_events WHERE user_id = ?", (g.user["id"],))})
    ev = db.one(
        "SELECT id, card_uid, outcome, message FROM scan_events WHERE user_id = ? AND id > ? AND card_uid IS NOT NULL ORDER BY id DESC LIMIT 1",
        (g.user["id"], since),
    )
    if not ev:
        return jsonify({"ok": True, "last_id": since})
    return jsonify({"ok": True, "last_id": ev["id"], "uid": ev["card_uid"], "outcome": ev["outcome"], "message": ev["message"]})


# ---------------------------------------------------------------- reader bridge (token auth, no CSRF)

def _bridge_user():
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    db = get_db()
    user = services.user_for_token(db, token)
    if user:
        db.commit()
    return user


@bp.route("/api/bridge/ping")
def bridge_ping():
    user = _bridge_user()
    if not user:
        return jsonify({"ok": False, "message": "Invalid or missing token."}), 401
    lec = services.open_session_for_user(get_db(), user)
    return jsonify({"ok": True, "user": user["name"],
                    "session": {"id": lec["id"], "name": lec["name"], "subject": lec["subject_name"]} if lec else None})


@bp.route("/api/bridge/scan", methods=["POST"])
def bridge_scan():
    user = _bridge_user()
    if not user:
        return jsonify({"ok": False, "message": "Invalid or missing token."}), 401
    db = get_db()
    uid = _json().get("uid", "")
    lec = services.open_session_for_user(db, user)
    if not lec:
        norm = services.normalize_uid(uid)
        services.log_event(db, user["id"], None, norm or str(uid)[:40], None, "no_session", "Card tapped but no session is open.")
        db.commit()
        return jsonify({"ok": False, "outcome": "no_session", "uid": norm,
                        "message": "No open session. Start one in the web app (the UID was saved for registration)."})
    return jsonify(services.process_scan(db, lec, uid, user["id"], method="bridge"))
