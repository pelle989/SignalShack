"""/announce page, display PIN gate, PWA manifest."""

from app.core import config_mgr, db, security
from tests.test_admin import client, do_setup, get_csrf


def set_pins(c, announce="", display=""):
    c.post("/admin/settings/pins", data={"announce_pin": announce,
                                         "display_pin": display,
                                         "csrf": get_csrf(c)})


# ---------------- /announce

def test_announce_disabled_by_default(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        r = c.get("/announce")
        assert "This page is off" in r.text
        # posting while disabled writes nothing
        c.post("/announce", data={"pin": "1234", "text": "nope"})
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM announcement").fetchone()["c"] == 0
        conn.close()


def test_announce_with_pin(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        set_pins(c, announce="4321")
        # wrong pin rejected
        r = c.post("/announce", data={"pin": "0000", "text": "hax"})
        assert "Wrong PIN" in r.text
        # right pin posts, 12h expiry, priority flag honored
        r = c.post("/announce", data={"pin": "4321", "text": "Trash night",
                                      "priority": "1"})
        assert "Posted" in r.text
        conn = db.connect()
        row = conn.execute("SELECT text, priority, expires_at"
                           " FROM announcement").fetchone()
        conn.close()
        assert row["text"] == "Trash night" and row["priority"] == 1
        assert row["expires_at"] is not None


def test_announce_rate_limited(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        set_pins(c, announce="4321")
        security._attempts.clear()
        for _ in range(10):
            c.post("/announce", data={"pin": "0000", "text": "x"})
        r = c.post("/announce", data={"pin": "4321", "text": "legit"})
        assert "Too many" in r.text


# ---------------- display PIN

def test_display_open_by_default(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        assert "board" in c.get("/display").text


def test_display_pin_gate_and_unlock(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        set_pins(c, display="9876")
        r = c.get("/display")
        assert "Enter the display PIN" in r.text
        r = c.post("/display/unlock", data={"pin": "1111"})
        assert "Wrong PIN" in r.text
        r = c.post("/display/unlock", data={"pin": "9876"}, follow_redirects=False)
        assert r.status_code == 303
        assert "board" in c.get("/display").text          # cookie unlocks


def test_pin_validation(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        r = c.post("/admin/settings/pins", data={"announce_pin": "abc",
                                                 "display_pin": "",
                                                 "csrf": get_csrf(c)})
        assert "4–8 digits" in r.text
        conn = db.connect()
        assert config_mgr.get_settings(conn)["announce_pin"] == ""
        conn.close()


# ---------------- PWA

def test_manifest_served_and_linked(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        m = c.get("/static/manifest.json")
        assert m.status_code == 200 and "SignalShack" in m.text
        assert "manifest.json" in c.get("/display").text
