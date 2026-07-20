"""App restart button — auth-gated, CSRF-checked, exit deferred past response."""

from app.admin import routes as admin_routes
from app.core import db
from tests.test_admin import client, do_setup, get_csrf


def test_restart_button(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(admin_routes, "_schedule_exit",
                        lambda *a, **k: calls.append(1))
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        page = c.get("/admin")
        assert "Restart SignalShack" in page.text
        c.post("/admin/restart", data={"csrf": "wrong"})
        assert calls == []                    # bad CSRF: nothing happens
        r = c.post("/admin/restart", data={"csrf": csrf})
        assert calls == [1] and "Restarting" in r.text
        conn = db.connect()
        assert conn.execute("SELECT 1 FROM event_log WHERE"
                            " event_type='restart_requested'").fetchone()
        conn.close()


def test_restart_requires_login(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(admin_routes, "_schedule_exit",
                        lambda *a, **k: calls.append(1))
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        c.post("/admin/logout")
        c.post("/admin/restart", data={"csrf": "x"})
        assert calls == []                    # logged out: guard redirects
