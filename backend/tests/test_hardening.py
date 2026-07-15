"""Hardening sweep: secrets never persist in logs, security headers present."""

import json

from app.core import db, diagnostics
from tests.test_admin import client, do_setup, get_csrf

SNEAKY = "sk-live-FAKE-abc123-SHOULD-NEVER-PERSIST"


def test_fake_secret_never_reaches_logs_or_bundle(tmp_path, monkeypatch):
    """Plan §12: submit fake secrets through real flows; assert absence
    everywhere except where the user intentionally put them."""
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        c.cookies.clear()
        # wrong-password attempt using a secret-looking string
        c.post("/admin/login", data={"password": SNEAKY})
        c.post("/admin/login", data={"password": "hunter2hunter2"})
        # announcement containing a key-looking string (user content — displayed,
        # but must never leak into event logs or diagnostics)
        c.post("/admin/announcements",
               data={"text": f"wifi code {SNEAKY}", "hours": 1, "csrf": get_csrf(c)})

    conn = db.connect()
    log_dump = json.dumps([dict(r) for r in conn.execute(
        "SELECT * FROM event_log").fetchall()])
    assert SNEAKY not in log_dump

    bundle = diagnostics.build_bundle(conn)
    assert SNEAKY.encode() not in bundle
    conn.close()


def test_security_headers_present(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        r = c.get("/display")
        assert r.headers["x-content-type-options"] == "nosniff"
        r = c.get("/admin/login")
        assert r.headers["x-frame-options"] == "DENY"


def test_session_cookie_flags(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        # cookie is issued at the wizard's password step
        r = c.post("/admin/setup", data={"password": "hunter2hunter2",
                                         "password2": "hunter2hunter2"},
                   follow_redirects=False)
        cookie_header = r.headers.get("set-cookie", "")
        assert "HttpOnly" in cookie_header
        assert "SameSite=lax" in cookie_header
