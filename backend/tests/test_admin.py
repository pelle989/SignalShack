"""Admin layer: setup, auth, CSRF, rate limit, host guard, seed sync invariant."""

import json

from fastapi.testclient import TestClient

from app.core import db, security
from app.main import app
from app.rules.sync import sync_seeds


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("SIGNALSHACK_NO_JOBS", "1")
    security._attempts.clear()
    return TestClient(app)


def do_setup(c, password="hunter2hunter2"):
    """Walk the multi-step wizard (manual-coordinates path, no network)."""
    c.post("/admin/setup", data={"password": password, "password2": password},
           follow_redirects=False)
    return c.post("/admin/setup/location", data={
        "latitude": "40.68", "longitude": "-73.47",
        "label": "Home", "timezone": "America/New_York"}, follow_redirects=False)


def get_csrf(c):
    raw = c.cookies.get(security.SESSION_COOKIE)
    return security._serializer().loads(raw)["csrf"]


# ---------------- setup & auth

def test_setup_creates_device_location_board_and_logs_in(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        assert c.get("/admin", follow_redirects=False).headers["location"] == "/admin/setup"
        r = do_setup(c)
        assert r.status_code == 303 and r.headers["location"] == "/admin/setup/done"
        assert c.get("/admin").status_code == 200      # session cookie set
    conn = db.connect()
    assert conn.execute("SELECT 1 FROM device WHERE id=1").fetchone()
    assert conn.execute("SELECT 1 FROM location WHERE is_primary=1").fetchone()
    assert conn.execute("SELECT 1 FROM board WHERE is_default=1").fetchone()
    conn.close()


def test_setup_rejects_weak_or_mismatched_password(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        r = c.post("/admin/setup", data={"password": "short", "password2": "short"})
        assert "at least 8" in r.text
        r = c.post("/admin/setup", data={"password": "longenough1",
                                         "password2": "different1"})
        assert "match" in r.text


def test_login_wrong_password_and_rate_limit(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        c.cookies.clear()
        for _ in range(5):
            r = c.post("/admin/login", data={"password": "nope-nope"})
            assert "Wrong password" in r.text
        r = c.post("/admin/login", data={"password": "hunter2hunter2"})
        assert "Too many attempts" in r.text            # limit hit even with right pw


def test_login_success_grants_admin(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        c.cookies.clear()
        assert c.get("/admin", follow_redirects=False).status_code == 303
        r = c.post("/admin/login", data={"password": "hunter2hunter2"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/admin").status_code == 200


# ---------------- CSRF & host guard

def test_announcement_requires_csrf(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        c.post("/admin/announcements", data={"text": "evil", "hours": 12})  # no token
        conn = db.connect()
        assert conn.execute("SELECT COUNT(*) c FROM announcement").fetchone()["c"] == 0
        c.post("/admin/announcements",
               data={"text": "trash night", "hours": 12, "csrf": get_csrf(c)})
        rows = conn.execute("SELECT text FROM announcement").fetchall()
        conn.close()
        assert [r["text"] for r in rows] == ["trash night"]


def test_host_guard_blocks_rebinding(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        r = c.get("/admin/login", headers={"Host": "evil.example.com"})
        assert r.status_code == 403
        assert c.get("/admin/login", headers={"Host": "signalshack.local"}).status_code == 200
        assert c.get("/admin/login", headers={"Host": "192.168.1.50"}).status_code == 200


# ---------------- rule toggles & seed sync invariant

def test_rule_toggle_roundtrip(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        csrf = get_csrf(c)
        conn = db.connect()
        rid = conn.execute("SELECT id FROM signal_rule WHERE seed_id='T7'").fetchone()["id"]
        assert conn.execute("SELECT enabled FROM rule_user_state WHERE rule_id=?",
                            (rid,)).fetchone()["enabled"] == 1   # first install: enabled
        c.post("/admin/rules/T7/toggle", data={"csrf": csrf})
        assert conn.execute("SELECT enabled FROM rule_user_state WHERE rule_id=?",
                            (rid,)).fetchone()["enabled"] == 0
        c.post("/admin/rules/T7/toggle", data={"csrf": csrf})
        assert conn.execute("SELECT enabled FROM rule_user_state WHERE rule_id=?",
                            (rid,)).fetchone()["enabled"] == 1
        conn.close()


def test_seed_update_survival_invariant(tmp_path, monkeypatch):
    """Invariant 1: version bumps update definitions, never user state;
    brand-new seeds arrive disabled with the 'new' badge."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.migrate()
    conn = db.connect()
    stats = sync_seeds(conn)
    assert stats["first_install"] and stats["added"] == 24

    # user disables T6, then an update ships v3 with new copy + a new rule
    rid = conn.execute("SELECT id FROM signal_rule WHERE seed_id='T6'").fetchone()["id"]
    with conn:
        conn.execute("UPDATE rule_user_state SET enabled=0 WHERE rule_id=?", (rid,))
    import app.rules.sync as sync_mod
    data = json.loads(sync_mod.SEEDS_PATH.read_text())
    data["seed_version"] = 3
    new_rule = {"id": "X1", "priority": 40, "topic": "test",
                "conditions": [{"field": "f", "op": ">=", "value": 1}],
                "output": "New rule copy."}
    data["rules"].append(new_rule)
    alt = tmp_path / "seeds.json"
    alt.write_text(json.dumps(data))
    monkeypatch.setattr(sync_mod, "SEEDS_PATH", alt)

    stats = sync_seeds(conn)
    assert stats["added"] == 1 and stats["updated"] == 24
    # user state survived the update
    assert conn.execute("SELECT enabled FROM rule_user_state WHERE rule_id=?",
                        (rid,)).fetchone()["enabled"] == 0
    # new rule arrived dormant with badge
    x1 = conn.execute(
        "SELECT us.enabled, us.acknowledged_new FROM signal_rule r"
        " JOIN rule_user_state us ON us.rule_id=r.id WHERE r.seed_id='X1'").fetchone()
    assert x1["enabled"] == 0 and x1["acknowledged_new"] == 0
    conn.close()
