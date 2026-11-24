import json
import re
import time

from participation import services
from participation.db import connect


def make_world(c):
    """Subject + 3 students (2 with cards) + 1 lecture for today; returns ids."""
    c.post("/subjects", {"name": "Production Management", "code": "PM"})
    c.post("/students", {"name": "Anna", "moodle_id": "s1", "card_uid": "04:A2:3B:C4"})
    c.post("/students", {"name": "Ben", "moodle_id": "s2", "card_uid": "04B2"+"3BC4"})
    c.post("/students", {"name": "Cleo", "moodle_id": "s3", "card_uid": ""})
    c.post("/students", {"name": "Outsider", "moodle_id": "s9", "card_uid": "DEADBEEF"})
    c.post("/subjects/1/enroll", {"student_id": ["1", "2", "3"]})
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    c.post("/lectures", {"subject_id": "1", "name": "L1", "lecture_date": today, "track_exit": "1"})
    return 1


# ------------------------------------------------------------------ auth & security

def test_login_page_and_redirect(client):
    assert client.get("/").status_code == 302
    r = client.get("/login")
    assert r.status_code == 200 and "Sign in" in r.get_data(as_text=True)
    assert "admin123" not in r.get_data(as_text=True)  # no more hard-coded credentials on screen


def test_csrf_required(client):
    r = client.c.post("/login", data={"username": "admin", "password": "x"})
    assert r.status_code == 400


def test_admin_forced_password_change(client):
    assert client.login().status_code == 302
    r = client.get("/")
    assert r.status_code == 302 and "/account/password" in r.headers["Location"]
    r = client.post("/account/password", {"current": "Admin-pass-123", "new": "short", "confirm": "short"})
    assert "at least 10" in r.get_data(as_text=True)
    r = client.post("/account/password", {"current": "Admin-pass-123", "new": "NewAdmin-pass-456", "confirm": "NewAdmin-pass-456"})
    assert r.status_code == 302
    assert client.get("/").status_code == 200


def test_login_throttle(client):
    for _ in range(8):
        client.login("admin", "wrong")
    r = client.login("admin", "Admin-pass-123")  # even the right password is blocked now
    assert "Too many failed attempts" in r.get_data(as_text=True)


def test_security_headers(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_open_redirect_blocked(client):
    r = client.post("/login?next=//evil.example", {"username": "admin", "password": "Admin-pass-123", "login_type": "staff"})
    assert "evil" not in r.headers["Location"]


# ------------------------------------------------------------------ scanning

def test_scan_lifecycle(admin, app):
    c = admin
    lid = make_world(c)
    # closed session rejects taps
    r = c.json(f"/api/session/{lid}/scan", {"uid": "04A23BC4"}).get_json()
    assert r["outcome"] == "closed"
    c.post(f"/session/{lid}/start")
    r = c.json(f"/api/session/{lid}/scan", {"uid": "04:a2:3b:c4", "source": "webnfc"}).get_json()
    assert r["outcome"] == "checked_in" and r["student"] == "Anna"
    time.sleep(0.01)
    # exit tap (grace is 0 in tests)
    r = c.json(f"/api/session/{lid}/scan", {"uid": "04A23BC4"}).get_json()
    assert r["outcome"] == "checked_out"
    r = c.json(f"/api/session/{lid}/scan", {"uid": "04A23BC4"}).get_json()
    assert r["outcome"] == "already_out"
    assert c.json(f"/api/session/{lid}/scan", {"uid": "FFFF0000"}).get_json()["outcome"] == "unknown"
    assert c.json(f"/api/session/{lid}/scan", {"uid": "DEADBEEF"}).get_json()["outcome"] == "not_enrolled"
    assert c.json(f"/api/session/{lid}/scan", {"uid": "zz!"}).get_json()["outcome"] == "invalid"

    st = c.get(f"/api/session/{lid}/state").get_json()
    assert st["counts"] == {"enrolled": 3, "present": 1, "left": 1}
    assert len(st["events"]) >= 6
    # incremental polling returns only newer events
    st2 = c.get(f"/api/session/{lid}/state?since={st['last_event_id']}").get_json()
    assert st2["events"] == []


def test_duplicate_within_grace(admin, app):
    app.config["EXIT_GRACE_SECONDS"] = 120
    c = admin
    lid = make_world(c)
    c.post(f"/session/{lid}/start")
    assert c.json(f"/api/session/{lid}/scan", {"uid": "04A23BC4"}).get_json()["outcome"] == "checked_in"
    assert c.json(f"/api/session/{lid}/scan", {"uid": "04A23BC4"}).get_json()["outcome"] == "duplicate"


def test_manual_mark_and_undo(admin):
    c = admin
    lid = make_world(c)
    r = c.json(f"/api/session/{lid}/mark", {"student_id": 3}).get_json()
    assert r["ok"]
    assert c.get(f"/api/session/{lid}/state").get_json()["counts"]["present"] == 1
    c.json(f"/api/session/{lid}/mark", {"student_id": 3, "present": False})
    assert c.get(f"/api/session/{lid}/state").get_json()["counts"]["present"] == 0
    assert not c.json(f"/api/session/{lid}/mark", {"student_id": 4}).get_json()["message"].endswith("present manually.")


def test_bridge_flow(admin, client):
    c = admin
    lid = make_world(c)
    c.post("/account/tokens", {"label": "lab"})
    page = c.get("/account").get_data(as_text=True)
    token = re.search(r"nfcp_[A-Za-z0-9_\-]+", page).group(0)

    anon = client.c.__class__  # separate un-cookied request client
    raw = client.c.application.test_client()
    h = {"Authorization": f"Bearer {token}"}
    assert raw.get("/api/bridge/ping").status_code == 401
    assert raw.get("/api/bridge/ping", headers=h).get_json()["session"] is None
    # no open session: UID is stored (for registration) but nobody is marked
    r = raw.post("/api/bridge/scan", json={"uid": "04A23BC4"}, headers=h).get_json()
    assert r["outcome"] == "no_session" and not r["ok"]
    c.post(f"/session/{lid}/start")
    r = raw.post("/api/bridge/scan", json={"uid": "04A23BC4"}, headers=h).get_json()
    assert r["outcome"] == "checked_in"
    assert raw.get("/api/bridge/ping", headers=h).get_json()["session"]["id"] == lid
    # token revocation
    c.post("/account/tokens/1/delete")
    assert raw.post("/api/bridge/scan", json={"uid": "04A23BC4"}, headers=h).status_code == 401


def test_latest_scan_for_registration(admin):
    c = admin
    base = c.get("/api/scans/latest").get_json()["last_id"]
    c.post("/account/tokens", {"label": "x"})
    token = re.search(r"nfcp_[A-Za-z0-9_\-]+", c.get("/account").get_data(as_text=True)).group(0)
    raw = c.c.application.test_client()
    raw.post("/api/bridge/scan", json={"uid": "AABBCCDD"}, headers={"Authorization": f"Bearer {token}"})
    r = c.get(f"/api/scans/latest?since={base}").get_json()
    assert r["uid"] == "AABBCCDD"


# ------------------------------------------------------------------ access control

def test_lecturers_are_isolated(admin, app):
    c = admin
    lid = make_world(c)
    c.post("/admin/users", {"username": "lena", "name": "Lena L", "password": "tempPass123", "role": "lecturer"})
    other = type(c)(app)
    other.login("lena", "tempPass123")
    other.post("/account/password", {"current": "tempPass123", "new": "LenaNewPass-1234", "confirm": "LenaNewPass-1234"})
    assert other.get(f"/session/{lid}").status_code == 403
    assert other.json(f"/api/session/{lid}/scan", {"uid": "04A23BC4"}).status_code == 403
    assert other.get("/export/subject/1.csv").status_code == 403
    assert other.get("/admin/users").status_code == 403
    assert "<strong>Production Management</strong>" not in other.get("/subjects").get_data(as_text=True)


def test_student_cannot_reach_staff(admin, app):
    c = admin
    make_world(c)
    s = type(c)(app)
    assert s.post("/login", {"username": "s1", "password": "04A23BC4", "login_type": "student"}).status_code == 302
    s.post("/me/pin", {"new": "secret-pin", "confirm": "secret-pin"})
    assert s.get("/subjects").status_code == 302  # bounced to login
    assert s.get("/api/session/1/state").status_code == 401
    assert s.get("/admin/users").status_code == 302


# ------------------------------------------------------------------ student portal

def test_student_first_login_sets_pin(admin, app):
    make_world(admin)
    s = type(admin)(app)
    assert "Invalid" in s.post("/login", {"username": "s1", "password": "nope", "login_type": "student"}).get_data(as_text=True)
    r = s.post("/login", {"username": "s1", "password": "04a2 3bc4", "login_type": "student"})
    assert "/me/pin" in r.headers["Location"]
    assert "/me/pin" in s.get("/me").headers["Location"]  # forced
    assert s.post("/me/pin", {"new": "abc", "confirm": "abc"}).status_code == 200
    assert s.post("/me/pin", {"new": "secret-pin", "confirm": "secret-pin"}).status_code == 302
    page = s.get("/me").get_data(as_text=True)
    assert "Production Management" in page
    # the card UID no longer works as password once a PIN exists
    s2 = type(admin)(app)
    assert "Invalid" in s2.post("/login", {"username": "s1", "password": "04A23BC4", "login_type": "student"}).get_data(as_text=True)
    assert s2.post("/login", {"username": "s1", "password": "secret-pin", "login_type": "student"}).status_code == 302


# ------------------------------------------------------------------ statistics, exports, import

def test_stats_only_count_held_lectures(admin, app):
    c = admin
    make_world(c)
    c.post("/lectures", {"subject_id": "1", "name": "Past", "lecture_date": "2020-01-01"})
    c.post("/lectures", {"subject_id": "1", "name": "Future", "lecture_date": "2999-01-01"})
    with app.app_context():
        from participation.db import get_db
        db = get_db()
        # attendance in the past lecture (id 2)
        db.run("INSERT INTO attendance (lecture_id, student_id, checked_in_at) VALUES (2, 1, '2020-01-01T09:00:00Z')")
        db.commit()
        stats = {s["name"]: s for s in services.compute_stats(db)}
    assert stats["Anna"]["total"] == 1 and stats["Anna"]["attended"] == 1 and stats["Anna"]["percentage"] == 100.0 and stats["Anna"]["grade"] == "1.0"
    assert stats["Ben"]["percentage"] == 0.0 and stats["Ben"]["status"] == "risk" and stats["Ben"]["grade"] == "5.0"


def test_min_minutes_rule(admin, app):
    c = admin
    make_world(c)
    c.post("/lectures", {"subject_id": "1", "name": "Strict", "lecture_date": "2020-01-02", "min_minutes": "30", "track_exit": "1"})
    with app.app_context():
        from participation.db import get_db
        db = get_db()
        db.run("INSERT INTO attendance (lecture_id, student_id, checked_in_at, left_at, duration_min) VALUES (2, 1, '2020-01-02T09:00:00Z', '2020-01-02T09:10:00Z', 10)")
        db.run("INSERT INTO attendance (lecture_id, student_id, checked_in_at, left_at, duration_min) VALUES (2, 2, '2020-01-02T09:00:00Z', '2020-01-02T10:00:00Z', 60)")
        db.run("INSERT INTO attendance (lecture_id, student_id, checked_in_at) VALUES (2, 3, '2020-01-02T09:00:00Z')")
        db.commit()
        stats = {s["name"]: s["attended"] for s in services.compute_stats(db, subject_id=1)}
    assert stats == {"Anna": 0, "Ben": 1, "Cleo": 0}


def test_csv_export_neutralises_formulas(admin):
    c = admin
    make_world(c)
    c.post("/students", {"name": "=HYPERLINK(\"http://x\")", "moodle_id": "s7"})
    c.post("/subjects/1/enroll", {"student_id": ["5"]})
    body = c.get("/export/lecture/1.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in body and "\n=HYPERLINK" not in body
    assert c.get("/export/subject/1.csv").status_code == 200


def test_csv_import(admin):
    c = admin
    c.post("/subjects", {"name": "Acc"})
    csv_text = "name,moodle_id,card_uid\nZoe,z1,0a0b0c0d\nYan,y1,\nBad,b1,xyz\nZoe Renamed,z1,\n"
    import io
    r = c.c.post("/subjects/1/import", data={"csrf_token": c.token(), "file": (io.BytesIO(csv_text.encode()), "s.csv")},
                 content_type="multipart/form-data", follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "2 created, 1 updated, 2 enrolled" in page
    assert "invalid" in page.lower() or "Card UID must be" in page
    assert "Zoe Renamed" in page


def test_uid_normalisation():
    assert services.normalize_uid("04:a2-3b c4") == "04A23BC4"
    assert services.normalize_uid("xyz") is None and services.normalize_uid("") is None and services.normalize_uid("AB") is None


def test_xss_escaped(admin):
    c = admin
    c.post("/students", {"name": "<script>alert(1)</script>", "moodle_id": "x1"})
    page = c.get("/students").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_demo_data_and_dashboard(admin):
    c = admin
    assert c.post("/admin/demo", {"action": "load"}).status_code == 302
    dash = c.get("/").get_data(as_text=True)
    assert "Attendance by lecture" in dash and "Production Management" in dash
    assert c.get("/stats").status_code == 200 and c.get("/calendar").status_code == 200
    assert c.get("/sessions").status_code == 200 and c.get("/lectures").status_code == 200
    assert c.get("/students/1").status_code == 200 and c.get("/subjects/1").status_code == 200
    c.post("/admin/demo", {"action": "clear"})
    assert "<strong>Production Management</strong>" not in c.get("/subjects").get_data(as_text=True)


def test_healthz(client):
    assert client.get("/healthz").get_json()["ok"] is True
