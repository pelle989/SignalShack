"""Gate 1 punch list: backup exclusions, diagnostics scrubbing, config rollback."""

import io
import json
import zipfile
from datetime import datetime

from app.core import backup, config_mgr, db, diagnostics
from tests.test_admin import client, do_setup, get_csrf

FAKE_SECRET = "sk-THIS-IS-A-FAKE-SECRET-9922"


def seeded_conn(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO announcement (text, priority, created_at)"
                     " VALUES ('private family note', 0, ?)",
                     (datetime.now().isoformat(timespec='seconds'),))
        conn.execute("INSERT INTO secret (service_name, label, ciphertext, nonce)"
                     " VALUES ('test', 'k', ?, ?)",
                     (FAKE_SECRET.encode(), b"nonce"))
    return conn


def test_backup_includes_state_excludes_private(tmp_path, monkeypatch):
    conn = seeded_conn(tmp_path, monkeypatch)
    bundle = backup.export_bundle(conn)
    raw = json.dumps(bundle)
    assert bundle["locations"][0]["label"] == "Home"
    assert len(bundle["rule_state"]) == 24
    assert FAKE_SECRET not in raw                       # invariant 5
    assert "private family note" not in raw             # announcements excluded
    assert bundle["schema_version"] >= 2


def test_diagnostics_bundle_is_scrubbed(tmp_path, monkeypatch):
    conn = seeded_conn(tmp_path, monkeypatch)
    data = diagnostics.build_bundle(conn)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    assert {"about.json", "self_check.json", "events.json",
            "sources.json", "README.txt"} <= names
    everything = b"".join(z.read(n) for n in names)
    assert FAKE_SECRET.encode() not in everything
    assert b"private family note" not in everything


def test_self_check_reports_plain_language(tmp_path, monkeypatch):
    conn = seeded_conn(tmp_path, monkeypatch)
    checks = diagnostics.self_check(conn)
    by_name = {c["name"]: c for c in checks}
    assert by_name["Location configured"]["ok"] is True
    assert by_name["open_meteo data"]["ok"] is False    # nothing fetched in tests
    assert "Refresh Now" in by_name["open_meteo data"]["detail"]


def test_config_apply_validate_and_rollback(tmp_path, monkeypatch):
    conn = seeded_conn(tmp_path, monkeypatch)
    # invalid rejected before apply
    ok, problems = config_mgr.apply_settings(conn, {"night_start": "25:99"})
    assert not ok and any("HH:MM" in p for p in problems)
    # valid applies
    ok, _ = config_mgr.apply_settings(conn, {"night_start": "21:00",
                                             "night_end": "05:30",
                                             "timezone": "America/New_York",
                                             "display_poll_seconds": 30})
    assert ok
    assert config_mgr.get_settings(conn)["night_start"] == "21:00"
    # health-check failure rolls back to previous
    def bomb(_conn):
        raise RuntimeError("display broke")
    ok, problems = config_mgr.apply_settings(
        conn, {"night_start": "20:00", "night_end": "05:30",
               "timezone": "America/New_York", "display_poll_seconds": 30},
        health_check=bomb)
    assert not ok and "rolled back" in problems[0]
    assert config_mgr.get_settings(conn)["night_start"] == "21:00"   # previous wins
    rolled = conn.execute("SELECT status FROM config ORDER BY version DESC"
                          " LIMIT 1").fetchone()
    assert rolled["status"] == "rolled_back"


def test_settings_route_end_to_end(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch) as c:
        do_setup(c)
        r = c.post("/admin/settings", data={
            "timezone": "America/New_York", "night_start": "23:00",
            "night_end": "06:00", "display_poll_seconds": 25,
            "csrf": get_csrf(c)})
        assert "Saved and health-checked" in r.text
        r = c.get("/admin/backup/export")
        assert r.headers["content-type"].startswith("application/json")
        assert json.loads(r.content)["kind"] == "signalshack-backup"
