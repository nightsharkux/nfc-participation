import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from participation import create_app  # noqa: E402
from participation import db as dbmod  # noqa: E402


def _fresh_pg_schema(url):
    """Run the suite against real Postgres: each test gets an empty `nfc_test` schema (never touches `public`)."""
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS nfc_test CASCADE")
        c.execute("CREATE SCHEMA nfc_test")
    sep = "&" if "?" in url else "?"
    return url + sep + "options=-csearch_path%3Dnfc_test"


@pytest.fixture()
def app(tmp_path):
    dbmod.reset_schema_cache()
    pg_url = os.environ.get("TEST_DATABASE_URL", "")
    if pg_url:
        pg_url = _fresh_pg_schema(pg_url)
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE_URL": pg_url,
        "SQLITE_PATH": str(tmp_path / "t.db"),
        "ADMIN_PASSWORD": "Admin-pass-123",
        "EXIT_GRACE_SECONDS": 0,
    })
    return app


class Client:
    """Test client that tracks the CSRF token like a browser would."""

    def __init__(self, app):
        self.c = app.test_client()

    def token(self):
        for url in ("/login", "/account/password", "/me/pin"):  # whichever page this session may view
            m = re.search(r'name="csrf-token" content="([^"]+)"', self.c.get(url).get_data(as_text=True))
            if m:
                return m.group(1)
        raise AssertionError("no CSRF token found")

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.token())
        return self.c.post(url, data=data, **kw)

    def json(self, url, body, method="POST"):
        return self.c.open(url, method=method, json=body, headers={"X-CSRF-Token": self.token()})

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def login(self, user="admin", pw="Admin-pass-123"):
        return self.post("/login", {"username": user, "password": pw, "login_type": "staff"})


@pytest.fixture()
def client(app):
    return Client(app)


@pytest.fixture()
def admin(client):
    """Admin who already replaced the initial temporary password."""
    r = client.login()
    assert r.status_code == 302
    r = client.post("/account/password", {"current": "Admin-pass-123", "new": "NewAdmin-pass-456", "confirm": "NewAdmin-pass-456"})
    assert r.status_code == 302
    return client
